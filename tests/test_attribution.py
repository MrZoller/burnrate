"""Local token attribution (issue #16): parser, rollup, and the endpoint.

Covers the tolerant JSONL parser against the fixture files under fixtures/jsonl/,
the incremental byte-offset reader, the SQLite rollup (per project/model/sidechain
sums, per-session spans, and the watermark that prevents double-counting), and the
/api/attribution endpoint's shape for both windows and the empty case.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    assert turns[0].context_tokens == 100 + 200000


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
    totals = store.attribution_totals(168)
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

    sessions = store.attribution_sessions(168)

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
    totals = store.attribution_totals(168)
    assert dict(totals["by_project"])["/home/dev/proj-burnrate"] == 150  # counted once


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
    sessions = store.attribution_sessions(168)
    assert sessions[0]["total_tokens"] == 300  # both turns
    span = (sessions[0]["end_ts"] - sessions[0]["start_ts"]).total_seconds() / 3600
    assert span == pytest.approx(1.0, abs=0.01)


def test_turns_older_than_retention_are_not_stored(tmp_path):
    now = datetime.now(UTC)
    root = _tree(
        tmp_path / "projects",
        {
            "a.jsonl": [
                _assistant(session="old", ts=now - timedelta(days=45)),
                _assistant(session="fresh", ts=now),
            ],
        },
    )
    store = Store(tmp_path / "b.db")
    store.aggregate_jsonl(root, retention_days=30)

    sessions = {s["session_id"] for s in store.attribution_sessions(90 * 24)}
    assert sessions == {"fresh"}


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
