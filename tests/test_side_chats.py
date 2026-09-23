"""Parent-owned Side Chat lifecycle through the public indexing/retrieval surfaces."""
import json
from pathlib import Path
import uuid

import pytest

import db
import indexer
import side_chats
from evidence_find import find_candidates
from evidence_inspect import EvidenceInspectError, inspect_ref
from manage_query import ManageFilters, query_manage_sessions
from session_identity import canonical_session_id

OWNER = "side-chat-parent"
CHILD = "11111111-1111-4111-8111-111111111111"
SID = canonical_session_id("pi", OWNER)


def record(kind, **fields):
    data = {"version": 1, "kind": kind, "ownerSessionId": OWNER, "sideChatId": CHILD,
            "timestamp": "2026-09-01T12:01:00Z", **fields}
    return {"type": "custom", "id": uuid.uuid4().hex[:8], "parentId": "a",
            "customType": side_chats.CUSTOM_TYPE, "data": data}


def opening(**fields):
    return record("open", openingLeafId="a", focusedContent={"kind": "terminal-selection", "label": "Selection", "text": "Focused widget"}, **fields)


def turn(sequence=1, **fields):
    return record("turn", sequence=sequence, question="What are the alternatives?",
                  answer="A semaphore avoids the thundering herd.", model="test-model", **fields)


def source(path, records, owner=OWNER):
    entries = [
        {"type": "session", "version": 3, "id": owner, "cwd": str(path.parent), "timestamp": "2026-09-01T12:00:00Z"},
        {"type": "message", "id": "u", "parentId": None, "timestamp": "2026-09-01T12:00:01Z",
         "message": {"role": "user", "content": "Parent-only task"}},
        {"type": "message", "id": "a", "parentId": "u", "timestamp": "2026-09-01T12:00:02Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Parent-only response"}]}},
        *records,
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "data"
    artifacts = root / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(root))
    monkeypatch.setattr(db, "DB_PATH", str(root / "sessions.db"))
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("cli.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: pytest.fail("Unexpected headline request"))
    monkeypatch.setattr("summarizer.summarize", lambda **kw: pytest.fail("Unexpected summary request"))
    path = tmp_path / "parent.jsonl"
    source(path, [opening(), turn()])
    return path, artifacts


def index(path):
    return indexer.index_source_transcript("pi", str(path), indexer.NO_SUMMARY_INDEX_OPTIONS)


def test_incremental_archive_is_separate_searchable_and_inspectable(store):
    path, artifacts = store
    result = index(path)
    assert result.session_id == SID
    conn = db.get_connection()
    children = side_chats.list_side_chats(conn, SID)
    assert len(children) == 1
    child = children[0]
    text = Path(child["transcript_path"]).read_text()
    parent = Path(result.transcript_path).read_text()
    assert child["transcript_path"] in parent
    assert child["first_question"] in parent  # Routing fallback only.
    assert "thundering herd" not in parent and "Focused widget" not in parent
    assert "Focused widget" in text and "thundering herd" in text
    assert "Parent-only response" not in text
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    session = db.get_session(conn, SID)
    assert session["summary"] is None and session["headline"] is None
    assert session["user_message_count"] == 1
    assert "thundering" not in session["user_messages"]
    found = find_candidates(conn, topic="thundering herd")["results"]
    assert len(found) == 1 and found[0]["ref"] == f"session/{SID}"
    ref = found[0]["inspect_refs"]["side_chats"][0]["ref"]
    packet = inspect_ref(conn, ref, q="thundering")
    assert packet["evidence"][0]["artifact"] == "side_chat_transcript"
    assert "thundering" in packet["evidence"][0]["text"]
    assert "Focused widget" in inspect_ref(conn, ref, q="widget")["evidence"][0]["text"]
    parent_packet = inspect_ref(conn, f"session/{SID}")
    assert parent_packet["evidence"] == []
    assert parent_packet["inspect_refs"]["side_chats"][0]["ref"] == ref
    assert not inspect_ref(conn, f"session/{SID}", q="thundering")["evidence"]
    page = query_manage_sessions(conn, ManageFilters(query="thundering"))
    assert page.total == 1
    assert page.sessions[0]["side_chats"][0]["matched"]
    conn.close()


def test_hook_modes_publish_while_main_idle_and_only_headline_on_close(store, monkeypatch):
    from hooks import pi_index
    path, _ = store
    monkeypatch.setattr("sys.argv", ["pi_index.py", "--mode", "side-chat", "--session-file", str(path)])
    pi_index.main()
    conn = db.get_connection()
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] is None
    source(path, [opening(), turn(), record("close")])
    calls = []
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: calls.append(kw) or "Explore concurrent access")
    monkeypatch.setattr("sys.argv", ["pi_index.py", "--mode", "side-chat-close", "--session-file", str(path)])
    pi_index.main()
    assert len(calls) == 1 and calls[0]["side_chat"] is True
    assert db.get_session(conn, SID)["summary"] is None
    assert side_chats.list_side_chats(conn, SID)[0]["headline"]
    conn.close()


def test_all_branches_duplicate_deliveries_and_clone_ownership(store):
    path, _ = store
    # A later entry selects a branch without any archive records in its ancestry.
    branch = {"type": "message", "id": "branch", "parentId": "a", "message": {"role": "user", "content": "Another branch"}}
    source(path, [opening(), turn(), turn(), branch])
    index(path)
    index(path)
    conn = db.get_connection()
    child = side_chats.list_side_chats(conn, SID)[0]
    assert child["turn_count"] == 1 and child["opening_leaf_id"] == "a"
    clone = path.with_name("clone.jsonl")
    source(clone, [opening(), turn()], owner="clone-owner")
    result = index(clone)
    assert not side_chats.list_side_chats(conn, result.session_id)
    assert len(find_candidates(conn, topic="semaphore")["results"]) == 1
    conn.close()


def test_invalid_records_and_empty_chats_never_become_artifacts(store):
    path, _ = store
    source(path, [opening(), turn(sequence=True), turn(sideChatId="../../escape"),
                  turn(ownerSessionId="another-owner"), record("turn", sequence=2, question={}, answer="x", model="x")])
    index(path)
    conn = db.get_connection()
    assert not side_chats.list_side_chats(conn, SID)
    assert not find_candidates(conn, topic="semaphore")["results"]
    with pytest.raises(EvidenceInspectError):
        inspect_ref(conn, f"sidechat/{SID}/../../escape")
    conn.close()


def test_headlines_on_close_fallback_recovery_and_no_parent_context(store, monkeypatch):
    path, artifacts = store
    index(path)
    # No close: a normal close-only headline pass must not generate anything.
    side_chats.refresh_headlines(SID)
    source(path, [opening(), turn(), record("close")])
    index(path)
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: None)
    side_chats.refresh_headlines(SID)
    conn = db.get_connection()
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] is None
    requests = []

    def generate(**kwargs):
        requests.append(kwargs)
        return "Explore semaphore alternatives"

    monkeypatch.setattr("summarizer.generate_headline", generate)
    side_chats.refresh_headlines(SID)
    side_chats.refresh_headlines(SID)
    assert len(requests) == 1
    assert requests[0]["side_chat"] is True
    assert "Focused widget" in requests[0]["transcript_text"]
    assert "Parent-only" not in requests[0]["transcript_text"]
    assert "Explore semaphore alternatives" in (artifacts / f"{SID}.md").read_text()
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] == "Explore semaphore alternatives"
    index(path)
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] == "Explore semaphore alternatives"
    assert find_candidates(conn, topic="semaphore alternatives")["results"]
    # A later durable turn invalidates the old headline; recover unfinished chats.
    source(path, [opening(), turn(), turn(2, timestamp="2026-09-01T12:02:00Z")])
    index(path)
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] is None
    side_chats.refresh_headlines(SID, recover_open=True)
    assert len(requests) == 2
    conn.close()


def test_headline_result_cannot_overwrite_newer_turns_or_resurrect_deleted_parent(store, monkeypatch):
    path, artifacts = store
    source(path, [opening(), turn(), record("close")])
    index(path)

    def changed_during_request(**kwargs):
        source(path, [opening(), turn(), turn(2)])
        index(path)
        return "Stale headline"

    monkeypatch.setattr("summarizer.generate_headline", changed_during_request)
    side_chats.refresh_headlines(SID)
    conn = db.get_connection()
    assert side_chats.list_side_chats(conn, SID)[0]["headline"] is None

    def deleted_during_request(**kwargs):
        from cli import _delete_managed_session
        _delete_managed_session(conn, SID)
        return "Deleted headline"

    monkeypatch.setattr("summarizer.generate_headline", deleted_during_request)
    side_chats.refresh_headlines(SID, recover_open=True)
    assert db.get_session(conn, SID) is None
    assert not (artifacts / SID).exists()
    conn.close()


def test_parent_collapsing_filters_retention_and_delete(store):
    from cli import _delete_managed_session, build_footprint_audit
    path, artifacts = store
    second = "22222222-2222-4222-8222-222222222222"
    source(path, [opening(), turn(), opening(sideChatId=second), turn(sideChatId=second)])
    index(path)
    conn = db.get_connection()
    db.upsert_session(conn, session_id=SID, summary="Routine logistics, no code changes", user_messages="semaphore")
    assert len(find_candidates(conn, topic="semaphore")["results"]) == 1
    assert len(find_candidates(conn, topic="semaphore")["results"][0]["inspect_refs"]["side_chats"]) == 2
    assert not find_candidates(conn, topic="semaphore", project="wrong")["results"]
    assert not find_candidates(conn, topic="semaphore", since="2027-01-01")["results"]
    assert find_candidates(conn, topic="semaphore", session=SID)["results"]
    from cli import _check_integrity, _fix_issues
    issues = _check_integrity(conn)
    assert issues["orphaned_subagent_dirs"] == []
    _fix_issues(conn, issues)
    assert all(Path(c["transcript_path"]).exists() for c in side_chats.list_side_chats(conn, SID))
    audit = build_footprint_audit(conn, session_ids=[SID])["sessions"][0]
    assert "side_chats_present" in audit["prune"]["blocking"]
    assert len([a for a in audit["artifacts"] if a["kind"] == "side_chat_transcript"]) == 2
    assert audit["artifact_bytes"] == sum(p.stat().st_size for p in artifacts.rglob("*.md"))
    _delete_managed_session(conn, SID)
    assert path.exists()
    assert not side_chats.list_side_chats(conn, SID)
    assert not (artifacts / SID).exists()
    assert not find_candidates(conn, topic="semaphore")["results"]
    conn.close()
    index(path)  # Raw source retained: explicit backfill can regenerate all children.
    conn = db.get_connection()
    assert len(side_chats.list_side_chats(conn, SID)) == 2
    conn.close()
