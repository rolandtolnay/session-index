# Summarization — Context & Constraints

## Ollama Single-Model Constraint

Ollama serves one model at a time. Gemma 4 E4B is loaded for tab-title generation (local-llm project) where it's 2.8x faster than Qwen with equal quality. Swapping models adds ~10-15s latency per call, making dual-model usage impractical for hooks.

Tab titles run more frequently than summaries, so the model that wins on tab titles stays hot. Summarization accepts the quality trade-off.

Any new summarization approach must either use gemma4:e4b or bypass Ollama entirely (e.g., Pi-based approach). Never assume a second local model can be loaded without latency penalty.

## Current Quality

Current summarization with gemma4:e4b + Variant F prompt: **10.74/15** composite.
This is a known regression from qwen3.5:4b (**12.05/15**) accepted due to the single-model constraint.

Qwen produces more specific summaries (ticket numbers, component names, technical details) but can't be used while Gemma 4 is serving tab titles.

## Decision: Decouple Summarization from Ollama

Gemma 4 E4B stays loaded for tab-title generation (local-llm project). Summarization will use a separate approach that bypasses Ollama entirely, avoiding the single-model constraint and the quality regression vs Qwen.

The benchmark harness and ground truth set (19 sessions) are ready for evaluating any new approach — see `tests/eval_results/LEARNINGS.md`.
