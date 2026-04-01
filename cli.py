#!/usr/bin/env python3
"""CLI for session-index: search, backfill, status.

Usage:
    uv run cli.py search "query"
    uv run cli.py backfill [--force] [--prune]
    uv run cli.py status [--fix]
"""

import argparse
import glob
import os
import sys
import time

from db import get_connection, init_db, upsert_session, search_flexible, get_stats, rebuild_fts, DB_PATH
from parser import parse_jsonl, clean_user_messages
from summarizer import summarize
from transcript import write_transcript, TRANSCRIPT_DIR


def cmd_search(args: argparse.Namespace) -> None:
    """Search the session index."""
    conn = get_connection()
    init_db(conn)
    results = search_flexible(
        conn,
        query=args.query,
        project=getattr(args, "project", None),
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        limit=args.limit,
    )
    conn.close()

    if not results:
        print("No results found.")
        return

    is_browse = not args.query

    for r in results:
        print(f"\n{'─' * 60}")
        slug = r.get("slug") or r["session_id"][:12]
        project = r.get("project") or "unknown"
        date = (r.get("started_at") or "")[:10]
        duration = r.get("duration_seconds", 0)
        duration_str = f"{duration // 60}m{duration % 60}s" if duration else "?"

        print(f"  {slug}  |  {project}  |  {date}  |  {duration_str}")

        if is_browse:
            print(f"  session_id: {r['session_id']}")
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

    conn = get_connection()
    init_db(conn)

    # Prune noise sessions before processing
    if args.prune:
        pruned = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL "
            "AND (summary LIKE '%no coding%' OR summary LIKE '%no changes%' "
            "OR summary LIKE '%no active%')"
        ).fetchone()[0]
        if pruned:
            conn.execute(
                "DELETE FROM sessions WHERE summary IS NOT NULL "
                "AND (summary LIKE '%no coding%' OR summary LIKE '%no changes%' "
                "OR summary LIKE '%no active%')"
            )
            conn.commit()
            print(f"Pruned {pruned} noise session(s)")

    # Find JSONL files (optionally filtered)
    if args.session:
        # Single session: search all project dirs for this session ID
        pattern = os.path.join(projects_dir, "*", f"{args.session}.jsonl")
        jsonl_files = sorted(glob.glob(pattern))
    else:
        pattern = os.path.join(projects_dir, "*", "*.jsonl")
        jsonl_files = sorted(glob.glob(pattern))

    if not jsonl_files:
        print("No JSONL files found.")
        conn.close()
        return

    # Filter by project if requested (requires parsing to check project name)
    # We do this lazily during iteration to avoid parsing everything upfront

    # Check which sessions already have summaries (skip unless --force)
    existing = set()
    if not args.force and not args.transcripts_only:
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

            # Filter by project name if --project given
            if args.project and session.project.lower() != args.project.lower():
                skipped += 1
                continue

            if args.transcripts_only:
                # Transcript-only: regenerate for any session that has messages,
                # regardless of threshold (every DB entry deserves a transcript)
                if not session.messages:
                    skipped += 1
                    continue
                # Only regenerate transcript, skip summary
                transcript_path = write_transcript(
                        session.session_id,
                        session.messages,
                        slug=session.slug,
                        project=session.project,
                        branch=session.branch,
                        timestamp=session.started_at,
                    )
                if transcript_path:
                    conn.execute(
                        "UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
                        (transcript_path, session.session_id),
                    )
                    conn.commit()

                elapsed = time.monotonic() - start
                print(f"[{i}/{total}] {session_id[:12]}... transcript ({elapsed:.1f}s)")
                processed += 1
            else:
                # Full processing: apply message threshold for new entries
                if session.user_message_count < 3 or session.assistant_message_count < 1:
                    skipped += 1
                    continue
                # Generate summary + transcript
                summary = summarize(
                    project=session.project,
                    branch=session.branch,
                    user_messages=clean_user_messages(session.user_messages),
                    files_touched=session.files_touched,
                )

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


# ── Status / Doctor ──────────────────────────────────────────────────────────

def _check_integrity(conn) -> dict:
    """Run all integrity checks. Returns a dict of issues found."""
    issues = {
        "missing_summary": [],       # session_ids with NULL summary
        "recoverable": [],           # subset of missing_summary where JSONL still exists
        "missing_transcript": [],    # session_ids with NULL transcript_path
        "transcript_recoverable": [],  # subset where JSONL still exists
        "dangling_transcript": [],   # session_ids where transcript_path points to missing file
        "orphaned_transcripts": [],  # transcript files on disk with no DB row
    }

    projects_dir = os.path.expanduser("~/.claude/projects")

    # Missing summaries
    cursor = conn.execute(
        "SELECT session_id FROM sessions WHERE summary IS NULL"
    )
    for row in cursor:
        sid = row[0]
        issues["missing_summary"].append(sid)
        # Check if JSONL still exists (recoverable)
        pattern = os.path.join(projects_dir, "*", f"{sid}.jsonl")
        if glob.glob(pattern):
            issues["recoverable"].append(sid)

    # Missing transcripts
    cursor = conn.execute(
        "SELECT session_id FROM sessions WHERE transcript_path IS NULL"
    )
    for row in cursor:
        sid = row[0]
        issues["missing_transcript"].append(sid)
        pattern = os.path.join(projects_dir, "*", f"{sid}.jsonl")
        if glob.glob(pattern):
            issues["transcript_recoverable"].append(sid)

    # Dangling transcript paths
    cursor = conn.execute(
        "SELECT session_id, transcript_path FROM sessions WHERE transcript_path IS NOT NULL"
    )
    for row in cursor:
        if not os.path.exists(row[1]):
            issues["dangling_transcript"].append(row[0])

    # Orphaned transcript files
    if os.path.isdir(TRANSCRIPT_DIR):
        db_paths = set()
        cursor = conn.execute(
            "SELECT transcript_path FROM sessions WHERE transcript_path IS NOT NULL"
        )
        for row in cursor:
            db_paths.add(row[0])

        for fname in os.listdir(TRANSCRIPT_DIR):
            fpath = os.path.join(TRANSCRIPT_DIR, fname)
            if fpath not in db_paths:
                issues["orphaned_transcripts"].append(fpath)

    return issues


def cmd_status(args: argparse.Namespace) -> None:
    """Show index statistics and integrity check."""
    if not os.path.exists(DB_PATH):
        print("No database found. Run `backfill` to create one.")
        return

    conn = get_connection()
    init_db(conn)
    stats = get_stats(conn)

    # Stats
    print(f"Sessions:        {stats['total_sessions']}")
    print(f"With summary:    {stats['with_summary']}")
    print(f"Missing summary: {stats['missing_summary']}")

    if stats["earliest"]:
        print(f"Date range:      {stats['earliest'][:10]} to {stats['latest'][:10]}")

    if stats["projects"]:
        print(f"\nBy project:")
        for project, count in stats["projects"]:
            print(f"  {project}: {count}")

    # Integrity checks
    issues = _check_integrity(conn)
    total_issues = (
        len(issues["missing_transcript"])
        + len(issues["dangling_transcript"])
        + len(issues["orphaned_transcripts"])
    )

    print(f"\nIntegrity:")
    if not issues["missing_summary"] and total_issues == 0:
        print("  All clear")
    else:
        if issues["missing_summary"]:
            recoverable = len(issues["recoverable"])
            unrecoverable = len(issues["missing_summary"]) - recoverable
            parts = []
            if recoverable:
                parts.append(f"{recoverable} recoverable via backfill")
            if unrecoverable:
                parts.append(f"{unrecoverable} unrecoverable (JSONL deleted)")
            print(f"  Missing summary: {len(issues['missing_summary'])} ({', '.join(parts)})")
        if issues["missing_transcript"]:
            recoverable = len(issues["transcript_recoverable"])
            unrecoverable = len(issues["missing_transcript"]) - recoverable
            parts = []
            if recoverable:
                parts.append(f"{recoverable} recoverable via `backfill --transcripts-only --force`")
            if unrecoverable:
                parts.append(f"{unrecoverable} unrecoverable (JSONL deleted)")
            print(f"  Missing transcript: {len(issues['missing_transcript'])} ({', '.join(parts)})")
        if issues["dangling_transcript"]:
            print(f"  Dangling transcript paths: {len(issues['dangling_transcript'])}")
        if issues["orphaned_transcripts"]:
            print(f"  Orphaned transcript files: {len(issues['orphaned_transcripts'])}")

        if total_issues > 0:
            if args.fix:
                fixed = _fix_issues(conn, issues)
                print(f"\n  Fixed {fixed} issue(s)")
            else:
                print(f"\n  Run `status --fix` to repair {total_issues} issue(s)")

        if issues["recoverable"]:
            print(f"  Run `backfill` to regenerate {len(issues['recoverable'])} missing summary/summaries")

    conn.close()


def _fix_issues(conn, issues: dict) -> int:
    """Apply instant (non-LLM) fixes. Returns count of fixes applied."""
    fixed = 0

    # Null out dangling transcript paths
    for sid in issues["dangling_transcript"]:
        conn.execute(
            "UPDATE sessions SET transcript_path = NULL WHERE session_id = ?",
            (sid,),
        )
        fixed += 1

    # Remove orphaned transcript files
    for fpath in issues["orphaned_transcripts"]:
        try:
            os.remove(fpath)
            fixed += 1
        except OSError:
            pass

    if fixed:
        conn.commit()

    # Rebuild FTS as a final step
    rebuild_fts(conn)

    return fixed


def main() -> None:
    parser = argparse.ArgumentParser(description="Session Index CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    sp_search = subparsers.add_parser("search", help="Full-text search")
    sp_search.add_argument("query", nargs="?", default=None, help="Search query (optional if filters provided)")
    sp_search.add_argument("--project", "-p", help="Filter by project name (prefix match)")
    sp_search.add_argument("--since", help="Only sessions from this date (YYYY-MM-DD)")
    sp_search.add_argument("--until", help="Only sessions before this date (YYYY-MM-DD)")
    sp_search.add_argument("--limit", type=int, default=20)
    sp_search.set_defaults(func=cmd_search)

    # backfill
    sp_backfill = subparsers.add_parser("backfill", help="Process all JSONL files")
    sp_backfill.add_argument("--force", action="store_true", help="Re-process sessions with existing summaries")
    sp_backfill.add_argument("--prune", action="store_true", help="Delete noise sessions before processing")
    sp_backfill.add_argument("--project", help="Only process sessions for this project name")
    sp_backfill.add_argument("--session", help="Only process this specific session ID")
    sp_backfill.add_argument("--transcripts-only", action="store_true",
                             help="Only regenerate transcripts (skip summary generation)")
    sp_backfill.set_defaults(func=cmd_backfill)

    # status
    sp_status = subparsers.add_parser("status", help="Index statistics and integrity check")
    sp_status.add_argument("--fix", action="store_true", help="Repair dangling paths and orphaned files")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
