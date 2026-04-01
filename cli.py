#!/usr/bin/env python3
"""CLI for session-index: search, backfill, stats.

Usage:
    uv run cli.py search "query"
    uv run cli.py backfill [--force]
    uv run cli.py stats
    uv run cli.py rebuild-fts
"""

import argparse
import glob
import os
import sys
import time

from db import get_connection, init_db, upsert_session, search, get_stats, rebuild_fts
from parser import parse_jsonl
from summarizer import summarize
from transcript import write_transcript


def cmd_search(args: argparse.Namespace) -> None:
    """Search the session index."""
    conn = get_connection()
    init_db(conn)
    results = search(conn, args.query, limit=args.limit)
    conn.close()

    if not results:
        print("No results found.")
        return

    for r in results:
        print(f"\n{'─' * 60}")
        slug = r.get("slug") or r["session_id"][:12]
        project = r.get("project") or "unknown"
        date = (r.get("started_at") or "")[:10]
        duration = r.get("duration_seconds", 0)
        duration_str = f"{duration // 60}m{duration % 60}s" if duration else "?"

        print(f"  {slug}  |  {project}  |  {date}  |  {duration_str}")

        if r.get("branch"):
            print(f"  branch: {r['branch']}")
        if r.get("summary"):
            print(f"  {r['summary']}")
        elif r.get("user_messages"):
            first = r["user_messages"].split("\n---\n")[0][:120]
            print(f"  {first}")
        if r.get("files_touched"):
            files = r["files_touched"][:200]
            print(f"  files: {files}")

    print(f"\n{'─' * 60}")
    print(f"  {len(results)} result(s)")


def cmd_backfill(args: argparse.Namespace) -> None:
    """Process all JSONL files from ~/.claude/projects/."""
    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.exists(projects_dir):
        print(f"Projects dir not found: {projects_dir}")
        return

    # Find all JSONL files
    pattern = os.path.join(projects_dir, "*", "*.jsonl")
    jsonl_files = sorted(glob.glob(pattern))

    if not jsonl_files:
        print("No JSONL files found.")
        return

    conn = get_connection()
    init_db(conn)

    # Check which sessions already have summaries (skip unless --force)
    existing = set()
    if not args.force:
        cursor = conn.execute(
            "SELECT session_id FROM sessions WHERE summary IS NOT NULL"
        )
        existing = {row[0] for row in cursor.fetchall()}

    total = len(jsonl_files)
    processed = 0
    skipped = 0
    errors = 0

    for i, path in enumerate(jsonl_files, 1):
        session_id = os.path.splitext(os.path.basename(path))[0]

        if session_id in existing:
            skipped += 1
            continue

        try:
            start = time.monotonic()
            session = parse_jsonl(path)

            if session.user_message_count < 3:
                skipped += 1
                continue

            # Generate summary
            summary = summarize(
                project=session.project,
                branch=session.branch,
                user_messages=session.user_messages,
                files_touched=session.files_touched,
            )

            # Write transcript
            transcript_path = None
            if session.messages:
                transcript_path = write_transcript(
                    session.session_id,
                    session.messages,
                    slug=session.slug,
                    project=session.project,
                    branch=session.branch,
                    timestamp=session.started_at,
                )

            # Upsert
            upsert_session(
                conn,
                session_id=session.session_id,
                slug=session.slug or None,
                project_path=session.project_path or None,
                project=session.project or None,
                branch=session.branch or None,
                model=session.model or None,
                started_at=session.started_at or None,
                ended_at=session.ended_at or None,
                duration_seconds=session.duration_seconds or None,
                user_message_count=session.user_message_count,
                user_messages="\n---\n".join(session.user_messages) if session.user_messages else None,
                files_touched=", ".join(session.files_touched) if session.files_touched else None,
                tools_used=session.tools_used or None,
                summary=summary,
                transcript_path=transcript_path,
            )

            elapsed = time.monotonic() - start
            summary_status = f"summary ({elapsed:.1f}s)" if summary else "no summary"
            print(f"[{i}/{total}] {session_id[:12]}... {summary_status}")
            processed += 1

        except Exception as e:
            print(f"[{i}/{total}] {session_id[:12]}... ERROR: {e}")
            errors += 1

    conn.close()
    print(f"\nDone: {processed} processed, {skipped} skipped, {errors} errors (of {total} total)")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show index statistics."""
    conn = get_connection()
    init_db(conn)
    stats = get_stats(conn)
    conn.close()

    print(f"Total sessions: {stats['total_sessions']}")
    print(f"With summary:   {stats['with_summary']}")
    print(f"Missing summary: {stats['missing_summary']}")

    if stats["earliest"]:
        print(f"\nDate range: {stats['earliest'][:10]} → {stats['latest'][:10]}")

    if stats["projects"]:
        print(f"\nBy project:")
        for project, count in stats["projects"]:
            print(f"  {project}: {count}")


def cmd_rebuild_fts(args: argparse.Namespace) -> None:
    """Rebuild the FTS index."""
    conn = get_connection()
    rebuild_fts(conn)
    conn.close()
    print("FTS index rebuilt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Session Index CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    sp_search = subparsers.add_parser("search", help="Full-text search")
    sp_search.add_argument("query", help="Search query")
    sp_search.add_argument("--limit", type=int, default=20)
    sp_search.set_defaults(func=cmd_search)

    # backfill
    sp_backfill = subparsers.add_parser("backfill", help="Process all JSONL files")
    sp_backfill.add_argument("--force", action="store_true", help="Re-process sessions with existing summaries")
    sp_backfill.set_defaults(func=cmd_backfill)

    # stats
    sp_stats = subparsers.add_parser("stats", help="Show index statistics")
    sp_stats.set_defaults(func=cmd_stats)

    # rebuild-fts
    sp_rebuild = subparsers.add_parser("rebuild-fts", help="Rebuild FTS index")
    sp_rebuild.set_defaults(func=cmd_rebuild_fts)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
