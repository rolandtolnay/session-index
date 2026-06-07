import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from evidence_find import find_candidates
from tests.evidence_helpers import make_memory_conn, seed_evidence_graph, seed_session_with_mutations


def test_find_topic_returns_compact_session_refs_without_evidence_text(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    data = find_candidates(conn, topic="session index", limit=2)

    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["ref"] == "session/pi:abc"
    assert result["inspect_refs"]["primary"] == "session/pi:abc"
    assert result["match"] == {"kind": "topic", "topic": "session index", "match_mode": "exact"}
    assert result["session"]["summary"] == "Worked on session index evidence retrieval."
    assert "artifacts" not in result
    assert "evidence" not in result
    assert "text" not in result


def test_find_topic_falls_back_to_fuzzy_candidates_when_exact_topic_is_empty(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(
        conn,
        tmp_path,
        summary="Implemented deterministic file history retrieval for session-collapsed mutations.",
    )

    data = find_candidates(conn, topic="deterministc file hystory", limit=2)

    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["ref"] == "session/pi:abc"
    assert result["match"]["kind"] == "topic"
    assert result["match"]["topic"] == "deterministc file hystory"
    assert result["match"]["match_mode"] == "fuzzy_fallback"
    assert result["match"]["score"] > 0


def test_find_topic_exact_scope_wins_over_possible_fuzzy_candidates(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(
        conn,
        tmp_path,
        summary="Exact evidence workflow session.",
    )
    db.upsert_session(
        conn,
        session_id="pi:fuzzy",
        source="pi",
        project="session-index",
        started_at="2026-05-31T11:00:00Z",
        summary="Evidence workflow fuzzy distractor with many adjacent terms.",
        user_messages="adjacent topic",
        transcript_path=str(tmp_path / "fuzzy.md"),
        tool_log_path=str(tmp_path / "fuzzy.tools.md"),
    )

    data = find_candidates(conn, topic="Exact evidence", limit=5)

    assert [result["ref"] for result in data["results"]] == ["session/pi:abc"]
    assert data["results"][0]["match"]["match_mode"] == "exact"


def test_find_topic_scoped_tool_uses_fuzzy_scope_but_keeps_tool_match_kind(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(
        conn,
        tmp_path,
        summary="Implemented deterministic file history retrieval for session-collapsed mutations.",
    )

    data = find_candidates(conn, topic="deterministc file hystory", tool="edit", limit=5)

    assert [result["ref"] for result in data["results"]] == ["tool/pi:abc/12"]
    match = data["results"][0]["match"]
    assert match["kind"] == "tool_call"
    assert match["topic_scope"]["topic"] == "deterministc file hystory"
    assert match["topic_scope"]["match_mode"] == "fuzzy_fallback"
    assert match["topic_scope"]["score"] > 0


def test_find_fuzzy_topic_fallback_honors_structured_filters(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(
        conn,
        tmp_path,
        summary="Implemented deterministic file history retrieval.",
    )
    db.upsert_session(
        conn,
        session_id="pi:other-project",
        source="pi",
        project="other-project",
        started_at="2026-05-31T11:00:00Z",
        summary="Implemented deterministic file history retrieval.",
        user_messages="",
        transcript_path=str(tmp_path / "other-project.md"),
        tool_log_path=str(tmp_path / "other-project.tools.md"),
    )

    assert find_candidates(conn, topic="deterministc file hystory", project="other", limit=5)["results"][0]["ref"] == "session/pi:other-project"
    assert find_candidates(conn, topic="deterministc file hystory", project="session", since="2026-06-01", limit=5)["results"] == []
    assert find_candidates(conn, topic="deterministc file hystory", session="pi:abc", limit=5)["results"][0]["ref"] == "session/pi:abc"


def test_find_fuzzy_topic_fallback_omits_weak_broad_matches(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path, summary="Worked on session index evidence retrieval.")

    assert find_candidates(conn, topic="banana rocket ocean", limit=5)["results"] == []
    assert find_candidates(conn, topic="banana evidence", limit=5)["results"] == []


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

    assert skill["ref"] == "tool/pi:abc/13"
    assert skill["match"]["kind"] == "skill_invocation"
    assert mutation["ref"] == "session/pi:abc"
    assert mutation["match"]["kind"] == "file_mutation_session"
    assert mutation["match"]["match_count"] == 1
    assert mutation["match"]["distinct_path_count"] == 1
    assert mutation["match"]["representative_paths"] == ["etc/prd/example.md"]
    assert mutation["inspect_refs"]["related_tools"] == ["tool/pi:abc/12"]
    assert question["ref"] == "question/pi:abc/14/0"
    assert question["inspect_refs"]["tool"] == "tool/pi:abc/14"
    assert question["match"]["was_recommended"] is True
    assert subagent["ref"] == "subagent/pi:abc/0"
    assert subagent["inspect_refs"]["parent_call"] == "tool/pi:abc/15"
    assert subagent["match"]["transcript_path"].endswith("agent-child.md")
    for result in [skill, mutation, question, subagent]:
        assert "artifacts" not in result


def test_find_mutated_collapsed_sessions_are_recent_first_with_counts_and_representatives(tmp_path):
    conn = make_memory_conn()
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:old",
        started_at="2026-05-30T10:00:00Z",
        mutations=[
            (1, "edit", "etc/prd/example.md"),
            (2, "edit", "src/other.py"),
            (3, "write", "etc/prd/example.md"),
            (4, "edit", "etc/prd/second.md"),
        ],
    )
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:new",
        started_at="2026-05-31T10:00:00Z",
        mutations=[(1, "edit", "etc/prd/new.md")],
    )

    results = find_candidates(conn, mutated="etc/prd", limit=5)["results"]

    assert [result["ref"] for result in results] == ["session/pi:new", "session/pi:old"]
    old_match = results[1]["match"]
    assert old_match["match_count"] == 3
    assert old_match["distinct_path_count"] == 2
    assert old_match["representative_paths"] == ["etc/prd/example.md", "etc/prd/second.md"]


def test_find_mutated_collapsed_session_caps_representative_paths_and_related_tools(tmp_path):
    conn = make_memory_conn()
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:many",
        started_at="2026-05-31T10:00:00Z",
        mutations=[
            (1, "edit", "area/a.md"),
            (1, "edit", "area/a.md"),
            (1, "edit", "area/a.md"),
            (2, "edit", "area/b.md"),
            (2, "edit", "area/b.md"),
            (3, "edit", "area/c.md"),
            (4, "edit", "area/d.md"),
            (5, "edit", "area/e.md"),
            (6, "edit", "area/f.md"),
            (7, "edit", "area/g.md"),
        ],
    )

    result = find_candidates(conn, mutated="area/", limit=5)["results"][0]

    assert result["match"]["representative_paths"] == [
        "area/a.md",
        "area/b.md",
        "area/c.md",
        "area/d.md",
        "area/e.md",
    ]
    assert result["inspect_refs"]["related_tools"] == [
        "tool/pi:many/1",
        "tool/pi:many/2",
        "tool/pi:many/3",
        "tool/pi:many/4",
        "tool/pi:many/5",
    ]


def test_find_topic_scoped_mutation_uses_fuzzy_scope_when_exact_topic_is_empty(tmp_path):
    conn = make_memory_conn()
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:file-history",
        started_at="2026-05-31T10:00:00Z",
        summary="Implemented deterministic file history retrieval for session-collapsed mutations.",
        mutations=[(1, "edit", "etc/prd/file-conversation-history.md")],
    )

    result = find_candidates(conn, topic="deterministc file hystory", mutated="file-conversation", limit=5)["results"][0]

    assert result["ref"] == "session/pi:file-history"
    assert result["match"]["kind"] == "file_mutation_session"
    assert result["match"]["topic_scope"]["match_mode"] == "fuzzy_fallback"


def test_find_topic_scoped_mutation_applies_limit_after_exact_mutation_criterion(tmp_path):
    conn = make_memory_conn()
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:fuzzy-distractor",
        started_at="2026-05-31T11:00:00Z",
        summary="Implemented deterministic file history retrieval for session-collapsed mutations.",
        mutations=[],
    )
    seed_session_with_mutations(
        conn,
        tmp_path,
        session_id="pi:fuzzy-target",
        started_at="2026-05-31T10:00:00Z",
        summary="Implemented file history retrieval for session-collapsed mutations.",
        mutations=[(1, "edit", "etc/prd/file-conversation-history.md")],
    )

    results = find_candidates(conn, topic="deterministc file hystory", mutated="file-conversation", limit=1)["results"]

    assert [result["ref"] for result in results] == ["session/pi:fuzzy-target"]


def test_find_topic_scoped_mutation_handles_more_than_500_fuzzy_sessions(tmp_path):
    conn = make_memory_conn()
    for index in range(600):
        seed_session_with_mutations(
            conn,
            tmp_path,
            session_id=f"pi:fuzzy-{index:03d}",
            started_at=f"2026-05-31T10:{index // 60:02d}:{index % 60:02d}Z",
            summary="Implemented deterministic file history retrieval for session-collapsed mutations.",
            mutations=[(1, "edit", "etc/prd/file-conversation-history.md")] if index == 599 else [],
        )

    results = find_candidates(conn, topic="deterministc file hystory", mutated="file-conversation", limit=1)["results"]

    assert [result["ref"] for result in results] == ["session/pi:fuzzy-599"]


def test_find_mutation_event_mode_preserves_event_level_file_mutations(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    result = find_candidates(conn, mutated="prd/example", mutation_mode="event")["results"][0]

    assert result["ref"] == "tool/pi:abc/12"
    assert result["inspect_refs"]["primary"] == "tool/pi:abc/12"
    assert result["match"]["kind"] == "file_mutation"
    assert result["match"]["path"] == "etc/prd/example.md"


def test_find_event_filters_compose_or_fail_clearly(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    assert find_candidates(conn, mutated="example.md", tool="edit")["results"][0]["ref"] == "session/pi:abc"
    assert find_candidates(conn, mutated="example.md", tool="edit", mutation_mode="event")["results"][0]["match"]["tool"] == "edit"
    assert find_candidates(conn, mutated="example.md", tool="bash")["results"] == []
    assert find_candidates(conn, subagent="scout", tool="subagent_run")["results"][0]["ref"] == "subagent/pi:abc/0"
    assert find_candidates(conn, skill="review", tool="skill")["results"][0]["ref"] == "tool/pi:abc/13"
    with pytest.raises(ValueError, match="Cannot combine event criteria"):
        find_candidates(conn, mutated="example.md", subagent="scout")


def test_find_filters_compose(tmp_path):
    conn = make_memory_conn()
    seed_evidence_graph(conn, tmp_path)

    assert find_candidates(conn, tool="edit", project="session", since="2026-05-01", until="2026-05-31", session="pi:abc")["results"]
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
        "timestamp": None, "tool_name": "edit", "tool": "edit", "is_error": 0, "skill_name": None,
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
