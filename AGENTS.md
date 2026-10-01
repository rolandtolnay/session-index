# Session Index — Agent Guide

Session Index records Claude Code, Pi, and Codex conversations as searchable sessions with Clean Transcripts, summaries, headlines, and extracted facts, then injects relevant recent sessions into new conversations. It is a single-user tool: the person running you owns the indexed data. Agents reach it through the `session-search` skill, the `cli.py` commands, and the `sessions` terminal browser. Domain terms are defined in `CONTEXT.md`; non-obvious decisions are recorded in `docs/adr/`.

## Working in this repo
- Run scripts with `uv run` (not `python3`). Python 3.11+; runtime dependencies stay minimal (`rapidfuzz` is the one allowed addition, see `docs/adr/0001-rapidfuzz-for-evidence-find.md`).
- `~/.session-index/` is the user's live data: `sessions.db` (SQLite, WAL mode), `transcripts/{session_id}.md`, `logs/session-index.log` (monthly rotation), and `refresh-jobs/{source}/{session-id}/`. Source JSONL lives in `~/.claude/projects/{encoded_path}/`, `~/.pi/agent/sessions/--<cwd>--/`, and `$CODEX_HOME/sessions/YYYY/MM/DD/`. Read any of it freely. Writing to it (backfills, `prune`, `manage` deletions, migrations, re-indexing) needs the user's go-ahead and a backup first: `sqlite3 ~/.session-index/sessions.db ".backup ~/.session-index/backups/sessions-<date>-<purpose>.db"`.
- Summaries, headlines, Substance Bands, and Pi/GPT benchmarks run through Pi on the user's `openai-codex` subscription, and the legacy benchmark uses local Ollama; neither is billed per call, so run them as the task needs. Anything billed per call, such as an API key or a paid tier, needs an estimated cost and the user's decision first.
- The tests use temporary directories and fixture databases, never the live data. Run them without asking: `uv run --with pytest -m pytest tests/` (about 20 seconds; terminal-rendering tests skip without `tmux`). Before reporting done, run the tests covering what you changed, and the full suite when hooks, the database, or indexing changed.

## Invariants
- **Hooks never block:** every hook exits 0, wraps its work in try/except, and sets its own timeout. Indexing and summaries run in detached per-session workers queued by Claude Stop/SessionEnd, Pi turn/shutdown, and Codex Stop, so no hook or extension event waits on them.
- **Message threshold:** a session is indexed once it has at least one user and one assistant message.
- **Summary refresh cadence:** the first qualifying snapshot summarizes immediately; later refreshes wait for 180 idle seconds or 10,000 new rendered conversation characters, with a 60-second cooldown on the content trigger.
- **Models:** summaries and Substance Bands share one headless Pi call (`openai-codex/gpt-5.6-luna`, medium thinking); headlines come from a separate call over the full transcript. `client.py` is the legacy Ollama fallback, and `gemma4:e2b` is the only local model it may use because Ollama serves one model at a time (see `SUMMARIZATION.md`).

## Log format
```
HH:MM:SS.mmm [sid_6] hook_name          | message
```

## Where to look
- `CONTEXT.md` before naming a new concept; `docs/adr/` before reopening a settled decision.
- `docs/debugging.md` when a hook, refresh, or context injection misbehaves.
- `docs/session-index-cli-onboarding.md` when answering a question about past sessions with `find`, `inspect`, or `query`.
- `SUMMARIZATION.md` for the production summarization configuration and its constraints; `docs/benchmarking.md` to evaluate a prompt or model change.
- `clean_pi_transcript.py` turns a raw Pi JSONL into readable Markdown (`uv run clean_pi_transcript.py <file>`). It is deliberately not exposed through the CLI or any skill.

## Skill maintenance
`skills/session-search/` is the agent-facing interface. When a CLI command or option changes, update `skills/session-search/SKILL.md` and the thin wrapper in `skills/session-search/scripts/`. Wrappers resolve the repo root, import the `cli.py` command function, parse only their own arguments, and delegate; CLI logic lives in `cli.py` only. Installed Claude/Codex/Pi skill paths are symlinks into this repo, so changes take effect without reinstalling.

## Session manager TUI (`manage_tui.py`)
The `sessions` alias opens `cli.py manage`, a dependency-free curses browser. The user plans to grow it with richer previews and more per-session actions, so new work should extend the structure below rather than add a parallel mechanism.

**Layout.** The header is the view controls: `/` search, `f` filters, `s` sort, each key followed by its current value, plus `c reset` only when something is set. The body is the session list and the preview, side by side at 112+ columns and stacked below that. The footer is a status line and an action bar. Panels (search, filters, sort, help) and the delete confirmation replace the whole screen so nothing competes with them.

**Show each fact once.** Header values are the only place view state appears. The list heading shows the position (`12 of 2,861`), so rows carry no numbers. The preview has a fixed identity block (headline, project, date · provider · ID, state) and a scrollable body (match excerpts, summary, Side Chats); in stacked mode it drops what the list row already shows, and `PREVIEWED_MATCH_FIELDS` keeps excerpts from repeating the headline or summary. The status line reports only outcomes the screen cannot show otherwise (hide/unhide, delete, errors, near-match fallback) and clears on the next key.

**Color roles.** Colors come from the user's Pi theme (`~/.pi/agent/themes/claude-code-dark.json`), mapped to the nearest xterm-256 colors in `PALETTE`, so the browser looks like the rest of their harness. Draw code names a role from `self.styles` rather than a curses color, so a palette change is one edit and `NO_COLOR` (bold/dim/reverse only) keeps the same hierarchy. State is also spelled out in text (`hidden`), so color never carries meaning alone.

| Role | Use for | Not for |
|------|---------|---------|
| `strong` | The one primary item in a region: preview headline, panel title, user-set header values | List titles (default weight), metadata |
| default (`0`) | Body text: list titles, summaries, status messages | — |
| `muted` | Metadata, captions, placeholders, default-valued controls, key-hint labels | Anything the user must act on |
| `accent` | Keys in hints, text inputs, the active picker tab | Content, IDs, decoration |
| `border` | Rules, the pane divider, panel frames | Text |
| `selected` | The focused row in the list or a picker: white on the dark selection background; inside it, `selected_accent` for the `›` marker and `selected_muted` for metadata | — |
| `state` | Non-default session state (currently `hidden`) | Errors |
| `danger` | Destructive actions and errors | Warnings about state |

**Conventions.** Key hints go through `draw_keys` (accent key, muted label) so every hint reads the same, and any key shown in a hint also has a `HELP_LINES` row. Clipping ends in an ellipsis (`put`, `ellipsized`) so a truncated project name cannot pass for a whole one. Indexed text is user data, never terminal markup: session fields go through `plain()` and `put` replaces any remaining control characters. The list is one continuous scroll over absolute positions (`index`, `top`); query chunks (`CHUNK_SIZE`) are a fetch detail the user never sees. ↑/↓ move one session, Shift+↑/↓ (and Option/Ctrl, ←/→) jump one screen of rows.

**Adding a session action.** Add it to `session_actions()` (the label can depend on session state), handle its key in `handle_key`, and add a `HELP_LINES` row; the action bar builds itself from `session_actions()`. An action that destroys data gets its own confirmation screen like delete, where only `y` proceeds and other keys do nothing.

**Checking a UI change.** Render it in tmux against a copy of the database (`sqlite3 ~/.session-index/sessions.db ".backup <scratch>/copy.db"`) at 150×40, 100×34, and 60×24, with and without `NO_COLOR`, and look for clipping, overlap, and lost hierarchy. `tests/test_manage_terminal.py` shows the harness.
