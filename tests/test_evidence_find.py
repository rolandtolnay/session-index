import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from evidence_find import find_candidates
from tests.evidence_helpers import make_memory_conn, seed_evidence_graph


def test_find_topic_returns_compact_session_refs_without_evidence_text(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    data = find_candidates(conn, topic="session index", limit=2)

    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["ref"] == "session/pi:abc"
    assert result["inspect_refs"]["primary"] == "session/pi:abc"
    assert result["match"] == {"kind": "topic", "topic": "session index"}
    assert result["session"]["summary"] == "Worked on session index evidence retrieval."
    assert "artifacts" not in result
    assert "evidence" not in result
    assert "text" not in result


def test_find_tool_returns_event_level_tool_ref(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    result = find_candidates(conn, tool="edit")["results"][0]

    assert result["ref"] == "tool/pi:abc/12"
    assert result["match"]["kind"] == "tool_call"
    assert result["match"]["tool"] == "edit"


def test_find_skill_mutation_question_and_subagent_candidates(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    skill = find_candidates(conn, skill="review")["results"][0]
    mutation = find_candidates(conn, mutated="prd/example")["results"][0]
    question = find_candidates(conn, tool="question", question_recommended=True)["results"][0]
    subagent = find_candidates(conn, subagent="scout")["results"][0]

    assert skill["ref"] == "skill/pi:abc/1"
    assert skill["match"]["kind"] == "skill_invocation"
    assert mutation["ref"] == "tool/pi:abc/12"
    assert mutation["match"]["kind"] == "file_mutation"
    assert mutation["match"]["path"] == "etc/prd/example.md"
    assert question["ref"] == "question/pi:abc/14/0"
    assert question["inspect_refs"]["tool"] == "tool/pi:abc/14"
    assert question["match"]["was_recommended"] is True
    assert subagent["ref"] == "subagent/pi:abc/0"
    assert subagent["inspect_refs"]["parent_call"] == "tool/pi:abc/15"
    assert subagent["match"]["transcript_path"].endswith("agent-child.md")
    for result in [skill, mutation, question, subagent]:
        assert "artifacts" not in result


def test_find_event_filters_compose_or_fail_clearly(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    assert find_candidates(conn, mutated="example.md", tool="edit")["results"][0]["match"]["tool"] == "edit"
    assert find_candidates(conn, mutated="example.md", tool="bash")["results"] == []
    assert find_candidates(conn, subagent="scout", tool="subagent_run")["results"][0]["ref"] == "subagent/pi:abc/0"
    with pytest.raises(ValueError, match="Skill Invocations are not Tool Calls"):
        find_candidates(conn, skill="review", tool="skill")
    with pytest.raises(ValueError, match="Cannot combine event criteria"):
        find_candidates(conn, mutated="example.md", subagent="scout")


def test_find_filters_compose(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    assert find_candidates(conn, tool="edit", project="session", since="2026-05-01", until="2026-05-31", session="pi:abc")["results"]
    assert find_candidates(conn, skill="review", topic="evidence workflow", project="session", since="2026-05-01", until="2026-05-31", session="pi:abc")["results"][0]["ref"] == "skill/pi:abc/1"
    assert find_candidates(conn, tool="edit", project="other")["results"] == []


def test_find_session_filters_are_not_topic_matches(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    result = find_candidates(conn, project="session", since="2026-05-01", until="2026-05-31", session="pi:abc")["results"][0]

    assert result["ref"] == "session/pi:abc"
    assert result["match"] == {
        "kind": "session_filter",
        "project": "session",
        "since": "2026-05-01",
        "until": "2026-05-31",
        "session": "pi:abc",
    }


def test_find_topic_event_filters_use_scoped_sessions(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)
    db.upsert_session(
        conn,
        session_id="pi:other",
        source="pi",
        project="session-index",
        started_at="2026-05-31T11:00:00Z",
        summary="Unrelated work.",
        user_messages="Different topic",
        transcript_path=str(tmp_path / "other.md"),
        tool_log_path=str(tmp_path / "other.tools.md"),
    )
    db.replace_tool_calls(conn, "pi:other", [{
        "session_id": "pi:other", "source": "pi", "scope": "main", "sequence": 1,
        "timestamp": None, "tool_name": "edit", "tool": "edit", "is_error": 0,
    }])

    data = find_candidates(conn, topic="evidence workflow", tool="edit", limit=5)

    assert [result["ref"] for result in data["results"]] == ["tool/pi:abc/12"]


def test_find_topic_session_scope_applies_before_limit(tmp_path):
    conn = make_memory_conn()
    db.upsert_session(
        conn,
        session_id="pi:distractor",
        source="pi",
        project="session-index",
        started_at="2026-05-31T11:00:00Z",
        summary="Common evidence workflow distractor.",
        user_messages="common evidence workflow",
        transcript_path=str(tmp_path / "distractor.md"),
        tool_log_path=str(tmp_path / "distractor.tools.md"),
    )
    db.upsert_session(
        conn,
        session_id="pi:target",
        source="pi",
        project="session-index",
        started_at="2026-05-31T10:00:00Z",
        summary="Common evidence workflow target.",
        user_messages="common evidence workflow",
        transcript_path=str(tmp_path / "target.md"),
        tool_log_path=str(tmp_path / "target.tools.md"),
    )

    data = find_candidates(conn, topic="common evidence", session="pi:target", limit=1)

    assert [result["ref"] for result in data["results"]] == ["session/pi:target"]
