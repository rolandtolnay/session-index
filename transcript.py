"""Cleaned .md transcript writer.

Writes conversation transcripts to ~/.session-index/transcripts/{session_id}.md
Uses inline role tags with bracket tool calls.
"""

import os

TRANSCRIPT_DIR = os.path.join(os.path.expanduser("~/.session-index"), "transcripts")


def write_transcript(
    session_id: str,
    messages: list[dict[str, str]],
    *,
    slug: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Write a cleaned transcript to disk. Returns the file path."""
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPT_DIR, f"{session_id}.md")

    lines: list[str] = []

    # Header
    header_parts = []
    if slug:
        header_parts.append(slug)
    header_parts.append(project or "unknown")
    if branch:
        header_parts.append(branch)
    if timestamp:
        header_parts.append(timestamp[:10])  # date only
    lines.append(" | ".join(header_parts))
    lines.append("---")
    lines.append("")

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
        lines.append("")  # blank line between messages

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path
