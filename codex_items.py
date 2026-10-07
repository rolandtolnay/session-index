"""Translate completed Codex UI items into the legacy parser's event vocabulary.

Completed user items own their turn; assistant/tool items deduplicate by ID.
Raw response messages can
also contain injected context. Keep older turns and deduplicate by item/call ID,
never by text (a user can legitimately repeat the same message).
"""

from __future__ import annotations

import json
import shlex


class CodexFormatError(ValueError):
    """A conversation is present but its encoding cannot be safely indexed."""


def _message_text(item: dict) -> str:
    parts = []
    for block in item.get("content", []):
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif str(block.get("type", "")).lower() in {"image", "localimage", "input_image"}:
            parts.append("[Image attachment]")
    text = "\n".join(parts).strip()
    if not text:
        raise CodexFormatError(f"Unsupported completed {item.get('type')} content")
    return text


def _events(entry: dict, item: dict) -> list[dict]:
    def event(kind: str, **payload) -> dict:
        return {"timestamp": entry.get("timestamp", ""), "type": kind, "payload": payload}

    kind = item.get("type")
    identity = item.get("id", "")
    if kind == "UserMessage":
        return [event("event_msg", type="user_message", message=_message_text(item))]
    if kind == "AgentMessage":
        return [event("response_item", type="message", role="assistant",
                      content=[{"text": _message_text(item)}])]
    if kind == "FileChange":
        return [event("event_msg", type="patch_apply_end", call_id=identity,
                      changes=item.get("changes", {}), success=item.get("status") == "completed",
                      status=item.get("status"), stdout=item.get("stdout", ""), stderr=item.get("stderr", ""))]
    if kind == "CommandExecution":
        command = item.get("command", [])
        args = {"cmd": shlex.join(command) if isinstance(command, list) else command,
                "workdir": item.get("cwd", "")}
        output = item.get("aggregated_output") or item.get("formatted_output") or "\n".join(
            str(item.get(k) or "") for k in ("stdout", "stderr"))
        if item.get("exit_code") is not None:
            output += f"\nExit code: {item['exit_code']}"
        name = "exec_command"
    elif kind == "McpToolCall":
        name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
        args = item.get("arguments", {})
        result = item.get("result", {})
        output = json.dumps(result, ensure_ascii=False)
        if isinstance(result, dict) and result.get("isError"):
            output = "Error: " + output
    else:
        return []
    return [
        event("response_item", type="function_call", call_id=identity, name=name,
              arguments=args, status="failed" if item.get("status") in {"failed", "declined"} else "completed"),
        event("response_item", type="function_call_output", call_id=identity, output=output),
    ]


def normalize_completed_items(entries: list[dict]) -> list[dict]:
    turns = []
    turn = ""
    latest = {}
    first = {}
    message_turns = set()
    message_ids = set()
    tool_ids = set()
    supported = {"UserMessage", "AgentMessage", "FileChange", "CommandExecution", "McpToolCall"}
    for index, entry in enumerate(entries):
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            turns.append(turn)
            continue
        if payload.get("turn_id"):
            turn = payload["turn_id"]
        turns.append(turn)
        if entry.get("type") != "event_msg" or payload.get("type") != "item_completed":
            continue
        item = payload.get("item", {})
        if not isinstance(item, dict) or item.get("type") not in supported:
            continue
        identity = item.get("id")
        if not identity:
            raise CodexFormatError("Completed conversation/tool item has no ID")
        latest[(turn, identity)] = index
        first.setdefault((turn, identity), index)
        if item["type"] in {"UserMessage", "AgentMessage"}:
            message_ids.add(identity)
            if turn:
                message_turns.add((turn, "user" if item["type"] == "UserMessage" else "assistant"))
        else:
            tool_ids.add(identity)

    result = []
    for index, (entry, turn) in enumerate(zip(entries, turns)):
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            result.append(entry)
            continue
        kind = payload.get("type")
        if entry.get("type") == "event_msg" and kind == "item_completed":
            item = payload.get("item", {})
            if isinstance(item, dict) and item.get("type") in supported:
                key = (turn, item["id"])
                if first[key] == index:
                    result.extend(_events(entry, entries[latest[key]]["payload"]["item"]))
                continue
        role = payload.get("role") if kind == "message" else ("user" if kind == "user_message" else None)
        # Raw assistant output can reach the rollout before its completed item.
        # A completed commentary item must not hide a later, unmirrored final.
        if role and ((role == "user" and (turn, role) in message_turns) or payload.get("id") in message_ids):
            continue
        if payload.get("call_id") in tool_ids:
            continue
        result.append(entry)
    return result
