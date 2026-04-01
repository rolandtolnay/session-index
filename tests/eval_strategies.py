"""Evaluate message selection strategies for the summarizer.

Phase 3: Generate summaries under 5 strategies using local Ollama.
Phase 4: Score each strategy's summaries against ground-truth search queries
         using FTS5 search on summary column only.

Usage:
    uv run tests/eval_strategies.py [--generate] [--score] [--all]

    --generate  Run Phase 3: generate summaries (slow, ~165 Ollama calls)
    --score     Run Phase 4: score summaries against ground-truth queries
    --all       Run both phases
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parser import clean_user_messages

# ── Config ───────────────────────────────────────────────────────────────────

DB_PATH = os.path.expanduser("~/.session-index/sessions.db")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "eval_results")
SUMMARIES_FILE = os.path.join(RESULTS_DIR, "summaries.json")
QUERIES_FILE = os.path.join(RESULTS_DIR, "ground_truth_queries.json")
SCORES_FILE = os.path.join(RESULTS_DIR, "scores.json")
REPORT_FILE = os.path.join(RESULTS_DIR, "report.txt")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3.5:4b"

SYSTEM_PROMPT = """\
You summarize coding sessions for a searchable index. Future AI assistants will search these summaries to find relevant past work. Write 1-3 sentences capturing what was done and why, so the right session surfaces when someone searches for the topic.

Example input: User asked to fix login timeout. Modified auth/session.ts and tests. Branch: fix/login-timeout.
Example output: Fixed session timeout bug by increasing token TTL from 15m to 1h and adding automatic refresh before expiry.

Example input: User asked to add dark mode toggle. Created ThemeProvider, modified App.tsx and settings page. Branch: feat/dark-mode.
Example output: Implemented dark mode with a ThemeProvider context, CSS variables for color tokens, and a toggle in user settings that persists to localStorage.
"""

STRATEGIES = {
    "A_first30": lambda msgs: msgs[:30],
    "B_bookend_10_20": lambda msgs: msgs[:10] + msgs[-20:] if len(msgs) > 30 else msgs[:30],
    "C_bookend_5_25": lambda msgs: msgs[:5] + msgs[-25:] if len(msgs) > 30 else msgs[:30],
    "D_evenly_sampled": lambda msgs: [msgs[i] for i in range(0, len(msgs), max(1, len(msgs) // 30))][:30] if len(msgs) > 30 else msgs[:30],
    "E_hybrid_5_5_20": lambda msgs: _hybrid_select(msgs),
}


def _hybrid_select(msgs):
    """First 5 + last 5 + 20 evenly from middle."""
    if len(msgs) <= 30:
        return msgs[:30]
    first = msgs[:5]
    last = msgs[-5:]
    middle = msgs[5:-5]
    step = max(1, len(middle) // 20)
    sampled = [middle[i] for i in range(0, len(middle), step)][:20]
    return first + sampled + last


# ── LLM ──────────────────────────────────────────────────────────────────────

def call_llm(prompt):
    """Call local Ollama with the summarizer prompt."""
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "options": {"temperature": 0.1, "num_predict": 200, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data.get("message", {}).get("content", "").strip()


def build_prompt(project, branch, files, selected_msgs):
    """Build the summarizer prompt from selected messages."""
    parts = [f"Project: {project}"]
    if branch:
        parts.append(f"Branch: {branch}")
    if files:
        parts.append(f"Files: {', '.join(files[:20])}")
    parts.append("")
    parts.append("User messages:")
    for msg in selected_msgs:
        if len(msg) > 500:
            msg = msg[:500] + "..."
        parts.append(f"- {msg}")
    parts.append("\nSummary:")
    return "\n".join(parts)


# ── Phase 3: Generate summaries ─────────────────────────────────────────────

def load_sessions():
    """Load test sessions from DB."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT session_id, project, branch, user_messages, files_touched,
               user_message_count
        FROM sessions
        WHERE user_message_count >= 25 AND summary IS NOT NULL
              AND transcript_path IS NOT NULL
        ORDER BY user_message_count DESC
    """).fetchall()
    conn.close()
    return rows


def generate_summaries():
    """Phase 3: Generate summaries for all sessions under all strategies."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    rows = load_sessions()

    # Resume from partial results if they exist
    results = {}
    if os.path.exists(SUMMARIES_FILE):
        with open(SUMMARIES_FILE) as f:
            results = json.load(f)

    total_calls = len(rows) * len(STRATEGIES)
    completed = sum(
        1 for sid_data in results.values()
        for _ in sid_data.get("strategies", {})
    )
    print(f"Generating summaries: {len(rows)} sessions × {len(STRATEGIES)} strategies = {total_calls} calls")
    if completed > 0:
        print(f"Resuming from {completed}/{total_calls} completed")
    print()

    for i, row in enumerate(rows):
        sid = row["session_id"]
        raw_msgs = row["user_messages"].split("\n---\n")
        msgs = clean_user_messages(raw_msgs)
        project = row["project"]
        branch = row["branch"] or ""
        files = (row["files_touched"] or "").split(", ")[:20]

        if sid not in results:
            results[sid] = {
                "project": project,
                "branch": branch,
                "user_message_count": row["user_message_count"],
                "cleaned_message_count": len(msgs),
                "strategies": {},
            }

        for name, selector in STRATEGIES.items():
            if name in results[sid].get("strategies", {}):
                continue  # Already generated

            selected = selector(msgs)
            prompt = build_prompt(project, branch, files, selected)

            t0 = time.time()
            try:
                summary = call_llm(prompt)
            except Exception as e:
                summary = f"ERROR: {e}"
            elapsed = time.time() - t0

            results[sid]["strategies"][name] = {
                "summary": summary,
                "msg_count": len(selected),
                "prompt_chars": len(prompt),
                "elapsed_s": round(elapsed, 1),
            }

            completed += 1
            print(f"  [{completed}/{total_calls}] {sid[:8]}… {name} "
                  f"({len(selected)} msgs, {elapsed:.1f}s)")

            # Save after each call for resumability
            with open(SUMMARIES_FILE, "w") as f:
                json.dump(results, f, indent=2)

        if (i + 1) % 5 == 0:
            print(f"\n--- {i+1}/{len(rows)} sessions done ---\n")

    print(f"\nDone. Summaries saved to {SUMMARIES_FILE}")
    return results


# ── Phase 4: Score with FTS5 search ─────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_sessions (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE,
    summary TEXT,
    project TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS eval_fts USING fts5(
    summary,
    project,
    content=eval_sessions,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS eval_ai AFTER INSERT ON eval_sessions BEGIN
    INSERT INTO eval_fts(rowid, summary, project)
    VALUES (new.rowid, new.summary, new.project);
END;

CREATE TRIGGER IF NOT EXISTS eval_ad AFTER DELETE ON eval_sessions BEGIN
    INSERT INTO eval_fts(eval_fts, rowid, summary, project)
    VALUES ('delete', old.rowid, old.summary, old.project);
END;
"""


def fts_search(conn, query, limit=20):
    """Search eval_fts, returning session_ids ranked by relevance."""
    # Match individual terms (same as production search())
    terms = query.split()
    # Use OR to be more lenient — a query like "multi-currency IBAN" should
    # match if either term appears
    safe_query = " OR ".join(f'"{t}"' for t in terms if len(t) > 2)
    if not safe_query:
        return []
    try:
        cursor = conn.execute("""
            SELECT es.session_id, rank
            FROM eval_fts fts
            JOIN eval_sessions es ON es.rowid = fts.rowid
            WHERE eval_fts MATCH :query
            ORDER BY rank
            LIMIT :limit
        """, {"query": safe_query, "limit": limit})
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        return []


def score_strategy(summaries, queries, strategy_name):
    """Score one strategy by building a temp FTS index and running all queries."""
    # Create in-memory DB with only summaries from this strategy
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    # Also insert ALL other sessions from the real DB with their existing summaries
    # so that ranking is realistic (target must compete with other sessions)
    real_conn = sqlite3.connect(DB_PATH)
    real_conn.row_factory = sqlite3.Row
    all_sessions = real_conn.execute(
        "SELECT session_id, summary, project FROM sessions WHERE summary IS NOT NULL"
    ).fetchall()
    real_conn.close()

    test_sids = set(summaries.keys())

    for row in all_sessions:
        sid = row["session_id"]
        if sid in test_sids:
            # Use the strategy's summary for test sessions
            strat_data = summaries[sid]["strategies"].get(strategy_name, {})
            summary = strat_data.get("summary", "")
        else:
            # Use existing summary for non-test sessions
            summary = row["summary"] or ""
        conn.execute(
            "INSERT OR REPLACE INTO eval_sessions (session_id, summary, project) VALUES (?, ?, ?)",
            (sid, summary, row["project"]),
        )
    conn.commit()

    # Score each query
    scores = {"summary_dependent": [], "literal": []}

    for sid, query_data in queries.items():
        for qtype in ["summary_dependent", "literal"]:
            for query in query_data.get(qtype, []):
                results = fts_search(conn, query)
                if sid in results[:3]:
                    score = 1.0
                elif sid in results[:10]:
                    score = 0.5
                else:
                    score = 0.0
                scores[qtype].append({
                    "session_id": sid,
                    "query": query,
                    "score": score,
                    "rank": results.index(sid) + 1 if sid in results else -1,
                    "total_results": len(results),
                })

    conn.close()
    return scores


def run_scoring():
    """Phase 4: Score all strategies against ground-truth queries."""
    if not os.path.exists(SUMMARIES_FILE):
        print("No summaries found. Run --generate first.")
        return
    if not os.path.exists(QUERIES_FILE):
        print(f"No ground-truth queries found at {QUERIES_FILE}")
        print("Place the merged JSON from Phase 2 agents there.")
        return

    with open(SUMMARIES_FILE) as f:
        summaries = json.load(f)
    with open(QUERIES_FILE) as f:
        queries = json.load(f)

    # Only score sessions that have both summaries and queries
    common_sids = set(summaries.keys()) & set(queries.keys())
    print(f"Scoring {len(common_sids)} sessions across {len(STRATEGIES)} strategies")
    print(f"Total queries: {sum(len(q.get('summary_dependent', [])) + len(q.get('literal', [])) for q in queries.values())}")
    print()

    # Filter to common sessions
    filtered_queries = {k: v for k, v in queries.items() if k in common_sids}

    all_scores = {}
    for strategy_name in STRATEGIES:
        print(f"  Scoring {strategy_name}...")
        scores = score_strategy(summaries, filtered_queries, strategy_name)
        all_scores[strategy_name] = scores

    # Save raw scores
    with open(SCORES_FILE, "w") as f:
        json.dump(all_scores, f, indent=2)

    # Generate report
    report = generate_report(all_scores, summaries, filtered_queries)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"\n{report}")
    print(f"\nScores saved to {SCORES_FILE}")
    print(f"Report saved to {REPORT_FILE}")


def generate_report(all_scores, summaries, queries):
    """Generate a human-readable report from scores."""
    lines = ["=" * 70]
    lines.append("SUMMARIZER STRATEGY EVALUATION REPORT")
    lines.append("=" * 70)
    lines.append("")

    # Aggregate scores per strategy
    lines.append("AGGREGATE SCORES (higher is better, max 1.0)")
    lines.append("-" * 70)
    lines.append(f"{'Strategy':<25} {'Summary-Dep':>12} {'Literal':>12} {'Combined':>12}")
    lines.append("-" * 70)

    strategy_agg = {}
    for strategy_name, scores in all_scores.items():
        sd_scores = [s["score"] for s in scores["summary_dependent"]]
        lit_scores = [s["score"] for s in scores["literal"]]
        sd_mean = sum(sd_scores) / len(sd_scores) if sd_scores else 0
        lit_mean = sum(lit_scores) / len(lit_scores) if lit_scores else 0
        combined = (sd_mean + lit_mean) / 2
        strategy_agg[strategy_name] = {
            "sd_mean": sd_mean, "lit_mean": lit_mean, "combined": combined,
            "sd_scores": sd_scores, "lit_scores": lit_scores,
        }
        lines.append(f"{strategy_name:<25} {sd_mean:>12.3f} {lit_mean:>12.3f} {combined:>12.3f}")

    # Winner
    lines.append("-" * 70)
    winner = max(strategy_agg, key=lambda k: strategy_agg[k]["sd_mean"])
    lines.append(f"WINNER (by summary-dependent score): {winner} "
                 f"({strategy_agg[winner]['sd_mean']:.3f})")
    lines.append("")

    # Breakdown by message count bucket
    lines.append("BREAKDOWN BY SESSION LENGTH")
    lines.append("-" * 70)
    buckets = {"25-34": (25, 34), "35-49": (35, 49), "50+": (50, 200)}

    for bucket_name, (lo, hi) in buckets.items():
        bucket_sids = {
            sid for sid, data in summaries.items()
            if lo <= data["user_message_count"] <= hi
            and sid in queries
        }
        if not bucket_sids:
            continue

        lines.append(f"\n  {bucket_name} messages ({len(bucket_sids)} sessions):")
        lines.append(f"  {'Strategy':<25} {'Summary-Dep':>12} {'Literal':>12}")

        for strategy_name, scores in all_scores.items():
            sd = [s["score"] for s in scores["summary_dependent"]
                  if s["session_id"] in bucket_sids]
            lit = [s["score"] for s in scores["literal"]
                   if s["session_id"] in bucket_sids]
            sd_mean = sum(sd) / len(sd) if sd else 0
            lit_mean = sum(lit) / len(lit) if lit else 0
            lines.append(f"  {strategy_name:<25} {sd_mean:>12.3f} {lit_mean:>12.3f}")

    # Per-session detail for summary-dependent queries
    lines.append("")
    lines.append("PER-SESSION DETAIL (summary-dependent queries only)")
    lines.append("-" * 70)

    # Group by session
    session_details = {}
    for strategy_name, scores in all_scores.items():
        for s in scores["summary_dependent"]:
            sid = s["session_id"]
            if sid not in session_details:
                session_details[sid] = {
                    "project": summaries.get(sid, {}).get("project", "?"),
                    "msgs": summaries.get(sid, {}).get("user_message_count", 0),
                    "queries": {},
                }
            q = s["query"]
            if q not in session_details[sid]["queries"]:
                session_details[sid]["queries"][q] = {}
            session_details[sid]["queries"][q][strategy_name] = s["score"]

    for sid in sorted(session_details, key=lambda s: session_details[s]["msgs"], reverse=True):
        detail = session_details[sid]
        lines.append(f"\n  {sid[:12]}… ({detail['project']}, {detail['msgs']} msgs)")
        for query, strat_scores in detail["queries"].items():
            scores_str = "  ".join(
                f"{name[:1]}={score:.0f}" for name, score in strat_scores.items()
            )
            lines.append(f"    \"{query}\"")
            lines.append(f"      {scores_str}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("Legend: A=first30 B=bookend10+20 C=bookend5+25 D=even E=hybrid5+5+20")
    lines.append("Scores: 1.0=top3, 0.5=top10, 0.0=not found")
    lines.append("=" * 70)

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Evaluate summarizer strategies")
    parser.add_argument("--generate", action="store_true", help="Phase 3: generate summaries")
    parser.add_argument("--score", action="store_true", help="Phase 4: score against queries")
    parser.add_argument("--all", action="store_true", help="Run both phases")
    args = parser.parse_args()

    if not any([args.generate, args.score, args.all]):
        parser.print_help()
        return

    if args.generate or args.all:
        generate_summaries()
    if args.score or args.all:
        run_scoring()


if __name__ == "__main__":
    main()
