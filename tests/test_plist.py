"""Plist rendering.

These were shell-only until a `sed` replacement was found silently corrupting the
database path, which the suite had no way to see. Rendering moved into the
package so this class of bug is testable.
"""

import plistlib
import subprocess
import sys

import pytest

from burnrate.plist import main, render

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>__LABEL__</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>BURNRATE_DB</key>
      <string>__DB__</string>
    </dict>
  </dict>
</plist>
"""


def _db_from(rendered: str) -> str:
    return plistlib.loads(rendered.encode())["EnvironmentVariables"]["BURNRATE_DB"]


def test_an_ampersand_survives_substitution():
    """The bug this file exists for: `&` is "the matched text" in a sed
    replacement, so /tmp/a&b.db was written as /tmp/a__DB__b.db. That is
    well-formed XML, so plutil -lint passed and the agent used a database nobody
    asked for -- silent, unlike the `|` and `<` cases, which failed loudly."""
    rendered = render(TEMPLATE, {"LABEL": "x", "DB": "/tmp/a&b.db"})

    assert _db_from(rendered) == "/tmp/a&b.db"
    assert "__DB__" not in rendered


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/a&b.db",
        "/tmp/a<b.db",
        "/tmp/a>b.db",
        "/tmp/a|b.db",
        "/tmp/it's.db",
        '/tmp/say "hi".db',
        "/tmp/a&amp;b.db",
        "/tmp/back\\slash.db",
        "/tmp/plain.db",
    ],
)
def test_every_awkward_path_round_trips_through_a_real_plist_parser(path):
    rendered = render(TEMPLATE, {"LABEL": "com.mrzoller.burnrate", "DB": path})

    assert _db_from(rendered) == path


def test_a_value_containing_a_placeholder_is_not_expanded():
    """Substitution must be literal, or one value could inject another and the
    result would depend on the order the keys happened to be applied in."""
    rendered = render(TEMPLATE, {"LABEL": "__DB__", "DB": "/tmp/real.db"})

    parsed = plistlib.loads(rendered.encode())
    assert parsed["Label"] == "__DB__"
    assert parsed["EnvironmentVariables"]["BURNRATE_DB"] == "/tmp/real.db"


def test_a_value_containing_a_placeholder_does_not_fail_the_render(tmp_path):
    """Regression: the leftover-placeholder check scanned the RENDERED output, so
    a legal database path like /tmp/__DB__/x.db -- which render() preserves
    exactly as designed -- was mistaken for an unfilled template token and the
    install died on a valid path. The template is the only thing that knows what
    still needs a value."""
    src = tmp_path / "t.plist"
    src.write_text(TEMPLATE)
    dst = tmp_path / "out.plist"

    rc = main([str(src), str(dst), "LABEL", "x", "DB", "/tmp/__DB__/x.db"])

    assert rc == 0
    assert _db_from(dst.read_text()) == "/tmp/__DB__/x.db"


def test_an_unsubstituted_placeholder_is_an_error(tmp_path, capsys):
    """A template gaining a key the installer does not pass would otherwise ship
    a plist with a literal __THING__ in it, which launchd reads as a real path."""
    src = tmp_path / "t.plist"
    src.write_text(TEMPLATE.replace("__LABEL__", "__SOMETHING_NEW__"))
    dst = tmp_path / "out.plist"

    rc = main([str(src), str(dst), "DB", "/tmp/a.db"])

    assert rc == 1
    assert "__SOMETHING_NEW__" in capsys.readouterr().err
    assert not dst.exists()


def test_an_odd_number_of_arguments_is_rejected(tmp_path):
    assert main([str(tmp_path / "a"), str(tmp_path / "b"), "DB"]) == 2


def test_the_real_template_renders_and_parses(tmp_path):
    """The shipped template with the values install.sh actually passes."""
    src = tmp_path / "src.plist"
    template = (
        __import__("pathlib")
        .Path(__file__)
        .parent.parent.joinpath("deploy/com.mrzoller.burnrate.plist.template")
        .read_text()
    )
    src.write_text(template)
    dst = tmp_path / "out.plist"

    rc = main(
        [
            str(src),
            str(dst),
            "LABEL",
            "com.mrzoller.burnrate",
            "PYTHON",
            "/repo/.venv/bin/python",
            "REPO",
            "/repo",
            "DB",
            "/data/a&b.db",
            "HOST",
            "0.0.0.0",
            "PORT",
            "8377",
            "INTERVAL",
            "15",
            "PROJECTS",
            "/home/dev/.claude/projects",
            "LOG",
            "/logs/burnrate.log",
        ]
    )

    assert rc == 0
    parsed = plistlib.loads(dst.read_bytes())
    env = parsed["EnvironmentVariables"]
    assert env["BURNRATE_DB"] == "/data/a&b.db"
    assert env["BURNRATE_POLL_INTERVAL"] == "15"
    assert env["BURNRATE_HOST"] == "0.0.0.0"
    assert env["BURNRATE_PORT"] == "8377"
    assert env["BURNRATE_PROJECTS_DIR"] == "/home/dev/.claude/projects"
    assert parsed["Label"] == "com.mrzoller.burnrate"


def test_the_module_is_runnable_the_way_install_sh_runs_it(tmp_path):
    """install.sh invokes `python -m burnrate.plist`, so the entry point is part
    of the contract, not an implementation detail."""
    src = tmp_path / "src.plist"
    src.write_text(TEMPLATE)
    dst = tmp_path / "out.plist"

    result = subprocess.run(
        [sys.executable, "-m", "burnrate.plist", str(src), str(dst), "LABEL", "x", "DB", "/a&b.db"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _db_from(dst.read_text()) == "/a&b.db"


def test_a_carriage_return_survives_a_conforming_xml_parser():
    """Regression: XML 1.0 requires a conforming parser to normalise a literal CR to
    LF, so a path containing one produced a plist whose value depended on the reader --
    `plutil -extract` returned the CR, expat returned LF. Two parsers, two different
    databases, from one file. plistlib uses expat, so this is the strict side."""
    path = "/tmp/burnrate\rcarriage.db"

    rendered = render(TEMPLATE, {"LABEL": "x", "DB": path})

    assert "&#13;" in rendered, "the CR must be a character reference, not a raw byte"
    assert _db_from(rendered) == path


@pytest.mark.parametrize("path", ["/a\rb.db", "/a\r\nb.db", "/a\nb.db", "/a\r\rb.db"])
def test_line_break_shapes_all_round_trip(path):
    assert _db_from(render(TEMPLATE, {"LABEL": "x", "DB": path})) == path


@pytest.mark.parametrize("bad", ["\x00", "\x01", "\x07", "\x0b", "\x0c", "\x1f"])
def test_a_character_xml_cannot_carry_is_refused(bad):
    """These cannot be expressed in XML 1.0 at all, not even as a character reference.
    Better to name the character than to emit a file that fails `plutil -lint` with
    nothing pointing at the cause."""
    with pytest.raises(ValueError, match="XML cannot represent"):
        render(TEMPLATE, {"LABEL": "x", "DB": f"/tmp/bad{bad}.db"})


def test_nothing_is_written_when_a_value_cannot_be_represented(tmp_path, capsys):
    """install.sh bootstraps whatever is on disk, so a half-rendered plist would be
    worse than none."""
    src = tmp_path / "t.plist"
    src.write_text(TEMPLATE)
    dst = tmp_path / "out.plist"

    rc = main([str(src), str(dst), "LABEL", "x", "DB", "/tmp/bell\x07.db"])

    assert rc == 1
    assert "XML cannot represent" in capsys.readouterr().err
    assert not dst.exists()
