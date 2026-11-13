"""Headless Pi children launched from a Claude session are its Subagent Runs."""
from pathlib import Path

import db
from evidence_find import find_candidates
from session_identity import canonical_session_id
from tests.test_claude_design import claude_rows, index, rows, store, write_rows  # noqa: F401

PARENT = "33333333-3333-4333-8333-333333333333"


def pi_child(root):
    return write_rows(root / ".claude/projects/project" / PARENT / "pi-subagents/consultant-0a1b2c3d/session.jsonl", [
        {"type": "session", "version": 3, "id": "child-native", "cwd": "/tmp/project", "timestamp": "2026-11-12T10:00:00Z"},
        {"type": "message", "id": "u", "parentId": None, "message": {"role": "user", "content": "Review the retry change"}},
        {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The retry can deliver a webhook twice."}]}},
    ])


def test_pi_child_is_a_subagent_run_of_its_claude_parent(store):
    root, artifacts = store
    child = pi_child(root)
    # The child's own Pi lifecycle reports it first; it never becomes a session.
    assert index("pi", child).skipped_reason == "nested Pi subagent session"
    parent = write_rows(root / ".claude/projects/project" / f"{PARENT}.jsonl", [
        *claude_rows(PARENT, header=False, entrypoint="cli"),
        {"type": "assistant", "entrypoint": "cli", "sessionId": PARENT, "timestamp": "2026-10-27T12:00:02Z", "cwd": "/tmp/project",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "explore", "name": "Agent", "input": {"subagent_type": "Explore", "prompt": "Map the worker"}}]}},
    ])
    index("claude", parent)
    owner = canonical_session_id("claude", PARENT)
    assert [row["session_id"] for row in rows("sessions")] == [owner]
    facts = rows("subagent_runs")
    # The run directory names the agent; a native Agent request never claims it by order.
    assert [(row["requested_agent_type"], row["match_confidence"]) for row in facts] == [
        ("Explore", "request_only"), ("consultant", "artifact_only")]
    child_path = Path(facts[-1]["transcript_path"])
    assert child_path.parent == artifacts / owner
    assert "The retry can deliver a webhook twice." in child_path.read_text()
    assert f"- Subagent Run: consultant → `{child_path}`" in (artifacts / f"{owner}.md").read_text()
    conn = db.get_connection()
    try:
        assert len(find_candidates(conn, subagent="consultant")["results"]) == 1
    finally:
        conn.close()


def test_previously_indexed_pi_child_is_not_top_level(store):
    root, _ = store
    conn = db.get_connection()
    try:
        db.init_db(conn)
        db.upsert_session(conn, session_id="pi:child", source="pi", native_session_id="child-native",
                          source_path=str(root / ".claude/projects/project" / PARENT / "pi-subagents/scout-0a1b2c3d/session.jsonl"))
        db.upsert_session(conn, session_id="pi:top", source="pi", native_session_id="top-native",
                          source_path=str(root / ".pi/agent/sessions/project/top.jsonl"))
        top_level = conn.execute(f"SELECT session_id FROM sessions WHERE {db.TOP_LEVEL_SESSION_PREDICATE}").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in top_level] == ["pi:top"]
