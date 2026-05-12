#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""Benchmark Pi GPT summarization models against session-index ground truth.

Generates summaries through headless Pi print mode and optionally scores them
with a GPT judge. Designed for isolated benchmark runs; it does not modify the
session-index database.

Examples:
    uv run tests/pi_gpt_benchmark.py generate \
      --model openai-codex/gpt-5.4-mini \
      --inputs current,rich \
      --output tests/eval_results/pi_gpt_54mini.json

    uv run tests/pi_gpt_benchmark.py score \
      --input tests/eval_results/pi_gpt_combined.json \
      --output tests/eval_results/pi_gpt_scores.json

    uv run tests/pi_gpt_benchmark.py report \
      --input tests/eval_results/pi_gpt_combined.json \
      --scores tests/eval_results/pi_gpt_scores.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser import clean_user_messages  # noqa: E402
from summarizer import SYSTEM_PROMPT_LOCAL as EXISTING_VARIANT_F_PROMPT  # noqa: E402

DB_PATH = Path(os.path.expanduser("~/.session-index/sessions.db"))
GROUND_TRUTH_PATH = ROOT / "tests" / "eval_results" / "ground_truth.json"
DEFAULT_OUTPUT_DIR = ROOT / "tests" / "eval_results"
DEFAULT_JUDGE_MODEL = "openai-codex/gpt-5.5"

COMPACT_PROMPT = """\
You summarize coding sessions so an AI assistant can find relevant past work by keyword search.

Start with an action verb: Implemented, Fixed, Refactored, Added, Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, Created, Removed, Replaced, Extracted.

Write 1-4 sentences capturing what this session accomplished and why. Include specific topics, technologies, files, functions, components, ticket IDs, commands, and decisions so keyword searches find this session. If the session spans multiple topics, mention all important ones.

Distinguish planning, research, debugging, and implementation. Only state facts visible in the provided session data. Do not describe the overall project; describe what happened in this session. Never answer the user's questions directly.
"""

RICH_RULES = """\

Rich transcript rules:
- Prioritize the user's initial goal, explicit follow-up requests, final outcomes, and decisions over incidental shell output or setup detours.
- Mention failed hypotheses or intermediate debugging only when they explain the final outcome.
- Include concrete searchable names from the transcript: tickets, PRs, files, functions, components, commands, APIs, providers, and root causes.
- Do not claim code was implemented, committed, deployed, or merged unless the transcript says so.
"""

RICH_F_PROMPT = EXISTING_VARIANT_F_PROMPT + RICH_RULES

FACTS_FIRST_PROMPT = """\
You summarize full coding-session transcripts for a searchable archive.

Before writing, silently identify:
1. the user's goal,
2. the final outcome,
3. concrete files/components/functions/tickets/APIs touched or discussed,
4. important decisions and root causes,
5. side topics that should still be searchable.

Output only the final summary, not your notes. Write 2-4 sentences. Start with an action verb such as Implemented, Fixed, Refactored, Added, Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, Created, Removed, Replaced, or Extracted.

Optimize for high-precision search keywords and factual accuracy. Distinguish planning/research/debugging from implementation. Only state facts visible in the transcript. Never answer the user's questions directly.
"""

FINAL_OUTCOME_PROMPT = """\
You summarize coding sessions for future AI retrieval.

Write 2-4 factual sentences about what THIS SESSION accomplished. The summary should be useful when someone searches for the specific work later.

Priority order:
1. final user-visible outcome or decision,
2. root cause or key finding,
3. concrete changed/discussed files, functions, components, commands, tickets, and PRs,
4. important rejected approaches or constraints.

Avoid over-weighting early failed attempts, environment setup, tool errors, or assistant narration unless they materially affected the outcome. Use implementation verbs only for actual implementation; use Planned/Investigated/Debugged when that is what happened. Do not infer beyond the transcript.
"""

HYBRID_RICH_PROMPT = """\
You summarize coding sessions so an AI assistant can find relevant past work by keyword search.

Start with an action verb: Implemented, Fixed, Refactored, Added, Configured, Migrated, Debugged, Investigated, Planned, Designed, Updated, Created, Removed, Replaced, Extracted.

Write 2-4 sentences capturing what was done and why. Include specific searchable keywords: ticket IDs, PR numbers, file paths, components, functions, APIs, commands, models, providers, root causes, and decisions. If the session spans multiple topics, cover each important topic.

Use the full transcript, but summarize the session outcome rather than the transcript chronology. Prioritize the user's goal, final state, explicit decisions, and successful changes. Mention intermediate debugging/setup only if it explains the outcome. Distinguish planning/research/debugging from implementation. Only state facts visible in the transcript.

Example output style: Implemented SYN-342 payout UX: extracted AddExternalBankAccountModal as a reusable component, added payout-page empty-state and dropdown add-bank-account flows, handled pending verification and non-GBP currency filtering via useEbaSupport(), and created PR #31.
"""

PROMPT_VARIANTS = {
    "compact": COMPACT_PROMPT,
    "existing_f": EXISTING_VARIANT_F_PROMPT,
    "rich_f": RICH_F_PROMPT,
    "facts_first": FACTS_FIRST_PROMPT,
    "final_outcome": FINAL_OUTCOME_PROMPT,
    "hybrid_rich": HYBRID_RICH_PROMPT,
}

JUDGE_SYSTEM_PROMPT = """\
You are a strict evaluator of coding-session summaries for a searchable archive.
Score each candidate summary against the provided ground-truth annotations only.
Return valid JSON only, with no markdown.
"""


def _load_ground_truth() -> dict[str, Any]:
    with open(GROUND_TRUTH_PATH) as f:
        return json.load(f)


def _session_ids_from_arg(value: str | None) -> list[str]:
    if value:
        return [s.strip() for s in value.split(",") if s.strip()]
    return list(_load_ground_truth().keys())


def _load_session(session_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """
        SELECT project, branch, user_messages, files_touched, user_message_count,
               summary, transcript_path, source, source_path
        FROM sessions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError(f"Session not found in DB: {session_id}")

    project, branch, user_messages_raw, files_raw, msg_count, original, transcript_path, source, source_path = row
    return {
        "session_id": session_id,
        "project": project or "",
        "branch": branch or "",
        "user_messages": user_messages_raw.split("\n---\n") if user_messages_raw else [],
        "files_touched": [f.strip() for f in files_raw.split(",") if f.strip()] if files_raw else [],
        "msg_count": msg_count or 0,
        "original_summary": original,
        "transcript_path": transcript_path,
        "source": source,
        "source_path": source_path,
    }


def _select_messages(msgs: list[str], budget: int = 30) -> list[str]:
    if len(msgs) <= budget:
        return msgs
    first = msgs[:5]
    last = msgs[-5:]
    middle = msgs[5:-5]
    step = max(1, len(middle) // (budget - 10))
    sampled = [middle[i] for i in range(0, len(middle), step)][: budget - 10]
    return first + sampled + last


def _build_current_input(session: dict[str, Any]) -> str:
    """Match the deployed local-LLM input shape as closely as possible."""
    parts = [f"Project: {session['project']}"]
    if session["branch"]:
        parts.append(f"Branch: {session['branch']}")
    if session["files_touched"]:
        parts.append(f"Files: {', '.join(session['files_touched'][:20])}")
    parts.append("")
    parts.append("User messages:")
    for i, msg in enumerate(_select_messages(clean_user_messages(session["user_messages"]))):
        budget = 2000 if i == 0 else 500
        if len(msg) > budget:
            msg = msg[:budget] + "..."
        parts.append(f"- {msg}")
    parts.append("\nSummary:")
    return "\n".join(parts)


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return ""
    return p.read_text(errors="replace")


def _build_rich_input(session: dict[str, Any]) -> str:
    """Use full cleaned transcript plus less aggressively capped metadata."""
    transcript = _read_text(session.get("transcript_path"))
    parts = [
        "Summarize the coding session below for a searchable archive.",
        "",
        f"Project: {session['project']}",
    ]
    if session["branch"]:
        parts.append(f"Branch: {session['branch']}")
    parts.append(f"User message count: {session['msg_count']}")
    if session.get("source"):
        parts.append(f"Source: {session['source']}")
    if session["files_touched"]:
        parts.append("Files touched:")
        for file_name in session["files_touched"][:80]:
            parts.append(f"- {file_name}")
    parts.append("")

    if transcript:
        parts.append("Full cleaned transcript:")
        parts.append(transcript)
    else:
        parts.append("User messages:")
        for msg in clean_user_messages(session["user_messages"]):
            parts.append(f"- {msg}")

    parts.append("\nSummary:")
    return "\n".join(parts)


def _build_input(session: dict[str, Any], input_variant: str) -> str:
    if input_variant == "current":
        return _build_current_input(session)
    if input_variant == "rich":
        return _build_rich_input(session)
    raise ValueError(f"Unknown input variant: {input_variant}")


def _call_pi(prompt: str, *, model: str, thinking: str, system_prompt: str, timeout: int) -> tuple[str, float, str]:
    cmd = [
        "pi",
        "-p",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--model",
        model,
        "--thinking",
        thinking,
        "--system-prompt",
        system_prompt,
    ]
    env = {
        **os.environ,
        "PI_SKIP_VERSION_CHECK": "1",
    }
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise RuntimeError(f"pi exited {proc.returncode}: {stderr[-1000:]}")
    return proc.stdout.strip(), elapsed, proc.stderr.strip()


def _variant_key(model: str, input_variant: str, thinking: str, prompt_variant: str = "compact") -> str:
    safe_model = model.replace("/", "__")
    return f"{safe_model}|{thinking}|{input_variant}|prompt:{prompt_variant}"


def generate(args: argparse.Namespace) -> None:
    session_ids = _session_ids_from_arg(args.sessions)
    input_variants = [v.strip() for v in args.inputs.split(",") if v.strip()]
    prompt_variant = args.prompt_variant
    system_prompt = PROMPT_VARIANTS[prompt_variant]
    existing: list[dict[str, Any]] = []
    done: set[tuple[str, str, str, str, str]] = set()
    out_path = Path(args.output)
    if args.resume and out_path.exists():
        existing = json.loads(out_path.read_text())
        for item in existing:
            if not item.get("error"):
                done.add((item["session_id"], item["model"], item["thinking"], item["input_variant"], item.get("prompt_variant", "compact")))

    results = existing[:]
    total = len(session_ids) * len(input_variants)
    completed = 0
    for session_id in session_ids:
        session = _load_session(session_id)
        for input_variant in input_variants:
            completed += 1
            key = (session_id, args.model, args.thinking, input_variant, prompt_variant)
            if key in done:
                print(f"[{completed}/{total}] skip {session_id[:8]} {input_variant} {prompt_variant}", file=sys.stderr)
                continue
            print(f"[{completed}/{total}] {session_id[:8]} {args.model} {args.thinking} {input_variant} {prompt_variant}", file=sys.stderr)
            prompt = _build_input(session, input_variant)
            started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                summary, elapsed, stderr = _call_pi(
                    prompt,
                    model=args.model,
                    thinking=args.thinking,
                    system_prompt=system_prompt,
                    timeout=args.timeout,
                )
                result = {
                    "session_id": session_id,
                    "project": session["project"],
                    "msg_count": session["msg_count"],
                    "model": args.model,
                    "thinking": args.thinking,
                    "input_variant": input_variant,
                    "prompt_variant": prompt_variant,
                    "variant_key": _variant_key(args.model, input_variant, args.thinking, prompt_variant),
                    "prompt_chars": len(prompt),
                    "summary": summary,
                    "elapsed_s": round(elapsed, 3),
                    "started_at": started_at,
                }
                if stderr:
                    result["stderr_tail"] = stderr[-1000:]
            except Exception as e:  # keep benchmark resumable
                result = {
                    "session_id": session_id,
                    "project": session["project"],
                    "msg_count": session["msg_count"],
                    "model": args.model,
                    "thinking": args.thinking,
                    "input_variant": input_variant,
                    "prompt_variant": prompt_variant,
                    "variant_key": _variant_key(args.model, input_variant, args.thinking, prompt_variant),
                    "prompt_chars": len(prompt),
                    "error": str(e),
                    "elapsed_s": 0.0,
                    "started_at": started_at,
                }
            results.append(result)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Wrote {len(results)} results to {out_path}", file=sys.stderr)


def combine(args: argparse.Namespace) -> None:
    combined: list[dict[str, Any]] = []
    for path in args.inputs:
        combined.extend(json.loads(Path(path).read_text()))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    print(f"Wrote {len(combined)} combined results to {out}")


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _score_prompt(variant_results: list[dict[str, Any]], ground_truth: dict[str, Any]) -> str:
    items = []
    for item_index, result in enumerate(variant_results):
        sid = result["session_id"]
        gt = ground_truth[sid]
        items.append({
            "item_index": item_index,
            "session_id": sid,
            "project": gt.get("project"),
            "msg_count": gt.get("msg_count"),
            "ground_truth": {
                "key_topics": gt.get("key_topics"),
                "what_happened": gt.get("what_happened"),
                "key_decisions": gt.get("key_decisions"),
                "session_nature": gt.get("session_nature"),
            },
            "candidate_summary": result.get("summary", ""),
        })
    return json.dumps({
        "rubric": {
            "coverage": "1 misses most key decisions/work; 3 captures about 60%; 5 captures all key topics/decisions",
            "accuracy": "1 has multiple hallucinations; 3 minor inaccuracies/unsupported claims; 5 factually perfect against ground truth",
            "framing": "1 reads as generic project description; 3 acceptable; 5 clearly summarizes this session and distinguishes planning/research/debugging/implementation",
        },
        "instructions": [
            "Score each item independently from 1 to 5 for coverage, accuracy, and framing.",
            "Use integer or .5 scores only.",
            "Penalize vague summaries that omit searchable keywords, ticket IDs, components, files, or decisions in ground truth.",
            "Penalize unsupported claims even if plausible.",
            "Return one score for every item, preserving item_index and session_id exactly.",
            "Return JSON object exactly shaped as: {\"scores\":[{\"item_index\":0,\"session_id\":\"...\",\"coverage\":4,\"accuracy\":5,\"framing\":4,\"notes\":\"brief\"}]}",
        ],
        "items": items,
    }, ensure_ascii=False)


def score(args: argparse.Namespace) -> None:
    results = [r for r in json.loads(Path(args.input).read_text()) if not r.get("error")]
    ground_truth = _load_ground_truth()
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_variant[result["variant_key"]].append(result)

    all_scores: dict[str, Any] = {"judge_model": args.judge_model, "thinking": args.thinking, "variants": {}}
    for variant_key, variant_results in sorted(by_variant.items()):
        print(f"Scoring {variant_key} ({len(variant_results)} summaries)", file=sys.stderr)
        prompt = _score_prompt(variant_results, ground_truth)
        try:
            text, elapsed, _stderr = _call_pi(
                prompt,
                model=args.judge_model,
                thinking=args.thinking,
                system_prompt=JUDGE_SYSTEM_PROMPT,
                timeout=args.timeout,
            )
            parsed = _extract_json_object(text)
            raw_scores = parsed.get("scores", [])
            expected_by_index = {i: r["session_id"] for i, r in enumerate(variant_results)}
            normalized_by_index: dict[int, dict[str, Any]] = {}
            fallback_scores = []
            for raw in raw_scores:
                item = dict(raw)
                try:
                    item_index = int(item.get("item_index"))
                except (TypeError, ValueError):
                    item_index = None
                if item_index in expected_by_index:
                    item["item_index"] = item_index
                    item["session_id"] = expected_by_index[item_index]
                    # Keep the last score for a duplicated index; judges sometimes
                    # emit a self-correction after a malformed preliminary entry.
                    normalized_by_index[item_index] = item
                else:
                    fallback_scores.append(item)
            normalized_scores = [normalized_by_index[i] for i in sorted(normalized_by_index)] + fallback_scores
            all_scores["variants"][variant_key] = {
                "elapsed_s": round(elapsed, 3),
                "expected_count": len(variant_results),
                "score_count": len(normalized_scores),
                "scores": normalized_scores,
            }
        except Exception as e:
            all_scores["variants"][variant_key] = {"error": str(e), "scores": []}
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))
    print(f"Wrote scores to {args.output}", file=sys.stderr)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def report(args: argparse.Namespace) -> None:
    results = [r for r in json.loads(Path(args.input).read_text()) if not r.get("error")]
    scores_doc = json.loads(Path(args.scores).read_text()) if args.scores else {"variants": {}}
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_variant[result["variant_key"]].append(result)

    rows = []
    for variant_key, items in sorted(by_variant.items()):
        score_entries = scores_doc.get("variants", {}).get(variant_key, {}).get("scores", [])
        composite = []
        coverage = []
        accuracy = []
        framing = []
        expected_sessions = {item["session_id"] for item in items}
        deduped_scores: dict[int | str, dict[str, Any]] = {}
        for score_entry in score_entries:
            if score_entry.get("session_id") not in expected_sessions:
                continue
            key: int | str = score_entry.get("session_id")
            try:
                key = int(score_entry.get("item_index"))
            except (TypeError, ValueError):
                pass
            deduped_scores[key] = score_entry
        for score_entry in deduped_scores.values():
            try:
                c = float(score_entry["coverage"])
                a = float(score_entry["accuracy"])
                f = float(score_entry["framing"])
            except (KeyError, TypeError, ValueError):
                continue
            coverage.append(c)
            accuracy.append(a)
            framing.append(f)
            composite.append(c + a + f)
        rows.append({
            "variant_key": variant_key,
            "n": len(items),
            "avg_elapsed_s": round(_mean([float(i.get("elapsed_s", 0)) for i in items]), 2),
            "median_elapsed_s": round(sorted([float(i.get("elapsed_s", 0)) for i in items])[len(items) // 2], 2) if items else 0,
            "total_elapsed_s": round(sum(float(i.get("elapsed_s", 0)) for i in items), 2),
            "avg_prompt_chars": round(_mean([float(i.get("prompt_chars", 0)) for i in items])),
            "coverage": round(_mean(coverage), 2),
            "accuracy": round(_mean(accuracy), 2),
            "framing": round(_mean(framing), 2),
            "composite_15": round(_mean(composite), 2),
        })

    print(json.dumps(rows, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi GPT session summary benchmark")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate summaries with one Pi model")
    gen.add_argument("--model", required=True)
    gen.add_argument("--thinking", default="low")
    gen.add_argument("--inputs", default="current,rich")
    gen.add_argument("--prompt-variant", default="compact", choices=sorted(PROMPT_VARIANTS))
    gen.add_argument("--sessions")
    gen.add_argument("--output", required=True)
    gen.add_argument("--timeout", type=int, default=420)
    gen.add_argument("--resume", action="store_true")
    gen.set_defaults(func=generate)

    comb = sub.add_parser("combine", help="Combine generation JSON files")
    comb.add_argument("inputs", nargs="+")
    comb.add_argument("--output", required=True)
    comb.set_defaults(func=combine)

    sc = sub.add_parser("score", help="Score summaries with a GPT judge")
    sc.add_argument("--input", required=True)
    sc.add_argument("--output", required=True)
    sc.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    sc.add_argument("--thinking", default="low")
    sc.add_argument("--timeout", type=int, default=420)
    sc.set_defaults(func=score)

    rep = sub.add_parser("report", help="Print aggregate speed/quality report")
    rep.add_argument("--input", required=True)
    rep.add_argument("--scores")
    rep.set_defaults(func=report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
