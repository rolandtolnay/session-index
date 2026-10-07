"""Current Codex completed-item compatibility and ownership regressions."""
import json
import sqlite3
from pathlib import Path

import pytest

import db
import indexer
from codex_items import CodexFormatError
from codex_parser import parse_codex_jsonl
from session_identity import canonical_session_id
from sources import resolve_codex_source
from tool_facts import build_file_mutation_rows


def row(kind, **payload):
    return {"timestamp": "2026-10-06T10:00:01.000Z", "type": kind, "payload": payload}


def completed(kind, identity, **fields):
    return row("event_msg", type="item_completed", turn_id="turn-1",
               item={"type": kind, "id": identity, **fields})


def modern_rows():
    return [
        row("session_meta", id="parent", cwd="/tmp", thread_source="user"),
        row("event_msg", type="task_started", turn_id="turn-1"),
        row("response_item", type="message", role="user", content=[{"text": "Injected instructions"}]),
        completed("UserMessage", "u1", content=[{"type": "text", "text": "Fix it"}]),
        # Mirrored legacy events must not create duplicate messages or calls.
        row("event_msg", type="user_message", message="Fix it"),
        row("response_item", type="message", id="a1", role="assistant", content=[{"text": "Fixed"}]),
        completed("AgentMessage", "a1", content=[{"type": "Text", "text": "Fixed"}]),
        row("response_item", type="function_call", name="apply_patch", call_id="patch", arguments={}),
        completed("FileChange", "patch", status="completed", changes={
            "/tmp/old.py": {"type": "update", "move_path": "/tmp/new.py"}}),
        completed("FileChange", "failed-patch", status="failed", changes={"/tmp/failed.py": {"type": "add"}}),
        row("response_item", type="function_call", name="exec_command", call_id="cmd", arguments={}),
        completed("CommandExecution", "cmd", command=["sh", "-c", "exit 2"], cwd="/tmp",
                  status="completed", exit_code=2, aggregated_output="failure"),
        row("response_item", type="function_call", name="query", call_id="mcp", arguments={}),
        completed("McpToolCall", "mcp", server="example", tool="query", arguments={"key": 1},
                  status="completed", result={"isError": True, "content": [{"text": "denied"}]}),
    ]


def test_completed_items_preserve_visible_conversation_and_authoritative_tool_results():
    entries = modern_rows()
    # A replay of the same completed event is one item; identical text under a
    # new ID is a separate, genuine user exchange.
    entries += [entries[3], completed("UserMessage", "u2", content=[{"type": "text", "text": "Fix it"}])]
    session = parse_codex_jsonl("unused", entries=entries, enrich_metadata=False)
    assert session.user_messages == ["Fix it", "Fix it"]
    assert session.assistant_messages == ["Fixed"]
    assert session.messages[0]["role"] == "user"
    assert len(session.tool_calls) == 4
    assert [c.is_error for c in session.tool_calls] == [False, True, True, True]
    assert {r["path"] for r in build_file_mutation_rows(session.session_id, "codex", session.tool_calls)} == {
        "/tmp/old.py", "/tmp/new.py"}
    assert session.tool_calls[-1].arguments == {"key": 1}
    assert "denied" in session.tool_calls[-1].result


def test_legacy_turn_survives_transition_and_attachment_turn_qualifies():
    entries = [row("session_meta", id="mixed"),
               row("event_msg", type="task_started", turn_id="old"),
               row("event_msg", type="user_message", message="Earlier"),
               row("response_item", type="message", role="assistant", content=[{"text": "Earlier answer"}])]
    entries += modern_rows()[1:]
    entries += [completed("UserMessage", "image", content=[{"type": "localImage", "path": "/tmp/image.png"}])]
    session = parse_codex_jsonl("unused", entries=entries, enrich_metadata=False)
    assert session.user_messages == ["Earlier", "Fix it", "[Image attachment]"]
    assert session.assistant_messages == ["Earlier answer", "Fixed"]


def test_unrecognized_visible_message_encoding_fails_instead_of_silently_skipping():
    with pytest.raises(CodexFormatError):
        parse_codex_jsonl("unused", entries=[completed("UserMessage", "u", content=[{"type": "future"}])],
                         enrich_metadata=False)
    with pytest.raises(CodexFormatError):
        parse_codex_jsonl("unused", entries=[
            row("response_item", type="message", role="user", content=[{"text": "request"}]),
            row("response_item", type="message", role="assistant", content=[{"text": "answer"}]),
        ], enrich_metadata=False)


def test_metadata_is_refreshed_and_source_activity_timestamp_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "state_5.sqlite")
    conn.execute("CREATE TABLE threads (id, title, cwd, git_branch, model, created_at, created_at_ms, updated_at, updated_at_ms)")
    conn.execute("INSERT INTO threads VALUES ('parent','old','/tmp','main','test',1,1000,2,2000)")
    conn.commit()
    first = parse_codex_jsonl("unused", entries=modern_rows())
    conn.execute("UPDATE threads SET title = 'new', updated_at_ms = 3000")
    conn.commit()
    second = parse_codex_jsonl("unused", entries=modern_rows())
    conn.close()
    assert first.slug == "old" and second.slug == "new"
    assert second.ended_at == "2026-10-06T10:00:01.000Z"


def test_parent_owns_child_evidence_and_legacy_child_rows_leave_top_level_results(tmp_path, monkeypatch):
    import transcript
    import tool_log
    home = tmp_path / "codex"
    root = home / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(home))
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", str(data))
    monkeypatch.setattr(db, "DB_PATH", str(data / "sessions.db"))
    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", str(data / "transcripts"))
    monkeypatch.setattr(tool_log, "TRANSCRIPT_DIR", str(data / "transcripts"))
    parent = root / "rollout-parent.jsonl"
    child = root / "rollout-child.jsonl"
    parent.write_text("\n".join(map(json.dumps, modern_rows())) + "\n")
    child_rows = modern_rows()
    child_rows[0] = row("session_meta", id="child", session_id="parent", cwd="/tmp", thread_source="subagent",
                        source={"subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_role": "reviewer"}}})
    child.write_text("\n".join(map(json.dumps, child_rows)) + "\n")
    assert parse_codex_jsonl(str(child), enrich_metadata=False).native_session_id == "child"
    child_sid = canonical_session_id("codex", "child")
    conn = db.get_connection(); db.init_db(conn)
    db.upsert_session(conn, session_id=child_sid, native_session_id="child", source="codex")
    conn.close()
    skipped = indexer.index_source_transcript("codex", str(child), indexer.NO_SUMMARY_INDEX_OPTIONS)
    assert skipped.skipped_reason
    result = indexer.index_source_transcript("codex", str(parent), indexer.NO_SUMMARY_INDEX_OPTIONS)
    assert result.subagents == 1
    conn = db.get_connection()
    visible = conn.execute(f"SELECT native_session_id FROM sessions WHERE {db.TOP_LEVEL_SESSION_PREDICATE}").fetchall()
    assert [r[0] for r in visible] == ["parent"]
    preserved = conn.execute("SELECT is_subagent, parent_native_session_id FROM sessions WHERE session_id=?", (child_sid,)).fetchone()
    assert tuple(preserved) == (1, "parent")
    fact = conn.execute("SELECT transcript_path FROM subagent_runs WHERE parent_session_id=?", (result.session_id,)).fetchone()
    assert Path(fact[0]).is_file()
    assert conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id=? AND scope != 'main'", (result.session_id,)).fetchone()[0] == 2
    conn.close()
    # User-created forks must remain independent, even with a parent reference.
    fork = parse_codex_jsonl("unused", entries=[row("session_meta", id="fork", thread_source="user", forked_from_id="parent",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})], enrich_metadata=False)
    assert not fork.is_subagent


def test_source_resolution_rejects_ambiguous_or_wrong_identity(tmp_path, monkeypatch):
    root = tmp_path / "sessions"; root.mkdir()
    monkeypatch.setenv("SESSION_INDEX_CODEX_HOME", str(tmp_path))
    paths = [root / "rollout-one.jsonl", root / "rollout-two.jsonl"]
    for path in paths:
        path.write_text(json.dumps(row("session_meta", id="same")) + "\n")
    assert resolve_codex_source("wrong", str(paths[0])) is None
    with pytest.raises(ValueError, match="Ambiguous"):
        resolve_codex_source("same")


def test_child_accepts_addressed_agent_messages_without_importing_them_as_human_input():
    metadata = row("session_meta", id="child", thread_source="subagent", agent_path="/root/child",
                   source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})
    incoming = row("response_item", type="agent_message", author="/root", recipient="/root/child",
                   content=[{"type": "input_text", "text": "Review the patch"}])
    answer = completed("AgentMessage", "answer", content=[{"type": "Text", "text": "Looks good"}])
    child = parse_codex_jsonl("unused", entries=[metadata, incoming, answer], enrich_metadata=False)
    assert child.user_message_count == 1
    assert "Review the patch" in child.user_messages[0]
    parent = parse_codex_jsonl("unused", entries=[row("session_meta", id="parent", thread_source="user"), incoming, answer], enrich_metadata=False)
    assert parent.user_message_count == 0


def test_completed_commentary_does_not_hide_final_awaiting_completed_item():
    entries = [row("session_meta", id="partial"),
               row("event_msg", type="task_started", turn_id="turn-1"),
               completed("UserMessage", "u", content=[{"text": "Fix"}]),
               completed("AgentMessage", "commentary", content=[{"text": "Checking"}]),
               row("response_item", type="message", id="final", role="assistant", content=[{"text": "Done"}])]
    before = parse_codex_jsonl("unused", entries=entries, enrich_metadata=False)
    entries.append(completed("AgentMessage", "final", content=[{"text": "Done"}]))
    after = parse_codex_jsonl("unused", entries=entries, enrich_metadata=False)
    assert before.assistant_messages == after.assistant_messages == ["Checking", "Done"]


def test_child_transcript_excludes_inherited_precreation_exchange(tmp_path):
    from codex_parser import parse_codex_subagent
    from subagent_parser import SubagentInfo
    entries = [row("session_meta", id="child", timestamp="2026-10-06T10:00:01.000Z", thread_source="subagent"),
               row("event_msg", type="user_message", message="Inherited parent prompt"),
               row("response_item", type="message", role="assistant", content=[{"text": "Inherited parent answer"}]),
               completed("UserMessage", "own-u", content=[{"text": "Child task"}]),
               completed("AgentMessage", "own-a", content=[{"text": "Child result"}])]
    for e in entries[1:3]:
        e['timestamp'] = "2026-10-06T09:59:00.000Z"
    path = tmp_path / 'rollout-child.jsonl'
    path.write_text('\n'.join(map(json.dumps, entries)) + '\n')
    parsed = parse_codex_subagent(SubagentInfo(str(path), None, 'child', 'reviewer'))
    assert [m['content'] for m in parsed.messages] == ['Child task', 'Child result']
