# Summarization — Context & Constraints

## Ollama Single-Model Constraint

Ollama serves one model at a time. `gemma4:e2b` is the only supported local Ollama model for fallback/tab-title workflows. Swapping models adds latency and, with `keep_alive: -1`, can leave multiple model runners resident in RAM.

Tab titles and Pi Bash Summary run frequently, so E2B stays hot. Session summarization accepts the local fallback quality trade-off because production summaries bypass Ollama by default.

Any new summarization approach must either use `gemma4:e2b` or bypass Ollama entirely (e.g., Pi-based approach). Never assume a second local model can be loaded without latency or RAM penalty.

## Current Quality

Production summarization now uses headless Pi print mode with `openai-codex/gpt-5.4-mini`, low thinking, the compact GPT prompt, and rich transcript input. The Pi call disables sessions, tools, extensions, skills, prompt templates, and context files so summarization does not create recursive index entries or load unrelated project context.

After a summary succeeds, a second isolated headless Pi process uses fixed `openai-codex/gpt-5.4-mini` with low thinking to compress that summary into a Session Headline. Headlines target 8-15 words with a hard 15-word limit, preserve distinguishing identifiers/components/outcomes, and omit project, branch, and date metadata because recent-context formatting appends those deterministically. A headline failure does not invalidate the summary and preserves any prior headline.

## Active-session refresh lifecycle

Claude, Pi, and Codex share a detached per-session refresh coordinator. The first snapshot with at least one user and one assistant message writes deterministic artifacts and immediately attempts a Session Summary and Session Headline. Every later assistant-turn event refreshes deterministic artifacts immediately. Descriptions regenerate after either 180 seconds without a newer assistant turn or 10,000 newly rendered user/assistant characters since the last successful summary; content-trigger attempts have a 60-second cooldown. Failed summaries preserve prior descriptions and do not advance the successful-summary content watermark. Claude SessionEnd and Pi shutdown force a final refresh; Codex has no distinct exit event and therefore finalizes through its idle trigger.

Benchmark result on the 19-session ground-truth set: **13.47/15** composite for `gpt-5.4-mini + rich + compact prompt`.

Historical baselines:
- gemma4:e4b + Variant F prompt: **10.74/15**
- qwen3.5:4b + improved prompt: **12.05/15**
- gpt-5.5 + rich input: ~**13.9/15**, but roughly 2x slower than gpt-5.4-mini

## Decision: Decouple Summarization from Ollama

Gemma 4 E2B stays loaded for local hook workflows. Summarization bypasses Ollama entirely by default, avoiding the single-model constraint and local-model quality trade-offs.

If Pi is unavailable or disabled via `SESSION_INDEX_DISABLE_PI_SUMMARIZER`, summary generation falls back to the legacy Gemini/local path; that local path uses `gemma4:e2b`. Session Headline generation requires Pi and remains absent (or preserves its prior value) when the isolated Pi call fails.

Relevant benchmark artifacts:
- `tests/eval_results/pi_gpt_benchmark_report.md`
- `tests/eval_results/pi_gpt_prompt_benchmark_report.md`
- `tests/eval_results/LEARNINGS.md`
