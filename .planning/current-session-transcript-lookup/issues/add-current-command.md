## What to build

Add the core `current` command for the Session Index-owned environment contract. From active runtime environment variables, resolve the current session identity exactly, derive deterministic artifact paths, and expose the full v1 CLI surface: default canonical ID output, `--path`, `--native`, and `--json`. When the required environment is missing or inconsistent, fail clearly without consulting the database, latest sessions, terminal state, or any registry.

## Acceptance criteria

- [ ] `current` prints the Canonical Session ID from `SESSION_INDEX_*` environment data.
- [ ] `current --path` prints the deterministic Clean Transcript markdown path for that Canonical Session ID.
- [ ] `current --native` prints the provider-native session ID.
- [ ] `current --json` returns IDs, source, Source Transcript path, Clean Transcript path, Tool Log path, artifact existence booleans, resolution method, and optional Pi `leaf_id` when present.
- [ ] Pi canonical/native ID normalization preserves the `pi:` namespace for canonical IDs while native IDs remain unprefixed.
- [ ] Derived Clean Transcript and Tool Log paths follow existing artifact naming conventions and do not require a database row.
- [ ] Missing or inconsistent required env exits non-zero with a clear message that `current` only works inside an active agent runtime exposing Session Index env.
- [ ] Resolver and CLI tests cover the accepted env contract, path derivation, existence booleans, JSON output, and no-env failure.

## Blocked by

None - can start immediately
