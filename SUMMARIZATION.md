# Summarization — Context & Constraints

## Ollama Single-Model Constraint

Ollama serves one model at a time. `gemma4:e2b` is the only supported local Ollama model for fallback/tab-title workflows. Swapping models adds latency and, with `keep_alive: -1`, can leave multiple model runners resident in RAM.

Tab titles and Pi Bash Summary run frequently, so E2B stays hot. Session summarization accepts the local fallback quality trade-off because production summaries bypass Ollama by default.

Any new summarization approach must either use `gemma4:e2b` or bypass Ollama entirely (e.g., Pi-based approach). Never assume a second local model can be loaded without latency or RAM penalty.

## Current Quality

Production summarization uses headless Pi print mode with `openai-codex/gpt-5.6-luna`, medium thinking, rich transcript input, and the compact GPT summary prompt augmented with Substance Band classification. The same call returns a structured summary, band (`substantial`, `useful`, `low_value`), and one-sentence evidence reason. Bands assess future reference value, not length, effort, recency, or project importance; malformed or missing assessments preserve the prior band and otherwise remain unknown. The Pi call disables sessions, tools, extensions, skills, prompt templates, and context files so summarization does not create recursive index entries or load unrelated project context.

A second isolated headless Pi process (same model and thinking) generates the Session Headline directly from the same rich transcript input, independently of the summary. Headlines target 8-15 words with a hard 15-word limit, preserve distinguishing identifiers/components/outcomes, and omit project, branch, and date metadata because recent-context formatting appends those deterministically. Summary and headline failures are independent: each preserves its own prior value without invalidating the other.

## Active-session refresh lifecycle

Claude, Pi, and Codex share a detached per-session refresh coordinator. The first snapshot with at least one user and one assistant message writes deterministic artifacts and immediately attempts a Session Summary and Session Headline. Every later assistant-turn event refreshes deterministic artifacts immediately. Descriptions regenerate after either 180 seconds without a newer assistant turn or 10,000 newly rendered user/assistant characters since the last successful summary; content-trigger attempts have a 60-second cooldown. Failed summaries preserve prior descriptions and do not advance the successful-summary content watermark. Claude SessionEnd and Pi shutdown force a final refresh; Codex has no distinct exit event and therefore finalizes through its idle trigger.

A 12-real-session pilot plus three synthetic controls compared joint summary/classification with a separate Luna judge. Both matched all first-pass reference labels; joint labels were stable on three of four repeated cases versus four of four for the dedicated judge. Blind real-session summary scores were 14.75/15 joint versus 14.29/15 summary-only. Joint generation avoids another full-transcript inference; adjacent-band drift remains a known limitation. See `tests/eval_results/substance_comparison/report.md` (local evaluation artifacts).

Benchmark result on the 19-session ground-truth set (August 2026, blind Claude Opus judges — not comparable to earlier GPT-judged scores): **13.13/15** summary and **13.08/15** headline composite for `gpt-5.6-luna medium + rich + compact prompt`, vs 12.61 and 12.21 for the prior `gpt-5.4-mini` production configuration. Transcript-based headlines beat summary-based ones at equal model. See `tests/eval_results/luna_terra_2026_08/report.md`.

Historical baselines (GPT judges, earlier rounds):
- gemma4:e4b + Variant F prompt: **10.74/15**
- qwen3.5:4b + improved prompt: **12.05/15**
- gpt-5.4-mini + rich + compact prompt: **13.47/15**
- gpt-5.5 + rich input: ~**13.9/15**, but roughly 2x slower than gpt-5.4-mini

## Decision: Decouple Summarization from Ollama

Gemma 4 E2B stays loaded for local hook workflows. Summarization bypasses Ollama entirely by default, avoiding the single-model constraint and local-model quality trade-offs.

If Pi is unavailable or disabled via `SESSION_INDEX_DISABLE_PI_SUMMARIZER`, summary generation falls back to the legacy Gemini/local path; that local path uses `gemma4:e2b`. Session Headline and Substance Band generation require Pi and remain absent (or preserve their prior values) when their generation fails. The legacy summary fallback does not fabricate a classification.

For eligible headlined sessions in the past seven days, `uv run backfill_substance.py` previews missing assessments; `uv run backfill_substance.py --apply` fills them using a dedicated Luna classification call over existing Clean Transcripts. This resumable migration leaves summaries, headlines, and transcript artifacts untouched, atomically preserves concurrent classifications, and skips observed metadata/transcript changes. Filesystem validation is best-effort: a concurrent turn may leave an older assessment until the next successful summary refresh, as with normal indexing. Normal refreshes always use the joint call.

Relevant benchmark artifacts:
- `tests/eval_results/pi_gpt_benchmark_report.md`
- `tests/eval_results/pi_gpt_prompt_benchmark_report.md`
- `tests/eval_results/LEARNINGS.md`
