#!/usr/bin/env python3
"""Evaluate excerpt extraction strategies against real transcripts.

Compares selection strategies (first_n, density, recency, hybrid) with and
without Q/A pairing, across realistic search queries on real data.

Metrics:
- Keyword density of returned blocks (higher = more relevant content selected)
- Sessions with excerpts vs. sessions where all matches were filtered
- Block coverage and budget utilization
- Q/A pair completeness (answer returned without its question, or vice versa)

Usage:
    uv run tests/eval_excerpts.py                    # full comparison
    uv run tests/eval_excerpts.py --strategy hybrid   # single strategy
    uv run tests/eval_excerpts.py --qa-only           # Q/A pairing comparison
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_connection, init_db, search_flexible
from transcript import (
    _parse_blocks, _keyword_density, _score_blocks, _block_role,
    _apply_qa_pairing, _ROLE_RE,
    STRATEGY_FIRST_N, STRATEGY_DENSITY, STRATEGY_RECENCY, STRATEGY_HYBRID,
)

# ── Realistic search queries ────────────────────────────────────────────────

QUERIES = [
    {"query": "auth token", "project": None, "label": "auth token"},
    {"query": "payout schedule", "project": None, "label": "payout schedule"},
    {"query": "hook debugging", "project": None, "label": "hook debugging"},
    {"query": "summarizer evaluation", "project": None, "label": "summarizer eval"},
    {"query": "transcript format", "project": None, "label": "transcript format"},
    {"query": "merge conflict", "project": None, "label": "merge conflict"},
    {"query": "linear ticket", "project": None, "label": "linear ticket"},
    {"query": "bank account", "project": None, "label": "bank account"},
    {"query": "session index", "project": None, "label": "session index"},
    {"query": "dark mode", "project": None, "label": "dark mode"},
    {"query": "refactor", "project": "dashboard", "label": "dashboard refactor"},
    {"query": "debug", "project": "synapto", "label": "synapto debug"},
    {"query": "skill", "project": "llm-toolkit", "label": "toolkit skill"},
    {"query": "test", "project": "mindsystem", "label": "mindsystem test"},
    {"query": "deploy", "project": "dashboard", "label": "dashboard deploy"},
]

MAX_BLOCKS = 5
MAX_LINES = 200
SEARCH_LIMIT = 5  # top 5 results per query

STRATEGIES = [STRATEGY_FIRST_N, STRATEGY_DENSITY, STRATEGY_RECENCY, STRATEGY_HYBRID]


def _load_transcripts_for_queries(conn):
    """Pre-load all transcript data needed for evaluation."""
    transcript_data = []  # list of (query, session_id, blocks, keywords)

    for q in QUERIES:
        results = search_flexible(
            conn, query=q["query"], project=q.get("project"), limit=SEARCH_LIMIT,
        )
        keywords = [k.lower() for k in q["query"].split() if len(k) > 2]

        for r in results:
            tp = r.get("transcript_path")
            if not tp or not os.path.exists(tp):
                continue
            with open(tp) as f:
                content = f.read()
            blocks = _parse_blocks(content)
            transcript_data.append({
                "query_label": q["label"],
                "session_id": r["session_id"],
                "blocks": blocks,
                "keywords": keywords,
            })

    return transcript_data


def evaluate_strategy(transcript_data, strategy, qa_pair):
    """Run a strategy across all pre-loaded transcripts, return detailed stats."""
    stats = {
        "strategy": strategy,
        "qa_pair": qa_pair,
        "sessions_total": len(transcript_data),
        "sessions_with_excerpts": 0,
        "sessions_all_filtered": 0,
        "sessions_no_match": 0,
        "total_blocks_matched": 0,
        "total_blocks_selected": 0,
        "total_blocks_from_pairing": 0,
        "skipped_large": 0,
        "skipped_budget": 0,
        "total_output_lines": 0,
        # Quality metrics
        "avg_density_selected": 0.0,
        "avg_density_skipped": 0.0,
        "qa_orphan_questions": 0,  # user block without adjacent assistant
        "qa_orphan_answers": 0,  # assistant block without adjacent user
        "per_query": {},
    }

    density_selected_sum = 0.0
    density_selected_count = 0
    density_skipped_sum = 0.0
    density_skipped_count = 0

    for td in transcript_data:
        blocks = td["blocks"]
        keywords = td["keywords"]
        label = td["query_label"]

        if label not in stats["per_query"]:
            stats["per_query"][label] = {
                "sessions": 0, "with_excerpts": 0, "all_filtered": 0,
                "matched": 0, "selected": 0, "paired_added": 0,
                "skipped_large": 0, "skipped_budget": 0,
                "output_lines": 0,
                "density_selected": 0.0, "density_count": 0,
            }
        pq = stats["per_query"][label]
        pq["sessions"] += 1

        # Score blocks
        scored = _score_blocks(blocks, keywords, strategy)
        total_matching = len(scored)
        stats["total_blocks_matched"] += total_matching
        pq["matched"] += total_matching

        if total_matching == 0:
            stats["sessions_no_match"] += 1
            continue

        # Select within budget
        selected_indices = set()
        total_lines = 0
        local_skipped_large = 0
        local_skipped_budget = 0

        for idx, _score in scored:
            block_lines = blocks[idx].count("\n") + 1
            if block_lines > MAX_LINES:
                local_skipped_large += 1
                continue
            if total_lines + block_lines > MAX_LINES:
                local_skipped_budget += 1
                continue
            if len(selected_indices) >= MAX_BLOCKS:
                local_skipped_budget += 1
                continue
            selected_indices.add(idx)
            total_lines += block_lines

        pre_pair_count = len(selected_indices)

        # Q/A pairing
        if qa_pair and selected_indices:
            candidates = _apply_qa_pairing(selected_indices, blocks, keywords)
            for idx in sorted(candidates - selected_indices):
                block_lines = blocks[idx].count("\n") + 1
                if block_lines > MAX_LINES:
                    continue
                if total_lines + block_lines > MAX_LINES:
                    break
                selected_indices.add(idx)
                total_lines += block_lines

        paired_added = len(selected_indices) - pre_pair_count
        stats["total_blocks_from_pairing"] += paired_added
        pq["paired_added"] += paired_added

        stats["skipped_large"] += local_skipped_large
        stats["skipped_budget"] += local_skipped_budget
        pq["skipped_large"] += local_skipped_large
        pq["skipped_budget"] += local_skipped_budget

        if not selected_indices:
            stats["sessions_all_filtered"] += 1
            pq["all_filtered"] += 1
        else:
            stats["sessions_with_excerpts"] += 1
            pq["with_excerpts"] += 1

        stats["total_blocks_selected"] += len(selected_indices)
        stats["total_output_lines"] += total_lines
        pq["selected"] += len(selected_indices)
        pq["output_lines"] += total_lines

        # Quality: density of selected vs. skipped
        all_matching_indices = {idx for idx, _ in scored}
        for idx, _ in scored:
            d = _keyword_density(blocks[idx], keywords)
            if idx in selected_indices:
                density_selected_sum += d
                density_selected_count += 1
                pq["density_selected"] += d
                pq["density_count"] += 1
            else:
                density_skipped_sum += d
                density_skipped_count += 1

        # Q/A orphan detection
        for idx in selected_indices:
            role = _block_role(blocks[idx])
            if role == "user":
                partner = idx + 1
                if partner < len(blocks) and _block_role(blocks[partner]) == "assistant":
                    if partner not in selected_indices:
                        stats["qa_orphan_questions"] += 1
            elif role == "assistant":
                partner = idx - 1
                if partner >= 0 and _block_role(blocks[partner]) == "user":
                    if partner not in selected_indices:
                        stats["qa_orphan_answers"] += 1

    if density_selected_count > 0:
        stats["avg_density_selected"] = density_selected_sum / density_selected_count
    if density_skipped_count > 0:
        stats["avg_density_skipped"] = density_skipped_sum / density_skipped_count

    return stats


def print_stats(s):
    """Print a strategy's detailed stats."""
    label = f"{s['strategy']}"
    if s["qa_pair"]:
        label += " + Q/A pairing"

    print(f"\n{'═' * 70}")
    print(f"  Strategy: {label}")
    print(f"{'═' * 70}")

    total = s["sessions_total"]
    if total == 0:
        print("  No data.")
        return

    print(f"  Sessions: {total}")
    print(f"    With excerpts:    {s['sessions_with_excerpts']} ({s['sessions_with_excerpts']*100//total}%)")
    print(f"    All filtered:     {s['sessions_all_filtered']} ({s['sessions_all_filtered']*100//total}%)")
    print(f"    No keyword match: {s['sessions_no_match']}")

    matched = s["total_blocks_matched"]
    if matched > 0:
        print(f"\n  Blocks: {matched} matched → {s['total_blocks_selected']} selected ({s['total_blocks_selected']*100//matched}%)")
        if s["total_blocks_from_pairing"] > 0:
            print(f"    From Q/A pairing: {s['total_blocks_from_pairing']}")
        print(f"    Skipped oversized: {s['skipped_large']}")
        print(f"    Skipped budget:    {s['skipped_budget']}")

        avg_out = s["total_output_lines"] // max(1, s["sessions_with_excerpts"])
        print(f"\n  Output: {avg_out} avg lines/session")

        print(f"\n  Quality:")
        print(f"    Avg density (selected): {s['avg_density_selected']:.4f} kw/line")
        print(f"    Avg density (skipped):  {s['avg_density_skipped']:.4f} kw/line")
        if s["avg_density_skipped"] > 0:
            ratio = s["avg_density_selected"] / s["avg_density_skipped"]
            print(f"    Selection quality:      {ratio:.2f}x (higher = picking denser blocks)")
        print(f"    Q/A orphan questions:   {s['qa_orphan_questions']}")
        print(f"    Q/A orphan answers:     {s['qa_orphan_answers']}")


def print_comparison(all_stats):
    """Side-by-side comparison table."""
    print(f"\n{'═' * 90}")
    print("  STRATEGY COMPARISON (max_blocks=5, max_lines=200)")
    print(f"{'═' * 90}")

    headers = ["Metric"] + [
        f"{s['strategy']}{' +QA' if s['qa_pair'] else ''}" for s in all_stats
    ]

    def pct(n, d):
        return f"{n*100//max(d,1)}%" if d > 0 else "—"

    rows = [
        ["Sessions w/ excerpts"] + [
            f"{s['sessions_with_excerpts']}/{s['sessions_total']}"
            for s in all_stats
        ],
        ["Blocks selected %"] + [
            pct(s["total_blocks_selected"], s["total_blocks_matched"])
            for s in all_stats
        ],
        ["Avg density (selected)"] + [
            f"{s['avg_density_selected']:.4f}" for s in all_stats
        ],
        ["Avg density (skipped)"] + [
            f"{s['avg_density_skipped']:.4f}" for s in all_stats
        ],
        ["Selection quality (ratio)"] + [
            f"{s['avg_density_selected']/max(s['avg_density_skipped'],0.0001):.2f}x"
            for s in all_stats
        ],
        ["Avg output lines"] + [
            str(s["total_output_lines"] // max(1, s["sessions_with_excerpts"]))
            for s in all_stats
        ],
        ["Q/A orphans"] + [
            str(s["qa_orphan_questions"] + s["qa_orphan_answers"])
            for s in all_stats
        ],
        ["Blocks from pairing"] + [
            str(s["total_blocks_from_pairing"]) for s in all_stats
        ],
    ]

    col_widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
    fmt = "  " + " | ".join(f"{{:<{w}}}" for w in col_widths)

    print(fmt.format(*headers))
    print("  " + "-+-".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt.format(*row))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=STRATEGIES)
    parser.add_argument("--qa-only", action="store_true",
                        help="Compare hybrid with and without Q/A pairing")
    args = parser.parse_args()

    conn = get_connection()
    init_db(conn)
    transcript_data = _load_transcripts_for_queries(conn)
    conn.close()

    print(f"Loaded {len(transcript_data)} transcript-query pairs")

    if args.qa_only:
        s_no = evaluate_strategy(transcript_data, STRATEGY_HYBRID, qa_pair=False)
        s_yes = evaluate_strategy(transcript_data, STRATEGY_HYBRID, qa_pair=True)
        print_stats(s_no)
        print_stats(s_yes)
        print_comparison([s_no, s_yes])
    elif args.strategy:
        s = evaluate_strategy(transcript_data, args.strategy, qa_pair=True)
        print_stats(s)
    else:
        all_stats = []
        for strategy in STRATEGIES:
            s = evaluate_strategy(transcript_data, strategy, qa_pair=False)
            print_stats(s)
            all_stats.append(s)
        # Also run the winner with Q/A pairing
        s_hybrid_qa = evaluate_strategy(transcript_data, STRATEGY_HYBRID, qa_pair=True)
        print_stats(s_hybrid_qa)
        all_stats.append(s_hybrid_qa)
        print_comparison(all_stats)


if __name__ == "__main__":
    main()
