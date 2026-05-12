# Summarization — Context & Constraints

## Ollama Single-Model Constraint

Ollama serves one model at a time. Gemma 4 E4B is loaded for tab-title generation (local-llm project) where it's 2.8x faster than Qwen with equal quality. Swapping models adds ~10-15s latency per call, making dual-model usage impractical for hooks.

Tab titles run more frequently than summaries, so the model that wins on tab titles stays hot. Summarization accepts the quality trade-off.

Any new summarization approach must either use gemma4:e4b or bypass Ollama entirely (e.g., Pi-based approach). Never assume a second local model can be loaded without latency penalty.

## Current Quality

Production summarization now uses headless Pi print mode with `openai-codex/gpt-5.4-mini`, low thinking, the compact GPT prompt, and rich transcript input. The Pi call disables sessions, tools, extensions, skills, prompt templates, and context files so summarization does not create recursive index entries or load unrelated project context.

Benchmark result on the 19-session ground-truth set: **13.47/15** composite for `gpt-5.4-mini + rich + compact prompt`.

Historical baselines:
- gemma4:e4b + Variant F prompt: **10.74/15**
- qwen3.5:4b + improved prompt: **12.05/15**
- gpt-5.5 + rich input: ~**13.9/15**, but roughly 2x slower than gpt-5.4-mini

## Decision: Decouple Summarization from Ollama

Gemma 4 E4B stays loaded for tab-title generation (local-llm project). Summarization bypasses Ollama entirely by default, avoiding the single-model constraint and the quality regression vs Qwen.

If Pi is unavailable or disabled via `SESSION_INDEX_DISABLE_PI_SUMMARIZER`, the code falls back to the legacy Gemini/local path.

Relevant benchmark artifacts:
- `tests/eval_results/pi_gpt_benchmark_report.md`
- `tests/eval_results/pi_gpt_prompt_benchmark_report.md`
- `tests/eval_results/LEARNINGS.md`
