"""Normalize legacy Session Index references without rewriting provider identity.

Only recognizable artifact names/paths, slash Inspection References, and generated
identity headers are rewritten. A bare UUID (including resume arguments and native
metadata) is not a Canonical Session ID without that context.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from session_identity import PREVIOUS_SHORT_ID, canonical_session_id, is_canonical_session_id

REFERENCE_IDS_FILE = "reference-ids.json"


@lru_cache(maxsize=4)
def _read_reference_ids(path: str, mtime_ns: int, size: int) -> dict[str, str]:
    mapping = json.loads(Path(path).read_text())
    if not isinstance(mapping, dict) or any(
        not re.fullmatch(PREVIOUS_SHORT_ID, old) or not is_canonical_session_id(new)
        or not new.startswith(old) for old, new in mapping.items()
    ):
        raise ValueError("Invalid historical artifact reference mapping")
    return mapping


def load_reference_ids(root: Path | None = None) -> dict[str, str]:
    if root is None:
        from db import DB_PATH

        root = Path(DB_PATH).parent
    path = root / REFERENCE_IDS_FILE
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    return dict(_read_reference_ids(str(path), stat.st_mtime_ns, stat.st_size))


UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NATIVE_LEGACY_ID = rf"(?:(?:pi|codex):)?{UUID}"
LEGACY_ID = rf"(?:{NATIVE_LEGACY_ID}|{PREVIOUS_SHORT_ID})"
_LEGACY = re.compile(rf"^{LEGACY_ID}$")
# Each match converts once. Full raw-source paths and arbitrary project paths
# are excluded; standalone .md names are part of injected recent context.
_REFERENCE = re.compile(
    rf"(?P<artifact>(?:\.session-index/transcripts/|(?<![\w/.:~-])transcripts/))"
    rf"(?P<path_id>{LEGACY_ID})(?=[/.]|$)"
    rf"|(?<![\w/.:~-])(?P<file_id>{LEGACY_ID})(?P<ext>\.tools\.md|\.md)(?![\w.-])"
    rf"|(?<![\w/])(?P<kind>session|tool|skill|question|subagent)/(?P<ref_id>{LEGACY_ID})(?![\w:-])"
    rf"|(?m:^(?P<header>Parent: |# Tool log — )(?P<header_id>{LEGACY_ID})$)"
)


def short_legacy_id(value: str, reference_ids: dict[str, str] | None = None) -> str:
    """Convert recognized artifact identity; unknown historical references stay literal."""
    if re.fullmatch(PREVIOUS_SHORT_ID, value):
        mapping = load_reference_ids() if reference_ids is None else reference_ids
        return mapping.get(value, value)
    if not _LEGACY.fullmatch(value):
        return value
    if ":" in value:
        source, native = value.split(":", 1)
    else:
        source, native = "claude", value
    return canonical_session_id(source, native)


def referenced_session_ids(text: str):
    for match in _REFERENCE.finditer(text):
        yield next(match[group] for group in ("path_id", "file_id", "ref_id", "header_id") if match[group])


def normalize_references(text: str, reference_ids: dict[str, str] | None = None) -> str:
    # Load once per text, only when a historical short ID needs expansion.
    if reference_ids is None and re.search(PREVIOUS_SHORT_ID + r"(?![0-9a-f])", text):
        reference_ids = load_reference_ids()
    def replace(match: re.Match[str]) -> str:
        groups = match.groupdict()
        if groups["path_id"]:
            return groups["artifact"] + short_legacy_id(groups["path_id"], reference_ids)
        if groups["file_id"]:
            return short_legacy_id(groups["file_id"], reference_ids) + groups["ext"]
        if groups["ref_id"]:
            return groups["kind"] + "/" + short_legacy_id(groups["ref_id"], reference_ids)
        return groups["header"] + short_legacy_id(groups["header_id"], reference_ids)

    return _REFERENCE.sub(replace, text)


# Text owned by Session Index: native metadata and source paths are deliberately
# absent. Used both for migration and future persistence to avoid drift.
TEXT_COLUMNS = {
    "sessions": ("user_messages", "files_touched", "summary", "headline", "substance_reason",
                 "transcript_path", "tool_log_path", "subagent_transcripts"),
    "skill_invocations": ("invocation_preview", "arguments", "subagent_transcript_path"),
    "file_mutations": ("path",),
    "subagent_runs": ("transcript_path", "task_preview"),
    "question_answers": ("header", "question", "selected_label"),
}


def normalize_row(table: str, row: dict) -> dict:
    return {key: normalize_references(value) if key in TEXT_COLUMNS.get(table, ())
            and isinstance(value, str) else value for key, value in row.items()}
