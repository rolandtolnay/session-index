"""Claude Design ownership through ingestion and evidence-query interfaces."""
import json
from pathlib import Path

import pytest

import db
import indexer
from evidence_find import find_candidates
from evidence_inspect import inspect_ref
from session_identity import canonical_session_id
from sources import discover_claude_sessions
from tests.test_pi_continuations import copy, transfer

PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / "data"
    artifacts = data / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "sessions.db"))
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("summarizer.summarize", lambda **kw: pytest.fail("Unexpected model call"))
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: pytest.fail("Unexpected model call"))
    return tmp_path, artifacts


def write_rows(path, rows, *, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def claude_rows(native=CHILD, *, header=True, entrypoint="sdk-cli", blocks=False):
    prompt = f"[claude-design v1] run design-run · parent pi session {PARENT}\n\nDesign the calendar" if header else "Repair the roadmap"
    return [
        {"type": role, "entrypoint": entrypoint, "sessionId": native,
         "timestamp": f"2026-10-27T12:00:0{i}Z", "cwd": "/tmp/project",
         "message": {"role": role, "content": [{"type": "text", "text": text}] if blocks or role == "assistant" else text}}
        for i, (role, text) in enumerate([( "user", prompt), ("assistant", "Implemented the calendar layout.")])
    ]


def child_file(root, **kwargs):
    return write_rows(root / ".claude/projects/project" / f"{kwargs.get('native', CHILD)}.jsonl", claude_rows(**kwargs))


def pi_rows(*, call=True, identity=CHILD, completion=True):
    details = {"claudeSessionId": identity, "runId": "design-run", "state": "running"}
    rows = [
        {"type": "session", "version": 3, "id": PARENT, "cwd": "/tmp/project", "timestamp": "2026-10-27T12:00:00Z"},
        {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Implement calendar"}},
        {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The calendar implementation is ready."},
            *([{"type": "toolCall", "id": "design-call", "name": "functions.claude_design", "arguments": {"task": "Design the calendar"}}] if call else []),
        ]}},
    ]
    if call:
        rows.append({"type": "message", "id": "r", "parentId": "a", "message": {
            "role": "toolResult", "toolCallId": "design-call", "toolName": "claude_design", "content": "Run receipt", "details": details}})
        if completion:
            rows.append({"type": "custom_message", "id": "c", "parentId": "r", "customType": "claude-design-completion",
                         "content": "Completed", "details": {**details, "state": "completed"}})
    return rows


def parent_file(root, **kwargs):
    return write_rows(root / ".pi/agent/sessions/project" / f"date_{PARENT}.jsonl", pi_rows(**kwargs))


def index(source, path):
    return indexer.index_source_transcript(source, str(path), indexer.NO_SUMMARY_INDEX_OPTIONS)


def rows(table):
    conn = db.get_connection()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM " + table)]
    finally:
        conn.close()


@pytest.mark.parametrize("child_first", [True, False])
def test_ownership_attachment_queries_and_raw_preservation(store, child_first):
    root, artifacts = store
    child = child_file(root, blocks=child_first)
    before = child.read_bytes()
    if child_first:
        # Recognition cannot require even the parent's raw source to exist yet.
        assert index("claude", child).skipped_reason
        assert rows("sessions") == []
    parent = parent_file(root)
    index("pi", parent)
    if not child_first:
        index("claude", child)
    owner = canonical_session_id("pi", PARENT)
    assert [row["session_id"] for row in rows("sessions")] == [owner]
    assert not (artifacts / f"{canonical_session_id('claude', CHILD)}.md").exists()
    run, = rows("subagent_runs")
    assert (run["requested_agent_type"], run["match_confidence"], run["status"], run["task_preview"]) == (
        "claude-design", "exact", "completed", "Design the calendar")
    child_path = Path(run["transcript_path"])
    assert child_path.parent == artifacts / owner
    assert "[claude-design v1]" in child_path.read_text()
    assert str(child_path) in (artifacts / f"{owner}.md").read_text()
    conn = db.get_connection()
    try:
        refs = inspect_ref(conn, f"session/{owner}")["inspect_refs"]["subagents"]
        assert len(refs) == 1 and refs[0]["requested_agent_type"] == "claude-design"
        found = find_candidates(conn, subagent="claude-design")["results"]
        assert len(found) == 1 and found[0]["ref"] == refs[0]["ref"]
        assert inspect_ref(conn, refs[0]["ref"])["evidence"]
    finally:
        conn.close()
    assert child.read_bytes() == before
    assert discover_claude_sessions() == []


def test_identity_mismatch_does_not_fall_back_to_order_or_consume_ordinary_request(store):
    root, _ = store
    parent = parent_file(root, identity="different-child")
    data = [json.loads(line) for line in parent.read_text().splitlines()]
    data[2]["message"]["content"].insert(0, {"type": "toolCall", "id": "worker", "name": "subagent", "arguments": {"agent": "worker"}})
    write_rows(parent, data)
    # No parent refresh: this also exercises exact source lookup before indexing.
    index("claude", child_file(root))
    facts = rows("subagent_runs")
    assert [(row["requested_agent_type"], row["match_confidence"]) for row in facts] == [
        ("worker", "request_only"), ("claude-design", "request_only"), ("claude-design", "artifact_only")]
    parent_text = Path(rows("sessions")[0]["transcript_path"]).read_text()
    assert f"→ see: {Path(facts[-1]['transcript_path']).name}" not in parent_text


def test_resume_updates_one_artifact_and_one_row(store):
    root, _ = store
    parent = parent_file(root)
    child = child_file(root)
    index("pi", parent)
    original, = rows("subagent_runs")
    write_rows(child, [{"type": "assistant", "entrypoint": "sdk-cli", "sessionId": CHILD,
                        "message": {"content": "Corrected the calendar spacing."}}], append=True)
    # The extension allocates a fresh invocation runId on resume, not a new header.
    write_rows(parent, [
        {"type": "message", "id": "resume", "parentId": "c", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "resume-call", "name": "claude_design", "arguments": {"session": CHILD, "task": "Correct spacing"}}]}},
        {"type": "message", "id": "resumed", "parentId": "resume", "message": {"role": "toolResult", "toolCallId": "resume-call", "toolName": "claude_design", "content": "Done",
            "details": {"claudeSessionId": CHILD, "runId": "resumed-run", "state": "failed"}}},
        # A delayed completion for the original invocation must not overwrite the resume.
        {"type": "custom_message", "id": "late", "parentId": "resumed", "customType": "claude-design-completion",
         "details": {"claudeSessionId": CHILD, "runId": "design-run", "state": "completed"}},
    ], append=True)
    before = child.read_bytes()
    index("claude", child)
    updated, = rows("subagent_runs")
    assert updated["transcript_path"] == original["transcript_path"]
    assert updated["match_confidence"] == "exact"
    assert (updated["call_tool_id"], updated["task_preview"], updated["status"]) == (
        "resume-call", "Correct spacing", "failed")
    assert "Corrected the calendar spacing." in Path(updated["transcript_path"]).read_text()
    assert child.read_bytes() == before


@pytest.mark.parametrize("header,entrypoint", [(False, "sdk-cli"), (True, "cli")])
def test_ordinary_claude_sessions_stay_top_level(store, header, entrypoint):
    root, _ = store
    child = child_file(root, header=header, entrypoint=entrypoint)
    assert [source.path for source in discover_claude_sessions()] == [str(child)]
    result = index("claude", child)
    assert not result.skipped_reason
    assert [row["session_id"] for row in rows("sessions")] == [canonical_session_id("claude", CHILD)]
    assert Path(result.transcript_path).is_file()


def test_recognised_child_removes_historical_standalone_row(store):
    root, _ = store
    child = child_file(root)
    conn = db.get_connection()
    try:
        db.init_db(conn)
        db.upsert_session(conn, session_id=canonical_session_id("claude", CHILD), source="claude", native_session_id=CHILD)
    finally:
        conn.close()
    index("claude", child)
    assert rows("sessions") == []


def test_child_refresh_follows_worktree_owner(store):
    root, artifacts = store
    parent = parent_file(root)
    child = child_file(root)
    original = index("pi", parent)
    destination = copy(parent, parent.parent / "continued.jsonl", "destination")
    transfer(parent, destination)
    index("pi", destination)
    write_rows(child, [{"type": "assistant", "entrypoint": "sdk-cli", "sessionId": CHILD,
                        "message": {"content": "Finished after transfer."}}], append=True)
    index("claude", child)
    session, = rows("sessions")
    run, = rows("subagent_runs")
    assert session["source_path"] == str(destination)
    assert run["parent_session_id"] == original.session_id
    assert Path(run["transcript_path"]).parent == artifacts / original.session_id
    assert "Finished after transfer." in Path(run["transcript_path"]).read_text()
