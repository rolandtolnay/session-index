"""Subagent JSONL parser.

Discovers and parses subagent JSONL files associated with a parent session.
Unlike the parent parser, subagent parsing preserves narration (it's the reasoning)
and keeps tool call signatures as compact `→ Tool: arg` lines.
"""

import glob
import json
import os
from collections import Counter
from dataclasses import dataclass, field

from parser import ParsedToolCall, _clean_text, _NOISE_TAGS, _ANSI_ESCAPE


@dataclass
class ParsedSubagent:
    agent_id: str = ""
    agent_type: str = ""
    parent_session_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: int = 0
    files_touched: list[str] = field(default_factory=list)
    tools_used: str = ""
    tool_call_count: int = 0
    messages: list[dict] = field(default_factory=list)
    initial_prompt: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)


@dataclass
class SubagentInfo:
    jsonl_path: str
    meta_path: str | None
    agent_id: str
    agent_type: str


# Agent ID prefixes for system agents that have no indexing value
_SKIP_PREFIXES = ("acompact-", "aprompt_suggestion-")


def discover_subagents(jsonl_path: str) -> list[SubagentInfo]:
    """Find subagent JSONL files associated with a parent session.

    Looks in {dirname}/{session_id}/subagents/agent-*.jsonl.
    Skips system agents (acompact-*, aprompt_suggestion-*).
    """
    session_id = os.path.splitext(os.path.basename(jsonl_path))[0]
    subagents_dir = os.path.join(os.path.dirname(jsonl_path), session_id, "subagents")

    if not os.path.isdir(subagents_dir):
        return []

    pattern = os.path.join(subagents_dir, "agent-*.jsonl")
    results = []

    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        # Extract agent ID: "agent-abc123.jsonl" -> "abc123"
        agent_id = fname.replace("agent-", "").replace(".jsonl", "")

        # Skip system agents (agent_id is "acompact-xxx" after stripping "agent-")
        if any(agent_id.startswith(p) for p in _SKIP_PREFIXES):
            continue

        # Check for .meta.json
        meta_path = path.replace(".jsonl", ".meta.json")
        if not os.path.exists(meta_path):
            meta_path = None

        agent_type = "unknown"
        if meta_path:
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                agent_type = meta.get("agentType", "unknown")
            except (json.JSONDecodeError, OSError):
                pass

        results.append(SubagentInfo(
            jsonl_path=path,
            meta_path=meta_path,
            agent_id=agent_id,
            agent_type=agent_type,
        ))

    return results


def _format_tool_signature(item: dict) -> str:
    """Format a tool_use block as a compact arrow signature."""
    name = item.get("name", "Unknown")
    inp = item.get("input", {})

    if name in ("Read", "Edit", "Write"):
        path = inp.get("file_path", "")
        return f"\u2192 {name}: {path}" if path else f"\u2192 {name}"
    elif name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 120:
            cmd = cmd[:120] + "\u2026"
        return f"\u2192 Bash: {cmd}" if cmd else f"\u2192 Bash"
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        if path:
            return f"\u2192 Grep: {pattern} in {path}"
        return f"\u2192 Grep: {pattern}"
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        return f"\u2192 Glob: {pattern}" if pattern else f"\u2192 Glob"
    elif name == "Agent":
        desc = inp.get("description", "")
        return f"\u2192 Agent: {desc}" if desc else f"\u2192 Agent"
    else:
        return f"\u2192 {name}"


def parse_subagent_jsonl(jsonl_path: str, meta_path: str | None = None) -> ParsedSubagent:
    """Parse a subagent JSONL file into a ParsedSubagent.

    Key differences from parent parse_jsonl():
    - Narration is NOT stripped (it's the agent's reasoning)
    - Tool signatures are kept as compact `→ Tool: arg` lines
    - Consecutive assistant messages are NOT merged (each is a step)
    - Error results are formatted as separate [ERROR] blocks
    """
    result = ParsedSubagent()
    entries: list[dict] = []

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return result

    if not entries:
        return result

    # Read meta
    if meta_path:
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            result.agent_type = meta.get("agentType", "unknown")
        except (json.JSONDecodeError, OSError):
            result.agent_type = "unknown"
    else:
        result.agent_type = "unknown"

    # First pass: collect tool results, files_touched, tools_used, timestamps
    tool_results: dict[str, dict] = {}
    raw_tool_calls: list[ParsedToolCall] = []
    files_set: set[str] = set()
    tool_counter: Counter = Counter()
    timestamps: list[str] = []

    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type not in ("user", "assistant"):
            continue

        ts = entry.get("timestamp", "")
        if ts:
            timestamps.append(ts)

        # Extract IDs from first entry
        if not result.agent_id:
            result.agent_id = entry.get("agentId", "")
        if not result.parent_session_id:
            result.parent_session_id = entry.get("sessionId", "")

        msg = entry.get("message", {})
        content = msg.get("content", "")

        # Collect tool results from user entries
        if entry_type == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tuid = item.get("tool_use_id", "")
                    if tuid:
                        result_content = item.get("content", "")
                        if isinstance(result_content, list):
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
                    if not isinstance(inp, dict):
                        inp = {}
                    raw_tool_calls.append(ParsedToolCall(
                        timestamp=ts,
                        tool_call_id=item.get("id", ""),
                        tool_name=name,
                        arguments=inp,
                    ))
                    if name in ("Read", "Edit", "Write"):
                        fp = inp.get("file_path", "")
                        if fp:
                            files_set.add(fp)

    # Timestamps and duration
    if timestamps:
        result.started_at = timestamps[0]
        result.ended_at = timestamps[-1]
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            result.duration_seconds = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            pass

    result.files_touched = sorted(files_set)
    result.tool_call_count = sum(tool_counter.values())
    if tool_counter:
        result.tools_used = ", ".join(
            f"{name}:{count}" for name, count in tool_counter.most_common()
        )
    result.tool_calls = []
    for call in raw_tool_calls:
        tr = tool_results.get(call.tool_call_id, {})
        result.tool_calls.append(ParsedToolCall(
            scope=call.scope,
            sequence=call.sequence,
            timestamp=call.timestamp,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=call.arguments,
            result=tr.get("content", ""),
            is_error=bool(tr.get("is_error", False)),
        ))

    # Second pass: build messages with subagent cleaning rules
    pending_tool_uses: list[dict] = []
    is_first_user = True

    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type not in ("user", "assistant"):
            continue

        msg = entry.get("message", {})
        content = msg.get("content", "")
        ts = entry.get("timestamp", "")

        if entry_type == "user":
            if entry.get("isMeta", False):
                continue

            # Check if only tool results
            if isinstance(content, list):
                is_only_results = all(
                    isinstance(item, dict) and item.get("type") == "tool_result"
                    for item in content if isinstance(item, dict)
                )
                if is_only_results:
                    # Check for errors in pending tool uses → emit ERROR blocks
                    for tu in pending_tool_uses:
                        tuid = tu.get("id", "")
                        tr = tool_results.get(tuid, {})
                        if tr.get("is_error", False):
                            error_text = tr.get("content", "")
                            if error_text:
                                # Truncate long errors
                                lines = error_text.splitlines()
                                if len(lines) > 30:
                                    lines = lines[:30]
                                    lines.append("... (truncated)")
                                result.messages.append({
                                    "role": "error",
                                    "content": "\n".join(lines),
                                    "timestamp": ts,
                                })
                    pending_tool_uses = []
                    continue

            # Extract user text
            if isinstance(content, str):
                text = _clean_text(content)
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                text = _clean_text("\n".join(parts))
            else:
                text = ""

            if text:
                role = "prompt" if is_first_user else "user"
                is_first_user = False
                result.messages.append({"role": role, "content": text, "timestamp": ts})
                if role == "prompt":
                    result.initial_prompt = text

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
                        # Do NOT strip narration — it's the agent's reasoning
                        if text:
                            parts.append(text)
                    elif item_type == "tool_use":
                        pending_tool_uses.append(item)
                        parts.append(_format_tool_signature(item))
                    # Skip thinking blocks
            elif isinstance(content, str) and content.strip():
                text = _clean_text(content)
                if text:
                    parts.append(text)

            if parts:
                combined = "\n".join(parts)
                if combined.strip():
                    # Do NOT merge consecutive assistant messages
                    result.messages.append({
                        "role": "agent",
                        "content": combined,
                        "timestamp": ts,
                    })

    return result
