## What to build

Document the current-session lookup feature using existing Session Index terminology. Explain the `current` command, its flags, JSON fields, the Session Index-owned runtime environment contract, provider compatibility behavior, exact-failure semantics, and v1 non-goals.

## Acceptance criteria

- [ ] Documentation shows `current`, `current --path`, `current --native`, and `current --json` usage.
- [ ] Documentation defines `source_path` as the raw provider Source Transcript, `transcript_path` as the generated Clean Transcript artifact, and `tool_log_path` as the generated Tool Log artifact.
- [ ] Documentation lists required and optional `SESSION_INDEX_*` variables, including optional Pi `leaf_id` metadata.
- [ ] Documentation states that the command does not require a database row and derives artifact paths from the Canonical Session ID.
- [ ] Documentation states that missing runtime identity fails clearly and no latest-session, terminal, registry, or database fallback is used in v1.
- [ ] Documentation notes that subagent transcript paths are out of scope for v1.

## Blocked by

- add-provider-runtime-wiring.md
