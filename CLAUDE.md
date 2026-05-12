# Session Index — Project Conventions

## Runtime
- Python 3.11+, stdlib only (no pip dependencies)
- Run scripts with `uv run` (not `python3`)
- Ollama client uses `gemma4:e4b` model — change in `client.py` if needed

## Architecture
- **Hooks never block:** All hooks exit 0, wrap everything in try/except, self-imposed timeouts
- **Message threshold:** Sessions need at least 1 user + 1 assistant message to be indexed
- **WAL mode:** SQLite uses WAL journal mode for concurrent read/write safety from hooks
- **Detached worker:** SessionEnd forks a detached subprocess so the LLM summary can complete after the hook's ~1.5s timeout. Response time is implicitly bounded by the 8192-token context window in `client.py`

## Data locations
- **Database:** `~/.session-index/sessions.db`
- **Transcripts:** `~/.session-index/transcripts/{session_id}.md`
- **Logs:** `~/.session-index/logs/session-index.log` (monthly rotation)
- **Source JSONL:** `~/.claude/projects/{encoded_path}/{session_id}.jsonl`

## Log format
```
HH:MM:SS.mmm [sid_6] hook_name          | message
```

## Testing
```bash
cd /path/to/session-index
uv run --with pytest -m pytest tests/
```

## Benchmarking

### Overview
Summary quality is evaluated against 19 ground-truth sessions in `tests/eval_results/ground_truth.json` (5 short, 5 medium, 9 long). Each has manual annotations: `key_topics`, `what_happened`, `key_decisions`, `session_nature`.

### Running benchmarks

The harness (`tests/benchmark.py`) supports two modes:

**Prompt mode** — test system prompt variants with fixed Config D settings:
```bash
uv run tests/benchmark.py \
  --sessions b6752ab6,b4dcf951,97df64cc,138cd1ed,f2d5afac,f3502323,29b37e3b,edddf940,533998b1,62279197,dc72bdfd,15b6c537,b8a5f3fe,040e3def,9a52498e,83aa1ebd,41673df3,91a78691,324ce4be \
  --prompts A,B,C,D,E,F \
  --model gemma4:e4b \
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

### Scoring rubric (applied by Claude Opus during manual scoring)

| Dimension | 1 | 3 | 5 |
|-----------|---|---|---|
| **Coverage** | Misses most key decisions | ~60% of key topics | All key decisions captured |
| **Accuracy** | Multiple hallucinations | Minor inaccuracies | Factually perfect |
| **Framing** | Reads as project description | Acceptable summary | Clear session summary, distinguishes planning vs implementation |

### Established winners
- **Config D** is the settled input/output configuration: 2000-char first message, scaled tokens (200/300/400), local backend. Do not re-test configs unless changing the input pipeline.
- **Prompt Variant F** is the current best prompt for Gemma 4 (10.74/15). It combines goal-framing, verb list, 4 real examples, and recency-ordered instructions.
- See `tests/eval_results/LEARNINGS.md` for full findings and result file index.

### Constraint: Ollama single-model
Ollama serves one model at a time. The active model (`gemma4:e4b`) is shared with tab-title generation in the local-llm project. Switching models adds ~10-15s latency, so summarization must use whatever model is currently loaded.

## Summarization context
See [SUMMARIZATION.md](SUMMARIZATION.md) for constraints, quality baselines, and next steps.
