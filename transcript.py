"""Cleaned .md transcript writer and excerpt extractor.

Writes conversation transcripts to ~/.session-index/transcripts/{session_id}.md
Uses inline role tags with bracket tool calls.
"""

import logging
import os
import re

TRANSCRIPT_DIR = os.path.join(os.path.expanduser("~/.session-index"), "transcripts")


def _format_message_time(timestamp: str) -> str:
    """Extract HH:MM:SS from an ISO 8601 timestamp. Returns '' if unavailable."""
    if not timestamp or len(timestamp) < 19:
        return ""
    try:
        # "2026-03-17T15:30:01.234Z" -> "15:30:01"
        return timestamp[11:19]
    except (IndexError, ValueError):
        return ""


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
        ts = _format_message_time(msg.get("timestamp", ""))
        if role == "user":
            if ts:
                lines.append(f"[user] {ts} {'─' * 32}")
            else:
                lines.append(f"[user] {'─' * 40}")
            lines.append(content)
        elif role == "assistant":
            if ts:
                lines.append(f"[assistant] {ts} {'─' * 25}")
            else:
                lines.append(f"[assistant] {'─' * 34}")
            lines.append(content)
        lines.append("")  # blank line between messages

    with open(path, "w") as f:
        f.write("\n".join(lines))

    return path


# ── Excerpt extraction ──────────────────────────────────────────────────────

_ROLE_RE = re.compile(r"^\[(user|assistant)\] (?:\d{2}:\d{2}:\d{2} )?─")

_excerpt_log = logging.getLogger("session-index.excerpt")

# Strategy names
STRATEGY_FIRST_N = "first_n"
STRATEGY_DENSITY = "density"
STRATEGY_RECENCY = "recency"
STRATEGY_HYBRID = "hybrid"


def _parse_blocks(content: str) -> list[str]:
    """Split transcript content into message blocks at role delimiters."""
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

    return blocks


def _keyword_density(block: str, keywords: list[str]) -> float:
    """Count keyword occurrences per line in block."""
    block_lower = block.lower()
    count = sum(block_lower.count(kw) for kw in keywords)
    lines = block.count("\n") + 1
    return count / max(lines, 1)


def _block_role(block: str) -> str | None:
    """Extract role from block's first line."""
    first_line = block.split("\n", 1)[0]
    if "[user]" in first_line:
        return "user"
    if "[assistant]" in first_line:
        return "assistant"
    return None


def _score_blocks(
    blocks: list[str],
    keywords: list[str],
    strategy: str,
) -> list[tuple[int, float]]:
    """Score matching blocks. Returns [(block_index, score)] sorted by score desc."""
    total = len(blocks)
    scored: list[tuple[int, float]] = []

    for i, block in enumerate(blocks):
        block_lower = block.lower()
        if not any(kw in block_lower for kw in keywords):
            continue

        if strategy == STRATEGY_FIRST_N:
            # Score by position: earlier = higher score (preserves original behavior)
            score = total - i
        elif strategy == STRATEGY_DENSITY:
            score = _keyword_density(block, keywords)
        elif strategy == STRATEGY_RECENCY:
            # Position weight: 0.0 (start) to 1.0 (end)
            score = i / max(total - 1, 1)
        elif strategy == STRATEGY_HYBRID:
            density = _keyword_density(block, keywords)
            position_weight = i / max(total - 1, 1)
            # Density matters most, recency provides a tiebreaker boost
            score = density * (0.5 + 0.5 * position_weight)
        else:
            score = total - i  # fallback to first_n

        scored.append((i, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _apply_qa_pairing(
    selected_indices: set[int],
    blocks: list[str],
    keywords: list[str],
) -> set[int]:
    """For each selected block, pull in its Q/A partner if adjacent."""
    paired = set(selected_indices)
    for idx in selected_indices:
        role = _block_role(blocks[idx])
        if role == "assistant" and idx > 0:
            partner = idx - 1
            if _block_role(blocks[partner]) == "user" and partner not in paired:
                paired.add(partner)
        elif role == "user" and idx + 1 < len(blocks):
            partner = idx + 1
            if _block_role(blocks[partner]) == "assistant" and partner not in paired:
                paired.add(partner)
    return paired


def extract_excerpts(
    transcript_path: str,
    keywords: list[str],
    max_blocks: int = 5,
    max_lines: int = 200,
    strategy: str = STRATEGY_HYBRID,
    qa_pair: bool = True,
) -> str | None:
    """Extract the most relevant message blocks from a transcript.

    Strategies:
      - first_n: first matching blocks chronologically (original behavior)
      - density: rank by keyword frequency per line
      - recency: prefer later blocks (where decisions/outcomes live)
      - hybrid: density × recency weight (default)

    When qa_pair=True, selected blocks pull in their adjacent Q/A partner.
    Oversized blocks are skipped with a note for the caller.
    """
    if not os.path.exists(transcript_path):
        return None

    with open(transcript_path) as f:
        content = f.read()

    blocks = _parse_blocks(content)

    kw_lower = [k.lower() for k in keywords if len(k) > 2]
    if not kw_lower:
        return None

    sid = os.path.basename(transcript_path).replace(".md", "")[:12]

    # Score and rank all matching blocks
    scored = _score_blocks(blocks, kw_lower, strategy)
    total_matching = len(scored)

    if total_matching == 0:
        return None

    # Select top blocks within budget
    selected_indices: set[int] = set()
    total_lines = 0
    skipped_large = 0
    skipped_budget = 0

    for idx, _score in scored:
        block_lines = blocks[idx].count("\n") + 1

        if block_lines > max_lines:
            skipped_large += 1
            _excerpt_log.debug(
                "%s: skipped block #%d (%d lines > %d max_lines), strategy=%s",
                sid, idx, block_lines, max_lines, strategy,
            )
            continue
        if total_lines + block_lines > max_lines:
            skipped_budget += 1
            continue
        if len(selected_indices) >= max_blocks:
            skipped_budget += 1
            continue

        selected_indices.add(idx)
        total_lines += block_lines

    # Q/A pairing: pull in adjacent partners (within remaining budget)
    if qa_pair and selected_indices:
        candidates = _apply_qa_pairing(selected_indices, blocks, kw_lower)
        for idx in sorted(candidates - selected_indices):
            block_lines = blocks[idx].count("\n") + 1
            if block_lines > max_lines:
                continue
            if total_lines + block_lines > max_lines:
                break
            selected_indices.add(idx)
            total_lines += block_lines

    skipped_total = skipped_large + skipped_budget
    if skipped_total > 0:
        _excerpt_log.info(
            "%s: %d/%d matching blocks selected, %d skipped "
            "(%d oversized, %d budget), strategy=%s, keywords=%s",
            sid, len(selected_indices), total_matching, skipped_total,
            skipped_large, skipped_budget, strategy, keywords,
        )

    if not selected_indices:
        if total_matching > 0:
            return (
                f"[{total_matching} matching block(s) too large for excerpts "
                f"— grep transcript for detail]"
            )
        return None

    # Output blocks in chronological order regardless of selection order
    result_blocks = [blocks[i] for i in sorted(selected_indices)]
    result = "\n\n".join(result_blocks)

    if skipped_total > 0:
        result += (
            f"\n\n[{skipped_total} more matching block(s) not shown "
            f"— grep transcript for detail]"
        )

    return result
