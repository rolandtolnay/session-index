"""Cleaned .md transcript writer and excerpt extractor.

Writes conversation transcripts to ~/.session-index/transcripts/{session_id}.md
Uses inline role tags with bracket tool calls.
"""

import os
import re

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
            lines.append(f"[user] {'─' * 40}")
            lines.append(content)
        elif role == "assistant":
            lines.append(f"[assistant] {'─' * 34}")
            lines.append(content)
        lines.append("")  # blank line between messages

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path


# ── Excerpt extraction ──────────────────────────────────────────────────────

_ROLE_RE = re.compile(r"^\[(user|assistant)\] ─|^(User|Assistant):")


def extract_excerpts(
    transcript_path: str,
    keywords: list[str],
    max_blocks: int = 3,
    max_lines: int = 60,
) -> str | None:
    """Extract message blocks from a transcript that match any keyword.

    Parses the transcript into message blocks (delimited by [user]/[assistant]
    markers), returns the first max_blocks blocks containing any keyword.
    Total output is capped at max_lines.
    """
    if not os.path.exists(transcript_path):
        return None

    with open(transcript_path) as f:
        content = f.read()

    # Split into message blocks at role delimiters
    blocks: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if _ROLE_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    # Skip the header block (first block before any role delimiter)
    if blocks and not _ROLE_RE.match(blocks[0]):
        blocks = blocks[1:]

    # Find blocks matching any keyword (case-insensitive)
    kw_lower = [k.lower() for k in keywords if len(k) > 2]
    if not kw_lower:
        return None

    matching: list[str] = []
    total_lines = 0
    for block in blocks:
        block_lower = block.lower()
        if any(kw in block_lower for kw in kw_lower):
            block_lines = block.count("\n") + 1
            if block_lines > max_lines:
                # Block too large — skip it rather than stopping
                continue
            if total_lines + block_lines > max_lines:
                break
            matching.append(block)
            total_lines += block_lines
            if len(matching) >= max_blocks:
                break

    if not matching:
        return None

    return "\n\n".join(matching)
