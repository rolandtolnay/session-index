#!/usr/bin/env python3
"""CLI for session-index: search, backfill, status.

Usage:
    uv run cli.py search "query"
    uv run cli.py backfill [--source claude|pi|all] [--force] [--prune]
    uv run cli.py status [--fix]
"""

import argparse
import glob
import json
import os
import shutil
import sys
import time

from current_session import CurrentSessionError, resolve_current_session
from db import (
    get_connection,
    init_db,
    search_flexible,
    get_session,
    get_stats,
    rebuild_fts,
    run_readonly_select,
    fact_table_schema_reference,
    session_columns,
    delete_sessions,
    DB_PATH,
)
from logger import log
from transcript import extract_excerpts, TRANSCRIPT_DIR


def _log_search(args: argparse.Namespace, count: int, elapsed_ms: int) -> None:
    """Log search call for auditing."""
    session_id = os.environ.get("SESSION_INDEX_CALLER_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID", "")
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


def _warn_missing_path(label: str, path: str) -> None:
    """Warn that a printed path is not currently openable."""
    print(f"Warning: {label} does not exist yet: {path}", file=sys.stderr)


def cmd_current(args: argparse.Namespace) -> None:
    """Print the exact active runtime session from Session Index env."""
    try:
        current = resolve_current_session()
    except CurrentSessionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)

    if args.json:
        print(json.dumps(current.to_json_dict(), sort_keys=True))
    elif args.path:
        print(current.transcript_path)
        if not current.transcript_exists:
            _warn_missing_path("Clean Transcript", current.transcript_path)
    elif args.native:
        print(current.native_session_id)
    else:
        print(current.session_id)


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
        if r.get("tool_log_path"):
            print(f"  tool log: {r['tool_log_path']}")

    print(f"\n{'─' * 60}")
    print(f"  {len(results)} result(s)")
    _log_search(args, len(results), int((time.monotonic() - start) * 1000))


def _log_excerpt(session_ids: list[str], query: str, elapsed_ms: int) -> None:
    """Log excerpt call for auditing."""
    caller_sid = os.environ.get("SESSION_INDEX_CALLER_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID", "")
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
        if session.get("tool_log_path"):
            print(f"  Tool log available: {session['tool_log_path']}")

    print(f"\n{'─' * 60}")
    _log_excerpt(
        [s["session_id"] for s in resolved],
        args.query,
        int((time.monotonic() - start) * 1000),
    )


def _backfill_options(args: argparse.Namespace):
    """Pick the indexing pass: deterministic-only (--no-summary) or full (+ LLM summary)."""
    from indexer import FULL_INDEX_OPTIONS, NO_SUMMARY_INDEX_OPTIONS

    return NO_SUMMARY_INDEX_OPTIONS if args.no_summary else FULL_INDEX_OPTIONS


def cmd_backfill(args: argparse.Namespace) -> None:
    """Process JSONL files from Claude Code and/or Pi."""
    from indexer import (
        IndexStage,
        index_source_transcript,
        parse_session_file,
    )
    from sources import discover_sessions

    source = getattr(args, "source", "all")
    try:
        source_files = discover_sessions(
            source,
            session_id=getattr(args, "session", None),
            pi_session_dir=getattr(args, "pi_session_dir", None),
        )
    except ValueError as e:
        print(str(e))
        return

    if not source_files:
        print("No JSONL files found.")
        return

    conn = get_connection()
    init_db(conn)

    # Prune noise sessions before processing
    if args.prune:
        prune_rows = conn.execute(
            "SELECT session_id FROM sessions WHERE summary IS NOT NULL "
            "AND (summary LIKE '%no coding%' OR summary LIKE '%no changes%' "
            "OR summary LIKE '%no active%')"
        ).fetchall()
        pruned = delete_sessions(conn, [row[0] for row in prune_rows])
        if pruned:
            print(f"Pruned {pruned} noise session(s)")

    options = _backfill_options(args)

    # Skip sessions already complete for the requested pass (--force re-does all).
    # The tool-log clause keeps re-indexing sessions that still lack tool logs /
    # fact tables (no tools at all, or never got a tool log) so they get caught up.
    existing = set()
    if not args.force:
        done_column = "transcript_path" if args.no_summary else "summary"
        cursor = conn.execute(
            f"SELECT session_id FROM sessions WHERE {done_column} IS NOT NULL "
            "AND (tools_used IS NULL OR tools_used = '' OR tool_log_path IS NOT NULL)"
        )
        existing = {row[0] for row in cursor.fetchall()}

    total = len(source_files)
    processed = 0
    skipped = 0
    errors = 0

    for i, source_file in enumerate(source_files, 1):
        source_name = source_file.source
        path = source_file.path
        display_id = os.path.splitext(os.path.basename(path))[0]

        try:
            start = time.monotonic()
            session = parse_session_file(source_name, path)
            session_id = session.session_id or display_id

            if not session.session_id:
                skipped += 1
                continue

            if session.session_id in existing:
                skipped += 1
                continue

            # Filter by project name before invoking expensive stages.
            if args.project and session.project.lower() != args.project.lower():
                skipped += 1
                continue

            result = index_source_transcript(source_name, path, options, parsed_session=session)
            if result.skipped_reason:
                skipped += 1
                continue

            elapsed = time.monotonic() - start
            statuses = []
            if IndexStage.SUMMARY in options.stages:
                statuses.append("summary" if result.summary_generated else "no summary")
            if IndexStage.CLEAN_TRANSCRIPT in options.stages:
                statuses.append("transcript" if result.transcript_path else "no transcript")
            if IndexStage.SUBAGENT_TRANSCRIPTS in options.stages:
                statuses.append(f"{result.subagents} subagent(s)")
            if IndexStage.TOOL_LOG in options.stages:
                statuses.append("tool log" if result.tool_log_path else "no tool log")
            status = ", ".join(statuses) if statuses else "metadata"
            print(f"[{i}/{total}] {source_name}:{session_id[:12]}... {status} ({elapsed:.1f}s)")
            processed += 1

        except Exception as e:
            print(f"[{i}/{total}] {source_name}:{display_id[:12]}... ERROR: {e}")
            errors += 1

    conn.close()
    print(f"\nDone: {processed} processed, {skipped} skipped, {errors} errors (of {total} total)")


# ── Query (read-only SQL escape hatch) ─────────────────────────────────────────

_QUERY_LIMIT_CAP = 1000

EXAMPLE_QUERIES = """\
-- 1. Sessions with the most direct subagent-request tool calls
SELECT session_id, COUNT(*) n FROM tool_calls
WHERE tool IN ('agent', 'subagent', 'subagent_run') AND scope='main'
GROUP BY session_id ORDER BY n DESC LIMIT 10;

-- 2. How often I picked the recommended answer (Claude + recovered Pi)
SELECT was_recommended, COUNT(*) FROM question_answers
WHERE was_recommended IS NOT NULL AND multi_select=0 GROUP BY was_recommended;

-- 3. Sessions that used a given skill
SELECT DISTINCT t.session_id, s.project, s.started_at
FROM tool_calls t JOIN sessions s ON s.session_id=t.session_id
WHERE t.skill_name='update-config' ORDER BY s.started_at DESC;

-- 4. Sessions that used a given subagent type
SELECT parent_session_id, COUNT(*) runs FROM subagent_runs
WHERE requested_agent_type='Explore' GROUP BY parent_session_id ORDER BY runs DESC;"""


def _log_query(sql: str, count: int, truncated: bool, elapsed_ms: int, error: str | None = None) -> None:
    """Log query call for auditing (mirrors _log_search)."""
    session_id = os.environ.get("SESSION_INDEX_CALLER_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID", "")
    one_line = " ".join((sql or "").split())[:200]
    if error:
        log(session_id, "query", f'sql="{one_line}" -> ERROR: {error} ({elapsed_ms}ms)')
    else:
        suffix = "+truncated" if truncated else ""
        log(session_id, "query", f'sql="{one_line}" -> {count} rows{suffix} ({elapsed_ms}ms)')


def _print_query_table(columns: list[str], rows: list[list]) -> None:
    """Print an aligned text table (columns capped at 60 chars)."""
    if not columns:
        print("(query returned no columns)")
        return

    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [min(60, len(c)) for c in columns]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = min(60, max(widths[i], len(cell)))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell[:widths[i]].ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt(columns))
    print("  ".join("-" * w for w in widths))
    for row in str_rows:
        print(fmt(row))
    print(f"\n{len(rows)} row(s)")


def cmd_query(args: argparse.Namespace) -> None:
    """Run a guarded read-only SELECT against the session index."""
    if args.schema:
        print(fact_table_schema_reference())
        print("\n-- sessions columns --")
        print(", ".join(session_columns()))
        print("\n-- example queries --")
        print(EXAMPLE_QUERIES)
        return

    if not args.sql:
        print("Provide a SQL query, or use --schema to see the tables and examples.", file=sys.stderr)
        raise SystemExit(2)

    if not os.path.exists(DB_PATH):
        print("No database found. Run `backfill` to create one.", file=sys.stderr)
        raise SystemExit(1)

    limit = max(1, min(args.limit, _QUERY_LIMIT_CAP))
    start = time.monotonic()
    try:
        columns, rows, truncated = run_readonly_select(args.sql, limit)
    except Exception as e:
        _log_query(args.sql, 0, False, int((time.monotonic() - start) * 1000), error=str(e))
        # Print verbatim so the caller can self-correct.
        print(f"Query error: {e}", file=sys.stderr)
        raise SystemExit(1)

    _log_query(args.sql, len(rows), truncated, int((time.monotonic() - start) * 1000))

    if args.json:
        print(json.dumps([dict(zip(columns, row)) for row in rows], default=str))
    else:
        _print_query_table(columns, rows)

    if truncated:
        print(
            f"\n[truncated at {limit} rows — raise --limit (max {_QUERY_LIMIT_CAP}) "
            f"or add LIMIT / aggregation to the query]",
            file=sys.stderr,
        )


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

    def source_jsonl_exists(row) -> bool:
        source_path = row["source_path"] if "source_path" in row.keys() else None
        if source_path and os.path.exists(source_path):
            return True
        sid = row["session_id"]
        native = row["native_session_id"] if "native_session_id" in row.keys() else sid
        if (row["source"] if "source" in row.keys() else "claude") == "claude":
            return bool(glob.glob(os.path.join(projects_dir, "*", f"{native}.jsonl")))
        return False

    # Missing summaries
    cursor = conn.execute(
        "SELECT session_id, native_session_id, source, source_path FROM sessions WHERE summary IS NULL"
    )
    for row in cursor:
        sid = row["session_id"]
        issues["missing_summary"].append(sid)
        if source_jsonl_exists(row):
            issues["recoverable"].append(sid)

    # Missing transcripts
    cursor = conn.execute(
        "SELECT session_id, native_session_id, source, source_path FROM sessions WHERE transcript_path IS NULL"
    )
    for row in cursor:
        sid = row["session_id"]
        issues["missing_transcript"].append(sid)
        if source_jsonl_exists(row):
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
        cursor = conn.execute(
            "SELECT tool_log_path FROM sessions WHERE tool_log_path IS NOT NULL"
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
                parts.append(f"{recoverable} recoverable via `backfill --no-summary --force`")
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

    # current
    sp_current = subparsers.add_parser("current", help="Show the active runtime session")
    current_output = sp_current.add_mutually_exclusive_group()
    current_output.add_argument(
        "--path",
        action="store_true",
        help="Print the deterministic clean transcript path; warn if it does not exist yet",
    )
    current_output.add_argument("--native", action="store_true", help="Print the provider-native session ID")
    current_output.add_argument("--json", action="store_true", help="Print full current-session metadata as JSON")
    sp_current.set_defaults(func=cmd_current)

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
    sp_backfill.add_argument("--force", action="store_true", help="Re-process sessions already indexed (skip the skip-if-done check)")
    sp_backfill.add_argument("--prune", action="store_true", help="Delete noise sessions before processing")
    sp_backfill.add_argument("--source", choices=("claude", "pi", "all"), default="all", help="Conversation source to process (default: all)")
    sp_backfill.add_argument("--pi-session-dir", help="Override Pi session directory")
    sp_backfill.add_argument("--project", help="Only process sessions for this project name")
    sp_backfill.add_argument("--session", help="Only process this specific session ID")
    sp_backfill.add_argument("--no-summary", action="store_true",
                             help="Skip the LLM summary; regenerate transcripts, tool logs, "
                                  "subagent transcripts, and fact tables only (fast, no network)")
    sp_backfill.set_defaults(func=cmd_backfill)

    # query
    sp_query = subparsers.add_parser("query", help="Run a read-only SELECT against the fact tables")
    sp_query.add_argument("sql", nargs="?", default=None, help="A single SELECT / WITH statement")
    sp_query.add_argument("--json", action="store_true", help="Output rows as JSON")
    sp_query.add_argument("--limit", type=int, default=50, help=f"Max rows (default 50, cap {_QUERY_LIMIT_CAP})")
    sp_query.add_argument("--schema", action="store_true", help="Print fact-table schema + examples and exit")
    sp_query.set_defaults(func=cmd_query)

    # status
    sp_status = subparsers.add_parser("status", help="Index statistics and integrity check")
    sp_status.add_argument("--fix", action="store_true", help="Repair dangling paths and orphaned files")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
