"""Deterministic retrieval for the session-management TUI."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db import init_db, upsert_session
from manage_query import ManageFilters, ManagePage, query_manage_sessions


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    yield connection
    connection.close()


def seed(conn, session_id: str, **fields) -> None:
    defaults = {
        "source": "pi",
        "native_session_id": f"native-{session_id}",
        "project": "session-index",
        "started_at": "2026-09-10T12:00:00Z",
    }
    hidden = fields.pop("hidden_from_recents", 0)
    defaults.update(fields)
    upsert_session(conn, session_id=session_id, **defaults)
    conn.execute(
        "UPDATE sessions SET hidden_from_recents = ? WHERE session_id = ?",
        (hidden, session_id),
    )
    conn.commit()


def ids(page: ManagePage) -> list[str]:
    return [row["session_id"] for row in page.sessions]


def test_literal_and_searches_across_fields_and_returns_labelled_excerpt(conn):
    seed(
        conn,
        "pi:cross-fields",
        headline="Build deterministic manager",
        summary="Added bounded ranking",
        user_messages="Please support pagination",
    )
    seed(conn, "pi:one-term", headline="Build deterministic manager", summary="Unrelated")

    page = query_manage_sessions(conn, ManageFilters(query="deterministic paginat"))

    assert ids(page) == ["pi:cross-fields"]
    assert page.total == 1
    assert page.approximate is False
    assert "Headline:" in page.sessions[0]["match_excerpt"]
    assert "User messages:" in page.sessions[0]["match_excerpt"]


@pytest.mark.parametrize(
    ("field", "value", "query"),
    [
        ("headline", "Rotate signing certificates", "certificates"),
        ("summary", "Resolved lunar caching behavior", "lunar"),
        ("user_messages", "Investigate websocket reconnects", "websocket"),
        ("project", "merchant-control-plane", "control-plane"),
        ("files_touched", "src/payments/reconcile_worker.py", "reconcile_worker"),
        ("session_id", "pi:canonical-abcdef123456", "abcdef123"),
        ("native_session_id", "019-native-identity-xyz", "identity-xyz"),
    ],
)
def test_search_covers_approved_fields_and_partial_terms(conn, field, value, query):
    kwargs = {field: value}
    session_id = kwargs.pop("session_id", "pi:field-match")
    seed(conn, session_id, **kwargs)

    page = query_manage_sessions(conn, ManageFilters(query=query))

    assert ids(page) == [session_id]
    assert page.sessions[0]["match_excerpt"]


def test_exact_results_suppress_typo_fallback_and_typo_only_query_is_approximate(conn):
    seed(conn, "pi:exact", headline="Deterministic retrieval engine")
    seed(conn, "pi:misspelled", headline="Deterministic retreival engine")

    exact = query_manage_sessions(conn, ManageFilters(query="retrieval"))
    approximate = query_manage_sessions(conn, ManageFilters(query="retrievel"))

    assert ids(exact) == ["pi:exact"]
    assert exact.approximate is False
    assert approximate.approximate is True
    assert "pi:exact" in ids(approximate)
    assert all("match_excerpt" in row for row in approximate.sessions)


def test_short_terms_and_punctuation_remain_literal_and_cannot_inject_sql(conn):
    seed(conn, "pi:safe", headline="A normal searchable session")
    seed(conn, "pi:literal", summary="Handled value foo); without parsing it")

    short = query_manage_sessions(conn, ManageFilters(query="nprmal"))
    punctuation = query_manage_sessions(conn, ManageFilters(query="foo);"))
    injection = query_manage_sessions(conn, ManageFilters(query="x' OR 1=1 --"))
    filtered_injection = query_manage_sessions(
        conn,
        ManageFilters(project="session-index' OR 1=1 --"),
    )

    assert short.sessions == []  # Six-character typo is not confused with unrelated short tokens.
    assert short.approximate is False
    assert ids(punctuation) == ["pi:literal"]
    assert injection.sessions == []
    assert filtered_injection.sessions == []
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_typo_fallback_can_combine_with_literal_short_terms_and_paths(conn):
    seed(conn, "pi:auth", headline="Authentication repair", files_touched="src/auth.py")
    seed(conn, "cc:auth", headline="Authentication repair", files_touched="src/other.py")
    assert ids(query_manage_sessions(conn, ManageFilters(query="pi authentcation"))) == ["pi:auth"]
    assert ids(query_manage_sessions(conn, ManageFilters(query="src/auth.py authentcation"))) == ["pi:auth"]
    assert not query_manage_sessions(conn, ManageFilters(query="src/missing.py authentcation")).sessions


def test_filters_apply_before_approximate_fallback(conn):
    seed(
        conn,
        "pi:in-scope",
        project="wanted",
        source="pi",
        headline="Implement retrievel controls",
        substance_band="useful",
        hidden_from_recents=1,
    )
    seed(
        conn,
        "cc:exact-outside",
        project="outside",
        source="claude",
        headline="Implement retrieval controls",
        substance_band="substantial",
        hidden_from_recents=0,
    )
    seed(
        conn,
        "pi:wrong-band",
        project="wanted",
        source="pi",
        headline="Implement retrievel controls",
        substance_band="low_value",
        hidden_from_recents=1,
    )

    page = query_manage_sessions(
        conn,
        ManageFilters(
            query="retrieval",
            project="wanted",
            source="pi",
            visibility="hidden",
            substance="useful",
            since="2026-09-10",
            until="2026-09-10",
        ),
    )

    assert ids(page) == ["pi:in-scope"]
    assert page.approximate is True
    assert page.total == 1


def test_default_inventory_includes_hidden_but_excludes_nested_pi_subagents(conn):
    seed(conn, "pi:visible", hidden_from_recents=0)
    seed(conn, "pi:hidden", hidden_from_recents=1)
    seed(
        conn,
        "pi:nested",
        source="pi",
        source_path="/tmp/agent/run-7/session.jsonl",
        hidden_from_recents=1,
    )

    all_sessions = query_manage_sessions(conn, ManageFilters())
    visible = query_manage_sessions(conn, ManageFilters(visibility="visible"))
    hidden = query_manage_sessions(conn, ManageFilters(visibility="hidden"))

    assert set(ids(all_sessions)) == {"pi:visible", "pi:hidden"}
    assert ids(visible) == ["pi:visible"]
    assert ids(hidden) == ["pi:hidden"]


def test_relevance_prefers_headline_over_repeated_incidental_prompt_matches(conn):
    seed(conn, "pi:headline", headline="Resolve distinctive quasar", user_messages="Short request")
    seed(
        conn,
        "pi:prompt",
        headline="Routine discussion",
        user_messages=("incidental distinctive quasar " * 200),
    )

    page = query_manage_sessions(conn, ManageFilters(query="distinctive quasar"))

    assert ids(page) == ["pi:headline", "pi:prompt"]
    assert page.total == 2


def test_stable_pagination_uses_session_id_tiebreaker_and_reports_full_total(conn):
    for session_id in ("pi:d", "pi:b", "pi:e", "pi:a", "pi:c"):
        seed(conn, session_id, started_at="2026-09-10T12:00:00Z")

    first = query_manage_sessions(conn, ManageFilters(), sort="newest", limit=2)
    second = query_manage_sessions(conn, ManageFilters(), sort="newest", offset=2, limit=2)
    third = query_manage_sessions(conn, ManageFilters(), sort="newest", offset=4, limit=2)

    assert ids(first) == ["pi:a", "pi:b"]
    assert ids(second) == ["pi:c", "pi:d"]
    assert ids(third) == ["pi:e"]
    assert first.total == second.total == third.total == 5


def test_newest_oldest_relevance_fallback_and_invalid_dates_last(conn):
    seed(conn, "pi:older", started_at="2026-09-09T12:00:00Z")
    seed(conn, "pi:newer", started_at="2026-09-11T12:00:00Z")
    seed(conn, "pi:missing", started_at=None)
    seed(conn, "pi:invalid", started_at="not-a-date")

    newest = query_manage_sessions(conn, ManageFilters(), sort="newest")
    oldest = query_manage_sessions(conn, ManageFilters(), sort="oldest")
    relevance = query_manage_sessions(conn, ManageFilters(), sort="relevance")

    assert ids(newest) == ["pi:newer", "pi:older", "pi:invalid", "pi:missing"]
    assert ids(oldest) == ["pi:older", "pi:newer", "pi:invalid", "pi:missing"]
    assert ids(relevance) == ids(newest)


def test_substance_sort_orders_bands_then_newest_with_unknown_before_low_value(conn):
    seed(conn, "pi:sub-old", substance_band="substantial", started_at="2026-09-09T12:00:00Z")
    seed(conn, "pi:sub-new", substance_band="substantial", started_at="2026-09-11T12:00:00Z")
    seed(conn, "pi:useful", substance_band="useful")
    seed(conn, "pi:unknown", substance_band=None)
    seed(conn, "pi:low", substance_band="low_value")

    page = query_manage_sessions(conn, ManageFilters(), sort="substance")

    assert ids(page) == ["pi:sub-new", "pi:sub-old", "pi:useful", "pi:unknown", "pi:low"]


def test_date_bounds_use_inclusive_local_calendar_days_for_offset_timestamps(conn):
    now = datetime.now().astimezone()
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    target = local_midnight.date().isoformat()
    # Represent the in-range instant with an offset far across local midnight,
    # so its textual date differs from the local calendar date on normal zones.
    local_offset = local_midnight.utcoffset() or timedelta(0)
    display_offset = timedelta(hours=-12 if local_offset >= timedelta(0) else 14)
    offset_zone = timezone(display_offset)
    inside = (local_midnight + timedelta(minutes=1)).astimezone(offset_zone).isoformat()
    before = (local_midnight - timedelta(minutes=1)).astimezone(offset_zone).isoformat()
    after = (local_midnight + timedelta(days=1)).astimezone(offset_zone).isoformat()
    seed(conn, "pi:inside", started_at=inside)
    seed(conn, "pi:before", started_at=before)
    seed(conn, "pi:after", started_at=after)
    seed(conn, "pi:bad-date", started_at="invalid")

    page = query_manage_sessions(conn, ManageFilters(since=target, until=target))

    assert ids(page) == ["pi:inside"]


@pytest.mark.parametrize(
    "filters",
    [
        ManageFilters(since="2026/09/10"),
        ManageFilters(until="2026-02-30"),
        ManageFilters(since="2026-09-11", until="2026-09-10"),
    ],
)
def test_invalid_date_bounds_raise_value_error(conn, filters):
    with pytest.raises(ValueError):
        query_manage_sessions(conn, filters)


def test_missing_project_source_and_substance_filters(conn):
    seed(conn, "pi:known", project="known", substance_band="useful")
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, native_session_id, project, started_at, substance_band
        ) VALUES (?, NULL, NULL, NULL, NULL, NULL)
        """,
        ("unknown",),
    )
    conn.commit()

    page = query_manage_sessions(
        conn,
        ManageFilters(project="", source="", substance="unknown"),
    )

    assert ids(page) == ["unknown"]
    assert page.sessions[0]["started_at"] is None


def test_invalid_enums_and_pagination_raise_value_error(conn):
    with pytest.raises(ValueError, match="visibility"):
        query_manage_sessions(conn, ManageFilters(visibility="maybe"))
    with pytest.raises(ValueError, match="substance"):
        query_manage_sessions(conn, ManageFilters(substance="valuable"))
    with pytest.raises(ValueError, match="sort"):
        query_manage_sessions(conn, ManageFilters(), sort="random")
    with pytest.raises(ValueError, match="offset"):
        query_manage_sessions(conn, ManageFilters(), offset=-1)
    with pytest.raises(ValueError, match="limit"):
        query_manage_sessions(conn, ManageFilters(), limit=-1)
