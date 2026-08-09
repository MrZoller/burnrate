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

    sessions = {s["session_id"] for s in store.attribution_sessions(90 * 24, now=now)}
    assert sessions == {"old", "fresh"}  # the old session is kept whole, not dropped
    # ...but the old turn contributes nothing to the hourly rollup.
    hourly_total = sum(t for _, t in store.attribution_totals(90 * 24, now=now)["by_project"])
    assert hourly_total == 15


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

    totals = store.attribution_totals(168, now=now)
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

    ids = sorted(s["session_id"] for s in store.attribution_sessions(168, now=now))
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

    sessions = store.attribution_sessions(90 * 24, now=now)
    assert len(sessions) == 1
    span_days = (sessions[0]["end_ts"] - sessions[0]["start_ts"]).total_seconds() / 86400
    assert span_days == pytest.approx(40, abs=0.1)
    assert sessions[0]["total_tokens"] == 1015  # lifetime: the 40-day-old turn included
    # Hourly stays bounded: the 40-day-old turn is past retention, so it is not folded
    # into any hourly window.
    hourly_total = sum(t for _, t in store.attribution_totals(90 * 24, now=now)["by_project"])
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

    total = sum(t for _, t in store.attribution_totals(24, now=now)["by_project"])
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
