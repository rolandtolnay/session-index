import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import discover_codex_sessions, discover_pi_sessions


def test_discover_pi_sessions_excludes_subagent_event_and_session_logs(tmp_path):
    root = tmp_path / "pi-sessions"
    parent_dir = root / "--Users-test-project--"
    top_level = parent_dir / "2026-06-07T10-00-00-000Z_019ea123-0000-7000-8000-000000000000.jsonl"
    run_dir = parent_dir / "2026-06-07T10-00-00-000Z_019ea123-0000-7000-8000-000000000000" / "runabc"
    subagent_session = run_dir / "run-0" / "session.jsonl"
    events = run_dir / "events.jsonl"

    subagent_session.parent.mkdir(parents=True)
    top_level.parent.mkdir(parents=True, exist_ok=True)
    top_level.write_text("{}\n")
    subagent_session.write_text("{}\n")
    events.write_text("{}\n")

    paths = [session.path for session in discover_pi_sessions(session_dir=str(root))]

    assert paths == [str(top_level)]


def test_discover_pi_sessions_excludes_events_even_when_filter_matches_parent_path(tmp_path):
    root = tmp_path / "pi-sessions"
    native_id = "019ea123-0000-7000-8000-000000000000"
    parent_dir = root / "--Users-test-project--"
    top_level = parent_dir / f"2026-06-07T10-00-00-000Z_{native_id}.jsonl"
    run_dir = parent_dir / f"2026-06-07T10-00-00-000Z_{native_id}" / "runabc"
    events = run_dir / "events.jsonl"

    run_dir.mkdir(parents=True)
    top_level.write_text("{}\n")
    events.write_text("{}\n")

    paths = [session.path for session in discover_pi_sessions(f"pi:{native_id}", session_dir=str(root))]

    assert paths == [str(top_level)]


def test_discover_codex_sessions_includes_active_and_archived_rollouts(tmp_path):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    native_id = "019efb69-5655-72e1-b7c4-95fdde95169e"
    active_rollout = active / "2026" / "06" / "24" / f"rollout-2026-06-24T23-54-05-{native_id}.jsonl"
    archived_rollout = archived / "rollout-2026-02-23T13-30-47-019c8a7b-11e0-7c40-b3c8-133cabf0dd48.jsonl"
    other = active / "2026" / "06" / "24" / "notes.jsonl"

    active_rollout.parent.mkdir(parents=True)
    archived_rollout.parent.mkdir(parents=True)
    active_rollout.write_text("{}\n")
    archived_rollout.write_text("{}\n")
    other.write_text("{}\n")

    sessions = discover_codex_sessions(
        session_dir=str(active),
        archived_dir=str(archived),
    )

    assert [s.source for s in sessions] == ["codex", "codex"]
    assert [s.path for s in sessions] == [str(active_rollout), str(archived_rollout)]


def test_discover_codex_sessions_filters_by_prefixed_session_id(tmp_path):
    active = tmp_path / "sessions"
    wanted = "019efb69-5655-72e1-b7c4-95fdde95169e"
    active_rollout = active / "2026" / "06" / "24" / f"rollout-2026-06-24T23-54-05-{wanted}.jsonl"
    other_rollout = active / "2026" / "06" / "24" / "rollout-2026-06-24T23-54-05-019efb67-579a-73c1-a80a-3f5a952fb690.jsonl"

    active_rollout.parent.mkdir(parents=True)
    active_rollout.write_text("{}\n")
    other_rollout.write_text("{}\n")

    paths = [
        session.path for session in discover_codex_sessions(
            f"codex:{wanted}",
            session_dir=str(active),
            archived_dir=str(tmp_path / "missing-archive"),
        )
    ]

    assert paths == [str(active_rollout)]
