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
import shutil
import sys
import time

from db import get_connection, init_db, upsert_session, search_flexible, get_session, get_stats, rebuild_fts, DB_PATH
from logger import log
from parser import parse_jsonl, clean_user_messages
from subagent_parser import discover_subagents, parse_subagent_jsonl
from summarizer import summarize
from transcript import write_transcript, write_subagent_transcript, SubagentRef, extract_excerpts, TRANSCRIPT_DIR


def _log_search(args: argparse.Namespace, count: int, elapsed_ms: int) -> None:
    """Log search call for auditing."""
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    params = []
    if args.query:
        params.append(f'query="{args.query}"')
    if getattr(args, "project", None):
        params.append(f"project={args.project}")
    if getattr(args, "since", None):
        params.append(f"since={args.since}")
    if getattr(args, "until", None):
        params.append(f"until={args.until}")
    if args.limit != 20:
        params.append(f"limit={args.limit}")
    log(session_id, "search", f"{' '.join(params)} -> {count} results ({elapsed_ms}ms)")


def cmd_search(args: argparse.Namespace) -> None:
    """Search the session index."""
    start = time.monotonic()
    conn = get_connection()
    init_db(conn)
    use_or = getattr(args, "any", False)
    results = search_flexible(
        conn,
        query=args.query,
        project=getattr(args, "project", None),
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        limit=args.limit,
        use_or=use_or,
    )

    # Zero-results fallback: retry with OR if AND returned nothing
    or_fallback = False
    if not results and args.query and not use_or and len(args.query.split()) > 1:
        results = search_flexible(
            conn,
            query=args.query,
            project=getattr(args, "project", None),
            since=getattr(args, "since", None),
            until=getattr(args, "until", None),
            limit=args.limit,
            use_or=True,
        )
        or_fallback = bool(results)

    conn.close()

    if not results:
        _log_search(args, 0, int((time.monotonic() - start) * 1000))
        if args.query and len(args.query.split()) > 1:
            print("No results found. Try fewer keywords or use OR between terms.")
        else:
            print("No results found.")
        return

    if or_fallback:
        print("No exact matches. Showing partial matches:")

    for r in results:
        print(f"\n{'─' * 60}")
        sid = r["session_id"]
        project = r.get("project") or "unknown"
        date = (r.get("started_at") or "")[:10]
        duration = r.get("duration_seconds", 0)
        duration_str = f"{duration // 60}m{duration % 60}s" if duration else "?"

        print(f"  {sid}  |  {project}  |  {date}  |  {duration_str}")

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
    _log_search(args, len(results), int((time.monotonic() - start) * 1000))


def _log_excerpt(session_ids: list[str], query: str, elapsed_ms: int) -> None:
    """Log excerpt call for auditing."""
    caller_sid = os.environ.get("CLAUDE_SESSION_ID", "")
    ids_str = ",".join(s[:12] for s in session_ids)
    log(caller_sid, "excerpt", f'sessions=[{ids_str}] query="{query}" ({elapsed_ms}ms)')


def _print_agent_excerpts(main_transcript_path: str, keywords: list[str]) -> None:
    """Scan subagent transcripts for keyword matches, print top hit + count of rest.

    Subagent prompts and tool calls live in <main_transcript_path minus .md>/agent-*.md.
    `extract_excerpts` against the parent session can't surface them.
    """
    agent_dir = main_transcript_path[:-3] if main_transcript_path.endswith(".md") else main_transcript_path
    if not os.path.isdir(agent_dir):
        return

    agent_files = sorted(glob.glob(os.path.join(agent_dir, "agent-*.md")))
    kw_filtered = [k for k in keywords if len(k) > 2]
    if not kw_filtered:
        return

    scored: list[tuple[int, str]] = []
    for agent_path in agent_files:
        try:
            with open(agent_path) as f:
                content_lower = f.read().lower()
        except OSError:
            continue
        score = sum(content_lower.count(k.lower()) for k in kw_filtered)
        if score > 0:
            scored.append((score, agent_path))

    if not scored:
        return

    scored.sort(reverse=True)
    top_score, top_path = scored[0]
    top_excerpt = extract_excerpts(top_path, keywords, max_blocks=3, max_lines=60)
    if top_excerpt:
        agent_name = os.path.basename(top_path).replace(".md", "")
        print(f"  ┄┄┄ {agent_name} ({top_score} keyword hits) ┄┄┄")
        for line in top_excerpt.splitlines():
            print(f"  {line}")

    remaining = len(scored) - 1
    if remaining > 0:
        print(f"  [{remaining} more agent transcript(s) matched — read {agent_dir}/ directly for more]")


def cmd_excerpt(args: argparse.Namespace) -> None:
    """Extract transcript excerpts from specific sessions."""
    start = time.monotonic()
    conn = get_connection()
    init_db(conn)

    MAX_SESSIONS = 3
    identifiers = args.sessions[:MAX_SESSIONS]
    keywords = args.query.split()

    if len(args.sessions) > MAX_SESSIONS:
        print(f"Note: limited to {MAX_SESSIONS} sessions (requested {len(args.sessions)})")

    resolved = []
    for ident in identifiers:
        session = get_session(conn, ident)
        if session is None:
            print(f"Session not found: {ident}")
            continue
        if not session.get("transcript_path"):
            print(f"No transcript available for: {session['session_id']}")
            continue
        resolved.append(session)

    conn.close()

    if not resolved:
        _log_excerpt([i for i in identifiers], args.query, int((time.monotonic() - start) * 1000))
        print("No valid sessions to excerpt.")
        return

    for session in resolved:
        sid = session["session_id"]
        project = session.get("project") or "unknown"
        date = (session.get("started_at") or "")[:10]

        print(f"\n{'─' * 60}")
        print(f"  {sid}  |  {project}  |  {date}")

        excerpt = extract_excerpts(
            session["transcript_path"],
            keywords,
            max_blocks=4,
            max_lines=100,
        )
        if excerpt:
            print(f"  ┄┄┄ excerpts ┄┄┄")
            for line in excerpt.splitlines():
                print(f"  {line}")
        else:
            print(f"  No matching excerpts for: {' '.join(keywords)}")

        _print_agent_excerpts(session["transcript_path"], keywords)

    print(f"\n{'─' * 60}")
    _log_excerpt(
        [s["session_id"] for s in resolved],
        args.query,
        int((time.monotonic() - start) * 1000),
    )


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
    if not args.force and not args.transcripts_only and not args.subagents:
        cursor = conn.execute(
            "SELECT session_id FROM sessions WHERE summary IS NOT NULL"
        )
        existing = {row[0] for row in cursor.fetchall()}

    # For --subagents without --force, skip sessions that already have subagent_transcripts
    existing_subagents = set()
    if args.subagents and not args.force:
        cursor = conn.execute(
            "SELECT session_id FROM sessions WHERE subagent_transcripts IS NOT NULL"
        )
        existing_subagents = {row[0] for row in cursor.fetchall()}

    total = len(jsonl_files)
    processed = 0
    skipped = 0
    errors = 0

    for i, path in enumerate(jsonl_files, 1):
        session_id = os.path.splitext(os.path.basename(path))[0]

        if not args.subagents and session_id in existing:
            skipped += 1
            continue

        try:
            start = time.monotonic()
            session = parse_jsonl(path)

            # Filter by project name if --project given
            if args.project and session.project.lower() != args.project.lower():
                skipped += 1
                continue

            if args.subagents:
                # Subagent processing mode
                already_processed = session_id in existing_subagents

                # --transcripts-only: regenerate parent transcripts (with subagent links)
                # even for sessions whose subagents were already processed
                if args.transcripts_only and session.messages:
                    # Discover subagents to build reference links
                    sub_infos = discover_subagents(path)
                    sub_refs = []
                    for info in sub_infos:
                        parsed = parse_subagent_jsonl(info.jsonl_path, info.meta_path)
                        if parsed.messages:
                            sub_refs.append(SubagentRef(agent_type=parsed.agent_type, agent_id=parsed.agent_id))
                    write_transcript(
                        session.session_id,
                        session.messages,
                        project=session.project,
                        branch=session.branch,
                        timestamp=session.started_at,
                        subagents=sub_refs or None,
                    )
                    if already_processed:
                        elapsed = time.monotonic() - start
                        print(f"[{i}/{total}] {session_id[:12]}... transcript ({elapsed:.1f}s)")
                        processed += 1
                        continue

                if already_processed:
                    skipped += 1
                    continue

                subagent_infos = discover_subagents(path)
                if not subagent_infos:
                    skipped += 1
                    continue

                parsed_subagents = []
                for info in subagent_infos:
                    parsed = parse_subagent_jsonl(info.jsonl_path, info.meta_path)
                    if parsed.messages:
                        parsed_subagents.append(parsed)

                if not parsed_subagents:
                    skipped += 1
                    continue

                # Aggregate subagent files_touched into parent
                all_files = set(session.files_touched)
                for sub in parsed_subagents:
                    all_files.update(sub.files_touched)
                enriched_files = sorted(all_files)

                # Write subagent transcripts
                subagent_paths = []
                for sub in parsed_subagents:
                    sub_path = write_subagent_transcript(session.session_id or session_id, sub)
                    subagent_paths.append(sub_path)

                # Update DB with enriched files and subagent paths
                upsert_session(
                    conn,
                    session_id=session.session_id or session_id,
                    files_touched=", ".join(enriched_files) if enriched_files else None,
                    subagent_transcripts=", ".join(subagent_paths) if subagent_paths else None,
                )

                elapsed = time.monotonic() - start
                print(f"[{i}/{total}] {session_id[:12]}... {len(parsed_subagents)} subagent(s) ({elapsed:.1f}s)")
                processed += 1

            elif args.transcripts_only:
                # Transcript-only: regenerate for any session that has messages,
                # regardless of threshold (every DB entry deserves a transcript)
                if not session.messages:
                    skipped += 1
                    continue
                # Discover subagents for reference links
                sub_infos = discover_subagents(path)
                sub_refs = []
                for info in sub_infos:
                    parsed_sub = parse_subagent_jsonl(info.jsonl_path, info.meta_path)
                    if parsed_sub.messages:
                        sub_refs.append(SubagentRef(agent_type=parsed_sub.agent_type, agent_id=parsed_sub.agent_id))
                # Only regenerate transcript, skip summary
                transcript_path = write_transcript(
                        session.session_id,
                        session.messages,
                        project=session.project,
                        branch=session.branch,
                        timestamp=session.started_at,
                        subagents=sub_refs or None,
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
                if session.user_message_count < 1 or session.assistant_message_count < 1:
                    skipped += 1
                    continue

                # For short sessions, include last assistant message for context
                SHORT_SESSION_THRESHOLD = 5
                last_assistant = None
                if session.user_message_count <= SHORT_SESSION_THRESHOLD and session.assistant_messages:
                    last_assistant = session.assistant_messages[-1]

                # Generate summary + transcript
                summary = summarize(
                    project=session.project,
                    branch=session.branch,
                    user_messages=clean_user_messages(session.user_messages),
                    files_touched=session.files_touched,
                    last_assistant_message=last_assistant,
                )

                transcript_path = None
                if session.messages:
                    # Discover subagents for reference links in transcript
                    sub_infos = discover_subagents(path)
                    sub_refs = []
                    for info in sub_infos:
                        parsed_sub = parse_subagent_jsonl(info.jsonl_path, info.meta_path)
                        if parsed_sub.messages:
                            sub_refs.append(SubagentRef(agent_type=parsed_sub.agent_type, agent_id=parsed_sub.agent_id))
                    transcript_path = write_transcript(
                        session.session_id,
                        session.messages,
                        project=session.project,
                        branch=session.branch,
                        timestamp=session.started_at,
                        subagents=sub_refs or None,
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
        "dangling_subagent": [],     # session_ids where subagent_transcripts paths are missing
        "orphaned_subagent_dirs": [],  # subagent dirs on disk with no DB reference
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
            if not os.path.isfile(fpath):
                continue  # skip subagent directories
            if fpath not in db_paths:
                issues["orphaned_transcripts"].append(fpath)

    # Dangling subagent transcript paths
    cursor = conn.execute(
        "SELECT session_id, subagent_transcripts FROM sessions "
        "WHERE subagent_transcripts IS NOT NULL"
    )
    for row in cursor:
        sid = row[0]
        paths = [p.strip() for p in row[1].split(",") if p.strip()]
        if any(not os.path.exists(p) for p in paths):
            issues["dangling_subagent"].append(sid)

    # Orphaned subagent directories (dirs in transcripts/ with no DB reference)
    if os.path.isdir(TRANSCRIPT_DIR):
        # Collect all session_ids that have subagent_transcripts
        db_subagent_sids = set()
        cursor = conn.execute(
            "SELECT session_id FROM sessions "
            "WHERE subagent_transcripts IS NOT NULL"
        )
        for row in cursor:
            db_subagent_sids.add(row[0])

        for fname in os.listdir(TRANSCRIPT_DIR):
            fpath = os.path.join(TRANSCRIPT_DIR, fname)
            if os.path.isdir(fpath) and fname not in db_subagent_sids:
                issues["orphaned_subagent_dirs"].append(fpath)

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
        + len(issues["dangling_subagent"])
        + len(issues["orphaned_subagent_dirs"])
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
        if issues["dangling_subagent"]:
            print(f"  Dangling subagent paths: {len(issues['dangling_subagent'])}")
        if issues["orphaned_subagent_dirs"]:
            print(f"  Orphaned subagent dirs: {len(issues['orphaned_subagent_dirs'])}")

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

    # Null out dangling subagent transcript paths
    for sid in issues["dangling_subagent"]:
        conn.execute(
            "UPDATE sessions SET subagent_transcripts = NULL WHERE session_id = ?",
            (sid,),
        )
        fixed += 1

    # Remove orphaned subagent directories
    for dpath in issues["orphaned_subagent_dirs"]:
        try:
            shutil.rmtree(dpath)
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
    sp_search.add_argument("--any", action="store_true", help="Match ANY term (OR) instead of ALL terms (AND)")
    sp_search.add_argument("--limit", type=int, default=20)
    sp_search.set_defaults(func=cmd_search)

    # excerpt
    sp_excerpt = subparsers.add_parser("excerpt", help="Extract transcript passages from specific sessions")
    sp_excerpt.add_argument("sessions", nargs="+", help="Session ID(s) or 8+ char prefix (max 3)")
    sp_excerpt.add_argument("--query", "-q", required=True, help="Keywords to focus extraction")
    sp_excerpt.set_defaults(func=cmd_excerpt)

    # backfill
    sp_backfill = subparsers.add_parser("backfill", help="Process all JSONL files")
    sp_backfill.add_argument("--force", action="store_true", help="Re-process sessions with existing summaries")
    sp_backfill.add_argument("--prune", action="store_true", help="Delete noise sessions before processing")
    sp_backfill.add_argument("--project", help="Only process sessions for this project name")
    sp_backfill.add_argument("--session", help="Only process this specific session ID")
    sp_backfill.add_argument("--transcripts-only", action="store_true",
                             help="Only regenerate transcripts (skip summary generation)")
    sp_backfill.add_argument("--subagents", action="store_true",
                             help="Process subagent transcripts for sessions")
    sp_backfill.set_defaults(func=cmd_backfill)

    # status
    sp_status = subparsers.add_parser("status", help="Index statistics and integrity check")
    sp_status.add_argument("--fix", action="store_true", help="Repair dangling paths and orphaned files")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
