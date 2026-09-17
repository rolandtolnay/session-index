"""Stable Session Index identity, separate from provider-native identity."""

from __future__ import annotations

import hashlib
import re

PREFIXES = {"claude": "cc", "pi": "pi", "codex": "codex"}
HASH_HEX_LENGTH = 16
_SHORT_ID = re.compile(rf"^(cc|pi|codex):[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
PREVIOUS_SHORT_ID = r"(?:cc|pi|codex):[0-9a-f]{12}"


def canonical_session_id(source: str, native_session_id: str) -> str:
    """Hash an explicit native ID; never infer native identity from a short ID."""
    prefix = PREFIXES[source]
    native = native_session_id.strip()
    if not native or ":" in native or "/" in native or "\\" in native:
        raise ValueError("Expected an unprefixed provider-native session ID")
    digest = hashlib.sha256(f"{source}:{native}".encode("utf-8")).hexdigest()[:HASH_HEX_LENGTH]
    return f"{prefix}:{digest}"


def is_canonical_session_id(value: str, source: str | None = None) -> bool:
    return bool(_SHORT_ID.fullmatch(value)) and (
        source is None or value.startswith(PREFIXES[source] + ":")
    )


def refresh_session_id(source: str, value: str) -> str:
    """Queue adapters accept explicit native IDs or already canonical IDs."""
    if is_canonical_session_id(value, source):
        return value
    if re.fullmatch(PREVIOUS_SHORT_ID, value):
        raise ValueError("A 12-hex ID is not a native identity; refresh from the provider-native ID")
    # Provider hooks / older runtime processes may supply namespaced native IDs.
    # This is an ingestion adapter, not an alias exposed by session lookup.
    prefix = "claude:" if source == "claude" else f"{source}:"
    return canonical_session_id(source, value.removeprefix(prefix))
