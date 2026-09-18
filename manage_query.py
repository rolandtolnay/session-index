"""Deterministic retrieval for the interactive session manager."""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from rapidfuzz import fuzz, process

from db import top_level_session_predicate


@dataclass(frozen=True)
class ManageFilters:
    query: str = ""
    project: str | None = None
    source: str | None = None
    visibility: str = "all"
    since: str = ""
    until: str = ""
    substance: str | None = None


@dataclass
class ManagePage:
    sessions: list[dict]
    total: int
    approximate: bool = False


_SEARCH_FIELDS = (
    ("headline", "Headline", 120.0),
    ("summary", "Summary", 90.0),
    ("project", "Project", 65.0),
    ("session_id", "Session ID", 60.0),
    ("native_session_id", "Native ID", 55.0),
    ("files_touched", "Files", 45.0),
    ("user_messages", "User messages", 20.0),
)
_FIELD_LABELS = {name: label for name, label, _weight in _SEARCH_FIELDS}
_FIELD_WEIGHTS = {name: weight for name, _label, weight in _SEARCH_FIELDS}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FUZZY_MIN_TERM_LENGTH = 5
_FUZZY_THRESHOLD = 85.0
_MAX_FUZZY_TOKENS_PER_FIELD = 400
_MAX_FUZZY_FIELD_CHARS = 20_000
_VALID_VISIBILITIES = {"all", "visible", "hidden"}
_VALID_SUBSTANCES = {"substantial", "useful", "low_value", "unknown"}
_VALID_SORTS = {"auto", "newest", "oldest", "relevance", "substance"}


def _calendar_bound(name: str, value: str) -> date | None:
    if not value:
        return None
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"{name} must be a local calendar date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid calendar date: {value!r}") from exc


def _parse_started_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Naive source timestamps are interpreted as local time, matching the
        # manager's display behavior.
        parsed = parsed.astimezone()
    return parsed


def _local_started_date(value: Any) -> date | None:
    parsed = _parse_started_at(value)
    if parsed is None:
        return None
    return parsed.astimezone().date()


def _timestamp(value: Any) -> float | None:
    parsed = _parse_started_at(value)
    if parsed is None:
        return None
    try:
        return parsed.astimezone(timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _validate(filters: ManageFilters, sort: str, offset: int, limit: int) -> tuple[date | None, date | None]:
    if filters.visibility not in _VALID_VISIBILITIES:
        raise ValueError("visibility must be one of: all, visible, hidden")
    if filters.substance is not None and filters.substance not in _VALID_SUBSTANCES:
        raise ValueError("substance must be one of: substantial, useful, low_value, unknown")
    if sort not in _VALID_SORTS:
        raise ValueError("sort must be one of: auto, newest, oldest, relevance, substance")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 0:
        raise ValueError("limit must be non-negative")

    since = _calendar_bound("since", filters.since)
    until = _calendar_bound("until", filters.until)
    if since is not None and until is not None and since > until:
        raise ValueError("since must not be later than until")
    return since, until


def _load_scope(conn: sqlite3.Connection, filters: ManageFilters) -> list[dict[str, Any]]:
    clauses = [top_level_session_predicate("s")]
    params: dict[str, Any] = {}

    if filters.project is not None:
        if filters.project == "":
            clauses.append("COALESCE(s.project, '') = ''")
        else:
            clauses.append("s.project = :project")
            params["project"] = filters.project
    if filters.source is not None:
        if filters.source == "":
            clauses.append("COALESCE(s.source, '') = ''")
        else:
            clauses.append("s.source = :source")
            params["source"] = filters.source
    if filters.visibility == "visible":
        clauses.append("COALESCE(s.hidden_from_recents, 0) = 0")
    elif filters.visibility == "hidden":
        clauses.append("s.hidden_from_recents = 1")
    if filters.substance is not None:
        if filters.substance == "unknown":
            clauses.append("COALESCE(s.substance_band, '') = ''")
        else:
            clauses.append("s.substance_band = :substance")
            params["substance"] = filters.substance

    cursor = conn.execute(
        f"SELECT s.* FROM sessions s WHERE {' AND '.join(clauses)}",
        params,
    )
    columns = [description[0] for description in cursor.description or ()]
    rows = cursor.fetchall()
    if rows and isinstance(rows[0], sqlite3.Row):
        return [dict(row) for row in rows]
    return [dict(zip(columns, row)) for row in rows]


def _query_terms(query: str) -> list[str]:
    # Whitespace is the only query syntax. Preserve punctuation so values such
    # as IDs and file fragments are searched literally rather than as FTS.
    return list(dict.fromkeys(part.casefold() for part in query.split() if part))


def _field_values(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(row.get(field) or "")
        for field, _label, _weight in _SEARCH_FIELDS
    }


def _contains_word(text: str, term: str) -> bool:
    for token in _WORD_RE.findall(text.casefold()):
        if token == term:
            return True
    return False


def _exact_match(row: dict[str, Any], terms: list[str]) -> tuple[float, dict[str, list[str]]] | None:
    values = _field_values(row)
    lowered = {field: value.casefold() for field, value in values.items()}
    matches: dict[str, list[str]] = {}
    score = 0.0

    for term in terms:
        fields = [field for field, text in lowered.items() if term in text]
        if not fields:
            return None
        matches[term] = fields
        best = max(
            _FIELD_WEIGHTS[field]
            + (25.0 if _contains_word(lowered[field], term) else 0.0)
            + min(len(term), 20)
            for field in fields
        )
        score += best

    whole_query = " ".join(terms)
    if len(terms) > 1:
        if whole_query in lowered["headline"]:
            score += 100.0
        elif whole_query in lowered["summary"]:
            score += 65.0

    return score, matches


def _fuzzy_tokens(value: str) -> list[str]:
    unique: dict[str, None] = {}
    for token in _WORD_RE.findall(value[:_MAX_FUZZY_FIELD_CHARS].casefold()):
        if token not in unique:
            unique[token] = None
            if len(unique) >= _MAX_FUZZY_TOKENS_PER_FIELD:
                break
    return list(unique)


def _fuzzy_match(row: dict[str, Any], terms: list[str]) -> tuple[float, dict[str, tuple[str, str]]] | None:
    values = {field: value.casefold() for field, value in _field_values(row).items()}
    tokens_by_field = None
    matches: dict[str, tuple[str, str]] = {}
    score = 0.0
    for term in terms:
        # Exact terms can accompany a typo (e.g. "pi authentcation"). Short
        # words, IDs, and punctuation are never themselves fuzzed.
        exact_fields = [field for field, value in values.items() if term in value]
        if exact_fields:
            field = max(exact_fields, key=_FIELD_WEIGHTS.__getitem__)
            matches[term] = (field, term)
            score += 100.0 + _FIELD_WEIGHTS[field]
            continue
        if len(term) < _FUZZY_MIN_TERM_LENGTH or _WORD_RE.fullmatch(term) is None:
            return None
        if tokens_by_field is None:
            tokens_by_field = {field: _fuzzy_tokens(value) for field, value in values.items()}
        best: tuple[float, float, str, str] | None = None
        for field, tokens in tokens_by_field.items():
            found = process.extractOne(term, tokens, scorer=fuzz.ratio, score_cutoff=_FUZZY_THRESHOLD)
            if found is None:
                continue
            token, similarity, _index = found
            candidate = (float(similarity), _FIELD_WEIGHTS[field], field, token)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            return None
        similarity, weight, field, token = best
        matches[term] = (field, token)
        score += similarity + weight
    return score / len(terms), matches


def _snippet(value: str, needle: str, width: int = 180) -> str:
    compact = " ".join(value.split())
    if not compact:
        return ""
    position = compact.casefold().find(needle.casefold())
    if position < 0 or len(compact) <= width:
        return compact[:width] + ("…" if len(compact) > width else "")
    start = max(0, position - width // 3)
    end = min(len(compact), start + width)
    if end - start < width:
        start = max(0, end - width)
    return ("…" if start else "") + compact[start:end] + ("…" if end < len(compact) else "")


def _excerpt(
    row: dict[str, Any],
    terms: list[str],
    matches: dict[str, list[str]] | dict[str, tuple[str, str]],
    *,
    approximate: bool,
) -> str:
    selected: list[tuple[str, str]] = []
    covered: set[str] = set()

    for field, _label, _weight in _SEARCH_FIELDS:
        field_terms: list[tuple[str, str]] = []
        for term in terms:
            if approximate:
                matched_field, needle = matches[term]  # type: ignore[index,misc]
                if matched_field == field:
                    field_terms.append((term, needle))
            elif field in matches[term]:  # type: ignore[operator,index]
                field_terms.append((term, term))
        if not field_terms:
            continue
        selected.append((field, field_terms[0][1]))
        covered.update(term for term, _needle in field_terms)
        if len(covered) == len(terms) or len(selected) == 3:
            break

    parts = []
    for field, needle in selected:
        text = _snippet(str(row.get(field) or ""), needle)
        if text:
            parts.append(f"{_FIELD_LABELS[field]}: {text}")
    return " | ".join(parts)


def _newest_key(row: dict[str, Any]) -> tuple[bool, float, str]:
    started = _timestamp(row.get("started_at"))
    return (started is None, -(started or 0.0), str(row.get("session_id") or ""))


def _oldest_key(row: dict[str, Any]) -> tuple[bool, float, str]:
    started = _timestamp(row.get("started_at"))
    return (started is None, started or 0.0, str(row.get("session_id") or ""))


def _substance_key(row: dict[str, Any]) -> tuple[int, bool, float, str]:
    band = row.get("substance_band")
    band_rank = {"substantial": 0, "useful": 1, "low_value": 3}.get(band, 2)
    invalid, newest, session_id = _newest_key(row)
    return band_rank, invalid, newest, session_id


def query_manage_sessions(
    conn: sqlite3.Connection,
    filters: ManageFilters,
    *,
    sort: str = "auto",
    offset: int = 0,
    limit: int = 20,
) -> ManagePage:
    """Return one deterministic page from the filtered top-level inventory."""
    since, until = _validate(filters, sort, offset, limit)
    scoped = _load_scope(conn, filters)

    if since is not None or until is not None:
        dated = []
        for row in scoped:
            local_date = _local_started_date(row.get("started_at"))
            if local_date is None:
                continue
            if since is not None and local_date < since:
                continue
            if until is not None and local_date > until:
                continue
            dated.append(row)
        scoped = dated

    terms = _query_terms(filters.query or "")
    scores: dict[str, float] = {}
    approximate = False
    if terms:
        exact_rows: list[dict[str, Any]] = []
        exact_matches: dict[str, dict[str, list[str]]] = {}
        for row in scoped:
            match = _exact_match(row, terms)
            if match is None:
                continue
            score, fields = match
            session_id = str(row.get("session_id") or "")
            scores[session_id] = score
            exact_matches[session_id] = fields
            exact_rows.append(row)

        if exact_rows:
            scoped = exact_rows
            for row in scoped:
                session_id = str(row.get("session_id") or "")
                row["match_excerpt"] = _excerpt(row, terms, exact_matches[session_id], approximate=False)
        else:
            fuzzy_rows: list[dict[str, Any]] = []
            fuzzy_matches: dict[str, dict[str, tuple[str, str]]] = {}
            for row in scoped:
                match = _fuzzy_match(row, terms)
                if match is None:
                    continue
                score, fields = match
                session_id = str(row.get("session_id") or "")
                scores[session_id] = score
                fuzzy_matches[session_id] = fields
                fuzzy_rows.append(row)
            scoped = fuzzy_rows
            approximate = bool(fuzzy_rows)
            for row in scoped:
                session_id = str(row.get("session_id") or "")
                row["match_excerpt"] = _excerpt(row, terms, fuzzy_matches[session_id], approximate=True)

    effective_sort = sort
    if sort == "auto":
        effective_sort = "relevance" if terms else "newest"
    elif sort == "relevance" and not terms:
        effective_sort = "newest"

    if effective_sort == "newest":
        scoped.sort(key=_newest_key)
    elif effective_sort == "oldest":
        scoped.sort(key=_oldest_key)
    elif effective_sort == "substance":
        scoped.sort(key=_substance_key)
    else:
        # Relevance is primary; recency and canonical ID make equal scores
        # useful and stable without allowing long prompt frequency to dominate.
        scoped.sort(
            key=lambda row: (
                -scores.get(str(row.get("session_id") or ""), -math.inf),
                *_newest_key(row),
            )
        )

    total = len(scoped)
    return ManagePage(sessions=scoped[offset:offset + limit], total=total, approximate=approximate)
