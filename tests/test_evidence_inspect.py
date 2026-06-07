import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evidence_inspect import EvidenceInspectError, inspect_ref
from tests.evidence_helpers import make_memory_conn, seed_evidence_graph


def test_inspect_session_without_query_returns_artifact_metadata_and_subagent_refs(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    paths = seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    packet = inspect_ref(conn, "session/pi:abc")

    assert packet["ref"] == "session/pi:abc"
    assert packet["match"] == {"kind": "session"}
    assert packet["evidence"] == []
    assert packet["artifacts"]["clean_transcript"] == {"path": paths["transcript_path"], "exists": True}
    assert packet["artifacts"]["tool_log"] == {"path": paths["tool_log_path"], "exists": True}
    assert packet["artifacts"]["subagent_transcripts"] == {"count": 1}
    assert packet["inspect_refs"]["subagents"] == [{
        "ref": "subagent/pi:abc/0",
        "requested_agent_type": "scout",
        "task_preview": "Inspect evidence flow",
    }]
    assert "source_path" not in packet


def test_inspect_session_returns_clean_transcript_snippet_with_same_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    paths = seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    packet = inspect_ref(conn, "session/pi:abc", q="scoped evidence")

    assert packet["ref"] == "session/pi:abc"
    assert packet["match"] == {"kind": "session", "query": "scoped evidence"}
    assert packet["artifacts"]["clean_transcript"] == {"path": paths["transcript_path"], "exists": True}
    assert packet["inspect_refs"]["subagents"][0]["ref"] == "subagent/pi:abc/0"
    assert packet["evidence"][0]["artifact"] == "clean_transcript"
    assert packet["evidence"][0]["locator"]["type"] == "snippet"
    assert "Evidence inspect retrieves scoped text" in packet["evidence"][0]["text"]


def test_inspect_tool_returns_tool_log_section_and_file_mutations(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    packet = inspect_ref(conn, "tool/pi:abc/12")

    assert packet["match"]["kind"] == "tool_call"
    assert packet["match"]["file_mutations"] == ["etc/prd/example.md"]
    assert packet["evidence"][0]["artifact"] == "tool_log"
    assert packet["evidence"][0]["locator"]["sequence"] == 12
    assert '"path": "etc/prd/example.md"' in packet["evidence"][0]["text"]


def test_inspect_question_returns_metadata_and_tool_log_section(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    packet = inspect_ref(conn, "question/pi:abc/14/0")

    assert packet["match"]["kind"] == "question_answer"
    assert packet["match"]["question"] == "Which approach?"
    assert packet["match"]["was_recommended"] is True
    assert packet["evidence"][0]["locator"]["sequence"] == 14


def test_inspect_parent_skill_invocation_returns_transcript_artifact_without_inlining(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    paths = seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    packet = inspect_ref(conn, "skill/pi:abc/1")

    assert packet["ref"] == "skill/pi:abc/1"
    assert packet["match"]["kind"] == "skill_invocation"
    assert packet["match"]["skill_name"] == "review"
    assert packet["artifacts"]["primary_transcript"] == {"path": paths["transcript_path"], "exists": True}
    assert "clean_transcript" not in packet["artifacts"]
    assert packet["locator"] == {"tool_sequence": 13}
    assert packet["evidence"] == []
    assert "Discuss session index evidence" not in json.dumps(packet)


def test_inspect_subagent_skill_invocation_uses_subagent_primary_and_parent_context(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    paths = seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")
    conn.execute(
        """
        INSERT INTO skill_invocations (
            session_id, source, sequence, timestamp, skill_name, invocation_preview, arguments,
            transcript_message_index, tool_sequence, child_index, subagent_transcript_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("pi:abc", "pi", 2, "2026-05-31T10:04:00Z", "diagnose", None, None, None, 16, 0, paths["subagent_path"]),
    )

    packet = inspect_ref(conn, "skill/pi:abc/2")

    assert packet["match"]["skill_name"] == "diagnose"
    assert packet["artifacts"]["primary_transcript"] == {"path": paths["subagent_path"], "exists": True}
    assert packet["artifacts"]["clean_transcript"] == {"path": paths["transcript_path"], "exists": True}
    assert packet["locator"] == {"tool_sequence": 16, "child_index": 0}
    assert packet["evidence"] == []
    assert "Found scoped evidence details" not in json.dumps(packet)


def test_inspect_subagent_default_and_query_focused(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    default = inspect_ref(conn, "subagent/pi:abc/0")
    focused = inspect_ref(conn, "subagent/pi:abc/0", q="details")

    assert default["match"]["kind"] == "subagent_run"
    assert default["evidence"][0]["locator"]["type"] == "task_area"
    assert "Inspect evidence flow" in default["evidence"][0]["text"]
    assert focused["evidence"][0]["artifact"] == "subagent_transcript"
    assert focused["evidence"][0]["locator"]["type"] == "snippet"
    assert "scoped evidence details" in focused["evidence"][0]["text"]


@pytest.mark.parametrize("ref,code", [
    ("bad/ref", "invalid_ref"),
    ("session/missing", "session_not_found"),
    ("tool/pi:abc/999", "stale_ref"),
])
def test_inspect_errors_are_structured(tmp_path, monkeypatch, ref, code):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, write_artifacts=True, summary="summary")

    with pytest.raises(EvidenceInspectError) as exc:
        inspect_ref(conn, ref, q="evidence")

    payload = exc.value.to_json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
