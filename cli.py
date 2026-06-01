#!/usr/bin/env python3
"""CLI for session-index: find, inspect, query, backfill, status.

Use `query` for aggregates/custom SQL, `find` for compact evidence candidates,
and `inspect` for scoped transcript/tool/subagent evidence text.
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
    get_stats,
    rebuild_fts,
    run_readonly_select,
    query_reference,
    delete_sessions,
    DB_PATH,
)
from logger import log
from evidence_find import find_candidates
from evidence_inspect import EvidenceInspectError, inspect_ref
from transcript import TRANSCRIPT_DIR


def _parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def cmd_find(args: argparse.Namespace) -> None:
    """Print compact JSON Evidence Find candidates."""
    conn = get_connection()
    try:
        init_db(conn)
        data = find_candidates(
            conn,
            topic=args.topic,
            tool=args.tool,
            skill=args.skill,
            mutated=args.mutated,
            subagent=args.subagent,
            question_recommended=args.question_recommended,
            project=args.project,
            since=args.since,
            until=args.until,
            session=args.session,
            limit=args.limit,
        )
    except ValueError as e:
        print(json.dumps({"error": {"code": "invalid_find", "message": str(e)}}))
        raise SystemExit(2)
    finally:
        conn.close()
    print(json.dumps(data, default=str, sort_keys=True))


def cmd_inspect(args: argparse.Namespace) -> None:
    """Print a JSON Evidence Packet for one Inspection Reference."""
    conn = get_connection()
    try:
        init_db(conn)
        packet = inspect_ref(conn, args.ref, q=args.q, max_snippets=args.max_snippets)
    except EvidenceInspectError as e:
        print(json.dumps(e.to_json(), default=str, sort_keys=True))
        raise SystemExit(1)
    finally:
        conn.close()
    print(json.dumps(packet, default=str, sort_keys=True))


def add_find_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--topic", help="Session/topic candidate discovery; returns session/<id> refs with summaries, not evidence text")
    parser.add_argument("--tool", help="Tool Call candidate discovery; returns tool/<session_id>/<sequence> refs")
    parser.add_argument("--skill", help="Skill invocation candidates; returns tool refs for matching skill tool calls")
    parser.add_argument("--mutated", help="File Mutation path fragment from file_mutations; returns tool refs")
    parser.add_argument("--subagent", help="Requested/observed subagent type; returns subagent refs with candidate-specific transcript_path")
    parser.add_argument(
        "--question-recommended",
        type=_parse_bool,
        choices=[True, False],
        help="For --tool question, filter by true/false recommended answer selection; returns question refs",
    )
    parser.add_argument("--project", "-p", help="Filter by project name (prefix match)")
    parser.add_argument("--since", help="Only sessions from this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only sessions before this date (YYYY-MM-DD)")
    parser.add_argument("--session", help="Only this canonical session ID")
    parser.add_argument("--limit", type=int, default=20, help="Maximum candidates to return")


def add_inspect_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref",
        required=True,
        help="Inspection Reference, e.g. session/<id>, tool/<id>/<seq>, question/<id>/<seq>/<idx>, subagent/<id>/<child>",
    )
    parser.add_argument("--q", help="Query text for session/subagent Evidence Snippets; omit for session artifact metadata or subagent task area")
    parser.add_argument("--max-snippets", type=int, default=5, help="Maximum transcript Evidence Snippet blocks")


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


def add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("sql", nargs="?", default=None, help="A single read-only SELECT / WITH statement")
    parser.add_argument("--json", action="store_true", help="Output rows as JSON")
    parser.add_argument("--limit", type=int, default=50, help=f"Max rows (default 50, cap {_QUERY_LIMIT_CAP})")
    parser.add_argument("--schema", action="store_true", help="Print curated fact-table reference + Inspection Reference examples and exit")


def _log_query(sql: str, count: int, truncated: bool, elapsed_ms: int, error: str | None = None) -> None:
    """Log query calls for auditing."""
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
        print(query_reference())
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
    parser = argparse.ArgumentParser(
        description=(
            "Session Index CLI. Decision tree: use query for aggregates/custom SQL, "
            "find for compact evidence candidates, inspect for scoped evidence text."
        )
    )
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

    # find
    sp_find = subparsers.add_parser(
        "find",
        help="Compact JSON evidence candidates (no transcript/tool-log evidence text)",
        description=(
            "Evidence Find: compact JSON candidates with Inspection References. "
            "Use query for aggregates/custom SQL and inspect for scoped evidence text."
        ),
    )
    add_find_arguments(sp_find)
    sp_find.set_defaults(func=cmd_find)

    # inspect
    sp_inspect = subparsers.add_parser(
        "inspect",
        help="Resolve one Inspection Reference into a JSON Evidence Packet",
        description="Evidence Inspect: scoped evidence text from refs returned by find.",
    )
    add_inspect_arguments(sp_inspect)
    sp_inspect.set_defaults(func=cmd_inspect)

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
    sp_query = subparsers.add_parser(
        "query",
        help="Run read-only SQL; --schema prints a curated fact-table reference",
        description=(
            "Query: read-only SELECT/WITH for aggregates, rankings, audits, and custom joins. "
            "Use --schema for table semantics and examples that construct refs for inspect."
        ),
    )
    add_query_arguments(sp_query)
    sp_query.set_defaults(func=cmd_query)

    # status
    sp_status = subparsers.add_parser("status", help="Index statistics and integrity check")
    sp_status.add_argument("--fix", action="store_true", help="Repair dangling paths and orphaned files")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
