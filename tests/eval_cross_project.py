#!/usr/bin/env python3
"""Evaluate cross-project injection hit rate for SessionStart hook.

Simulates what the SessionStart hook would inject for each historical
session, then checks if those cross-project references appear in the
subsequent conversation transcript.

NOTE: Only meaningful for sessions created AFTER the session-index system
was installed and the SessionStart hook was active. Backfilled sessions
won't show cross-project references since the injection didn't exist yet.

Usage:
    uv run tests/eval_cross_project.py
    uv run tests/eval_cross_project.py --verbose
    uv run tests/eval_cross_project.py --since 2026-04-01
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_connection, init_db, DB_PATH


def _sessions_before(conn, timestamp: str, project: str, limit: int = 10) -> list[dict]:
    """Get cross-project sessions that existed in the 24h before a timestamp."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return []
    since = (dt - timedelta(hours=24)).isoformat()
    cursor = conn.execute("""
        SELECT project, summary FROM sessions
        WHERE started_at >= :since AND started_at < :before
        AND project != :exclude AND project IS NOT NULL
        ORDER BY started_at DESC
        LIMIT :limit
    """, {"since": since, "before": timestamp, "exclude": project, "limit": limit})
    return [dict(row) for row in cursor.fetchall()]


def _extract_reference_tokens(session: dict) -> set[str]:
    """Extract searchable tokens from a cross-project session."""
    tokens = set()
    project = session.get("project", "")
    if project:
        tokens.add(project.lower())
    return tokens


def _check_references(transcript_path: str, tokens: set[str]) -> set[str]:
    """Check if any reference tokens appear in the transcript. Returns matched tokens."""
    if not transcript_path or not os.path.exists(transcript_path):
        return set()
    try:
        with open(transcript_path) as f:
            content = f.read().lower()
    except OSError:
        return set()
    return {t for t in tokens if t in content}


def main():
    parser = argparse.ArgumentParser(description="Evaluate cross-project injection hit rate")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--since", help="Only evaluate sessions from this date (YYYY-MM-DD)")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print("No database found.")
        return

    conn = get_connection()
    init_db(conn)

    # Get all sessions with transcripts
    where = "WHERE transcript_path IS NOT NULL"
    params = {}
    if args.since:
        where += " AND started_at >= :since"
        params["since"] = args.since

    cursor = conn.execute(f"""
        SELECT session_id, project, started_at, transcript_path
        FROM sessions
        {where}
        ORDER BY started_at ASC
    """, params)
    sessions = [dict(row) for row in cursor.fetchall()]

    if not sessions:
        print("No sessions with transcripts found.")
        conn.close()
        return

    # Evaluate
    total_analyzed = 0
    sessions_with_injection = 0
    total_injected = 0
    total_referenced = 0
    project_stats = defaultdict(lambda: {"injected": 0, "referenced": 0})

    for s in sessions:
        project = s.get("project", "")
        started_at = s.get("started_at", "")
        if not project or not started_at:
            continue

        # Simulate what SessionStart would have injected
        cross_project = _sessions_before(conn, started_at, project)
        if not cross_project:
            total_analyzed += 1
            continue

        sessions_with_injection += 1
        total_analyzed += 1

        # Collect all cross-project names
        all_tokens = set()
        injected_projects = set()
        for cp in cross_project:
            tokens = _extract_reference_tokens(cp)
            all_tokens |= tokens
            cp_name = cp.get("project", "")
            if cp_name:
                injected_projects.add(cp_name)

        total_injected += len(injected_projects)

        # Check transcript for references
        matched = _check_references(s["transcript_path"], all_tokens)
        if matched:
            total_referenced += len(matched)
            for m in matched:
                project_stats[m]["referenced"] += 1
            if args.verbose:
                print(f"  HIT: {s['session_id'][:12]} ({project}) referenced: {matched}")

        for ip in injected_projects:
            project_stats[ip.lower()]["injected"] += 1

    conn.close()

    # Report
    print(f"\nCross-Project Injection Evaluation")
    print(f"{'=' * 50}")
    print(f"Sessions analyzed:               {total_analyzed}")
    print(f"Sessions with cross-project:     {sessions_with_injection}")
    print()
    if total_injected > 0:
        print(f"Per-injection stats:")
        print(f"  Total injected project refs:  {total_injected}")
        print(f"  Referenced in conversation:   {total_referenced} ({100*total_referenced/total_injected:.1f}%)")
        print(f"  Not referenced:               {total_injected - total_referenced} ({100*(total_injected-total_referenced)/total_injected:.1f}%)")
    else:
        print(f"No cross-project injections found in the evaluated sessions.")
        print(f"This is expected if the system was recently installed — ")
        print(f"run again after 1-2 weeks of normal use.")

    if project_stats:
        print(f"\nBy project (hit rate):")
        for proj, stats in sorted(project_stats.items(), key=lambda x: x[1]["injected"], reverse=True):
            inj = stats["injected"]
            ref = stats["referenced"]
            pct = f"{100*ref/inj:.0f}%" if inj else "0%"
            print(f"  {proj}: {ref}/{inj} ({pct})")


if __name__ == "__main__":
    main()
