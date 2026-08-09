"""Read-only, tolerant parsing of Claude Code session JSONLs.

The transcripts under ``~/.claude/projects/**/*.jsonl`` are the only local record
of what actually consumed tokens on this machine. This module turns them into
per-turn facts the store can roll up; it never writes to them and never raises on
bad input.

Only ``type:"assistant"`` records carry token usage, so they are the only ones we
extract. Everything else -- user turns, attachments, titles, file-history deltas --
is skipped and counted, not an error. A line that will not decode, a record that is
not an object, a synthetic or errored assistant turn, one with no usage or an
unreadable timestamp: each is dropped quietly and tallied, so a single corrupt line
in a 500 MB tree can never take a panel down with it.

The reader is incremental. JSONL files are append-only, so a byte offset per file is
enough to resume: on each run only the bytes past the recorded offset are read, and
only through the last complete (newline-terminated) line, leaving a half-written
final line for next time. Nothing here holds the OAuth token; message *content* is
never read, only the usage counts, cwd, model, sessionId, timestamp and sidechain
flag.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The stand-in model on interrupted/placeholder assistant turns. It carries no real
# usage and must not be attributed to a real model.
SYNTHETIC_MODEL = "<synthetic>"

# Used when a record omits the field. Kept rather than dropped so the usage is still
# counted somewhere rather than silently lost.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Turn:
    """One assistant turn's contribution, normalized for aggregation."""

    ts: datetime
    project: str
    model: str
    is_sidechain: bool
    session_id: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int

    @property
    def total_tokens(self) -> int:
        """Gross tokens processed on this turn -- a proxy for consumption, not a bill."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    @property
    def context_tokens(self) -> int:
        """How much context this turn carried: fresh input plus what was read from cache."""
        return self.input_tokens + self.cache_read_tokens


@dataclass
class ParseStats:
    """Line-level tally of one parse pass, so drift is visible rather than silent."""

    lines: int = 0
    malformed: int = 0
    skipped: int = 0
    emitted: int = 0


def parse_lines(lines: Iterable[str], stats: ParseStats | None = None) -> Iterator[Turn]:
    """Yield a :class:`Turn` for every usable assistant record in ``lines``.

    ``stats`` (if given) is updated in place: ``malformed`` counts lines that would
    not decode into an object, ``skipped`` counts records deliberately ignored (not
    an assistant turn, synthetic, errored, no usage, no timestamp), and ``emitted``
    counts turns yielded. Never raises.
    """
    tally = stats if stats is not None else ParseStats()
    for line in lines:
        text = line.strip()
        if not text:
            continue
        tally.lines += 1
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            tally.malformed += 1
            continue
        if not isinstance(record, dict):
            tally.malformed += 1
            continue
        turn = turn_from_record(record)
        if turn is None:
            tally.skipped += 1
            continue
        tally.emitted += 1
        yield turn


def turn_from_record(record: dict) -> Turn | None:
    """Extract a :class:`Turn`, or ``None`` for any record we do not attribute.

    Unknown fields are ignored by construction -- only the handful named below are
    read -- so a new Claude Code version adding keys changes nothing here.
    """
    if record.get("type") != "assistant":
        return None
    # An API error turn is not real usage; it reports a failed request.
    if record.get("isApiErrorMessage"):
        return None

    message = record.get("message")
    if not isinstance(message, dict):
        return None

    model = message.get("model")
    if not isinstance(model, str) or not model or model == SYNTHETIC_MODEL:
        return None

    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    cache_creation = _non_negative_int(usage.get("cache_creation_input_tokens"))
    cache_read = _non_negative_int(usage.get("cache_read_input_tokens"))
    if input_tokens + output_tokens + cache_creation + cache_read == 0:
        # Nothing to attribute -- a turn that reported a usage object of all zeros or
        # unreadable values contributes no tokens and would only add empty rows.
        return None

    ts = _parse_timestamp(record.get("timestamp"))
    if ts is None:
        # Without a usable timestamp the turn cannot be placed in any window.
        return None

    return Turn(
        ts=ts,
        project=_str_or(record.get("cwd"), UNKNOWN),
        model=model,
        is_sidechain=bool(record.get("isSidechain")),
        session_id=_str_or(record.get("sessionId"), UNKNOWN),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def iter_jsonl_files(root: Path | str) -> list[Path]:
    """Every ``*.jsonl`` under ``root``, sorted; empty if ``root`` does not exist."""
    root = Path(root)
    if not root.is_dir():
        return []
    try:
        return sorted(root.rglob("*.jsonl"))
    except OSError:
        return []


def read_new_lines(path: Path | str, offset: int) -> tuple[list[str], int]:
    """Lines appended to ``path`` since byte ``offset``. Returns ``(lines, new_offset)``.

    Only complete, newline-terminated lines are returned; a trailing partial line is
    left unread so a file mid-write is never half-parsed, and ``new_offset`` advances
    only past what was consumed. If the file shrank below ``offset`` (rotated or
    truncated) the read restarts from the beginning. Any OS error yields no lines and
    the offset unchanged, so a transient read failure simply retries next run.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if offset > size:
        offset = 0  # truncated or rotated out from under us
    if offset >= size:
        return [], offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset

    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset  # no complete line appended yet
    consumed = data[: last_newline + 1]
    new_offset = offset + len(consumed)
    text = consumed.decode("utf-8", errors="replace")
    # Split ONLY on the line feed the JSONL format uses -- never str.splitlines(),
    # which also breaks on U+2028 / U+2029 / U+0085. JSON permits those raw inside a
    # string and Node's JSON.stringify (which writes these transcripts) does not
    # escape them, so a single record whose message content pasted in one of them
    # would otherwise be torn into fragments that each fail to parse -- the tokens
    # dropped and, because the offset still advances, never retried. `consumed`
    # always ends in "\n", so the final split element is an empty string; drop it.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, new_offset


def _non_negative_int(value: object) -> int:
    """Coerce a token count to a non-negative int; anything else reads as 0.

    ``bool`` is rejected before ``int`` because ``True`` is an ``int`` in Python and a
    stray boolean in a usage field should count as nothing, not as one token.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 else 0
    return 0


def _str_or(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _parse_timestamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 (``...Z``) timestamp to an aware UTC datetime, or ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
