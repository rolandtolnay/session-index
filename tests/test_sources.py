import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import discover_pi_sessions


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
