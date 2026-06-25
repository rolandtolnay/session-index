"""Shared staged session indexing pipeline for Claude Code, Pi, and Codex."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from codex_parser import parse_codex_jsonl
from parser import ParsedSession, ParsedToolCall, clean_user_messages, parse_jsonl as parse_claude_jsonl
from pi_parser import parse_pi_jsonl, discover_pi_subagents, parse_pi_subagent_jsonl
from subagent_parser import discover_subagents, parse_subagent_jsonl, ParsedSubagent, SubagentInfo
from subagent_runs import ParsedSubagentRun, build_subagent_runs


class IndexStage(str, Enum):
    SESSION_METADATA = "session_metadata"
    SUMMARY = "summary"
    CLEAN_TRANSCRIPT = "clean_transcript"
    SUBAGENT_TRANSCRIPTS = "subagent_transcripts"
    TOOL_LOG = "tool_log"


@dataclass(frozen=True)
class IndexOptions:
    stages: frozenset[IndexStage]


FAST_INDEX_OPTIONS = IndexOptions(frozenset({IndexStage.SESSION_METADATA}))
FULL_INDEX_OPTIONS = IndexOptions(frozenset(IndexStage))
# Everything except the expensive, non-deterministic LLM summary: the complete
# deterministic pass (metadata, clean transcript, subagent transcripts, tool log,
# and the structured fact tables that ride along with the tool-log stage).
NO_SUMMARY_INDEX_OPTIONS = IndexOptions(frozenset(IndexStage) - {IndexStage.SUMMARY})


@dataclass
class IndexResult:
    session_id: str = ""
    user_message_count: int = 0
    assistant_message_count: int = 0
    files_touched: int = 0
    subagents: int = 0
    subagent_runs: int = 0
    summary_generated: bool = False
    transcript_path: str | None = None
    tool_log_path: str | None = None
    skipped_reason: str = ""
    stages: frozenset[IndexStage] = field(default_factory=frozenset)


SUPPORTED_SOURCES = {"claude", "pi", "codex"}

_METADATA_FIELDS = {
    "source",
    "native_session_id",
    "source_path",
    "slug",
    "project_path",
    "project",
    "branch",
    "model",
    "started_at",
    "ended_at",
    "duration_seconds",
    "user_message_count",
    "user_messages",
    "files_touched",
    "tools_used",
    "parent_session_path",
    "parent_native_session_id",
}

_STAGE_FIELDS = {
    IndexStage.SESSION_METADATA: _METADATA_FIELDS,
    IndexStage.SUMMARY: {"summary"},
    IndexStage.CLEAN_TRANSCRIPT: {"transcript_path"},
    IndexStage.SUBAGENT_TRANSCRIPTS: {"subagent_transcripts"},
    IndexStage.TOOL_LOG: {"tool_log_path"},
}


def normalize_source(source: str) -> str:
    source = (source or "claude").lower()
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported session source: {source}")
    return source


def parse_session_file(source: str, path: str) -> ParsedSession:
    source = normalize_source(source)
    if source == "pi":
        return parse_pi_jsonl(path)
    if source == "codex":
        return parse_codex_jsonl(path)
    return parse_claude_jsonl(path)


def discover_session_subagents(source: str, path: str) -> list[SubagentInfo]:
    source = normalize_source(source)
    if source == "pi":
        return discover_pi_subagents(path)
    if source == "codex":
        return []
    return discover_subagents(path)


def parse_session_subagent(source: str, info: SubagentInfo) -> ParsedSubagent:
    source = normalize_source(source)
    if source == "pi":
        return parse_pi_subagent_jsonl(info.jsonl_path, info.agent_id, info.agent_type)
    if source == "codex":
        return ParsedSubagent(agent_id=info.agent_id, agent_type=info.agent_type, source_path=info.jsonl_path)
    return parse_subagent_jsonl(info.jsonl_path, info.meta_path)


def _stage_overwrite_fields(stages: frozenset[IndexStage]) -> set[str]:
    fields: set[str] = set()
    for stage in stages:
        fields.update(_STAGE_FIELDS[stage])
    return fields


def upsert_parsed_session(
    conn,
    session: ParsedSession,
    *,
    source: str,
    source_path: str,
    files_touched: list[str] | None = None,
    summary: str | None = None,
    transcript_path: str | None = None,
    tool_log_path: str | None = None,
    subagent_transcripts: list[str] | None = None,
    stage_overwrite_fields: set[str] | None = None,
    commit: bool = True,
) -> None:
    from db import upsert_session

    source = normalize_source(source)
    source_prefix = f"{source}:"
    native_session_id = (
        session.session_id.split(":", 1)[1]
        if session.session_id.startswith(source_prefix)
        else session.session_id
    )
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
        tool_log_path=tool_log_path,
        subagent_transcripts=", ".join(subagent_transcripts) if subagent_transcripts else None,
        parent_session_path=session.parent_session_path or None,
        parent_native_session_id=session.parent_native_session_id or None,
        overwrite_fields=stage_overwrite_fields,
        commit=commit,
    )


def _parse_subagents_for_stages(source: str, path: str, stages: frozenset[IndexStage]) -> list[ParsedSubagent]:
    needs_subagents = bool(stages & {
        IndexStage.SUMMARY,
        IndexStage.CLEAN_TRANSCRIPT,
        IndexStage.SUBAGENT_TRANSCRIPTS,
        IndexStage.TOOL_LOG,
    })
    if not needs_subagents:
        return []

    parsed_subagents: list[ParsedSubagent] = []
    for info in discover_session_subagents(source, path):
        parsed = parse_session_subagent(source, info)
        if parsed.messages:
            parsed_subagents.append(parsed)
    return parsed_subagents


def _subagent_refs(parsed_subagents: list[ParsedSubagent]):
    from transcript import SubagentRef

    return [
        SubagentRef(agent_type=sub.agent_type, agent_id=sub.agent_id)
        for sub in parsed_subagents
    ]


def _enriched_files(session: ParsedSession, parsed_subagents: list[ParsedSubagent]) -> list[str]:
    all_files = set(session.files_touched)
    for sub in parsed_subagents:
        all_files.update(sub.files_touched)
    return sorted(all_files)


def _summarize_session(session: ParsedSession, enriched_files: list[str], parsed_subagents: list[ParsedSubagent]) -> str | None:
    from summarizer import summarize
    from transcript import render_transcript

    subagent_refs = _subagent_refs(parsed_subagents)
    transcript_text = None
    if session.messages:
        transcript_text = render_transcript(
            session.messages,
            project=session.project,
            branch=session.branch,
            timestamp=session.started_at,
            subagents=subagent_refs or None,
        )

    short_session_threshold = 5
    last_assistant = None
    if session.user_message_count <= short_session_threshold and session.assistant_messages:
        last_assistant = session.assistant_messages[-1]

    return summarize(
        project=session.project,
        branch=session.branch,
        user_messages=clean_user_messages(session.user_messages),
        files_touched=enriched_files,
        last_assistant_message=last_assistant,
        transcript_text=transcript_text,
    )


def _write_clean_transcript(session: ParsedSession, parsed_subagents: list[ParsedSubagent]) -> str | None:
    from transcript import write_transcript

    if not session.messages:
        return None
    return write_transcript(
        session.session_id,
        session.messages,
        project=session.project,
        branch=session.branch,
        timestamp=session.started_at,
        subagents=_subagent_refs(parsed_subagents) or None,
    )


def _write_subagent_transcripts(session: ParsedSession, parsed_subagents: list[ParsedSubagent]) -> list[str]:
    from transcript import write_subagent_transcript

    paths: list[str] = []
    for sub in parsed_subagents:
        path = write_subagent_transcript(session.session_id, sub)
        sub.transcript_path = path
        sub.artifact_path = path
        paths.append(path)
    return paths


def _write_tool_log(session: ParsedSession, combined_tool_calls: list[ParsedToolCall], source: str) -> str | None:
    from tool_log import write_tool_log

    try:
        return write_tool_log(
            session.session_id,
            combined_tool_calls,
            project=session.project,
            source=source,
            started_at=session.started_at,
        )
    except OSError:
        return None


def normalize_subagent_runs(
    session: ParsedSession,
    *,
    source: str,
    parsed_subagents: list[ParsedSubagent] | None = None,
) -> list[ParsedSubagentRun]:
    return build_subagent_runs(
        parent_session_id=session.session_id,
        source=normalize_source(source),
        tool_calls=session.tool_calls,
        subagents=parsed_subagents or [],
    )


def index_source_transcript(
    source: str,
    path: str,
    options: IndexOptions,
    *,
    parsed_session: ParsedSession | None = None,
) -> IndexResult:
    """Index one provider-owned Source Transcript using explicit stage ownership."""
    from db import get_connection, init_db

    source = normalize_source(source)
    stages = frozenset(options.stages)
    session = parsed_session or parse_session_file(source, path)
    result = IndexResult(
        session_id=session.session_id,
        user_message_count=session.user_message_count,
        assistant_message_count=session.assistant_message_count,
        files_touched=len(session.files_touched),
        stages=stages,
    )

    if session.user_message_count < 1 or session.assistant_message_count < 1:
        result.skipped_reason = f"{session.user_message_count} user, {session.assistant_message_count} assistant msgs"
        return result

    parsed_subagents = _parse_subagents_for_stages(source, path, stages)

    enriched_files = session.files_touched
    if stages & {IndexStage.SUMMARY, IndexStage.SUBAGENT_TRANSCRIPTS}:
        enriched_files = _enriched_files(session, parsed_subagents)
        result.files_touched = len(enriched_files)

    summary = None
    if IndexStage.SUMMARY in stages:
        summary = _summarize_session(session, enriched_files, parsed_subagents)
        result.summary_generated = bool(summary)

    transcript_path = None
    if IndexStage.CLEAN_TRANSCRIPT in stages:
        transcript_path = _write_clean_transcript(session, parsed_subagents)
        result.transcript_path = transcript_path

    subagent_paths: list[str] = []
    if IndexStage.SUBAGENT_TRANSCRIPTS in stages:
        subagent_paths = _write_subagent_transcripts(session, parsed_subagents)
        result.subagents = len(subagent_paths)

    combined_tool_calls: list[ParsedToolCall] = []
    tool_log_path = None
    if IndexStage.TOOL_LOG in stages:
        from tool_events import combine_tool_calls

        combined_tool_calls = combine_tool_calls(session.tool_calls, parsed_subagents)
        tool_log_path = _write_tool_log(session, combined_tool_calls, source)
        result.tool_log_path = tool_log_path

    subagent_runs = normalize_subagent_runs(session, source=source, parsed_subagents=parsed_subagents)
    result.subagent_runs = len(subagent_runs)

    stage_overwrite_fields = _stage_overwrite_fields(stages)
    if IndexStage.SUMMARY in stages and summary is None:
        # summarizer.summarize() returns None on provider/runtime failure; preserve
        # existing searchable summaries when the stage produced no replacement.
        stage_overwrite_fields.discard("summary")

    conn = get_connection()
    try:
        init_db(conn)
        upsert_parsed_session(
            conn,
            session,
            source=source,
            source_path=path,
            files_touched=enriched_files,
            summary=summary,
            transcript_path=transcript_path,
            tool_log_path=tool_log_path,
            subagent_transcripts=subagent_paths,
            stage_overwrite_fields=stage_overwrite_fields,
            commit=False,
        )
        _persist_facts(conn, session, source, stages, subagent_runs, combined_tool_calls)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def _persist_facts(
    conn,
    session: ParsedSession,
    source: str,
    stages: frozenset[IndexStage],
    subagent_runs: list[ParsedSubagentRun],
    combined_tool_calls: list[ParsedToolCall],
) -> None:
    """Persist the structured fact tables. Coverage tracks the tool-log stage;
    idempotent via delete-then-insert. No FTS interaction."""
    from db import replace_file_mutations, replace_question_answers, replace_skill_invocations, replace_subagent_runs, replace_tool_calls
    from skill_facts import build_skill_invocation_rows
    from tool_facts import build_file_mutation_rows, build_question_rows, build_subagent_run_rows, build_tool_call_rows

    if IndexStage.TOOL_LOG in stages:
        session_id = session.session_id
        fact_builders = (
            (replace_tool_calls, build_tool_call_rows),
            (replace_question_answers, build_question_rows),
            (replace_file_mutations, build_file_mutation_rows),
        )
        for replace_rows, build_rows in fact_builders:
            replace_rows(conn, session_id, build_rows(session_id, source, combined_tool_calls), commit=False)
        replace_skill_invocations(
            conn,
            session_id,
            build_skill_invocation_rows(session_id, source, session.messages, combined_tool_calls, subagent_runs),
            commit=False,
        )

    if stages & {IndexStage.TOOL_LOG, IndexStage.SUBAGENT_TRANSCRIPTS}:
        replace_subagent_runs(conn, session.session_id, build_subagent_run_rows(subagent_runs), commit=False)


def index_fast(source: str, path: str) -> IndexResult:
    """Parse and upsert deterministic fields only."""
    return index_source_transcript(source, path, FAST_INDEX_OPTIONS)


def index_full(source: str, path: str) -> IndexResult:
    """Parse, summarize, write transcripts, and upsert a complete row."""
    return index_source_transcript(source, path, FULL_INDEX_OPTIONS)
