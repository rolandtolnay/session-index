"""Shared session indexing pipeline for Claude Code and Pi."""

from __future__ import annotations

from dataclasses import dataclass

from parser import ParsedSession, clean_user_messages, parse_jsonl as parse_claude_jsonl
from pi_parser import parse_pi_jsonl, discover_pi_subagents, parse_pi_subagent_jsonl
from subagent_parser import discover_subagents, parse_subagent_jsonl, ParsedSubagent, SubagentInfo


@dataclass
class IndexResult:
    session_id: str = ""
    user_message_count: int = 0
    assistant_message_count: int = 0
    files_touched: int = 0
    subagents: int = 0
    summary_generated: bool = False
    transcript_path: str | None = None
    skipped_reason: str = ""


SUPPORTED_SOURCES = {"claude", "pi"}


def normalize_source(source: str) -> str:
    source = (source or "claude").lower()
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported session source: {source}")
    return source


def parse_session_file(source: str, path: str) -> ParsedSession:
    source = normalize_source(source)
    if source == "pi":
        return parse_pi_jsonl(path)
    return parse_claude_jsonl(path)


def discover_session_subagents(source: str, path: str) -> list[SubagentInfo]:
    source = normalize_source(source)
    if source == "pi":
        return discover_pi_subagents(path)
    return discover_subagents(path)


def parse_session_subagent(source: str, info: SubagentInfo) -> ParsedSubagent:
    source = normalize_source(source)
    if source == "pi":
        return parse_pi_subagent_jsonl(info.jsonl_path, info.agent_id, info.agent_type)
    return parse_subagent_jsonl(info.jsonl_path, info.meta_path)


def upsert_parsed_session(
    conn,
    session: ParsedSession,
    *,
    source: str,
    source_path: str,
    files_touched: list[str] | None = None,
    summary: str | None = None,
    transcript_path: str | None = None,
    subagent_transcripts: list[str] | None = None,
) -> None:
    from db import upsert_session

    source = normalize_source(source)
    native_session_id = session.session_id.split(":", 1)[1] if source == "pi" and session.session_id.startswith("pi:") else session.session_id
    effective_files = files_touched if files_touched is not None else session.files_touched

    upsert_session(
        conn,
        session_id=session.session_id,
        source=source,
        native_session_id=native_session_id or session.session_id,
        source_path=source_path,
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
        files_touched=", ".join(effective_files) if effective_files else None,
        tools_used=session.tools_used or None,
        summary=summary,
        transcript_path=transcript_path,
        subagent_transcripts=", ".join(subagent_transcripts) if subagent_transcripts else None,
    )


def index_fast(source: str, path: str) -> IndexResult:
    """Parse and upsert deterministic fields only."""
    from db import get_connection, init_db

    session = parse_session_file(source, path)
    result = IndexResult(
        session_id=session.session_id,
        user_message_count=session.user_message_count,
        assistant_message_count=session.assistant_message_count,
        files_touched=len(session.files_touched),
    )

    if session.user_message_count < 1 or session.assistant_message_count < 1:
        result.skipped_reason = f"{session.user_message_count} user, {session.assistant_message_count} assistant msgs"
        return result

    conn = get_connection()
    init_db(conn)
    upsert_parsed_session(conn, session, source=source, source_path=path)
    conn.close()
    return result


def index_full(source: str, path: str) -> IndexResult:
    """Parse, summarize, write transcripts, and upsert a complete row."""
    from db import get_connection, init_db
    from summarizer import summarize
    from transcript import SubagentRef, write_subagent_transcript, write_transcript

    session = parse_session_file(source, path)
    result = IndexResult(
        session_id=session.session_id,
        user_message_count=session.user_message_count,
        assistant_message_count=session.assistant_message_count,
        files_touched=len(session.files_touched),
    )

    if session.user_message_count < 1 or session.assistant_message_count < 1:
        result.skipped_reason = f"{session.user_message_count} user, {session.assistant_message_count} assistant msgs"
        return result

    parsed_subagents: list[ParsedSubagent] = []
    for info in discover_session_subagents(source, path):
        parsed = parse_session_subagent(source, info)
        if parsed.messages:
            parsed_subagents.append(parsed)

    all_files = set(session.files_touched)
    for sub in parsed_subagents:
        all_files.update(sub.files_touched)
    enriched_files = sorted(all_files)

    short_session_threshold = 5
    last_assistant = None
    if session.user_message_count <= short_session_threshold and session.assistant_messages:
        last_assistant = session.assistant_messages[-1]

    summary = summarize(
        project=session.project,
        branch=session.branch,
        user_messages=clean_user_messages(session.user_messages),
        files_touched=session.files_touched,
        last_assistant_message=last_assistant,
    )
    result.summary_generated = bool(summary)

    transcript_path = None
    if session.messages:
        subagent_refs = [
            SubagentRef(agent_type=sub.agent_type, agent_id=sub.agent_id)
            for sub in parsed_subagents
        ]
        transcript_path = write_transcript(
            session.session_id,
            session.messages,
            project=session.project,
            branch=session.branch,
            timestamp=session.started_at,
            subagents=subagent_refs or None,
        )
        result.transcript_path = transcript_path

    subagent_paths: list[str] = []
    for sub in parsed_subagents:
        subagent_paths.append(write_subagent_transcript(session.session_id, sub))
    result.subagents = len(subagent_paths)
    result.files_touched = len(enriched_files)

    conn = get_connection()
    init_db(conn)
    upsert_parsed_session(
        conn,
        session,
        source=source,
        source_path=path,
        files_touched=enriched_files,
        summary=summary,
        transcript_path=transcript_path,
        subagent_transcripts=subagent_paths,
    )
    conn.close()
    return result
