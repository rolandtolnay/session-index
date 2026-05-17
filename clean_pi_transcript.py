#!/usr/bin/env python3
"""Create a human-readable Markdown view of a raw Pi JSONL transcript.

This is intentionally a local utility script, not part of the session-index CLI
or installed skills. Run it directly with `uv run clean_pi_transcript.py <path>`.

The output includes user messages, assistant messages, assistant thinking blocks,
and assistant tool calls. Tool results, usage metadata, signatures, and custom
internal events are omitted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


Message = dict[str, Any]


TOOL_TARGET_KEYS = ("path", "file", "url", "cwd", "glob", "pattern")
TOOL_FALLBACK_KEYS = ("query", "id", "to", "agent")


def load_jsonl(path: Path) -> list[Message]:
    entries: list[Message] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def select_active_branch(entries: list[Message]) -> list[Message]:
    """Return Pi's latest root-to-leaf branch, falling back to chronological order."""
    body = [entry for entry in entries if entry.get("type") != "session"]
    by_id = {entry.get("id"): entry for entry in body if entry.get("id")}

    leaf_id = ""
    for entry in reversed(body):
        if entry.get("id"):
            leaf_id = entry["id"]
            break

    branch_reversed: list[Message] = []
    seen: set[str] = set()
    current = leaf_id
    while current and current in by_id and current not in seen:
        seen.add(current)
        entry = by_id[current]
        branch_reversed.append(entry)
        current = entry.get("parentId")

    return list(reversed(branch_reversed)) if branch_reversed else body


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def fenced(text: Any, language: str = "text") -> str:
    body = as_text(text).rstrip("\n")
    longest_ticks = max((len(match.group(0)) for match in re.finditer(r"`+", body)), default=0)
    ticks = "`" * max(3, longest_ticks + 1)
    return f"{ticks}{language}\n{body}\n{ticks}"


def tool_target(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return "(no file/target argument)"

    parts: list[str] = []
    for key in TOOL_TARGET_KEYS:
        value = args.get(key)
        if value not in (None, ""):
            parts.append(f"{key}: {as_text(value)}")
    if parts:
        return "; ".join(parts)

    if name == "bash" and args.get("command"):
        command = as_text(args["command"]).replace("\n", " && ")
        if len(command) > 220:
            command = command[:217] + "..."
        return f"command: {command}"

    for key in TOOL_FALLBACK_KEYS:
        value = args.get(key)
        if value not in (None, ""):
            return f"{key}: {as_text(value)}"

    return "(no file/target argument)"


def iter_transcript_messages(entries: list[Message]) -> list[Message]:
    return [
        entry
        for entry in entries
        if entry.get("type") == "message" and entry.get("message", {}).get("role") in {"user", "assistant"}
    ]


def render_markdown(source: Path, entries: list[Message], *, all_events: bool) -> str:
    session = entries[0] if entries and entries[0].get("type") == "session" else {}
    selected_entries = entries if all_events else select_active_branch(entries)
    messages = iter_transcript_messages(selected_entries)

    lines: list[str] = [
        "# Cleaned Pi Transcript",
        "",
        f"- Source: `{source}`",
    ]

    if session:
        lines.extend(
            [
                f"- Session ID: `{session.get('id', '')}`",
                f"- Started: {session.get('timestamp', '')}",
                f"- Working directory: `{session.get('cwd', '')}`",
            ]
        )

    lines.extend(
        [
            f"- Branch mode: {'all chronological events' if all_events else 'active/latest branch'}",
            "- Included: user messages, assistant messages, assistant thinking blocks, and tool calls",
            "- Omitted: tool results, signatures, usage metadata, and custom/internal events",
            "",
            "---",
            "",
        ]
    )

    for turn_number, entry in enumerate(messages, 1):
        message = entry.get("message", {})
        role = message.get("role", "")
        label = "User" if role == "user" else "Assistant"
        timestamp = entry.get("timestamp") or message.get("timestamp") or ""

        lines.extend([f"## {turn_number}. {label} — {timestamp}", ""])

        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = []

        counters = {"text": 0, "thinking": 0, "toolCall": 0}
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type == "thinking":
                counters["thinking"] += 1
                suffix = f" {counters['thinking']}" if counters["thinking"] > 1 else ""
                lines.extend([f"### Thinking{suffix}", "", fenced(item.get("thinking", "")), ""])
                continue

            if item_type == "text":
                counters["text"] += 1
                if role == "user":
                    heading = "### User message"
                else:
                    heading = "### Message"
                if counters["text"] > 1:
                    heading += f" {counters['text']}"
                lines.extend([heading, "", fenced(item.get("text", "")), ""])
                continue

            if item_type == "toolCall":
                counters["toolCall"] += 1
                if counters["toolCall"] == 1:
                    lines.extend(["### Tool calls", ""])
                name = item.get("name", "unknown")
                lines.append(f"- `{name}` — {tool_target(name, item.get('arguments', {}))}")

        if counters["toolCall"]:
            lines.append("")
        lines.extend(["---", ""])

    return "\n".join(lines).rstrip() + "\n"


def default_output_path(source: Path) -> Path:
    return source.with_suffix(".cleaned.md")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a readable Markdown transcript from a raw Pi JSONL session file."
    )
    parser.add_argument("transcript", type=Path, help="Path to a raw Pi JSONL transcript")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output Markdown path. Defaults to <transcript>.cleaned.md",
    )
    parser.add_argument(
        "--all-events",
        action="store_true",
        help="Include all chronological user/assistant messages instead of only Pi's latest branch.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    source = args.transcript.expanduser().resolve()
    output = (args.output or default_output_path(source)).expanduser().resolve()

    if not source.exists():
        print(f"error: transcript not found: {source}", file=sys.stderr)
        return 2
    if output.exists() and not args.force:
        print(f"error: output already exists: {output} (use --force to overwrite)", file=sys.stderr)
        return 2

    try:
        entries = load_jsonl(source)
        markdown = render_markdown(source, entries, all_events=args.all_events)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    messages = iter_transcript_messages(entries if args.all_events else select_active_branch(entries))
    print(f"wrote {output}")
    print(f"included {len(messages)} user/assistant messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
