"""Historical Pi IDs resolve at the public find/inspect boundaries."""

import sqlite3
from pathlib import Path

import pytest

import db
from evidence_find import find_candidates
from evidence_inspect import EvidenceInspectError, inspect_ref
from tests.evidence_helpers import SESSION_ID, make_memory_conn, seed_evidence_graph

ALIAS = "pi:1234567890abcdef"
CHILD = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, write_artifacts=True)
    conn.execute("INSERT INTO pi_session_aliases VALUES (?, ?)", (ALIAS, SESSION_ID))
    yield conn
    conn.close()


def test_find_and_session_inspect_resolve_retired_id_without_duplicate_sessions(store):
    found = find_candidates(store, session=ALIAS)["results"]
    assert [result["ref"] for result in found] == [f"session/{SESSION_ID}"]
    packet = inspect_ref(store, f"session/{ALIAS}")
    assert packet["ref"] == f"session/{SESSION_ID}"
    assert packet["session"]["session_id"] == SESSION_ID
    assert packet["inspect_refs"]["subagents"][0]["ref"] == f"subagent/{SESSION_ID}/0"
    assert db.get_stats(store)["total_sessions"] == 1


def test_tool_alias_reads_owner_event_mutations_and_artifact(store):
    found = find_candidates(store, session=ALIAS, tool="edit")["results"]
    assert [result["ref"] for result in found] == [f"tool/{SESSION_ID}/12"]
    packet = inspect_ref(store, f"tool/{ALIAS}/12")
    assert packet["ref"] == f"tool/{SESSION_ID}/12"
    assert packet["session"]["session_id"] == SESSION_ID
    assert packet["match"]["file_mutations"] == ["etc/prd/example.md"]
    evidence = packet["evidence"][0]
    assert evidence["locator"]["sequence"] == 12
    assert '"path": "etc/prd/example.md"' in evidence["text"]
    assert Path(evidence["path"]).name == f"{SESSION_ID}.tools.md"


def test_other_event_aliases_read_canonical_rows(store, tmp_path):
    side_path = tmp_path / "side-chat.md"
    side_path.write_text("Focused compatibility question\n")
    store.execute("""
        INSERT INTO side_chats (
            parent_session_id, side_chat_id, opener, started_at, focused_content,
            turns_json, turn_count, first_question, content_hash, search_text, transcript_path
        ) VALUES (?, ?, 'user', '2026-05-31T10:00:00Z', '{}', '[]', 1,
                  'Compatibility question', 'hash', 'compatibility', ?)
    """, (SESSION_ID, CHILD, str(side_path)))

    question = inspect_ref(store, f"question/{ALIAS}/14/0")
    assert question["match"]["selected_label"] == "A (Recommended)"
    assert question["evidence"][0]["locator"]["sequence"] == 14
    skill = inspect_ref(store, f"skill/{ALIAS}/1")
    assert skill["match"]["skill_name"] == "review"
    assert skill["locator"] == {"tool_sequence": 13}
    subagent = inspect_ref(store, f"subagent/{ALIAS}/0")
    assert "Found scoped evidence details" in subagent["evidence"][0]["text"]
    sidechat = inspect_ref(store, f"sidechat/{ALIAS}/{CHILD}")
    assert sidechat["match"]["ref"] == f"sidechat/{SESSION_ID}/{CHILD}"
    assert sidechat["evidence"][0]["text"] == "Focused compatibility question"
    for packet in (question, skill, subagent, sidechat):
        assert packet["session"]["session_id"] == SESSION_ID


def test_prefix_deduplicates_owner_but_rejects_other_owner_candidates(store):
    prefix = ALIAS[:11]
    store.execute("INSERT INTO pi_session_aliases VALUES (?, ?)", ("pi:12345678fedcba09", SESSION_ID))
    assert db.get_session(store, prefix)["session_id"] == SESSION_ID
    assert db.get_session(store, prefix[:7]) is None

    db.upsert_session(store, session_id="pi:other", source="pi", native_session_id="other-native")
    store.execute("INSERT INTO pi_continuation_sources VALUES (?, ?, ?, NULL)",
                  ("/fixture/ancestor.jsonl", prefix + "-ancestor", "pi:other"))
    assert db.get_session(store, prefix) is None
    assert db.get_session(store, ALIAS)["session_id"] == SESSION_ID
    assert db.get_session(store, prefix + "-ancestor")["session_id"] == "pi:other"
    with pytest.raises(ValueError):
        find_candidates(store, session=prefix)
    with pytest.raises(EvidenceInspectError) as error:
        inspect_ref(store, f"tool/{prefix}/12")
    assert error.value.code == "session_not_found"


def test_exact_active_native_identity_precedes_alias(store):
    db.upsert_session(store, source="pi", session_id="pi:fork", native_session_id=ALIAS)
    assert db.get_session(store, ALIAS)["session_id"] == "pi:fork"


@pytest.mark.parametrize("ref,code", [
    (f"tool/{ALIAS}/not-a-sequence", "invalid_ref"),
    (f"tool/{ALIAS}/unexpected/12", "session_not_found"),
    (f"tool/{ALIAS}/999", "stale_ref"),
])
def test_aliases_do_not_reinterpret_invalid_or_missing_refs(store, ref, code):
    with pytest.raises(EvidenceInspectError) as error:
        inspect_ref(store, ref)
    assert error.value.code == code


def test_owner_deletion_removes_aliases_but_retains_registry_and_compatibility_files(store, tmp_path):
    artifact = tmp_path / f"{ALIAS}.md"
    artifact.write_text("Immutable compatibility artifact\n")
    db.upsert_session(store, session_id="pi:other", source="pi")
    store.execute("INSERT INTO pi_session_aliases VALUES ('pi:other-alias', 'pi:other')")
    store.execute("INSERT INTO pi_continuation_sources VALUES (?, ?, ?, NULL)",
                  ("/fixture/ancestor.jsonl", "ancestor-native", SESSION_ID))
    assert db.delete_sessions(store, [SESSION_ID]) == 1
    assert db.get_session(store, ALIAS) is None
    assert db.get_session(store, "pi:other-alias")["session_id"] == "pi:other"
    assert store.execute("SELECT owner_id FROM pi_continuation_sources").fetchone()[0] == SESSION_ID
    assert artifact.read_text() == "Immutable compatibility artifact\n"
    db.upsert_session(store, session_id=SESSION_ID, source="pi")
    assert db.get_session(store, ALIAS) is None


def test_old_schema_readonly_lookup_and_inspection_still_work(store, tmp_path):
    store.execute("DROP TABLE pi_session_aliases")
    store.execute("INSERT INTO pi_continuation_sources VALUES (?, ?, ?, NULL)",
                  ("/fixture/ancestor.jsonl", "ancestor-native", SESSION_ID))
    store.commit()
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as target:
        store.backup(target)
    conn = db._get_readonly_connection(str(path))
    try:
        assert db.get_session(conn, ALIAS) is None
        assert db.get_session(conn, "ancestor-native")["session_id"] == SESSION_ID
        assert db.get_session(conn, "ancestor-")["session_id"] == SESSION_ID
        assert find_candidates(conn, session=SESSION_ID)["results"]
        assert inspect_ref(conn, "tool/ancestor-native/12")["match"]["file_mutations"] == ["etc/prd/example.md"]
    finally:
        conn.close()
    assert db.delete_sessions(store, [SESSION_ID]) == 1
