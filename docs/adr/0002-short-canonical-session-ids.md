# Short canonical identity, preserved native identity

Superseded for hash length by [ADR 0003](0003-sixteen-hex-session-ids.md); native-identity and clean-break boundaries remain unchanged.

Canonical Session IDs use `cc:`, `pi:`, or `codex:` plus the first 12 lowercase hexadecimal characters of SHA-256 over UTF-8 `source:native_session_id` (`source` remains `claude`, `pi`, or `codex`). Hashing rather than truncating provider UUIDs avoids the timestamp-prefix collisions already present in Pi and Codex UUIDv7 sessions, while retaining deterministic, database-independent artifact paths. Native IDs remain explicit and unchanged; collisions fail closed rather than merging identities.

The cutover migrates indexed records and generated artifacts without rebuilding from potentially deleted provider logs. Recognized old artifact paths and Inspection References are normalized in generated text, including quoted history, both during migration and future rendering; native IDs and provider-owned paths are preserved. Legacy canonical-ID aliases and old-path symlinks are deliberately omitted: generated artifacts are normalized copies, and raw history or external references may retain obsolete pointers.
