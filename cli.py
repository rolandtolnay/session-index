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

from current_session import CurrentSession, CurrentSessionError, resolve_current_session
from db import (
    get_connection,
    init_db,
    get_stats,
    rebuild_fts,
    run_readonly_select,
    delete_sessions,
    DB_PATH,
    TOP_LEVEL_SESSION_PREDICATE,
)
from logger import log
from evidence_find import find_candidates, validate_date_filter
from evidence_inspect import EvidenceInspectError, inspect_ref
from query_reference import query_reference
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
            mutation_mode=args.mutation_mode,
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
    parser.add_argument("--topic", help="Session/topic candidate discovery; terms are AND-joined FTS (OR/NOT supported), with deterministic fuzzy fallback when exact matching is empty")
    parser.add_argument("--tool", help="Tool Call candidate discovery; returns tool/<session_id>/<sequence> refs")
    parser.add_argument("--skill", help="Skill Invocation candidates; returns skill/<session_id>/<sequence> refs")
    parser.add_argument("--mutated", help="File Mutation path fragment; returns session-collapsed candidates by default")
    parser.add_argument("--mutation-mode", choices=("session", "event"), default="session", help="For --mutated, return session-collapsed candidates (default) or exact event rows")
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
    parser.add_argument("--limit", type=int, default=8, help="Maximum candidates to return (default 8; the payload sets truncated=true when the limit fills)")


def add_inspect_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref",
        required=True,
        help="Inspection Reference, e.g. session/<id>, skill/<id>/<seq>, tool/<id>/<seq>, question/<id>/<seq>/<idx>, subagent/<id>/<child>",
    )
    parser.add_argument("--q", help="Query text for session/subagent Evidence Snippets; omit for session artifact metadata or subagent task area")
    parser.add_argument("--max-snippets", type=int, default=5, help="Maximum transcript Evidence Snippet blocks")


def add_current_arguments(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--path",
        action="store_true",
        help="Print the deterministic clean transcript path; warn if it does not exist yet",
    )
    output.add_argument(
        "--cleaned-paths",
        action="store_true",
        help="Print canonical Clean Transcript and Tool Log paths with existence status",
    )
    output.add_argument("--native", action="store_true", help="Print the provider-native session ID")
    output.add_argument("--json", action="store_true", help="Print full current-session metadata as JSON")


def add_prune_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("sessions", nargs="+", help="Exact Canonical Session ID(s) to audit/prune")
    parser.add_argument("--confirm", action="store_true", help="Delete eligible audited session IDs and owned generated artifacts")
    parser.add_argument("--json", action="store_true", help="Output audit/deletion result as JSON")


def _warn_missing_path(label: str, path: str) -> None:
    """Warn that a printed path is not currently openable."""
    print(f"Warning: {label} does not exist yet: {path}", file=sys.stderr)


def _resolve_current_or_exit() -> CurrentSession:
    try:
        return resolve_current_session()
    except CurrentSessionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


def _print_cleaned_paths(current: CurrentSession) -> None:
    transcript_status = "exists" if current.transcript_exists else "missing"
    tool_log_status = "exists" if current.tool_log_exists else "missing"
    print(f"Clean Transcript: {current.transcript_path} [{transcript_status}]")
    print(f"Tool Log: {current.tool_log_path} [{tool_log_status}]")


def cmd_current_cleaned_paths() -> None:
    """Print the canonical generated paths for the exact active session."""
    _print_cleaned_paths(_resolve_current_or_exit())


def cmd_current(args: argparse.Namespace) -> None:
    """Print the exact active runtime session from Session Index env."""
    current = _resolve_current_or_exit()

    if args.json:
        print(json.dumps(current.to_json_dict(), sort_keys=True))
    elif getattr(args, "cleaned_paths", False):
        _print_cleaned_paths(current)
    elif args.path:
        print(current.transcript_path)
        if not current.transcript_exists:
            _warn_missing_path("Clean Transcript", current.transcript_path)
    elif args.native:
        print(current.native_session_id)
    else:
        print(current.session_id)


def _backfill_options(args: argparse.Namespace):
    """Pick the indexing pass: deterministic-only by default, summaries opt-in."""
    from indexer import FULL_INDEX_OPTIONS, NO_SUMMARY_INDEX_OPTIONS

    return FULL_INDEX_OPTIONS if getattr(args, "with_summary", False) else NO_SUMMARY_INDEX_OPTIONS


def _completed_backfill_sessions(conn, options) -> set[str]:
    """Return sessions complete for the requested deterministic or LLM pass."""
    from indexer import IndexStage

    done_predicate = (
        "summary IS NOT NULL AND headline IS NOT NULL"
        if IndexStage.SUMMARY in options.stages
        else "transcript_path IS NOT NULL"
    )
    cursor = conn.execute(
        f"SELECT session_id FROM sessions WHERE {done_predicate} "
        "AND (tools_used IS NULL OR tools_used = '' OR ("
        "tool_log_path IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM tool_calls WHERE tool_calls.session_id = sessions.session_id"
        ")))"
    )
    return {row[0] for row in cursor.fetchall()}


def _print_backfill_skip(index: int, total: int, source: str, session_id: str, reason: str) -> None:
    print(f"[{index}/{total}] {source}:{session_id[:12]}... skipped ({reason})")


def cmd_backfill(args: argparse.Namespace) -> None:
    """Process JSONL files from Claude Code, Pi, and/or Codex."""
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
            codex_session_dir=getattr(args, "codex_session_dir", None),
            codex_archived_dir=getattr(args, "codex_archived_dir", None),
        )
    except ValueError as e:
        print(str(e))
        return

    if not source_files:
        print("No JSONL files found.")
        return

    conn = get_connection()
    init_db(conn)

    if args.prune:
        conn.close()
        print("backfill --prune is disabled; run `prune SESSION_ID...` for audit-first confirmed deletion.", file=sys.stderr)
        raise SystemExit(2)

    options = _backfill_options(args)

    # Skip sessions already complete for the requested pass (--force re-does all).
    # Tool-using sessions stay incomplete until both the Tool Log and structured
    # tool-call facts exist, so legacy rows get caught up automatically.
    existing = set()
    if not args.force:
        existing = _completed_backfill_sessions(conn, options)

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
                _print_backfill_skip(i, total, source_name, display_id, "missing session ID")
                skipped += 1
                continue

            if session.session_id in existing:
                _print_backfill_skip(i, total, source_name, session_id, "already complete")
                skipped += 1
                continue

            # Filter by project name before invoking expensive stages.
            if args.project and (session.project or "").lower() != args.project.lower():
                reason = f"project {session.project or '(unknown)'} does not match {args.project}"
                _print_backfill_skip(i, total, source_name, session_id, reason)
                skipped += 1
                continue

            result = index_source_transcript(source_name, path, options, parsed_session=session)
            if result.skipped_reason:
                _print_backfill_skip(i, total, source_name, session_id, result.skipped_reason)
                skipped += 1
                continue

            elapsed = time.monotonic() - start
            statuses = []
            if IndexStage.SUMMARY in options.stages:
                statuses.append("summary" if result.summary_generated else "no summary")
                statuses.append("headline" if result.headline_generated else "no headline")
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


# Column names LLM callers repeatedly guess that have no direct equivalent.
_QUERY_COLUMN_ALIASES = {
    "provider": "the session source column is named `source`",
    "provider_name": "the session source column is named `source`",
    "arguments": (
        "tool_calls does not store argument text (only skill_invocations has an `arguments` column); "
        "inspect tool/<session_id>/<sequence> to read the full call from the Tool Log"
    ),
    "arguments_preview": (
        "tool_calls does not store argument text; "
        "inspect tool/<session_id>/<sequence> to read the full call from the Tool Log"
    ),
    "operation": "file_mutations has no operation column; each row is one successful write/edit path",
}


def _query_error_hint(sql: str, error: str) -> str | None:
    """Turn common self-correctable SQL errors into a one-line fix hint."""
    import difflib

    from query_reference import known_columns

    if "no such column" in error:
        column = error.split("no such column:")[-1].strip().split(".")[-1].lower()
        alias = _QUERY_COLUMN_ALIASES.get(column)
        if alias:
            return f"{alias}. Run query --schema for exact columns."
        close = difflib.get_close_matches(column, known_columns(), n=3)
        if close:
            return f"Did you mean: {', '.join(close)}? Run query --schema for exact columns."
        return "Run query --schema for exact table columns."
    if "no such table" in error:
        return "Run query --schema for the exact table list."
    if "Only SELECT / WITH" in error and (sql or "").lstrip().lower().startswith("pragma"):
        return (
            "PRAGMA statements are blocked, but the table-valued form works: "
            "SELECT name, type FROM pragma_table_info('tool_calls')."
        )
    return None


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
        hint = _query_error_hint(args.sql, str(e))
        if hint:
            print(f"Hint: {hint}", file=sys.stderr)
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


# ── Footprint / Prune ───────────────────────────────────────────────────────

_LOW_VALUE_SUMMARY_MARKERS = (
    "no active",
    "no changes",
    "no code",
    "no coding",
    "no files",
    "no implementation",
    "no substantive",
    "did not make changes",
)
_HIGH_VALUE_FACT_TABLES = ("file_mutations", "skill_invocations", "subagent_runs", "question_answers")


def _split_artifact_paths(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(max(size, 0))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(size)} B"


def _path_size(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fpath = os.path.join(root, name)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                pass
    return total


def _is_generated_artifact_path(path: str) -> bool:
    if not path:
        return False
    try:
        root = os.path.realpath(TRANSCRIPT_DIR)
        candidate = os.path.realpath(os.path.expanduser(path))
        return os.path.commonpath([root, candidate]) == root
    except (OSError, ValueError):
        return False


def _session_rows_by_id(conn, session_ids: list[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not session_ids:
        return rows
    placeholders = ", ".join("?" for _ in session_ids)
    cursor = conn.execute(f"SELECT * FROM sessions WHERE session_id IN ({placeholders})", session_ids)
    for row in cursor.fetchall():
        rows[row["session_id"]] = dict(row)
    return rows


def _fact_counts(conn, session_id: str) -> dict[str, int]:
    counts = {
        "tool_calls": conn.execute("SELECT COUNT(*) FROM tool_calls WHERE session_id=?", (session_id,)).fetchone()[0],
        "file_mutations": conn.execute("SELECT COUNT(*) FROM file_mutations WHERE session_id=?", (session_id,)).fetchone()[0],
        "skill_invocations": conn.execute("SELECT COUNT(*) FROM skill_invocations WHERE session_id=?", (session_id,)).fetchone()[0],
        "question_answers": conn.execute("SELECT COUNT(*) FROM question_answers WHERE session_id=?", (session_id,)).fetchone()[0],
        "subagent_runs": conn.execute("SELECT COUNT(*) FROM subagent_runs WHERE parent_session_id=?", (session_id,)).fetchone()[0],
    }
    return counts


def _other_session_references_path(conn, path: str, session_id: str) -> bool:
    if not path:
        return False
    rows = conn.execute(
        """
        SELECT session_id, transcript_path, tool_log_path, subagent_transcripts
        FROM sessions
        WHERE session_id != ?
        """,
        (session_id,),
    ).fetchall()
    for row in rows:
        if path in {row["transcript_path"], row["tool_log_path"]}:
            return True
        if path in _split_artifact_paths(row["subagent_transcripts"]):
            return True

    row = conn.execute(
        "SELECT 1 FROM subagent_runs WHERE parent_session_id != ? AND transcript_path = ? LIMIT 1",
        (session_id, path),
    ).fetchone()
    return row is not None


def _other_session_references_inside_dir(conn, path: str, session_id: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        root = os.path.realpath(path)
    except OSError:
        return False

    rows = conn.execute(
        """
        SELECT session_id, transcript_path, tool_log_path, subagent_transcripts
        FROM sessions
        WHERE session_id != ?
        """,
        (session_id,),
    ).fetchall()
    candidates: list[str] = []
    for row in rows:
        candidates.extend([row["transcript_path"], row["tool_log_path"]])
        candidates.extend(_split_artifact_paths(row["subagent_transcripts"]))
    candidates.extend(
        row["transcript_path"]
        for row in conn.execute("SELECT transcript_path FROM subagent_runs WHERE parent_session_id != ?", (session_id,))
        if row["transcript_path"]
    )

    for candidate in candidates:
        if not candidate:
            continue
        try:
            if os.path.commonpath([root, os.path.realpath(candidate)]) == root:
                return True
        except (OSError, ValueError):
            continue
    return False


def _artifact_record(conn, session_id: str, kind: str, path: str) -> dict:
    exists = bool(path and os.path.exists(path))
    owned = _is_generated_artifact_path(path)
    shared = False
    if owned:
        shared = _other_session_references_inside_dir(conn, path, session_id) if os.path.isdir(path) else _other_session_references_path(conn, path, session_id)
    return {
        "kind": kind,
        "path": path,
        "exists": exists,
        "bytes": _path_size(path),
        "owned_generated_artifact": owned,
        "shared_with_other_session": shared,
        "deletable": bool(exists and owned and not shared),
    }


def _session_artifacts(conn, session: dict) -> list[dict]:
    session_id = session["session_id"]
    candidates: list[tuple[str, str]] = [
        ("clean_transcript", session.get("transcript_path") or ""),
        ("tool_log", session.get("tool_log_path") or ""),
    ]
    candidates.extend(("subagent_transcript", path) for path in _split_artifact_paths(session.get("subagent_transcripts")))
    candidates.extend(
        ("subagent_transcript", row["transcript_path"])
        for row in conn.execute(
            "SELECT transcript_path FROM subagent_runs WHERE parent_session_id=? AND transcript_path IS NOT NULL",
            (session_id,),
        )
    )
    candidates.extend([
        ("deterministic_clean_transcript", os.path.join(TRANSCRIPT_DIR, f"{session_id}.md")),
        ("deterministic_tool_log", os.path.join(TRANSCRIPT_DIR, f"{session_id}.tools.md")),
        ("deterministic_subagent_dir", os.path.join(TRANSCRIPT_DIR, session_id)),
    ])

    records: list[dict] = []
    seen: set[str] = set()
    for kind, path in candidates:
        if not path:
            continue
        key = os.path.realpath(os.path.expanduser(path))
        if key in seen:
            continue
        seen.add(key)
        records.append(_artifact_record(conn, session_id, kind, path))
    return records


def _prune_decision(session: dict, facts: dict[str, int]) -> dict:
    blocking: list[str] = []
    summary = (session.get("summary") or "").strip().lower()
    if not summary:
        blocking.append("missing_summary")
    elif not any(marker in summary for marker in _LOW_VALUE_SUMMARY_MARKERS):
        blocking.append("no_low_value_summary_signal")
    for table in _HIGH_VALUE_FACT_TABLES:
        if facts.get(table, 0):
            blocking.append(f"{table}_present")
    if (session.get("files_touched") or "").strip():
        blocking.append("files_touched_present")

    eligible = not blocking
    return {
        "eligible": eligible,
        "decision": "delete_confirmable" if eligible else "keep",
        "reason": "low_value_summary_with_no_durable_facts" if eligible else "uncertain_or_high_value",
        "blocking": blocking,
    }


def _session_footprint(conn, session: dict) -> dict:
    facts = _fact_counts(conn, session["session_id"])
    artifacts = _session_artifacts(conn, session)
    source_path = session.get("source_path") or ""
    return {
        "session_id": session["session_id"],
        "source": session.get("source"),
        "project": session.get("project"),
        "started_at": session.get("started_at"),
        "summary_preview": (session.get("summary") or "")[:160],
        "artifact_bytes": sum(item["bytes"] for item in artifacts if item["owned_generated_artifact"]),
        "artifacts": artifacts,
        "facts": facts,
        "source_jsonl": {"path": source_path or None, "exists": bool(source_path and os.path.exists(source_path)), "retained": True},
        "prune": _prune_decision(session, facts),
    }


def build_footprint_audit(
    conn,
    *,
    session_ids: list[str] | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> dict:
    session_ids = session_ids or []
    missing = []
    if session_ids:
        rows_by_id = _session_rows_by_id(conn, session_ids)
        rows = []
        for session_id in session_ids:
            row = rows_by_id.get(session_id)
            if row:
                rows.append(row)
            else:
                missing.append(session_id)
    else:
        params: dict[str, object] = {"limit": max(1, limit)}
        clauses: list[str] = []
        if project:
            clauses.append("project LIKE :project")
            params["project"] = f"{project}%"
        if since:
            clauses.append("started_at >= :since")
            params["since"] = since
        if until:
            if len(until) == 10:
                until = f"{until}T23:59:59.999999"
            clauses.append("started_at <= :until")
            params["until"] = until
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY started_at DESC LIMIT :limit",
                params,
            ).fetchall()
        ]

    sessions = [_session_footprint(conn, row) for row in rows]
    total_bytes = sum(item["artifact_bytes"] for item in sessions)
    return {
        "generated_artifacts": {
            "session_count": len(sessions),
            "total_bytes": total_bytes,
            "total_human": _format_bytes(total_bytes),
        },
        "missing_sessions": missing,
        "sessions": sessions,
    }


def _print_footprint_audit(audit: dict) -> None:
    total = audit["generated_artifacts"]
    print(f"Generated artifacts: {total['total_human']} across {total['session_count']} session(s)")
    if audit["missing_sessions"]:
        print(f"Missing sessions: {', '.join(audit['missing_sessions'])}")
    for session in audit["sessions"]:
        prune = session["prune"]
        print(
            f"{session['session_id']}: {_format_bytes(session['artifact_bytes'])} "
            f"{prune['decision']} ({prune['reason']})"
        )
        for artifact in session["artifacts"]:
            if artifact["exists"] or artifact["path"]:
                flags = []
                if not artifact["owned_generated_artifact"]:
                    flags.append("not-owned")
                if artifact["shared_with_other_session"]:
                    flags.append("shared")
                if not artifact["exists"]:
                    flags.append("missing")
                suffix = f" [{' '.join(flags)}]" if flags else ""
                print(f"  {artifact['kind']}: {_format_bytes(artifact['bytes'])} {artifact['path']}{suffix}")
        if session["source_jsonl"]["path"]:
            print(f"  source_jsonl retained: {session['source_jsonl']['path']}")
        if prune["blocking"]:
            print(f"  keep blockers: {', '.join(prune['blocking'])}")


def add_footprint_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", action="append", help="Audit one exact Canonical Session ID; repeatable")
    parser.add_argument("--project", help="Filter by project name prefix")
    parser.add_argument("--since", help="Only sessions from this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only sessions before this date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=20, help="Maximum sessions to audit when --session is not used")
    parser.add_argument("--json", action="store_true", help="Output audit as JSON")


def cmd_footprint(args: argparse.Namespace) -> None:
    if not os.path.exists(DB_PATH):
        print("No database found. Run `backfill` to create one.")
        return
    try:
        validate_date_filter("--since", args.since)
        validate_date_filter("--until", args.until)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2)
    conn = get_connection()
    try:
        init_db(conn)
        audit = build_footprint_audit(
            conn,
            session_ids=args.session or [],
            project=args.project,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(audit, default=str, sort_keys=True))
    else:
        _print_footprint_audit(audit)


def _delete_owned_generated_artifacts(audit: dict) -> dict:
    files: list[str] = []
    dirs: list[str] = []
    skipped: list[str] = []
    for session in audit["sessions"]:
        for artifact in session["artifacts"]:
            path = artifact["path"]
            if not artifact["exists"]:
                continue
            if not artifact["deletable"]:
                skipped.append(path)
                continue
            if os.path.isdir(path):
                dirs.append(path)
            else:
                files.append(path)

    deleted: list[str] = []
    for path in sorted(set(files), key=len, reverse=True):
        try:
            os.remove(path)
            deleted.append(path)
        except OSError:
            skipped.append(path)
    for path in sorted(set(dirs), key=len, reverse=True):
        try:
            shutil.rmtree(path)
            deleted.append(path)
        except OSError:
            skipped.append(path)
    return {"deleted_artifacts": deleted, "skipped_artifacts": sorted(set(skipped))}


def cmd_prune(args: argparse.Namespace) -> None:
    if not os.path.exists(DB_PATH):
        print("No database found. Run `backfill` to create one.")
        return

    session_ids = list(dict.fromkeys(args.sessions))
    conn = get_connection()
    try:
        init_db(conn)
        audit = build_footprint_audit(conn, session_ids=session_ids)
        blocked = [
            session["session_id"]
            for session in audit["sessions"]
            if not session["prune"]["eligible"]
        ]
        if args.json and not args.confirm:
            print(json.dumps({"dry_run": True, "audit": audit}, default=str, sort_keys=True))
            return
        if not args.json:
            _print_footprint_audit(audit)
            if not args.confirm:
                print("\nDry run only. Rerun with --confirm and the same explicit session ID(s) to delete eligible sessions.")
                return
        else:
            if not args.confirm:
                return

        if audit["missing_sessions"] or blocked:
            payload = {
                "deleted": False,
                "missing_sessions": audit["missing_sessions"],
                "blocked_sessions": blocked,
                "audit": audit,
            }
            if args.json:
                print(json.dumps(payload, default=str, sort_keys=True))
            else:
                if audit["missing_sessions"]:
                    print(f"Cannot prune missing session(s): {', '.join(audit['missing_sessions'])}", file=sys.stderr)
                if blocked:
                    print(f"Cannot prune session(s) classified keep: {', '.join(blocked)}", file=sys.stderr)
            raise SystemExit(2)

        deletion = _delete_owned_generated_artifacts(audit)
        deleted_rows = delete_sessions(conn, session_ids, commit=True)
        payload = {"deleted": True, "deleted_sessions": deleted_rows, "artifact_deletion": deletion, "audit": audit}
        if args.json:
            print(json.dumps(payload, default=str, sort_keys=True))
        else:
            print(f"\nDeleted {deleted_rows} session(s) and {len(deletion['deleted_artifacts'])} generated artifact path(s).")
            if deletion["skipped_artifacts"]:
                print(f"Skipped {len(deletion['skipped_artifacts'])} artifact path(s) that were missing, shared, or outside generated storage.")
    finally:
        conn.close()


# ── Status / Doctor ──────────────────────────────────────────────────────────

def _check_integrity(conn) -> dict:
    """Run all integrity checks. Returns a dict of issues found."""
    issues = {
        "missing_summary": [],       # session_ids with NULL summary
        "recoverable": [],           # subset of missing_summary where JSONL still exists
        "missing_headline": [],      # session_ids with NULL headline
        "headline_recoverable": [],  # subset with summary + Source Transcript for backfill
        "missing_transcript": [],    # session_ids with NULL transcript_path
        "transcript_recoverable": [],  # subset where JSONL still exists
        "dangling_transcript": [],   # session_ids where transcript_path points to missing file
        "orphaned_transcripts": [],  # transcript files on disk with no DB row
        "dangling_subagent": [],     # session_ids where subagent_transcripts paths are missing
        "orphaned_subagent_dirs": [],  # subagent dirs on disk with no DB reference
    }

    projects_dir = os.path.expanduser("~/.claude/projects")
    codex_session_dir = os.path.expanduser("~/.codex/sessions")
    codex_archived_dir = os.path.expanduser("~/.codex/archived_sessions")

    def source_jsonl_exists(row) -> bool:
        source_path = row["source_path"] if "source_path" in row.keys() else None
        if source_path and os.path.exists(source_path):
            return True
        sid = row["session_id"]
        native = row["native_session_id"] if "native_session_id" in row.keys() else sid
        source = row["source"] if "source" in row.keys() else "claude"
        if source == "claude":
            return bool(glob.glob(os.path.join(projects_dir, "*", f"{native}.jsonl")))
        if source == "codex":
            return bool(
                glob.glob(os.path.join(codex_session_dir, "**", f"*{native}.jsonl"), recursive=True)
                or glob.glob(os.path.join(codex_archived_dir, "**", f"*{native}.jsonl"), recursive=True)
            )
        return False

    # Missing summaries
    cursor = conn.execute(
        f"SELECT session_id, native_session_id, source, source_path FROM sessions "
        f"WHERE summary IS NULL AND {TOP_LEVEL_SESSION_PREDICATE}"
    )
    for row in cursor:
        sid = row["session_id"]
        issues["missing_summary"].append(sid)
        if source_jsonl_exists(row):
            issues["recoverable"].append(sid)

    # Missing headlines
    cursor = conn.execute(
        "SELECT session_id, native_session_id, source, source_path, summary "
        f"FROM sessions WHERE headline IS NULL AND {TOP_LEVEL_SESSION_PREDICATE}"
    )
    for row in cursor:
        sid = row["session_id"]
        issues["missing_headline"].append(sid)
        if row["summary"] and source_jsonl_exists(row):
            issues["headline_recoverable"].append(sid)

    # Missing transcripts
    cursor = conn.execute(
        f"SELECT session_id, native_session_id, source, source_path FROM sessions "
        f"WHERE transcript_path IS NULL AND {TOP_LEVEL_SESSION_PREDICATE}"
    )
    for row in cursor:
        sid = row["session_id"]
        issues["missing_transcript"].append(sid)
        if source_jsonl_exists(row):
            issues["transcript_recoverable"].append(sid)

    # Dangling transcript paths
    cursor = conn.execute(
        f"SELECT session_id, transcript_path FROM sessions "
        f"WHERE transcript_path IS NOT NULL AND {TOP_LEVEL_SESSION_PREDICATE}"
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
        f"WHERE subagent_transcripts IS NOT NULL AND {TOP_LEVEL_SESSION_PREDICATE}"
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
    print(f"With headline:   {stats['with_headline']}")
    print(f"Missing headline: {stats['missing_headline']}")

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
    if not issues["missing_summary"] and not issues["missing_headline"] and total_issues == 0:
        print("  All clear")
    else:
        if issues["missing_summary"]:
            recoverable = len(issues["recoverable"])
            unrecoverable = len(issues["missing_summary"]) - recoverable
            parts = []
            if recoverable:
                parts.append(f"{recoverable} recoverable via `backfill --with-summary`")
            if unrecoverable:
                parts.append(f"{unrecoverable} unrecoverable (JSONL deleted)")
            print(f"  Missing summary: {len(issues['missing_summary'])} ({', '.join(parts)})")
        if issues["missing_headline"]:
            recoverable = len(issues["headline_recoverable"])
            unavailable = len(issues["missing_headline"]) - recoverable
            parts = []
            if recoverable:
                parts.append(f"{recoverable} recoverable via `backfill --with-summary`")
            if unavailable:
                parts.append(f"{unavailable} require a summary and retained Source Transcript")
            print(f"  Missing headline: {len(issues['missing_headline'])} ({', '.join(parts)})")
        if issues["missing_transcript"]:
            recoverable = len(issues["transcript_recoverable"])
            unrecoverable = len(issues["missing_transcript"]) - recoverable
            parts = []
            if recoverable:
                parts.append(f"{recoverable} recoverable via `backfill --force`")
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

        description_repairs = set(issues["recoverable"]) | set(issues["headline_recoverable"])
        if description_repairs:
            print(f"  Run `backfill --with-summary` to repair {len(description_repairs)} session description(s)")

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
    add_current_arguments(sp_current)
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
    sp_backfill.add_argument("--source", choices=("claude", "pi", "codex", "all"), default="all", help="Conversation source to process (default: all)")
    sp_backfill.add_argument("--pi-session-dir", help="Override Pi session directory")
    sp_backfill.add_argument("--codex-session-dir", help="Override Codex active session directory")
    sp_backfill.add_argument("--codex-archived-dir", help="Override Codex archived session directory")
    sp_backfill.add_argument("--project", help="Only process sessions for this project name")
    sp_backfill.add_argument("--session", help="Only process this specific session ID")
    sp_backfill.add_argument("--with-summary", action="store_true",
                             help="Also regenerate LLM summaries and headlines (slower; may use network/local LLM)")
    sp_backfill.add_argument("--no-summary", action="store_true", help=argparse.SUPPRESS)
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

    # footprint
    sp_footprint = subparsers.add_parser(
        "footprint",
        help="Audit generated artifact disk usage and prune eligibility",
        description="Audit Session Index generated artifacts without deleting anything.",
    )
    add_footprint_arguments(sp_footprint)
    sp_footprint.set_defaults(func=cmd_footprint)

    # prune
    sp_prune = subparsers.add_parser(
        "prune",
        help="Audit-first deletion for explicitly confirmed low-value sessions",
        description=(
            "Dry-run by default. Deletes only explicit session IDs classified as low-value "
            "and only when --confirm is supplied. Source JSONL is never deleted."
        ),
    )
    add_prune_arguments(sp_prune)
    sp_prune.set_defaults(func=cmd_prune)

    # status
    sp_status = subparsers.add_parser("status", help="Index statistics and integrity check")
    sp_status.add_argument("--fix", action="store_true", help="Repair dangling paths and orphaned files")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
