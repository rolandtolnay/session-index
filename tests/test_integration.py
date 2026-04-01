"""Integration test: parse → db → search round-trip."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import parse_jsonl
from db import init_db, upsert_session, search, get_stats

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.jsonl")


def test_parse_to_db_to_search():
    """Full round-trip: parse JSONL → insert to DB → search by content."""
    session = parse_jsonl(FIXTURE)

    # Create in-memory DB
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Upsert parsed session
    upsert_session(
        conn,
        session_id=session.session_id,
        slug=session.slug,
        project_path=session.project_path,
        project=session.project,
        branch=session.branch,
        model=session.model,
        started_at=session.started_at,
        ended_at=session.ended_at,
        duration_seconds=session.duration_seconds,
        user_message_count=session.user_message_count,
        user_messages="\n---\n".join(session.user_messages),
        files_touched=", ".join(session.files_touched),
        tools_used=session.tools_used,
        summary="Fixed login bug in auth.py with None password validation",
    )

    # Search should find it
    results = search(conn, "login bug")
    assert len(results) >= 1
    assert results[0]["session_id"] == "test-session-abc123"

    # Search by file
    results = search(conn, "auth.py")
    assert len(results) >= 1

    # Stats
    stats = get_stats(conn)
    assert stats["total_sessions"] == 1
    assert stats["with_summary"] == 1

    conn.close()
