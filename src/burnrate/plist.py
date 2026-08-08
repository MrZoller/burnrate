"""Render the LaunchAgent plist from its template.

This exists because `sed` is the wrong tool for the job and was quietly getting
it wrong. Two separate hazards stack up in a shell one-liner:

  * In a `sed` replacement, `&` means "the text that matched". A database path of
    `/tmp/a&b.db` was rendered as `/tmp/a__DB__b.db` -- still well-formed XML, so
    `plutil -lint` passed and the agent simply used a different database than the
    one asked for. A `|` broke the expression outright and a `<` produced invalid
    XML; both of those at least failed loudly. The `&` case did not.
  * The values land inside XML text nodes, so `&`, `<` and `>` need escaping
    there too -- and escaping for both layers at once, in the right order, is
    where hand-rolled shell escaping goes wrong.

Doing it here instead means the substitution is exact, the XML escaping happens
once, and both are covered by the test suite rather than by inspection. Values
arrive as argv pairs so the shell's quoting is authoritative and nothing is
re-parsed on the way in.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

PLACEHOLDER = re.compile(r"__([A-Z][A-Z0-9_]*)__")


def render(template: str, values: dict[str, str]) -> str:
    """Replace each `__NAME__` in `template` with the XML-escaped value.

    One pass, not a loop of `str.replace`. Sequential replacement rescans text it
    has already written, so a value that happened to contain `__DB__` was itself
    expanded by a later key -- the values could interfere with each other, and the
    result depended on the order they were applied in. A single pass cannot look
    at its own output.

    Unknown placeholders are left in place rather than blanked, so `main` can
    report them instead of quietly shipping a plist with a hole in it.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        return escape(values[name])

    return PLACEHOLDER.sub(substitute, template)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or len(argv) % 2 != 0:
        print(
            "usage: python -m burnrate.plist SRC DST [NAME VALUE]...",
            file=sys.stderr,
        )
        return 2

    src, dst = Path(argv[0]), Path(argv[1])
    rest = argv[2:]
    values = dict(zip(rest[0::2], rest[1::2], strict=True))

    template = src.read_text()

    # Asked of the TEMPLATE, never of the rendered output. Scanning the output
    # cannot tell a placeholder the template still needs from placeholder-shaped
    # text that arrived inside a value: a database path of /tmp/__DB__/x.db is
    # perfectly legal, render() preserves it exactly as promised, and the output
    # scan then called it an unsubstituted token and failed the install. The
    # template is the only thing that knows what needs filling in.
    missing = sorted({m.group(1) for m in PLACEHOLDER.finditer(template)} - set(values))
    if missing:
        names = ", ".join(f"__{name}__" for name in missing)
        print(f"error: the template needs values not given: {names}", file=sys.stderr)
        return 1

    dst.write_text(render(template, values))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main(sys.argv[1:]))
