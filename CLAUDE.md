# Session Index — Project Conventions

## Runtime
- Python 3.11+, stdlib only (no pip dependencies)
- Run scripts with `uv run` (not `python3`)
- Ollama client uses `qwen3.5:4b` model — change in `client.py` if needed

## Architecture
- **Hooks never block:** All hooks exit 0, wrap everything in try/except, self-imposed timeouts
- **3-message threshold:** Sessions with < 3 user messages are skipped (too short to index)
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
