# Session Index — Project Conventions

## Runtime
- Python 3.11+, stdlib only (no pip dependencies)
- Run scripts with `uv run` (not `python3`)
- Summaries use headless Pi by default (`openai-codex/gpt-5.4-mini`, low thinking); `client.py` is legacy Ollama fallback

## Architecture
- **Hooks never block:** All hooks exit 0, wrap everything in try/except, self-imposed timeouts
- **Message threshold:** Sessions need at least 1 user + 1 assistant message to be indexed
- **WAL mode:** SQLite uses WAL journal mode for concurrent read/write safety from hooks
- **Detached worker:** SessionEnd forks a detached subprocess so the Pi/GPT summary can complete after the hook's ~1.5s timeout

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

## Local utilities
- `clean_pi_transcript.py`: local-only helper for turning a raw Pi JSONL transcript into readable Markdown with user messages, assistant messages, assistant thinking blocks, and tool-call names/targets while omitting tool results. Run directly when needed: `uv run clean_pi_transcript.py /path/to/session.jsonl`. This is intentionally not exposed through the project CLI or any skill.

## Skill maintenance
- `skills/session-search/` is the agent-facing interface for this project. When adding or changing CLI user-facing commands/options, update `skills/session-search/SKILL.md` and add/update thin wrappers in `skills/session-search/scripts/` as needed.
- Skill scripts should not duplicate CLI logic. They should resolve the repo root, import the relevant `cli.py` command function, parse only the skill entrypoint arguments, and delegate.
- Installed Claude/Pi skill paths are normally symlinks to this repo, so source changes are picked up without reinstall unless the install layout changes.

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
- **Production winner:** `openai-codex/gpt-5.4-mini` with low thinking, rich transcript input, and compact prompt: 13.47/15.
- **Quality ceiling tested:** `openai-codex/gpt-5.5` with rich input: ~13.9/15 but roughly 2x slower.
- Legacy local benchmarks remain in `tests/benchmark.py`; Pi/GPT benchmarks use `tests/pi_gpt_benchmark.py`.
- See `tests/eval_results/LEARNINGS.md`, `pi_gpt_benchmark_report.md`, and `pi_gpt_prompt_benchmark_report.md` for findings.

### Constraint: Ollama single-model
Ollama still serves one model at a time for local fallback/tab-title workflows. Production summarization bypasses Ollama by default through Pi, so do not optimize summary quality by swapping local models.

## Summarization context
See [SUMMARIZATION.md](SUMMARIZATION.md) for constraints, quality baselines, and next steps.
