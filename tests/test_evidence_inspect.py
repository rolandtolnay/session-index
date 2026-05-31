import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from evidence_inspect import EvidenceInspectError, inspect_ref
from parser import ParsedToolCall
from tool_log import write_tool_log


def _conn():
    conn = db.sqlite3.connect(":memory:")
    conn.row_factory = db.sqlite3.Row
    db.init_db(conn)
    return conn


def _seed(conn, tmp_path):
    transcript = tmp_path / "pi:abc.md"
    transcript.write_text("proj | main | 2026-05-31\n---\n\n[user] ────────────────────────────────────────\nDiscuss session index evidence\n\n[assistant] ──────────────────────────────────\nEvidence inspect retrieves scoped text.\n")
    tool_log = write_tool_log("pi:abc", [
        ParsedToolCall(sequence=12, scope="main", tool_name="edit", arguments={"path": "etc/prd/example.md"}, result="changed"),
        ParsedToolCall(sequence=14, scope="main", tool_name="question", arguments={"questions": []}, result="Which approach? -> A"),
    ])
    subdir = tmp_path / "pi:abc"
    subdir.mkdir(exist_ok=True)
    subagent_path = subdir / "agent-child.md"
    subagent_path.write_text("# scout\nParent: pi:abc\n---\n\n[prompt] 10:00 ──────────────────────────────\nInspect evidence flow\n\n[agent] 10:01 ──────────────────────────────\nFound scoped evidence details.\n")
    db.upsert_session(
        conn,
        session_id="pi:abc",
        source="pi",
        project="session-index",
        started_at="2026-05-31T10:00:00Z",
        summary="summary",
        user_messages="session index evidence",
        transcript_path=str(transcript),
        tool_log_path=tool_log,
        subagent_transcripts=str(subagent_path),
    )
    db.replace_tool_calls(conn, "pi:abc", [
        {"session_id": "pi:abc", "source": "pi", "scope": "main", "sequence": 12, "timestamp": None, "tool_name": "edit", "tool": "edit", "is_error": 0, "skill_name": None},
        {"session_id": "pi:abc", "source": "pi", "scope": "main", "sequence": 14, "timestamp": None, "tool_name": "question", "tool": "question", "is_error": 0, "skill_name": None},
    ])
    db.replace_file_mutations(conn, "pi:abc", [{
        "session_id": "pi:abc", "source": "pi", "scope": "main", "sequence": 12,
        "timestamp": None, "tool_name": "edit", "tool": "edit", "path": "etc/prd/example.md",
    }])
    db.replace_question_answers(conn, "pi:abc", [{
        "session_id": "pi:abc", "source": "pi", "sequence": 14, "question_index": 0,
        "header": "Choice", "question": "Which approach?", "selected_label": "A",
        "was_recommended": 0, "is_other": 0, "option_count": 2, "multi_select": 0,
    }])
    db.replace_subagent_runs(conn, "pi:abc", [{
        "parent_session_id": "pi:abc", "source": "pi", "requested_agent_type": "scout", "observed_agent_type": "scout",
        "call_tool": "subagent_run", "call_sequence": 15, "call_tool_id": "call-1", "child_index": 0,
        "agent_id": "child", "status": "ok", "started_at": None, "ended_at": None,
        "duration_seconds": None, "tool_call_count": 1, "transcript_path": str(subagent_path),
        "task_preview": "Inspect evidence flow", "match_confidence": "high",
    }])


def test_inspect_session_returns_clean_transcript_excerpt(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = _conn()
    _seed(conn, tmp_path)

    packet = inspect_ref(conn, "session/pi:abc", q="scoped evidence")

    assert packet["ref"] == "session/pi:abc"
    assert packet["match"] == {"kind": "session", "query": "scoped evidence"}
    assert packet["evidence"][0]["artifact"] == "clean_transcript"
    assert "Evidence inspect retrieves scoped text" in packet["evidence"][0]["text"]


def test_inspect_tool_returns_tool_log_section_and_file_mutations(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = _conn()
    _seed(conn, tmp_path)

    packet = inspect_ref(conn, "tool/pi:abc/12")

    assert packet["match"]["kind"] == "tool_call"
    assert packet["match"]["file_mutations"] == ["etc/prd/example.md"]
    assert packet["evidence"][0]["artifact"] == "tool_log"
    assert packet["evidence"][0]["locator"]["sequence"] == 12
    assert '"path": "etc/prd/example.md"' in packet["evidence"][0]["text"]


def test_inspect_question_returns_metadata_and_tool_log_section(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = _conn()
    _seed(conn, tmp_path)

    packet = inspect_ref(conn, "question/pi:abc/14/0")

    assert packet["match"]["kind"] == "question_answer"
    assert packet["match"]["question"] == "Which approach?"
    assert packet["match"]["was_recommended"] is False
    assert packet["evidence"][0]["locator"]["sequence"] == 14


def test_inspect_subagent_default_and_query_focused(tmp_path, monkeypatch):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = _conn()
    _seed(conn, tmp_path)

    default = inspect_ref(conn, "subagent/pi:abc/0")
    focused = inspect_ref(conn, "subagent/pi:abc/0", q="details")

    assert default["match"]["kind"] == "subagent_run"
    assert default["evidence"][0]["locator"]["type"] == "task_area"
    assert "Inspect evidence flow" in default["evidence"][0]["text"]
    assert focused["evidence"][0]["artifact"] == "subagent_transcript"
    assert "scoped evidence details" in focused["evidence"][0]["text"]


@pytest.mark.parametrize("ref,code", [
    ("bad/ref", "invalid_ref"),
    ("session/missing", "session_not_found"),
    ("tool/pi:abc/999", "stale_ref"),
])
def test_inspect_errors_are_structured(tmp_path, monkeypatch, ref, code):
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(tmp_path))
    conn = _conn()
    _seed(conn, tmp_path)

    with pytest.raises(EvidenceInspectError) as exc:
        inspect_ref(conn, ref, q="evidence")

    payload = exc.value.to_json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
