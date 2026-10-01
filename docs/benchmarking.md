# Benchmarking summaries and headlines

Use this when evaluating a prompt, model, or input-format change. The production configuration, its constraints, and current quality baselines are in [SUMMARIZATION.md](../SUMMARIZATION.md).

## Overview
Summary quality is evaluated against 19 ground-truth sessions in `tests/eval_results/ground_truth.json` (5 short, 5 medium, 9 long). Each has manual annotations: `key_topics`, `what_happened`, `key_decisions`, `session_nature`.

## Running benchmarks

Benchmarks call paid models; agree the run size and cost with the user before starting anything beyond a smoke test.

The legacy local harness (`tests/benchmark.py`) supports two modes:

**Prompt mode** — test system prompt variants with fixed Config D settings:
```bash
uv run tests/benchmark.py \
  --sessions b6752ab6,b4dcf951,97df64cc,138cd1ed,f2d5afac,f3502323,29b37e3b,edddf940,533998b1,62279197,dc72bdfd,15b6c537,b8a5f3fe,040e3def,9a52498e,83aa1ebd,41673df3,91a78691,324ce4be \
  --prompts A,B,C,D,E,F \
  --model gemma4:e2b \
  --output tests/eval_results/my_results.json
```

**Config mode** — test input/output settings (first_msg_budget, token scaling, backend):
```bash
uv run tests/benchmark.py \
  --sessions <ids> \
  --configs A,B,C,D,E,F \
  --model qwen3.5:4b \
  --output tests/eval_results/my_results.json
```

Use `--select-sessions` to list available sessions by bucket.

Pi/GPT benchmarks use `tests/pi_gpt_benchmark.py` (summaries via `generate`, headlines via `generate-headlines`).

## Scoring rubric (applied by Claude Opus during manual scoring)

| Dimension | 1 | 3 | 5 |
|-----------|---|---|---|
| **Coverage** | Misses most key decisions | ~60% of key topics | All key decisions captured |
| **Accuracy** | Multiple hallucinations | Minor inaccuracies | Factually perfect |
| **Framing** | Reads as project description | Acceptable summary | Clear session summary, distinguishes planning vs implementation |

## Established winners
- **Production winner (August 2026):** `openai-codex/gpt-5.6-luna` with medium thinking, rich transcript input, and compact prompt — best summary and headline composites in the blind-judged luna/terra eval; transcript-based headlines beat the old summary-based design at equal model. See `tests/eval_results/luna_terra_2026_08/report.md`.
- **Prior winner:** `openai-codex/gpt-5.4-mini` with low thinking, rich transcript input, and compact prompt: 13.47/15 (GPT judges; not comparable to the Opus-judged 2026-08 scores).
- **Quality ceiling tested:** `openai-codex/gpt-5.5` with rich input: ~13.9/15 but roughly 2x slower.
- See `tests/eval_results/LEARNINGS.md`, `pi_gpt_benchmark_report.md`, and `pi_gpt_prompt_benchmark_report.md` for findings.

## Constraint: Ollama single-model
Ollama still serves one model at a time for local fallback/tab-title workflows. `gemma4:e2b` is the only supported local fallback model. Production summarization bypasses Ollama by default through Pi, so do not optimize summary quality by swapping local models.
