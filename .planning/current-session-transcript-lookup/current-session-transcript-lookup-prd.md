# Current Session Transcript Lookup PRD

## Problem Statement

Agents working inside an active conversation need a reliable way to answer: “what session am I in, and where will Session Index store the clean markdown transcript for this conversation?” Today, an agent can search the index after the fact, but cannot reliably identify its own Current Session or the deterministic Clean Transcript path while the conversation is still running.

This is especially important when multiple agent sessions run in parallel. Guessing from the latest session, current project, or focused terminal is fragile and can point to the wrong conversation. The feature must be exact for the active agent/runtime process and honest when that exact runtime identity is unavailable.

## Solution

Add a `current` CLI command backed by a small resolver that reads a Session Index-owned environment contract from the active agent runtime. The resolver returns the Canonical Session ID, provider-native ID, source, Source Transcript path, deterministic Clean Transcript path, deterministic Tool Log path, file existence booleans, resolution method, and optional provider-specific metadata such as Pi `leaf_id`.

The command does not require the session to already exist in the database. It derives artifact paths from the Canonical Session ID so it can answer before full indexing has completed. If the required runtime environment is not present, it fails clearly instead of guessing.

## User Stories

1. As an agent in an active Pi session, I want to run `current` and get the Canonical Session ID, so that I can refer to the eventual database row and artifact filenames for this conversation.
2. As an agent in an active session, I want to run `current --path` and get the Clean Transcript path, so that I can tell the user exactly where the generated markdown transcript will be stored.
3. As an agent in an active session, I want to run `current --json` and get IDs, source path, artifact paths, existence booleans, resolution, and Pi leaf metadata when available, so that downstream tools can consume the current-session identity without parsing human output.
4. As an agent in an active session before indexing completes, I want `current` to work without a database row, so that the answer is available during new or still-running conversations.
5. As a caller outside an agent/runtime process, I want `current` to fail clearly when no runtime identity is available, so that I do not receive a misleading latest-session or terminal-based guess.
6. As a Claude caller where only Claude’s native session environment is available, I want the resolver to use it when sufficient, so that Claude support remains env-only without introducing registry state.

## Implementation Decisions

- Add a deep current-session resolver module with a small interface that reads environment variables, validates required fields, normalizes provider identity, derives deterministic artifact paths, and returns a structured result.
- The resolver owns Session Index’s v1 environment contract:
  - required: canonical session ID, native session ID, source, and Source Transcript path when available from the integration
  - optional: provider-specific metadata such as Pi `leaf_id`
- Prefer `SESSION_INDEX_*` variables as the public contract. Existing provider variables may be used only as compatibility inputs when they provide enough information to construct the same result.
- The Canonical Session ID is the primary ID in human output. Pi sessions keep the `pi:` namespace prefix; Claude sessions use the native UUID as the canonical ID.
- The CLI derives Clean Transcript and Tool Log paths from the Canonical Session ID using the existing artifact naming convention.
- The CLI does not look up or require a database row. File existence is reported by checking the derived artifact paths on disk.
- The v1 CLI surface is:
  - `current`: print Canonical Session ID
  - `current --path`: print Clean Transcript path
  - `current --native`: print provider-native session ID
  - `current --json`: print the full structured object
- The JSON field names follow existing Session Index terminology:
  - `source_path` means the raw provider Source Transcript
  - `transcript_path` means the generated Clean Transcript markdown artifact
  - `tool_log_path` means the generated Tool Log markdown artifact
- If required current-session env data is missing or inconsistent, the command exits non-zero with a clear message that `current` only works inside an active agent runtime that exposes Session Index env.
- No single global `current.json`, per-session registry, terminal mapping, or latest-by-project fallback is included in v1.
- Pi `leaf_id` is included in JSON when available as provider-specific metadata for tracing the active Pi branch, but it does not affect the session-level artifact paths.
- Subagent transcript paths are out of scope for v1; only main session artifacts are returned.

## Testing Decisions

- Tests should assert external behavior: given specific environment variables, the resolver and CLI return the expected IDs, paths, existence flags, and clear errors. Tests should not depend on database state or provider session directories.
- Add resolver unit tests for:
  - Pi canonical/native ID normalization
  - Claude canonical/native ID behavior
  - deterministic Clean Transcript and Tool Log path derivation
  - existence booleans for present and absent artifacts
  - optional Pi `leaf_id`
  - missing required env producing a clear failure
- Add CLI tests for:
  - default human output prints the Canonical Session ID
  - `--path` prints the Clean Transcript path
  - `--native` prints the provider-native ID
  - `--json` returns the full structured object
  - no runtime env exits non-zero with the intended message
- Prior art: existing transcript and tool-log tests redirect artifact directories with monkeypatching and assert deterministic file paths; the new tests should follow that style.
- No Pi extension or Claude hook tests are required for v1 unless implementation changes introduce testable logic beyond env wiring.

## Out of Scope

- Manual shell lookup from the same terminal or Ghostty tab.
- Terminal ID ownership mapping or handoff logic.
- A global current-session pointer file.
- A per-session runtime registry.
- Latest-session fallback by current project or cwd.
- Requiring or querying the database for `current`.
- Listing subagent transcript artifacts.
- Changing existing transcript or tool-log file naming conventions.
- Redacting existing Tool Logs, though future Tool Log redaction remains a separate concern.

## Further Notes

The v1 design is intentionally narrow: exact inside the active agent/runtime process, explicit failure everywhere else. This keeps parallel sessions safe because each runtime process carries its own current-session environment. Broader lookup modes can be added later as separate features if manual terminal or post-session workflows become important.
