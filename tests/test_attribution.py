"""Local token attribution (issue #16): parser, rollup, and the endpoint.

Covers the tolerant JSONL parser against the fixture files under fixtures/jsonl/,
the incremental byte-offset reader, the SQLite rollup (per project/model/sidechain
sums, per-session spans, and the watermark that prevents double-counting), and the
/api/attribution endpoint's shape for both windows and the empty case.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from burnrate import attribution
from burnrate.app import ATTRIBUTION_SCOPE, create_app
from burnrate.attribution import ParseStats, parse_lines, read_new_lines, turn_from_record
from burnrate.config import Config
from burnrate.store import LARGE_CONTEXT_TOKENS, Store

FIXTURES = Path(__file__).parent / "fixtures" / "jsonl"


def _read(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def _assistant(
    *,
    cwd: str = "/home/dev/proj-burnrate",
    model: str = "claude-opus-4-8",
    session: str = "s1",
    sidechain: bool = False,
    ts: datetime | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    message_id: str | None = None,
    request_id: str | None = None,
    **extra: object,
) -> str:
    ts = ts or datetime.now(UTC)
    record = {
        "type": "assistant",
        "cwd": cwd,
        "sessionId": session,
        "isSidechain": sidechain,
        "timestamp": ts.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
        **extra,
    }
    if message_id is not None:
        record["message"]["id"] = message_id
    if request_id is not None:
        record["requestId"] = request_id
    return json.dumps(record)


def _tree(root: Path, files: dict[str, list[str]]) -> Path:
    """Write a projects-shaped tree: root/<project-dir>/<session>.jsonl."""
    for index, (name, lines) in enumerate(files.items()):
        project_dir = root / f"proj-{index}"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / name).write_text("\n".join(lines) + ("\n" if lines else ""))
    return root


# ------------------------------------------------------------------ parser


def test_valid_fixture_extracts_only_assistant_turns():
    stats = ParseStats()
    turns = list(parse_lines(_read("valid.jsonl"), stats))

    assert stats.lines == 4
    assert stats.emitted == 2
    assert stats.skipped == 2  # the user turn and the ai-title record
    assert stats.malformed == 0
    assert [t.model for t in turns] == ["claude-opus-4-8", "claude-sonnet-5"]
    assert [t.is_sidechain for t in turns] == [False, True]
    assert turns[0].total_tokens == 100 + 50 + 10 + 200000
    # context = input + cache_read + cache_creation (a priming turn is large-context).
    assert turns[0].context_tokens == 100 + 200000 + 10


def test_malformed_lines_are_skipped_and_counted():
    stats = ParseStats()
    turns = list(parse_lines(_read("malformed.jsonl"), stats))

    assert stats.lines == 6
    assert stats.emitted == 2  # the two well-formed assistant records survive
    assert stats.malformed == 4  # bad text, unterminated object, and two non-objects
    assert len(turns) == 2


def test_unknown_fields_are_ignored():
    stats = ParseStats()
    turns = list(parse_lines(_read("unknown_fields.jsonl"), stats))

    assert stats.emitted == 1
    assert stats.skipped == 1  # the unrecognized record type
    # Unknown top-level keys (attributionSkill, futureField) and unknown usage keys
    # (service_tier, cache_creation, a novel token field) change nothing.
    assert turns[0].model == "claude-opus-5"
    assert turns[0].total_tokens == 80 + 30


def test_empty_file_yields_nothing():
    stats = ParseStats()
    turns = list(parse_lines(_read("empty.jsonl"), stats))

    assert turns == []
    assert stats == ParseStats()


def _assistant_record(usage: dict | None = None, *, with_ts: bool = True, **extra) -> dict:
    """A raw assistant record dict, for the drop-and-coerce cases below."""
    message = {"model": extra.pop("model", "claude-opus-4-8")}
    if usage is not None:
        message["usage"] = usage
    record: dict = {"type": "assistant", "message": message, **extra}
    if with_ts:
        record["timestamp"] = datetime.now(UTC).isoformat()
    return record


@pytest.mark.parametrize(
    "record",
    [
        {"type": "user", "message": {"role": "user"}},
        _assistant_record({"input_tokens": 5}, isApiErrorMessage=True),
        _assistant_record({"input_tokens": 5}, model="<synthetic>"),
        _assistant_record(usage=None),
        _assistant_record({"input_tokens": 0, "output_tokens": 0}),
        _assistant_record({"input_tokens": 5}, with_ts=False),
    ],
)
def test_records_without_real_usage_are_dropped(record):
    assert turn_from_record(record) is None


def test_token_counts_are_coerced_defensively():
    # A bool and a string in the usage fields both read as zero; only the real 100 counts.
    usage = {"input_tokens": 100, "output_tokens": True, "cache_read_input_tokens": "lots"}
    turn = turn_from_record(_assistant_record(usage))

    assert turn is not None
    assert turn.total_tokens == 100


def test_a_token_count_above_sqlite_range_reads_as_zero():
    """Codex #1: a bare huge integer is valid JSON but cannot be stored, and letting it
    through overflows the batch commit. It must read as 0, like any other bad value."""
    over_max = 9_223_372_036_854_775_807 + 1
    usage = {"input_tokens": over_max, "output_tokens": 50}
    turn = turn_from_record(_assistant_record(usage))

    assert turn is not None
    assert turn.input_tokens == 0  # the poison value is dropped
    assert turn.total_tokens == 50  # the legitimate field still counts


def test_an_out_of_range_token_count_does_not_freeze_aggregation(tmp_path):
    """The end-to-end failure Codex #1 describes: a record with an oversized token field
    must still let aggregate_jsonl COMMIT and advance the watermark, or every later pass
    re-reads the poison record and re-raises OverflowError forever."""
    now = datetime.now(UTC)
    over_max = 9_223_372_036_854_775_807 * 1000
    poison = json.dumps(
        {
            "type": "assistant",
            "cwd": "/work/a",
            "sessionId": "s1",
            "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": over_max, "output_tokens": 7},
            },
        }
    )
    healthy = _assistant(ts=now, cwd="/work/a", input_tokens=3, output_tokens=50)
    root = _tree(tmp_path / "projects", {"a.jsonl": [poison, healthy]})
    store = Store(tmp_path / "b.db")

    # No OverflowError, and the watermark advances so a second pass reads nothing new.
    first = store.aggregate_jsonl(root)
    second = store.aggregate_jsonl(root)

    assert first.emitted == 2
    assert second.emitted == 0  # committed and watermarked, not re-read forever
    # The poison field became 0 (so the poison record contributes only its output 7);
    # the healthy record's real tokens are intact.
    totals = store.attribution_totals(root, 168, now=now)
    assert dict(totals["by_project"])["/work/a"] == 7 + (3 + 50)


def test_a_field_at_the_sqlite_max_still_commits(tmp_path):
    """The BLOCK the per-field-at-2^63 bound missed: input_tokens at exactly
    SQLITE_MAX_INT passes a 2^63 guard, but total_tokens = max + 50 overflows at flush
    and rolls the batch (watermarks included) back. A plausibility cap rejects the field
    outright, so the sum stays small and the batch commits."""
    now = datetime.now(UTC)
    sqlite_max = 9_223_372_036_854_775_807
    poison = json.dumps(
        {
            "type": "assistant",
            "cwd": "/work/a",
            "sessionId": "s1",
            "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": sqlite_max, "output_tokens": 50},
            },
        }
    )
    root = _tree(tmp_path / "projects", {"a.jsonl": [poison]})
    store = Store(tmp_path / "b.db")

    first = store.aggregate_jsonl(root)  # must not raise OverflowError
    second = store.aggregate_jsonl(root)

    assert first.emitted == 1
    assert second.emitted == 0  # committed and watermark advanced, not re-read forever
    totals = store.attribution_totals(root, 168, now=now)
    assert dict(totals["by_project"])["/work/a"] == 50  # poison field 0, companion intact


def test_the_plausibility_ceiling_rejects_above_but_keeps_a_real_large_turn():
    """Just over MAX_TOKENS_PER_FIELD is garbage; a genuinely large real turn is kept."""
    over = turn_from_record(_assistant_record({"input_tokens": 1_000_000_001, "output_tokens": 5}))
    assert over is not None
    assert over.input_tokens == 0  # 1e9 + 1 -> rejected as implausible
    assert over.output_tokens == 5  # the companion field is untouched

    real = turn_from_record(_assistant_record({"input_tokens": 2_000_000, "output_tokens": 0}))
    assert real is not None
    assert real.input_tokens == 2_000_000  # a big real turn is unchanged, never clamped


def test_deeply_nested_json_is_malformed_not_a_freeze(tmp_path):
    """Codex round 5 #1: deeply-nested JSON raises RecursionError (a RuntimeError, not a
    ValueError), which must be caught as malformed. Left uncaught it escaped parse_lines
    and the fold loop before the watermark flush, freezing all attribution forever."""
    depth = 100_000
    nested = "[" * depth + "]" * depth
    # Guard the premise: this really is a RecursionError -- the case the old tuple missed.
    with pytest.raises(RecursionError):
        json.loads(nested)

    stats = ParseStats()
    assert list(parse_lines([nested], stats)) == []
    assert stats.malformed == 1

    now = datetime.now(UTC)
    healthy = _assistant(ts=now, cwd="/work/a", input_tokens=3, output_tokens=0)
    root = _tree(tmp_path / "projects", {"a.jsonl": [nested, healthy]})
    store = Store(tmp_path / "b.db")

    first = store.aggregate_jsonl(root)  # must not raise RecursionError
    second = store.aggregate_jsonl(root)

    assert first.emitted == 1  # only the healthy record; the poison line is malformed
    assert first.malformed == 1
    assert second.emitted == 0  # committed and watermark advanced, not re-read forever
    totals = store.attribution_totals(root, 168, now=now)
    assert dict(totals["by_project"])["/work/a"] == 3


def test_a_far_future_timestamp_is_dropped_without_freezing(tmp_path):
    """Codex #1: a timestamp that overflows datetime.max when shifted to UTC must be
    dropped at parse, not raise inside aggregate_jsonl (which would freeze the watermark
    and re-raise every later pass)."""
    poison_ts = "9999-12-31T23:59:59-12:00"  # parses, but astimezone(UTC) overflows
    assert (
        turn_from_record(
            {
                "type": "assistant",
                "cwd": "/work/a",
                "sessionId": "s1",
                "timestamp": poison_ts,
                "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 10}},
            }
        )
        is None
    )

    now = datetime.now(UTC)
    poison = json.dumps(
        {
            "type": "assistant",
            "cwd": "/work/a",
            "sessionId": "s1",
            "timestamp": poison_ts,
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    healthy = _assistant(ts=now, cwd="/work/a", input_tokens=3, output_tokens=0)
    root = _tree(tmp_path / "projects", {"a.jsonl": [poison, healthy]})
    store = Store(tmp_path / "b.db")

    first = store.aggregate_jsonl(root)  # must not raise OverflowError
    second = store.aggregate_jsonl(root)

    assert first.emitted == 1  # only the healthy turn; the poison record is dropped
    assert second.emitted == 0  # committed and watermark advanced, not re-read forever
    totals = store.attribution_totals(root, 168, now=now)
    assert dict(totals["by_project"])["/work/a"] == 3


def test_future_dated_data_is_excluded_from_the_window(tmp_path):
    """Codex #4: a future-dated bucket/session (clock skew or garbage) is not in any
    real window, so the upper bound must exclude it -- a lower-bound-only filter keeps
    it forever."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(session="present", ts=now, input_tokens=10, output_tokens=0),
                _assistant(
                    session="future", ts=now + timedelta(hours=2), input_tokens=999, output_tokens=0
                ),
            ],
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    total = sum(t for _, t in store.attribution_totals(root, 24, now=now)["by_project"])
    assert total == 10  # the future hour bucket is excluded

    ids = {s["session_id"] for s in store.attribution_sessions(root, 24, now=now)}
    assert ids == {"present"}  # the future-dated session is excluded


def test_lone_surrogate_metadata_is_repaired():
    """Codex round 6: a valid JSON string can carry a lone surrogate (an unpaired
    \\uD800). It decodes to a str fine but raises UnicodeEncodeError when SQLite binds
    it, and because the flush shares its transaction with the watermark that raise would
    roll the batch back and freeze the offset. The parser must repair cwd / model /
    sessionId to UTF-8-encodable strings so nothing unbindable reaches the store."""
    turn = turn_from_record(
        {
            "type": "assistant",
            "cwd": "/work/\ud800proj",
            "sessionId": "s\ud801",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {
                "model": "claude-\ud802opus",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    assert turn is not None
    # Every string that reaches a SQLite bind is now UTF-8-encodable (no raise).
    for value in (turn.project, turn.model, turn.session_id):
        value.encode("utf-8")


def test_a_lone_surrogate_does_not_freeze_aggregation(tmp_path):
    """The end-to-end failure Codex round 6 describes: a record whose cwd carries a lone
    surrogate must still let aggregate_jsonl COMMIT and advance the watermark. Bound
    verbatim it raises UnicodeEncodeError inside the flush, rolling the batch (watermarks
    included) back so every later pass re-reads the poison line and re-raises forever."""
    now = datetime.now(UTC)
    poison = json.dumps(
        {
            "type": "assistant",
            "cwd": "/work/\ud800bad",
            "sessionId": "s1",
            "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        }
    )
    healthy = _assistant(ts=now, cwd="/work/a", input_tokens=3, output_tokens=0)
    root = _tree(tmp_path / "projects", {"a.jsonl": [poison, healthy]})
    store = Store(tmp_path / "b.db")

    first = store.aggregate_jsonl(root)  # must not raise UnicodeEncodeError
    second = store.aggregate_jsonl(root)

    assert first.emitted == 2
    assert second.emitted == 0  # committed and watermark advanced, not re-read forever
    # The surrogate turn's tokens still count, under a repaired (encodable) project label.
    totals = dict(store.attribution_totals(root, 168, now=now)["by_project"])
    assert totals["/work/a"] == 3
    assert sum(totals.values()) == 3 + 11


def test_a_surrogate_bearing_jsonl_path_commits_and_is_watermarked(tmp_path, monkeypatch):
    """A filename from an undecodable directory entry can contain a surrogate.

    Its string reaches SQLite twice: as the watermark key and, for a turn without
    sessionId, inside the path-derived fallback session id. Both values must be
    bindable, or either failed bind rolls back the shared aggregation transaction
    and makes every later pass re-read the same transcript.
    """
    root = tmp_path / "projects"
    path = root / "proj-0" / "\ud800session.jsonl"
    now = datetime.now(UTC)
    line = json.dumps(
        {
            "type": "assistant",
            "cwd": "/work/a",
            "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        }
    )
    reads: list[int] = []

    monkeypatch.setattr(attribution, "scan_jsonl_files", lambda _: ([path], True))

    def read_once(_: Path, offset: int) -> tuple[list[str], int, bool]:
        reads.append(offset)
        return ([line], 1, True) if offset == 0 else ([], offset, True)

    monkeypatch.setattr(attribution, "read_new_lines_with_health", read_once)
    original_stat = Path.stat

    def stat_with_surrogate_path(self: Path, *, follow_symlinks: bool = True):
        if self == path:
            return SimpleNamespace(st_size=1, st_mtime=0.0)
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_with_surrogate_path)
    store = Store(tmp_path / "b.db")

    first = store.aggregate_jsonl(root)
    second = store.aggregate_jsonl(root)

    assert first.emitted == 1
    assert second.emitted == 0
    assert reads == [0, 1, 1]  # the committed watermark starts the second pass at one
    sessions = store.attribution_sessions(root, 168, now=now)
    assert len(sessions) == 1
    assert sessions[0]["session_id"].startswith("unknown:")
    sessions[0]["session_id"].encode("utf-8")  # the path-derived fallback bound to SQLite
    assert sessions[0]["total_tokens"] == 10  # unchanged pass did not double-count


def test_distinct_surrogate_paths_keep_sqlite_identities(tmp_path, monkeypatch):
    """Raw filename bytes need distinct watermarks and unknown-session fallbacks."""
    root = tmp_path / "projects"
    first = root / "proj-0" / "\ud800session.jsonl"
    second = root / "proj-0" / "\ud801session.jsonl"
    now = datetime.now(UTC)
    lines = {
        first: _assistant(ts=now, session=None, input_tokens=11),
        second: _assistant(ts=now, session=None, input_tokens=13),
    }

    monkeypatch.setattr(attribution, "scan_jsonl_files", lambda _: ([first, second], True))

    def read_once(path: Path, offset: int) -> tuple[list[str], int, bool]:
        return ([lines[path]], 1, True) if offset == 0 else ([], offset, True)

    monkeypatch.setattr(attribution, "read_new_lines_with_health", read_once)
    original_stat = Path.stat

    def stat_with_surrogate_paths(self: Path, *, follow_symlinks: bool = True):
        if self in lines:
            return SimpleNamespace(st_size=1, st_mtime=0.0)
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_with_surrogate_paths)
    store = Store(tmp_path / "b.db")

    assert store.aggregate_jsonl(root).emitted == 2
    assert store.aggregate_jsonl(root).emitted == 0
    with store._connect() as conn:
        watermark_count = conn.execute("SELECT COUNT(*) FROM jsonl_watermarks").fetchone()[0]
    sessions = store.attribution_sessions(root, 168, now=now)

    assert watermark_count == 2
    assert len(sessions) == 2
    assert {session["total_tokens"] for session in sessions} == {61, 63}
    assert all(session["session_id"].startswith("unknown:") for session in sessions)


def test_post_read_stat_failure_keeps_aggregated_turns_watermarked(tmp_path, monkeypatch):
    """A failed metadata refresh must not make already-folded bytes re-readable."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "first.jsonl": [
                _assistant(cwd="/work/first", input_tokens=10, output_tokens=0, ts=now)
            ],
            "second.jsonl": [
                _assistant(cwd="/work/second", input_tokens=20, output_tokens=0, ts=now)
            ],
        },
    )
    first = root / "proj-0" / "first.jsonl"
    original_stat = Path.stat
    first_stat_calls = 0
    fail_post_read = True

    def stat_with_one_post_read_failure(self: Path, *, follow_symlinks: bool = True):
        nonlocal first_stat_calls
        if self == first:
            first_stat_calls += 1
            if fail_post_read and first_stat_calls == 2:
                raise OSError("transient metadata failure")
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_with_one_post_read_failure)
    store = Store(tmp_path / "b.db")

    first_pass = store.aggregate_jsonl(root)
    fail_post_read = False
    second_pass = store.aggregate_jsonl(root)

    assert first_pass.scan_succeeded is False
    assert second_pass.emitted == 0
    totals = dict(store.attribution_totals(root, 168, now=now)["by_project"])
    assert totals == {"/work/second": 20, "/work/first": 10}


def test_a_non_boolean_sidechain_flag_is_not_a_subagent():
    """Codex round 6: isSidechain must be JSON `true` to mean a subagent. bool("false")
    is True, which would misfile a string-flagged main turn under Subagents; `is True`
    accepts only a real boolean, matching every other extracted field's type guard."""
    main = turn_from_record(_assistant_record({"input_tokens": 5}, isSidechain="false"))
    assert main is not None
    assert main.is_sidechain is False  # a truthy non-boolean is NOT a subagent

    sub = turn_from_record(_assistant_record({"input_tokens": 5}, isSidechain=True))
    assert sub is not None
    assert sub.is_sidechain is True  # a real JSON true still reads as a subagent


def test_an_api_error_flag_must_be_a_real_boolean():
    """Codex round 7: isApiErrorMessage drops a turn as a failed request, but a truthy
    non-boolean like the string "false" must NOT drop a legitimate usage turn -- the
    same `is True` convention isSidechain follows."""
    kept = turn_from_record(_assistant_record({"input_tokens": 5}, isApiErrorMessage="false"))
    assert kept is not None  # a truthy non-boolean does not mark it an API error
    assert kept.total_tokens == 5

    dropped = turn_from_record(_assistant_record({"input_tokens": 5}, isApiErrorMessage=True))
    assert dropped is None  # a real JSON true still drops the error turn


def test_session_model_follows_the_latest_timestamp_turn_within_a_pass(tmp_path):
    """Codex round 7: a session's stored model must track the latest turn by TIMESTAMP,
    not by fold order. A newer turn followed IN-FILE by an older-timestamped one (clock
    skew) must not relabel the session to the older turn's model."""
    now = datetime.now(UTC)
    newer = _assistant(session="s1", model="claude-opus-4-8", ts=now, input_tokens=10)
    older = _assistant(
        session="s1", model="claude-sonnet-5", ts=now - timedelta(hours=1), input_tokens=10
    )
    # newer is folded first; the old code's unconditional acc.model = turn.model would
    # then let the older sonnet turn win.
    root = _tree(tmp_path / "projects", {"a.jsonl": [newer, older]})
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    sessions = store.attribution_sessions(root, 168, now=now)
    assert len(sessions) == 1
    assert sessions[0]["model"] == "claude-opus-4-8"  # the latest-ts turn's model


def test_session_model_follows_latest_timestamp_across_passes(tmp_path):
    """The cross-pass half (the _flush_sessions CASE): a later incremental pass folding
    an OLDER-timestamped turn for the same session -- a new file written after the first
    aggregation committed -- must not overwrite the stored model, since end_ts stays the
    newer value."""
    now = datetime.now(UTC)
    store = Store(tmp_path / "b.db")

    newer = _assistant(session="s1", model="claude-opus-4-8", ts=now, input_tokens=10)
    root = _tree(tmp_path / "projects", {"a.jsonl": [newer]})
    store.aggregate_jsonl(root)  # pass 1: session s1 -> opus, end_ts = now

    older = _assistant(
        session="s1", model="claude-sonnet-5", ts=now - timedelta(hours=1), input_tokens=10
    )
    (root / "proj-0" / "b.jsonl").write_text(older + "\n")
    store.aggregate_jsonl(root)  # pass 2: older sonnet turn, same session

    sessions = store.attribution_sessions(root, 168, now=now)
    assert len(sessions) == 1
    assert sessions[0]["model"] == "claude-opus-4-8"  # newer-ts model preserved
    assert sessions[0]["total_tokens"] == (10 + 50) * 2  # both turns still counted


def test_project_labels_are_bounded_for_suffix_nested_paths():
    """Codex #6: when one path is a strict suffix of another, disambiguation stops at
    the cap and marks the shared label truncated, rather than expanding it into a
    near-full path."""
    from burnrate.app import _project_display_names

    names = _project_display_names(
        ["/Users/alice/client/app", "/mnt/backup/Users/alice/client/app"]
    )
    assert set(names.values()) == {"\u2026/alice/client/app"}  # basename + 2 parents, marked
    assert not any(v.startswith("/") for v in names.values())  # never a full path


def test_project_labels_resolve_a_plain_collision_without_a_marker():
    from burnrate.app import _project_display_names

    names = _project_display_names(["/clients/a/app", "/clients/b/app"])
    assert names == {"/clients/a/app": "a/app", "/clients/b/app": "b/app"}


def test_missing_cwd_and_session_fall_back_to_unknown():
    turn = turn_from_record(
        {
            "type": "assistant",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 10}},
        }
    )
    assert turn is not None
    assert turn.project == "unknown"
    assert turn.session_id == "unknown"


# ------------------------------------------------------- incremental reader


def test_read_new_lines_returns_only_complete_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("one\ntwo\npartial-no-newline")

    lines, offset = read_new_lines(path, 0)

    assert lines == ["one", "two"]
    assert offset == len("one\ntwo\n")


def test_read_new_lines_resumes_from_offset(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("one\ntwo\n")
    _, offset = read_new_lines(path, 0)

    with path.open("a") as handle:
        handle.write("three\n")
    lines, new_offset = read_new_lines(path, offset)

    assert lines == ["three"]
    assert new_offset == len("one\ntwo\nthree\n")


def test_read_new_lines_restarts_when_the_file_shrinks(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("one\ntwo\n")
    lines, offset = read_new_lines(path, 999)  # offset past a now-smaller file

    assert lines == ["one", "two"]
    assert offset == len("one\ntwo\n")


def test_read_new_lines_keeps_a_record_with_a_raw_unicode_line_separator(tmp_path):
    """U+2028 / U+2029 / U+0085 are legal raw inside a JSON string, and Node's
    JSON.stringify -- which writes these transcripts -- leaves them raw. A record whose
    content carries one is a single JSONL line: splitting on str.splitlines() would tear
    it into fragments that each fail to parse, dropping the tokens (and, since the
    offset still advances, never retrying them). We must split on "\\n" alone."""
    record = {
        "type": "assistant",
        "cwd": "/work/alpha",
        "sessionId": "s1",
        "timestamp": datetime.now(UTC).isoformat(),
        "message": {
            "model": "claude-opus-4-8",
            "content": "pasted\u2028web\u2029text\u0085here",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    line = json.dumps(record, ensure_ascii=False)  # raw separators, like Node
    path = tmp_path / "s.jsonl"
    path.write_bytes((line + "\n").encode("utf-8"))

    lines, offset = read_new_lines(path, 0)
    stats = ParseStats()
    turns = list(parse_lines(lines, stats))

    assert len(lines) == 1, "the raw separators must not split the record"
    assert stats.emitted == 1
    assert stats.malformed == 0
    assert turns[0].total_tokens == 15
    assert offset == path.stat().st_size


def test_iter_jsonl_files_is_empty_for_a_missing_root(tmp_path):
    assert attribution.iter_jsonl_files(tmp_path / "does-not-exist") == []


def test_scan_jsonl_files_reports_a_skipped_directory(tmp_path, monkeypatch):
    """A partial walk must not let the poller report an incomplete rollup as fresh."""
    root = tmp_path / "projects"
    root.mkdir()
    readable = root / "readable"

    def walk_with_unreadable_directory(path, *, onerror, followlinks):
        assert path == root
        assert followlinks is False
        yield root, ["readable", "locked"], []
        yield readable, [], ["session.jsonl"]
        onerror(PermissionError("locked subtree"))

    monkeypatch.setattr(attribution.os, "walk", walk_with_unreadable_directory)

    paths, scan_succeeded = attribution.scan_jsonl_files(root)

    assert paths == [readable / "session.jsonl"]
    assert scan_succeeded is False


# --------------------------------------------------------------- rollup


def test_aggregate_rolls_up_by_project_model_and_sidechain(tmp_path):
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(cwd="/work/alpha", model="claude-opus-4-8", session="s1", ts=now),
                _assistant(
                    cwd="/work/alpha",
                    model="claude-sonnet-5",
                    session="s1",
                    sidechain=True,
                    ts=now + timedelta(minutes=1),
                    input_tokens=10,
                    output_tokens=5,
                ),
            ],
            "b.jsonl": [
                _assistant(cwd="/work/beta", model="claude-opus-4-8", session="s2", ts=now),
            ],
        },
    )
    store = Store(tmp_path / "b.db")

    stats = store.aggregate_jsonl(root)

    assert stats.emitted == 3
    # Pin the query time past both turns: the sidechain turn is dated now+1min, whose
    # hour bucket rolls to the next hour when the test starts in the last minute of an
    # hour, and the hour_start <= now upper bound would then drop it (~1.6% of runs).
    totals = store.attribution_totals(root, 168, now=now + timedelta(minutes=1))
    assert dict(totals["by_project"]) == {"/work/alpha": 165, "/work/beta": 150}
    assert dict(totals["by_model"]) == {"claude-opus-4-8": 300, "claude-sonnet-5": 15}
    assert totals["by_agent"] == {0: 300, 1: 15}  # main vs sidechain


def test_sessions_span_first_to_last_turn(tmp_path):
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(
                    session="s1", ts=now - timedelta(hours=3), cache_read=LARGE_CONTEXT_TOKENS
                ),
                _assistant(session="s1", ts=now - timedelta(hours=1)),
            ],
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    sessions = store.attribution_sessions(root, 168)

    assert len(sessions) == 1
    span_hours = (sessions[0]["end_ts"] - sessions[0]["start_ts"]).total_seconds() / 3600
    assert span_hours == pytest.approx(2.0, abs=0.01)
    # The first turn read 200k from cache, so its context crosses the large threshold.
    assert sessions[0]["max_turn_context"] >= LARGE_CONTEXT_TOKENS


def test_watermark_prevents_double_counting(tmp_path):
    now = datetime.now(UTC)
    root = _tree(tmp_path / "projects", {"a.jsonl": [_assistant(session="s1", ts=now)]})
    store = Store(tmp_path / "b.db")

    store.aggregate_jsonl(root)
    second = store.aggregate_jsonl(root)  # nothing has grown

    assert second.emitted == 0
    assert second.files_with_new_data == 0
    totals = store.attribution_totals(root, 168)
    assert dict(totals["by_project"])["/home/dev/proj-burnrate"] == 150  # counted once


def test_cross_file_duplicate_response_is_counted_once_in_one_pass(tmp_path):
    """A resume/fork copy must not inflate the initial scan's additive rollups."""
    now = datetime.now(UTC)
    response = _assistant(
        session="s1",
        ts=now,
        message_id="msg-copied",
        request_id="req-copied",
    )
    root = _tree(tmp_path / "projects", {"original.jsonl": [response], "fork.jsonl": [response]})
    store = Store(tmp_path / "b.db")

    store.aggregate_jsonl(root)

    totals = store.attribution_totals(root, 168, now=now)
    sessions = store.attribution_sessions(root, 168, now=now)
    assert dict(totals["by_project"])["/home/dev/proj-burnrate"] == 150
    assert sessions[0]["total_tokens"] == 150


def test_cross_file_duplicate_response_is_skipped_in_a_later_pass(tmp_path):
    """The seen-response identity must survive the watermark transaction and later scans."""
    now = datetime.now(UTC)
    response = _assistant(
        session="s1",
        ts=now,
        message_id="msg-resumed",
        request_id="req-resumed",
    )
    root = _tree(tmp_path / "projects", {"original.jsonl": [response]})
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)
    _tree(root, {"resumed.jsonl": [response]})

    store.aggregate_jsonl(root)

    totals = store.attribution_totals(root, 168, now=now)
    sessions = store.attribution_sessions(root, 168, now=now)
    assert dict(totals["by_project"])["/home/dev/proj-burnrate"] == 150
    assert sessions[0]["total_tokens"] == 150


def test_responses_sharing_only_one_identity_component_are_both_counted(tmp_path):
    """Retries can share one ID, so only the complete message/request pair deduplicates."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "original.jsonl": [
                _assistant(
                    session="s1",
                    ts=now,
                    message_id="msg-first",
                    request_id="req-first",
                )
            ],
            "fork.jsonl": [
                _assistant(
                    session="s1",
                    ts=now,
                    message_id="msg-first",
                    request_id="req-second",
                ),
                _assistant(
                    session="s1",
                    ts=now,
                    message_id="msg-third",
                    request_id="req-first",
                ),
            ],
        },
    )
    store = Store(tmp_path / "b.db")

    store.aggregate_jsonl(root)

    totals = store.attribution_totals(root, 168, now=now)
    sessions = store.attribution_sessions(root, 168, now=now)
    assert dict(totals["by_project"])["/home/dev/proj-burnrate"] == 450
    assert sessions[0]["total_tokens"] == 450


def test_prune_removes_response_identities_past_attribution_retention(tmp_path):
    """The persistent dedup index is bounded with the attribution retention window."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {"session.jsonl": [_assistant(ts=now, message_id="msg-fresh", request_id="req-fresh")]},
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO response_identities"
            " (projects_root, message_id, request_id, response_ts) VALUES (?, ?, ?, ?)",
            (
                str(root.resolve()),
                "msg-expired",
                "req-expired",
                (now - timedelta(days=31)).isoformat(),
            ),
        )

    store.prune(attribution_days=30)

    with store._connect() as conn:
        identities = conn.execute(
            "SELECT message_id, request_id FROM response_identities ORDER BY message_id"
        ).fetchall()
    assert [(row["message_id"], row["request_id"]) for row in identities] == [
        ("msg-fresh", "req-fresh")
    ]


def test_response_identities_are_scoped_to_projects_root(tmp_path):
    """A copied identity in a different configured transcript root is independent."""
    now = datetime.now(UTC)
    response = _assistant(ts=now, message_id="msg-shared", request_id="req-shared")
    first_root = _tree(tmp_path / "first-projects", {"session.jsonl": [response]})
    second_root = _tree(tmp_path / "second-projects", {"session.jsonl": [response]})
    store = Store(tmp_path / "b.db")

    store.aggregate_jsonl(first_root)
    store.aggregate_jsonl(second_root)

    assert (
        dict(store.attribution_totals(first_root, 168, now=now)["by_project"])[
            "/home/dev/proj-burnrate"
        ]
        == 150
    )
    assert (
        dict(store.attribution_totals(second_root, 168, now=now)["by_project"])[
            "/home/dev/proj-burnrate"
        ]
        == 150
    )


def test_appended_turns_extend_an_existing_session(tmp_path):
    now = datetime.now(UTC)
    session_file = tmp_path / "projects" / "proj-0" / "a.jsonl"
    _tree(
        tmp_path / "projects", {"a.jsonl": [_assistant(session="s1", ts=now - timedelta(hours=1))]}
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(tmp_path / "projects")

    with session_file.open("a") as handle:
        handle.write(_assistant(session="s1", ts=now) + "\n")
    second = store.aggregate_jsonl(tmp_path / "projects")

    assert second.emitted == 1  # only the appended line
    sessions = store.attribution_sessions(tmp_path / "projects", 168)
    assert sessions[0]["total_tokens"] == 300  # both turns
    span = (sessions[0]["end_ts"] - sessions[0]["start_ts"]).total_seconds() / 3600
    assert span == pytest.approx(1.0, abs=0.01)


def test_retention_cutoff_bounds_hourly_but_not_sessions(tmp_path):
    """The 30-day cutoff gates the HOURLY fold only (Codex #4). An old turn still forms
    its session -- so the "lifetime" label stays honest -- but adds nothing to any
    hourly window."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(
                    session="old", ts=now - timedelta(days=45), input_tokens=1000, output_tokens=0
                ),
                _assistant(session="fresh", ts=now, input_tokens=15, output_tokens=0),
            ],
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root, retention_days=30)

    sessions = {s["session_id"] for s in store.attribution_sessions(root, 90 * 24, now=now)}
    assert sessions == {"old", "fresh"}  # the old session is kept whole, not dropped
    # ...but the old turn contributes nothing to the hourly rollup.
    hourly_total = sum(t for _, t in store.attribution_totals(root, 90 * 24, now=now)["by_project"])
    assert hourly_total == 15


def test_switching_projects_roots_hides_history_then_restores_its_namespace(tmp_path, monkeypatch):
    """A root switch is a view change, not a destructive rollup reset.

    The first spelling intentionally contains ``nested/..``: it must select the
    same persisted namespace as the canonical spelling used after switching back.
    """

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("burnrate.poller.Poller.start", _noop)
    monkeypatch.setattr("burnrate.poller.Poller.stop", _noop)
    now = datetime.now(UTC)
    old_root = _tree(
        tmp_path / "old-projects",
        {"old.jsonl": [_assistant(cwd="/work/old", ts=now, input_tokens=11, output_tokens=0)]},
    )
    (old_root / "nested").mkdir()
    new_root = _tree(
        tmp_path / "new-projects",
        {"new.jsonl": [_assistant(cwd="/work/new", ts=now, input_tokens=29, output_tokens=0)]},
    )
    db_path = tmp_path / "burnrate.db"

    old_config = Config(db_path=db_path, attribution_dir=old_root / "nested" / "..")
    old_app = create_app(old_config)
    old_app.state.store.aggregate_jsonl(old_config.attribution_dir)
    with TestClient(old_app) as old_client:
        assert old_client.get("/api/attribution").json()["total_tokens"] == 11

    new_config = Config(db_path=db_path, attribution_dir=new_root)
    new_app = create_app(new_config)
    with TestClient(new_app) as new_client:
        # The new tree has not been folded yet; prior-root history must not leak in.
        assert new_client.get("/api/attribution").json()["total_tokens"] == 0
        new_app.state.store.aggregate_jsonl(new_config.attribution_dir)
        new_body = new_client.get("/api/attribution").json()

    assert new_body["total_tokens"] == 29
    assert [row["label"] for row in new_body["by_project"]] == ["new"]

    restored_app = create_app(Config(db_path=db_path, attribution_dir=old_root))
    with TestClient(restored_app) as restored_client:
        restored = restored_client.get("/api/attribution").json()

    assert restored["total_tokens"] == 11
    assert [row["label"] for row in restored["by_project"]] == ["old"]


def test_migration_quarantines_existing_unscoped_rollups_without_clearing_them(
    tmp_path, monkeypatch
):
    """An old database cannot prove which root made a row, so it is retained but hidden.

    Assigning it to whichever root happens to be active at upgrade would silently
    mix old data into that root. Dropping it would violate the no-clearing decision.
    """

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("burnrate.poller.Poller.start", _noop)
    monkeypatch.setattr("burnrate.poller.Poller.stop", _noop)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE hourly_usage (
            hour_start TEXT NOT NULL, project TEXT NOT NULL, model TEXT NOT NULL,
            is_sidechain INTEGER NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            large_context_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_start, project, model, is_sidechain)
        );
        CREATE TABLE sessions_rollup (
            session_id TEXT PRIMARY KEY, project TEXT NOT NULL, model TEXT NOT NULL,
            start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, total_tokens INTEGER NOT NULL DEFAULT 0,
            max_turn_context INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE jsonl_watermarks (
            path TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER, mtime REAL
        );
        """
    )
    legacy.execute(
        "INSERT INTO hourly_usage VALUES (?, '/work/legacy', 'claude-opus-4-8', 0, 17, 0, 0, 0, 0)",
        (now.isoformat(),),
    )
    legacy.execute(
        "INSERT INTO sessions_rollup VALUES ('legacy-session', '/work/legacy',"
        " 'claude-opus-4-8', ?, ?, 17, 0)",
        (now.isoformat(), now.isoformat()),
    )
    legacy.commit()
    legacy.close()

    root = tmp_path / "new-projects"
    root.mkdir()
    app = create_app(Config(db_path=db_path, attribution_dir=root))
    with TestClient(app) as client:
        body = client.get("/api/attribution").json()

    assert body["total_tokens"] == 0
    assert body["top_sessions"] == []
    # Migration preserves the legacy records for an explicit legacy namespace; it
    # must not solve the isolation bug by deleting a user's retained history.
    with app.state.store._connect() as conn:
        assert conn.execute("SELECT SUM(input_tokens) FROM hourly_usage").fetchone()[0] == 17
        assert conn.execute("SELECT COUNT(*) FROM sessions_rollup").fetchone()[0] == 1


# --------------------------------------------------------------- endpoint


@pytest.fixture
def attribution_client(tmp_path, monkeypatch):
    """An app whose poller never runs, with attribution seeded from a fixture tree."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("burnrate.poller.Poller.start", _noop)
    monkeypatch.setattr("burnrate.poller.Poller.stop", _noop)

    def _build(files: dict[str, list[str]] | None = None) -> TestClient:
        projects = tmp_path / "projects"
        projects.mkdir(exist_ok=True)
        if files:
            _tree(projects, files)
        config = Config(db_path=tmp_path / "api.db", attribution_dir=projects)
        app = create_app(config)
        if files:
            app.state.store.aggregate_jsonl(projects)
        return TestClient(app)

    return _build


def test_attribution_endpoint_returns_the_panels(attribution_client):
    now = datetime.now(UTC)
    client = attribution_client(
        {
            "a.jsonl": [
                _assistant(
                    cwd="/work/alpha",
                    model="claude-opus-4-8",
                    ts=now,
                    cache_read=LARGE_CONTEXT_TOKENS,
                ),
                _assistant(
                    cwd="/work/beta", model="claude-sonnet-5", session="s2", sidechain=True, ts=now
                ),
            ],
        }
    )
    with client:
        body = client.get("/api/attribution?window=7d").json()

    assert body["window"] == "7d"
    assert body["hours"] == 168.0
    assert body["scope"] == ATTRIBUTION_SCOPE
    assert body["total_tokens"] > 0
    # by_project uses the readable basename, never the full path.
    labels = {row["label"] for row in body["by_project"]}
    assert labels == {"alpha", "beta"}
    assert {row["label"] for row in body["by_agent"]} == {"Main", "Subagents"}
    assert body["large_context"]["threshold_tokens"] == LARGE_CONTEXT_TOKENS
    assert body["large_context"]["share"] > 0
    assert "top_sessions" in body


def test_attribution_endpoint_handles_the_24h_window(attribution_client):
    now = datetime.now(UTC)
    client = attribution_client(
        {
            "a.jsonl": [
                _assistant(ts=now),  # inside 24h
                _assistant(session="old", ts=now - timedelta(days=3)),  # outside 24h
            ],
        }
    )
    with client:
        day = client.get("/api/attribution?window=24h").json()
        week = client.get("/api/attribution?window=7d").json()

    assert day["window"] == "24h"
    assert day["hours"] == 24.0
    assert day["total_tokens"] < week["total_tokens"]


def test_large_context_share_is_bounded_by_the_window(attribution_client):
    """Regression (blinded acceptance): a large-context turn 6 days ago and a tiny
    turn 1h ago in the SAME session must not make the 24h window read 100%
    large-context. The share is windowed from hourly_usage.large_context_tokens, so
    only in-window large-context tokens count toward it."""
    now = datetime.now(UTC)
    client = attribution_client(
        {
            "a.jsonl": [
                _assistant(  # large context, but OUTSIDE the 24h window
                    session="s1", ts=now - timedelta(days=6), cache_read=LARGE_CONTEXT_TOKENS
                ),
                _assistant(  # tiny, non-large, INSIDE the 24h window
                    session="s1", ts=now - timedelta(hours=1), input_tokens=15, output_tokens=0
                ),
            ],
        }
    )
    with client:
        day = client.get("/api/attribution?window=24h").json()
        week = client.get("/api/attribution?window=7d").json()

    # Only the 15-token, non-large turn is inside 24h -> no large-context tokens there.
    assert day["large_context"]["tokens"] == 0
    assert day["large_context"]["share"] == 0.0
    # Over 7d the large turn is in range and dominates -> a real, positive share.
    assert week["large_context"]["tokens"] >= LARGE_CONTEXT_TOKENS
    assert week["large_context"]["share"] > 0


def test_top_sessions_report_lifetime_totals_not_a_windowed_share(attribution_client):
    """The longest-sessions panel is descriptive, not a windowed percentage. Its token
    figure is the session's LIFETIME total (both turns), even for the 24h window in
    which only the later turn falls, and the ambiguous windowed `long_sessions` share
    is gone entirely."""
    now = datetime.now(UTC)
    client = attribution_client(
        {
            "a.jsonl": [
                _assistant(
                    session="s1",
                    cwd="/work/alpha",
                    ts=now - timedelta(days=5),
                    input_tokens=1000,
                    output_tokens=0,
                ),
                _assistant(
                    session="s1",
                    cwd="/work/alpha",
                    ts=now - timedelta(hours=1),
                    input_tokens=15,
                    output_tokens=0,
                ),
            ],
        }
    )
    with client:
        body = client.get("/api/attribution?window=24h").json()

    assert "long_sessions" not in body  # no faked windowed share for sessions
    top = body["top_sessions"]
    assert len(top) == 1
    assert top[0]["lifetime_tokens"] == 1015  # both turns, not only the in-window one
    assert "tokens" not in top[0]  # the ambiguous key is gone
    assert top[0]["duration_hours"] > 0


def test_attribution_endpoint_is_empty_but_valid_with_no_data(attribution_client):
    client = attribution_client()  # no tree, nothing aggregated
    with client:
        body = client.get("/api/attribution").json()

    assert body["total_tokens"] == 0
    assert body["scope"] == ATTRIBUTION_SCOPE  # scope is present even with no data
    assert body["by_project"] == []
    assert body["top_sessions"] == []


async def test_attribution_endpoint_exposes_stale_and_failed_aggregation_health(
    attribution_client, monkeypatch
):
    client = attribution_client()
    poller = client.app.state.poller
    now = datetime.now(UTC)

    with client:
        await poller._maybe_aggregate(now)
        fresh = client.get("/api/attribution").json()["aggregation"]

        assert fresh["healthy"] is True
        assert fresh["stale"] is False
        assert fresh["staleness_seconds"] is not None
        assert 0 <= fresh["staleness_seconds"] < 5

        poller.attribution_status.last_success_at = now - timedelta(seconds=1801)
        poller.attribution_status.last_attempt_at = now - timedelta(seconds=1800)
        stale = client.get("/api/attribution").json()["aggregation"]

        assert stale["healthy"] is False
        assert stale["stale"] is True
        assert stale["staleness_seconds"] == pytest.approx(1801, abs=5)
        assert stale["stale_after_seconds"] == 1800.0
        assert stale["consecutive_failures"] == 0

        # A failed pass must expose its health state, but not its raw exception text.
        poller.attribution_status.last_success_at = datetime.now(UTC)

        def boom(*args, **kwargs):
            raise RuntimeError("corrupt transcript tree: /private/logs")

        monkeypatch.setattr(client.app.state.store, "aggregate_jsonl", boom)
        await poller._maybe_aggregate(now + timedelta(minutes=10))
        response = client.get("/api/attribution")
        failed = response.json()["aggregation"]

    assert failed["healthy"] is False
    assert failed["stale"] is True
    assert failed["consecutive_failures"] == 1
    assert failed["last_success_at"] is not None
    assert failed["last_attempt_at"] is not None
    assert "corrupt transcript tree" not in response.text
    assert set(failed) == {
        "healthy",
        "stale",
        "staleness_seconds",
        "stale_after_seconds",
        "last_success_at",
        "last_attempt_at",
        "consecutive_failures",
    }


def test_attribution_endpoint_defaults_an_unknown_window(attribution_client):
    client = attribution_client()
    with client:
        body = client.get("/api/attribution?window=banana").json()

    assert body["window"] == "7d"


def test_attribution_endpoint_never_leaks_a_credential(attribution_client):
    now = datetime.now(UTC)
    client = attribution_client({"a.jsonl": [_assistant(ts=now)]})
    with client:
        raw = client.get("/api/attribution?window=7d").text

    assert "sk-ant" not in raw


def test_same_basename_projects_are_disambiguated(attribution_client):
    """Codex #6: /clients/a/app and /clients/b/app must not both render as "app" and
    each claim a top-N slot. Colliding basenames grow a parent segment."""
    now = datetime.now(UTC)
    client = attribution_client(
        {
            "a.jsonl": [_assistant(cwd="/clients/a/app", session="a", ts=now, input_tokens=100)],
            "b.jsonl": [_assistant(cwd="/clients/b/app", session="b", ts=now, input_tokens=50)],
        }
    )
    with client:
        body = client.get("/api/attribution?window=7d").json()

    labels = {row["label"] for row in body["by_project"]}
    assert labels == {"a/app", "b/app"}


# ------------------------------------------------- Codex review regressions


def _assistant_no_session(cwd: str, ts: datetime, tokens: int = 15) -> str:
    """An assistant record that omits sessionId entirely."""
    return json.dumps(
        {
            "type": "assistant",
            "cwd": cwd,
            "timestamp": ts.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": tokens, "output_tokens": 0},
            },
        }
    )


def test_context_tokens_includes_cache_creation():
    """Codex #1: a cache-priming turn (small input, no cache read, huge cache creation)
    is a large-context turn; context must count cache_creation."""
    turn = turn_from_record(
        json.loads(
            _assistant(input_tokens=5, output_tokens=1, cache_creation=200_000, cache_read=3)
        )
    )
    assert turn is not None
    assert turn.context_tokens == 5 + 3 + 200_000


def test_a_cache_priming_turn_counts_toward_large_context(tmp_path):
    """Codex #1, end to end: a priming turn's tokens land in large_context_tokens."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(
                    ts=now,
                    input_tokens=5,
                    output_tokens=1,
                    cache_creation=LARGE_CONTEXT_TOKENS,
                    cache_read=0,
                )
            ]
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    totals = store.attribution_totals(root, 168, now=now)
    assert totals["large_context_tokens"] == 5 + 1 + LARGE_CONTEXT_TOKENS


def test_missing_session_id_does_not_merge_across_files(tmp_path):
    """Codex #3: sessionId-less turns get a per-file identity, so two different files'
    unknowns stay two sessions rather than collapsing into one fabricated one."""
    now = datetime.now(UTC)
    projects = tmp_path / "projects"
    (projects / "projA").mkdir(parents=True)
    (projects / "projB").mkdir(parents=True)
    (projects / "projA" / "a.jsonl").write_text(_assistant_no_session("/work/a", now) + "\n")
    (projects / "projB" / "b.jsonl").write_text(_assistant_no_session("/work/b", now) + "\n")
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(projects)

    ids = sorted(s["session_id"] for s in store.attribution_sessions(projects, 168, now=now))
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(i.startswith("unknown:") for i in ids)


def test_session_lifetime_survives_the_retention_cutoff(tmp_path):
    """Codex #4: a session spanning >30 days reports its full lifetime span and total,
    while the hourly rollup stays bounded to the retention window."""
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(
                    session="s1", ts=now - timedelta(days=40), input_tokens=1000, output_tokens=0
                ),
                _assistant(
                    session="s1", ts=now - timedelta(hours=1), input_tokens=15, output_tokens=0
                ),
            ],
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root, retention_days=30)

    sessions = store.attribution_sessions(root, 90 * 24, now=now)
    assert len(sessions) == 1
    span_days = (sessions[0]["end_ts"] - sessions[0]["start_ts"]).total_seconds() / 86400
    assert span_days == pytest.approx(40, abs=0.1)
    assert sessions[0]["total_tokens"] == 1015  # lifetime: the 40-day-old turn included
    # Hourly stays bounded: the 40-day-old turn is past retention, so it is not folded
    # into any hourly window.
    hourly_total = sum(t for _, t in store.attribution_totals(root, 90 * 24, now=now)["by_project"])
    assert hourly_total == 15


def test_totals_cutoff_is_floored_to_the_hour(tmp_path):
    """Codex #7: hour_start is hour-floored, so the cutoff must be too, or the oldest
    boundary hour is dropped and up to ~59 min of in-window usage is lost."""
    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    # A turn 15 minutes into the boundary hour of a 24h window: its hour bucket equals
    # the floored cutoff (included), but sits before the un-floored :30 cutoff (dropped).
    boundary_ts = (now - timedelta(hours=24)).replace(minute=15)
    root = _tree(
        tmp_path / "projects",
        {"a.jsonl": [_assistant(ts=boundary_ts, input_tokens=42, output_tokens=0)]},
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root)

    total = sum(t for _, t in store.attribution_totals(root, 24, now=now)["by_project"])
    assert total == 42


def test_read_new_lines_drains_a_file_larger_than_the_cap(tmp_path):
    """Codex #8: a per-pass read cap must still deliver every line exactly once across
    successive calls, with the offset advancing correctly."""
    path = tmp_path / "big.jsonl"
    records = [json.dumps({"n": i}) for i in range(40)]
    path.write_text("\n".join(records) + "\n")

    collected: list[str] = []
    offset = 0
    for _ in range(500):  # bounded so a stall fails rather than hangs
        lines, offset = read_new_lines(path, offset, max_bytes=32)  # tiny cap forces many passes
        if not lines:
            break
        collected.extend(lines)

    assert collected == records  # every line, in order, none dropped or duplicated
    assert offset == path.stat().st_size


def test_read_new_lines_reads_a_single_line_longer_than_the_cap(tmp_path):
    """A line larger than the cap must still be read whole, or the drain would stall."""
    path = tmp_path / "one.jsonl"
    line = json.dumps({"x": "y" * 500})
    path.write_text(line + "\n")

    lines, offset = read_new_lines(path, 0, max_bytes=16)

    assert lines == [line]
    assert offset == path.stat().st_size
