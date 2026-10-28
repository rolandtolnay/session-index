"""Recognise Claude Design children without consulting their Pi parent."""
from __future__ import annotations

from dataclasses import dataclass
import glob
import json
import os
import re
from uuid import UUID

from parser import ParsedToolCall
from subagent_parser import SubagentInfo

# Keep in sync with claude-design/prompts.ts: DELEGATION_HEADER_PATTERN.
DELEGATION_HEADER_PATTERN = re.compile(r"^\[claude-design v(\d+)\] run (\S+) · parent pi session (\S+)$")


@dataclass(frozen=True)
class DesignChild:
    parent_native_id: str
    session_id: str


def probe_design_child(path: str) -> DesignChild | None:
    """Inspect only through the first user message, including text-block encoding."""
    try:
        with open(path) as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") not in {"user", "assistant"}:
                    continue
                if entry.get("entrypoint") != "sdk-cli":
                    return None
                if entry.get("type") != "user":
                    continue
                message = entry.get("message")
                content = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    content = "\n".join(block["text"] for block in content
                                        if isinstance(block, dict) and block.get("type") == "text"
                                        and isinstance(block.get("text"), str))
                if not isinstance(content, str) or not content:
                    return None
                match = DELEGATION_HEADER_PATTERN.fullmatch(content.splitlines()[0])
                session_id = entry.get("sessionId")
                if match and isinstance(session_id, str) and session_id:
                    # Claude supplies a UUID; it also becomes a filename component.
                    try:
                        UUID(session_id)
                    except ValueError:
                        return None
                    return DesignChild(match[3], session_id)
                return None
    except OSError:
        pass
    return None


def discover_design_children(parent_native_id: str) -> list[SubagentInfo]:
    """Claude sources remain in place, even when the designer used another cwd."""
    from sources import _claude_source_score

    by_identity: dict[str, list[tuple[str, DesignChild]]] = {}
    pattern = os.path.expanduser("~/.claude/projects/*/*.jsonl")
    for path in sorted(glob.glob(pattern)):
        child = probe_design_child(path)
        if child and child.parent_native_id == parent_native_id:
            by_identity.setdefault(child.session_id, []).append((path, child))
    infos = []
    for candidates in by_identity.values():
        path, child = (candidates[0] if len(candidates) == 1
                       else max(candidates, key=lambda candidate: _claude_source_score(candidate[0])))
        infos.append(SubagentInfo(path, None, child.session_id, "claude-design"))
    return infos


# claude-design/runs.ts resultDetails and completion-message details contract.
DESIGN_SESSION_KEY = "claudeSessionId"


def design_identity(call: ParsedToolCall) -> str:
    identity = call.result_details.get(DESIGN_SESSION_KEY) or call.arguments.get("session", "")
    return identity if isinstance(identity, str) else ""


def apply_design_completions(tool_calls: list[ParsedToolCall], branch: list[dict]) -> None:
    """Attach wait/completion state to its invocation, not all resumes of a session."""
    results = {}
    for entry in branch:
        record = None
        if entry.get("type") == "custom_message" and entry.get("customType") == "claude-design-completion":
            record = entry.get("details")
        elif entry.get("type") == "message":
            msg = entry.get("message")
            if isinstance(msg, dict) and msg.get("role") == "toolResult" and str(msg.get("toolName", "")).rsplit(".", 1)[-1] in {"claude_design", "claude_design_wait"}:
                record = msg.get("details")
        if isinstance(record, dict) and isinstance(record.get("runId"), str):
            results[record["runId"]] = record
    for call in tool_calls:
        if call.tool_name.rsplit(".", 1)[-1] == "claude_design":
            record = results.get(call.result_details.get("runId"))
            if record and record.get(DESIGN_SESSION_KEY) == design_identity(call):
                call.result_details = record


def parent_source_paths(native_id: str) -> list[str]:
    """Locate exact Pi origins, including registered worktree contributors."""
    import db
    from sources import discover_pi_sessions

    paths = set()
    if os.path.isfile(db.DB_PATH):
        conn = db.get_connection()
        try:
            db.init_db(conn)
            paths.update(row[0] for row in conn.execute(
                "SELECT source_path FROM sessions WHERE source='pi' AND native_session_id=? "
                "UNION SELECT source_path FROM pi_continuation_sources WHERE native_session_id=?",
                (native_id, native_id),
            ))
        finally:
            conn.close()
    if not paths:
        for candidate in discover_pi_sessions(native_id):
            try:
                with open(candidate.path) as handle:
                    header = json.loads(next(handle))
                if header.get("type") == "session" and header.get("id") == native_id:
                    paths.add(candidate.path)
            except (OSError, ValueError, StopIteration):
                continue
    from pi_continuations import refresh_paths
    return sorted({target for path in paths for target in refresh_paths(path) if os.path.isfile(target)})
