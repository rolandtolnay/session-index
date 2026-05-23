# Current Session Display PRD

## Problem Statement

When working in an active Pi session, the user often needs to copy the **Clean Transcript** path for the **Current Session**, and sometimes also needs the **Tool Log**, **Source Transcript**, or session IDs. The existing `current` CLI can resolve this metadata deterministically, but using it requires leaving the Pi TUI or asking the model to run shell commands. The user wants an on-demand display inside Pi that is user-visible only, copyable, and not forwarded to the model.

## Solution

Add a `/current-session` slash command to the Session Index Pi integration. The command opens a focused, read-only input-replacement UI, similar in interaction model to the existing question/prune focused UIs. It displays the Current Session’s absolute artifact and source paths plus canonical/native IDs, and can be dismissed with Enter, Esc, or `q`.

The display is transient: it is not persisted in the Pi session, does not append chat history, does not trigger an agent turn, and does not enter model context. The original v1 behavior was display-only and did not trigger indexing, backfill, transcript generation, or fallback guessing. That no-indexing constraint is superseded by `etc/prd/current-session-manual-indexing.md`, which adds an explicit focused-only `Ctrl+R` Manual Current Session Indexing action while preserving the no-guessing and model-invisible constraints.

## User Stories

1. As a Pi user, I want to run `/current-session`, so that I can deterministically see metadata for the active Current Session without involving the model.
2. As a Pi user, I want to copy the absolute Clean Transcript path, so that I can reference or open the generated transcript artifact from outside Pi.
3. As a Pi user, I want to see the Tool Log path and whether it exists, so that I can quickly locate detailed tool-call records when debugging.
4. As a Pi user, I want to see the Source Transcript path and whether it exists, so that I can fall back to the raw provider log when generated artifacts are missing.
5. As a Pi user, I want to see both the Canonical Session ID and native Pi session ID, so that I can use the right identifier for Session Index commands or raw Pi session matching.
6. As a Pi user, I want the display to be dismissible with Enter, Esc, or `q`, so that I can return to normal prompting after copying what I need.
7. As a Pi user, I want a clear user-only error if Current Session metadata cannot be resolved, so that the system does not silently guess the wrong session.

## Implementation Decisions

- Build a `/current-session` extension command in the Session Index Pi integration.
- Resolve metadata from the existing Current Session contract rather than inventing a new resolver.
- Refresh Session Index runtime environment from Pi’s session manager before resolving metadata.
- Treat the Python `current --json` behavior as the source of truth for validation, path derivation, and error semantics.
- Display these fields by default and only these fields in v1:
  - Clean Transcript path with existence status
  - Tool Log path with existence status
  - Canonical Session ID
  - native Pi session ID
  - Source Transcript path with existence status
- Render paths as absolute paths for reliable copying.
- Use a focused read-only TUI component that replaces the editor/input area until dismissed.
- Dismiss the focused display with Enter, Esc, or `q`.
- Keep the display transient and user-only: no `pi.sendMessage`, no custom message entry, no chat history persistence, no model context mutation.
- Do not implement verbose mode in v1.
- Do not show Leaf ID, CWD, resolver method, raw JSON, or subagent hints in v1.
- Superseded by `etc/prd/current-session-manual-indexing.md`: `/current-session` itself remains a user-only display, but focused `Ctrl+R` now explicitly triggers full Manual Current Session Indexing for the current snapshot.
- If metadata is unavailable or inconsistent, show a clear focused user-only error and do not guess from latest session, focused terminal, database, or raw session files.

## Testing Decisions

- Add focused tests for the pure display formatter.
- Formatter tests should assert external behavior, not implementation details:
  - field ordering
  - inclusion of only the agreed default fields
  - absolute path rendering
  - existence status labels for Clean Transcript, Tool Log, and Source Transcript
  - readable error formatting for unavailable metadata
- Existing current-session tests already cover the resolver and CLI JSON contract; do not duplicate those at the formatter level.
- Existing Pi extension env tests already cover Session Index env construction; extend only if the command orchestration introduces new env-overlay behavior worth testing.
- Focused UI key handling can be smoke-tested manually in Pi for v1 unless the component is easy to isolate without heavy TUI mocking.

## Out of Scope

- Persisted chat messages or commit-style custom chat renderers.
- Any information being sent to the model.
- Automatic transcript, tool-log, backfill, or indexing work. Explicit focused Manual Current Session Indexing via `Ctrl+R` is covered by `etc/prd/current-session-manual-indexing.md`.
- Verbose/debug display mode.
- Leaf ID, CWD, resolver method, or raw JSON display.
- Subagent transcript listing or subagent run display.
- Database-backed session metadata such as project, branch, model, dates, files touched, or indexed tool summaries.
- Clipboard integration or automatic path copying.

## Further Notes

The glossary term **Current Session Display** refers to this user-visible presentation of Current Session identity and artifact locations. The feature should preserve the existing Current Session invariant: metadata comes from the active runtime process, and missing identity must fail clearly rather than falling back to a guessed session.
