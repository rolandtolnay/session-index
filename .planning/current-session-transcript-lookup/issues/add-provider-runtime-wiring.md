## What to build

Add provider compatibility and runtime environment wiring for the `current` resolver where this repository owns the integration. Active Pi sessions should expose the Session Index `SESSION_INDEX_*` contract, including optional leaf metadata. Claude support should remain env-only by accepting existing Claude-native environment data only when it is sufficient to construct the same exact result. Do not introduce registry state, latest-session fallback, terminal mapping, or database lookup.

## Acceptance criteria

- [ ] Pi runtime integration exports the required `SESSION_INDEX_*` variables for the active main session when Session Index launches or indexes runtime work.
- [ ] Pi `leaf_id` is included as optional metadata when available and does not change session-level artifact paths.
- [ ] Claude-compatible environment input resolves when it provides sufficient native session identity and source transcript path.
- [ ] Compatibility inputs normalize to the same structured resolver result as the public `SESSION_INDEX_*` contract.
- [ ] Insufficient compatibility input fails clearly instead of guessing.
- [ ] Tests cover Pi runtime/env wiring logic where practical without requiring a live Pi runtime, and resolver tests cover Claude compatibility inputs.

## Blocked by

- add-current-command.md
