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
import os
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

# Largest value any single token field may plausibly hold. A single assistant turn's
# token count cannot approach a billion -- context windows are ~1e6 -- so anything above
# this is corruption, or a misplaced value in the wrong field (a nanosecond epoch
# timestamp, ~1.7e18, is the realistic trigger), and is treated as garbage -> 0.
#
# The ceiling is per FIELD on purpose, and it is what keeps every downstream SUM safe.
# Bounding only at SQLite's 2^63 limit was not enough: the values that actually reach
# the database are sums that are never re-bounded -- total_tokens (4 fields),
# context_tokens (3 fields), the per-hour and per-session accumulators (across records),
# the additive UPSERT, and the query-time SUM(). Any of those can exceed 2^63 from
# individually-"valid" fields and raise OverflowError at flush, which rolls back the
# whole batch INCLUDING the watermarks, so the next pass re-reads the same poison record
# and fails forever. A 1e9 per-field cap keeps every one of those sums comfortably below
# 2^63, closing both the flush-rollback freeze and the query-time overflow.
MAX_TOKENS_PER_FIELD = 1_000_000_000

# Most bytes one read_new_lines call pulls into memory at once. A first pass over a
# multi-hundred-MB transcript would otherwise read the whole file into a single bytes
# object; capping the read means the aggregation drains such a file in bounded chunks
# instead. Only whole newline-terminated lines within the chunk are returned, so the
# remainder is picked up by the next call. A single line longer than the cap is still
# read whole (see the loop below) -- the cap bounds the common case, not one giant line.
MAX_READ_BYTES = 8 * 1024 * 1024


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
    # Claude Code preserves this pair when it copies an assistant response into a
    # resumed, forked, or compacted transcript. Neither value alone is unique enough:
    # retries can reuse a message id while receiving a new request id.
    response_identity: tuple[str, str] | None = None

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
        """How much context this turn carried: fresh input, plus what was read from
        cache, plus what it wrote INTO the cache. A cache-priming turn -- tiny input,
        no cache read, but ~200k of cache creation -- is a large-context turn by any
        honest measure, so leaving cache_creation out systematically understates the
        large-context share for exactly the turns that load a big context."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


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
        except (ValueError, TypeError, RecursionError):
            # RecursionError (a RuntimeError, not a ValueError) is what deeply-nested
            # JSON raises. Left uncaught it escapes parse_lines -- which promises never to
            # raise -- and the unguarded fold loop in aggregate_jsonl, before the
            # watermark flush, so the offset never advances and every later pass re-reads
            # the same poison line and re-raises: a permanent total freeze of attribution,
            # the same class as the OverflowError and far-future-timestamp freezes.
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
    # An API error turn is not real usage; it reports a failed request. Require a real
    # boolean, as isSidechain does: a truthy non-bool (e.g. the string "false") must not
    # drop a legitimate usage turn.
    if record.get("isApiErrorMessage") is True:
        return None

    message = record.get("message")
    if not isinstance(message, dict):
        return None

    model = message.get("model")
    if not isinstance(model, str) or not model or model == SYNTHETIC_MODEL:
        return None
    model = _utf8_safe(model)

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

    response_identity = _response_identity(message.get("id"), record.get("requestId"))
    return Turn(
        ts=ts,
        project=_str_or(record.get("cwd"), UNKNOWN),
        model=model,
        is_sidechain=record.get("isSidechain") is True,
        session_id=_str_or(record.get("sessionId"), UNKNOWN),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        response_identity=response_identity,
    )


def scan_jsonl_files(root: Path | str) -> tuple[list[Path], bool]:
    """Return sorted JSONLs and whether the filesystem scan completed cleanly.

    An empty, readable projects directory is a valid scan. A missing root or an
    ``OSError`` is not: callers that report freshness must not mistake an
    inaccessible corpus for a successful no-op refresh.
    """
    root = Path(root)
    if not root.is_dir():
        return [], False
    paths: list[Path] = []
    errors: list[OSError] = []

    # Path.rglob deliberately suppresses directory-scanning errors on current Python.
    # A partial traversal is not a fresh rollup: retaining the readable files is useful,
    # but the caller must keep the previous success timestamp and show the failure.
    def onerror(error: OSError) -> None:
        errors.append(error)

    try:
        for directory, _, names in os.walk(root, onerror=onerror, followlinks=False):
            paths.extend(Path(directory) / name for name in names if name.endswith(".jsonl"))
    except OSError:
        return paths, False
    return sorted(paths), not errors


def iter_jsonl_files(root: Path | str) -> list[Path]:
    """Every ``*.jsonl`` under ``root``, sorted; empty if it cannot be scanned."""
    return scan_jsonl_files(root)[0]


def read_new_lines(
    path: Path | str, offset: int, max_bytes: int = MAX_READ_BYTES
) -> tuple[list[str], int]:
    """Lines appended to ``path`` since byte ``offset``. Returns ``(lines, new_offset)``.

    Only complete, newline-terminated lines are returned; a trailing partial line is
    left unread so a file mid-write is never half-parsed, and ``new_offset`` advances
    only past what was consumed. At most ~``max_bytes`` is pulled in per call (the read
    stops at the first chunk that contains a newline, so a huge file is drained in
    bounded pieces across successive calls); a single line longer than the cap is still
    read whole so progress is guaranteed. Any OS error yields no lines and the offset
    unchanged, so a transient read failure simply retries next run.
    """
    lines, new_offset, _ = read_new_lines_with_health(path, offset, max_bytes)
    return lines, new_offset


def read_new_lines_with_health(
    path: Path | str,
    offset: int,
    max_bytes: int = MAX_READ_BYTES,
    end_offset: int | None = None,
) -> tuple[list[str], int, bool]:
    """Like :func:`read_new_lines`, additionally reporting filesystem read health.

    ``end_offset`` lets a schema migration reread only bytes a committed watermark
    already included, without accidentally claiming newly appended turns as seen.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, False
    if end_offset is not None:
        size = min(size, end_offset)
    if offset > size:
        # The file is SMALLER than where we last read, so restart from the beginning.
        # Claude Code transcripts are append-only -- they only ever grow -- so in
        # practice this fires only on a genuine rotation/truncation, and full
        # file-identity reconciliation (detecting a same-size replacement, or an
        # append after a truncation that would double-count) is intentionally omitted:
        # it cannot arise under the append-only assumption this subsystem is built on.
        offset = 0
    if offset >= size:
        return [], offset, True
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunks: list[bytes] = []
            # Read in capped pieces, stopping as soon as a piece contains a newline.
            # In the common case the first piece has many lines and the loop ends after
            # one read; only a line longer than the cap forces further reads, and then
            # just enough to reach its terminating newline.
            while True:
                piece = handle.read(max(1, max_bytes))
                if not piece:
                    break
                chunks.append(piece)
                if b"\n" in piece:
                    break
            data = b"".join(chunks)
    except OSError:
        return [], offset, False

    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset, True  # no complete line appended yet
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
    return lines, new_offset, True


def _non_negative_int(value: object) -> int:
    """Coerce a token count to a plausible non-negative int; anything else reads as 0.

    ``bool`` is rejected before ``int`` because ``True`` is an ``int`` in Python and a
    stray boolean in a usage field should count as nothing, not as one token. Anything
    above ``MAX_TOKENS_PER_FIELD`` -- a bare huge integer, or a float that rounds to one
    -- is implausible for a single turn and treated as garbage, which (see that
    constant) is what keeps every downstream sum below SQLite's 2^63 limit rather than
    only bounding each field at that limit and letting the sums overflow.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= MAX_TOKENS_PER_FIELD else 0
    if isinstance(value, float):
        if not (math.isfinite(value) and value >= 0):
            return 0
        as_int = int(value)
        return as_int if as_int <= MAX_TOKENS_PER_FIELD else 0
    return 0


def _utf8_safe(value: str) -> str:
    """Return ``value`` if it encodes to UTF-8, else a lossy repair of it.

    A JSON string may carry a lone surrogate (an unpaired ``\\uD800``): it decodes
    to a Python ``str`` fine, but raises ``UnicodeEncodeError`` when SQLite binds
    it. Because the aggregation flush shares one transaction with the watermark
    write, that raise would roll back the whole batch and leave the byte offset
    un-advanced -- so every later pass re-reads the same line and re-raises, the
    same permanent-freeze failure the timestamp (``_parse_timestamp``) and
    recursion guards already exist to prevent. Repair rather than drop, so the
    turn's tokens still count somewhere.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _response_identity(message_id: object, request_id: object) -> tuple[str, str] | None:
    """Return Claude's stable response key when both components are SQLite-safe.

    Missing or malformed metadata must not suppress a real turn. Unlike display
    dimensions, lossy repair is unsafe here because two distinct IDs could collapse
    onto the same key, so an unbindable component disables deduplication for the turn.
    """
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(request_id, str) or not request_id:
        return None
    try:
        message_id.encode("utf-8")
        request_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return message_id, request_id


def _str_or(value: object, default: str) -> str:
    return _utf8_safe(value) if isinstance(value, str) and value else default


def _parse_timestamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 (``...Z``) timestamp to an aware UTC datetime, or ``None``.

    The conversion to UTC happens HERE, guarded, not merely a tz attachment. A
    far-future value like ``9999-12-31T23:59:59-12:00`` parses fine but overflows
    ``datetime.max`` when shifted to UTC -- and that shift happens downstream in the
    store's ``_iso``, inside ``aggregate_jsonl`` before the watermark flush, so the
    OverflowError freezes the offset and every later pass re-reads and re-raises. Doing
    the shift here under ``try`` drops such a record (``turn_from_record`` handles
    ``None``) instead of poisoning aggregation.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
