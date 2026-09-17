# Sixteen hexadecimal characters for canonical session IDs

Supersedes the 12-character hash length in [ADR 0002](0002-short-canonical-session-ids.md). Canonical IDs retain the same provider prefixes and SHA-256 input but use 16 hexadecimal characters (64 bits): four extra characters lower the approximate probability of any collision among one million sessions in one provider namespace from 0.1775% to 0.000002711%. Native identities and raw provider artifacts remain unchanged.

Expanding a truncated hash requires its native identity, so the offline migration saves a 12-to-16 mapping in `reference-ids.json` for future generated-text normalization. Native identities come from indexed records and prior migration backups; unknown artifact ownership fails closed instead of guessing. This map is not a lookup-alias registry, and no compatibility symlinks are created.
