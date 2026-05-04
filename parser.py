"""JSONL conversation parser.

Parses Claude Code JSONL logs into structured ParsedSession data.
Handles user messages (plain string, text arrays, tool_results),
assistant messages (text blocks, tool_use, thinking blocks),
and extracts metadata (session_id, slug, project, branch, model, etc.).
"""

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


@dataclass
class ParsedSession:
    session_id: str = ""
    slug: str = ""
    project_path: str = ""
    project: str = ""  # basename of project_path
    branch: str = ""
    model: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: int = 0
    user_messages: list[str] = field(default_factory=list)
    assistant_messages: list[str] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)  # [{"role": ..., "content": ...}]
    files_touched: list[str] = field(default_factory=list)
    tools_used: str = ""
    user_message_count: int = 0
    assistant_message_count: int = 0
    parent_session_path: str = ""
    parent_native_session_id: str = ""


def _git_root(cwd: str) -> str:
    """Derive git root from cwd. Returns cwd if git fails."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            # Group worktree sessions under the parent project
            wt = "/.claude-worktrees/"
            idx = root.find(wt)
            if idx != -1:
                root = root[:idx]
            return root
    except Exception:
        pass
    return cwd


def _format_tool_use(tool: dict[str, Any]) -> str:
    """Format a tool_use block as a bracket string."""
    name = tool.get("name", "Unknown")
    inp = tool.get("input", {})

    if name in ("Read", "Edit", "Write"):
        path = inp.get("file_path", "")
        return f"[{name} {path}]"
    elif name == "Bash":
        cmd = inp.get("command", "")
        return f"[Bash: {cmd}]"
    elif name in ("Grep", "Glob"):
        pattern = inp.get("pattern", "")
        return f"[{name}: {pattern}]"
    elif name == "Agent":
        desc = inp.get("description", "")
        return f"[Agent: {desc}]"
    else:
        return f"[{name}]"


def _format_bash_result(result_text: str, is_error: bool) -> str:
    """Format Bash tool result for transcript."""
    if not result_text:
        return ""
    lines = result_text.splitlines()
    if is_error:
        # Show full output for errors, capped at 30 lines
        if len(lines) > 30:
            lines = lines[:30]
            lines.append("... (truncated)")
        return "\n".join(lines)
    elif len(lines) <= 5:
        return result_text
    else:
        # First 2 + ... + last 3
        shown = lines[:2] + ["..."] + lines[-3:]
        return "\n".join(shown)


def _extract_user_text(content: Any) -> str:
    """Extract displayable text from user message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    # Skip tool results in user text extraction
                    continue
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _is_only_tool_results(content: Any) -> bool:
    """Check if user content is exclusively tool_result items."""
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, dict):
            if item.get("type") != "tool_result":
                return False
        elif isinstance(item, str) and item.strip():
            return False
    return True


def parse_jsonl(path: str) -> ParsedSession:
    """Parse a JSONL conversation file into a ParsedSession."""
    session = ParsedSession()
    entries: list[dict] = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not entries:
        return session

    # Build tool_use_id -> tool_result mapping for Bash results
    tool_results: dict[str, dict] = {}
    files_set: set[str] = set()
    tool_counter: Counter = Counter()
    timestamps: list[str] = []

    # Prefer descriptive slug emitted after plan acceptance (custom-title / agent-name
    # entries) over the generic three-word slug on user/assistant entries. Guard against
    # Claude Code occasionally capturing raw slash-command text as the custom title.
    for entry in entries:
        etype = entry.get("type", "")
        candidate = ""
        if etype == "custom-title":
            candidate = entry.get("customTitle", "")
        elif etype == "agent-name":
            candidate = entry.get("agentName", "")
        if candidate and _SLUG_RE.match(candidate):
            session.slug = candidate
            break

    # First pass: collect tool results and metadata
    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type not in ("user", "assistant"):
            continue

        ts = entry.get("timestamp", "")
        if ts:
            timestamps.append(ts)

        # Extract metadata
        if not session.session_id:
            session.session_id = entry.get("sessionId", "")
        if not session.slug and entry.get("slug"):
            session.slug = entry["slug"]
        if not session.branch and entry.get("gitBranch"):
            session.branch = entry["gitBranch"]
        if not session.project_path and entry.get("cwd"):
            session.project_path = _git_root(entry["cwd"])
            session.project = os.path.basename(session.project_path)

        msg = entry.get("message", {})

        # Extract model from assistant
        if entry_type == "assistant" and not session.model:
            model = msg.get("model", "")
            if model:
                session.model = model

        content = msg.get("content", "")

        # Collect tool results from user entries
        if entry_type == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tuid = item.get("tool_use_id", "")
                    if tuid:
                        result_content = item.get("content", "")
                        if isinstance(result_content, list):
                            # Extract text from content array
                            texts = [c.get("text", "") for c in result_content
                                     if isinstance(c, dict) and c.get("type") == "text"]
                            result_content = "\n".join(texts)
                        tool_results[tuid] = {
                            "content": result_content,
                            "is_error": item.get("is_error", False),
                        }

        # Collect tool_use info from assistant entries
        if entry_type == "assistant" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = item.get("name", "")
                    tool_counter[name] += 1
                    inp = item.get("input", {})
                    # Track files
                    if name in ("Read", "Edit", "Write"):
                        fp = inp.get("file_path", "")
                        if fp:
                            files_set.add(fp)

    # Session ID fallback from entries with sessionId field
    if not session.session_id:
        for entry in entries:
            sid = entry.get("sessionId", "")
            if sid:
                session.session_id = sid
                break

    # Timestamps
    if timestamps:
        session.started_at = timestamps[0]
        session.ended_at = timestamps[-1]
        try:
            from datetime import datetime, timezone
            t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            session.duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            pass

    # Files and tools
    session.files_touched = sorted(files_set)
    if tool_counter:
        session.tools_used = ", ".join(
            f"{name}:{count}" for name, count in tool_counter.most_common()
        )

    # Second pass: build messages
    pending_tool_uses: list[dict] = []  # tool_use blocks from current assistant turn

    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type not in ("user", "assistant"):
            continue

        msg = entry.get("message", {})
        content = msg.get("content", "")

        if entry_type == "user":
            # Skip system-injected meta messages (skill expansions, commit context, etc.)
            if entry.get("isMeta", False):
                continue
            # Skip entries that are only tool results
            if _is_only_tool_results(content):
                # Append error bash results to last assistant message
                for tu in pending_tool_uses:
                    if tu.get("name") == "Bash":
                        tuid = tu.get("id", "")
                        tr = tool_results.get(tuid, {})
                        if tr.get("is_error", False):
                            result_text = _format_bash_result(
                                tr.get("content", ""), is_error=True
                            )
                            if result_text and session.messages and session.messages[-1]["role"] == "assistant":
                                session.messages[-1]["content"] += f"\n{result_text}"
                pending_tool_uses = []
                continue

            raw_text = _extract_user_text(content)
            cmd = _extract_command(raw_text)
            cleaned = cmd if cmd else _clean_text(raw_text)
            if cleaned:
                session.user_messages.append(cleaned)
                session.messages.append({"role": "user", "content": cleaned, "timestamp": entry.get("timestamp", "")})

        elif entry_type == "assistant":
            parts = []
            pending_tool_uses = []

            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type == "text":
                        text = _clean_text(item.get("text", ""))
                        text = _strip_narration(text)
                        if text:
                            parts.append(text)
                    elif item_type == "tool_use":
                        pending_tool_uses.append(item)
                        # Inject subagent reference marker
                        if item.get("name") == "Agent":
                            inp = item.get("input", {})
                            agent_type = inp.get("subagent_type", "agent")
                            desc = inp.get("description", "")
                            parts.append(f"__SUBAGENT:{agent_type}:{desc}__")
                    # Skip thinking blocks
            elif isinstance(content, str) and content.strip():
                text = _clean_text(content)
                text = _strip_narration(text)
                if text:
                    parts.append(text)

            if parts:
                combined = "\n\n".join(parts)
                if not combined.strip():
                    continue
                session.assistant_messages.append(combined)
                # Merge with previous assistant message if consecutive
                if session.messages and session.messages[-1]["role"] == "assistant":
                    session.messages[-1]["content"] += "\n\n" + combined
                else:
                    session.messages.append({"role": "assistant", "content": combined, "timestamp": entry.get("timestamp", "")})

    session.user_message_count = len(session.user_messages)
    session.assistant_message_count = len(session.assistant_messages)
    return session


# ── User message cleaning for summarizer ─────────────────────────────────────

# XML tags that wrap system-injected noise in user messages
_NOISE_TAG_NAMES = (
    "local-command-caveat|local-command-stdout|"
    "command-name|command-message|command-args|"
    "task-notification|system-reminder"
)
_NOISE_TAGS = re.compile(
    rf"<(?:{_NOISE_TAG_NAMES})[^>]*>.*?</(?:{_NOISE_TAG_NAMES})>",
    re.DOTALL,
)

# ANSI terminal escape codes
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')


def _clean_text(text: str) -> str:
    """Strip system-injected XML noise and ANSI codes from text."""
    text = _NOISE_TAGS.sub("", text)
    text = _ANSI_ESCAPE.sub("", text)
    return text.strip()


# Command invocation extraction
_CMD_NAME_RE = re.compile(r"<command-name>\s*/?([\w:_-]+)\s*</command-name>")
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _extract_command(text: str) -> str | None:
    """Extract command invocation as '[/name] args' or None if not a command."""
    m = _CMD_NAME_RE.search(text)
    if not m:
        return None
    cmd = m.group(1)
    if f"/{cmd}" in _NOISE_COMMANDS:
        return None
    args_m = _CMD_ARGS_RE.search(text)
    args = args_m.group(1).strip() if args_m else ""
    if args:
        return f"[/{cmd}] {args}"
    return f"[/{cmd}]"


# Narration-only assistant messages (short single-sentence preambles before tool calls)
_NARRATION_RE = re.compile(
    r"^(?:Let me |Let's |Now I'll |Now let me |Now I need to |I need to |I'll |I will )",
    re.IGNORECASE,
)


def _strip_narration(text: str) -> str:
    """Drop short single-sentence narration-only messages. Return text unchanged otherwise."""
    if len(text) > 150:
        return text
    if re.search(r'\.\s+\S', text):
        return text  # multi-sentence — likely has substance after the narration
    if _NARRATION_RE.match(text):
        return ""
    return text

# Commands that are pure navigation/lifecycle — never contain useful content
_NOISE_COMMANDS = {"/clear", "/exit", "/compact", "/resume", "/init", "/login",
                   "/logout", "/status", "/config", "/help", "/model", "/cost",
                   "/memory", "/doctor", "/bug", "/terminal-setup", "/listen",
                   "/mcp", "/permissions", "/approved-tools"}


def clean_user_messages(messages: list[str]) -> list[str]:
    """Strip system-injected noise from user messages for the summarizer.

    Removes XML wrapper tags (command-name, local-command-stdout, etc.)
    and drops messages that consist entirely of noise commands.
    Preserves messages that contain real user text alongside commands.
    """
    cleaned = []
    for msg in messages:
        # Strip all noise XML tags
        text = _NOISE_TAGS.sub("", msg).strip()
        if not text:
            continue
        # Drop if what remains is just a noise command name
        if text.strip().rstrip("</> \n\t") in _NOISE_COMMANDS:
            continue
        cleaned.append(text)
    return cleaned
