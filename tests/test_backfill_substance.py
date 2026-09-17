"""Backfill safety: eligibility, resumability, failures and concurrent refreshes."""
from datetime import datetime, timedelta, timezone

import db
import summarizer
from backfill_substance import backfill_recent_substance


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sessions.db"))
    conn = db.get_connection()
    db.init_db(conn)
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    return conn, now


def _add(conn, tmp_path, now, sid, **extra):
    path = tmp_path / f"{sid}.md"
    path.write_text(f"[user] Work on {sid}\n[assistant] Finished a local change.")
    values = dict(session_id=sid, project=sid, source="pi", source_path=f"/sessions/{sid}.jsonl",
                  started_at=now.isoformat(), user_message_count=1, assistant_message_count=1,
                  assistant_char_count=24, summary="Existing summary", headline="Existing headline",
                  transcript_path=str(path))
    values.update(extra)
    db.upsert_session(conn, **values)
    return path


def test_backfill_previews_filters_preserves_descriptions_and_resumes_failures(tmp_path, monkeypatch):
    conn, now = _setup(tmp_path, monkeypatch)
    _add(conn, tmp_path, now, "good")
    _add(conn, tmp_path, now, "failed")
    _add(conn, tmp_path, now - timedelta(days=8), "old")
    _add(conn, tmp_path, now + timedelta(seconds=1), "future")
    _add(conn, tmp_path, now, "nested", source_path="/parent/run-0/session.jsonl")
    _add(conn, tmp_path, now, "missing", transcript_path=str(tmp_path / "absent.md"))
    _add(conn, tmp_path, now, "classified", substance_band="substantial", substance_reason="Existing assessment")
    conn.close()
    calls = []

    def classify(**kwargs):
        calls.append(kwargs["project"])
        return None if kwargs["project"] == "failed" else ("useful", "A concrete local change.")

    monkeypatch.setattr(summarizer, "classify_substance", classify)
    preview = backfill_recent_substance(now=now)
    assert set(preview["session_ids"]) == {"good", "failed"}
    assert calls == []
    report = backfill_recent_substance(apply=True, now=now)
    assert (report["eligible"], report["updated"], report["failed"], report["stale"]) == (2, 1, 1, 0)
    conn = db.get_connection()
    assert db.get_session(conn, "good")["substance_band"] == "useful"
    assert db.get_session(conn, "failed")["substance_band"] is None
    for sid in ("good", "failed", "classified"):
        row = db.get_session(conn, sid)
        assert (row["summary"], row["headline"]) == ("Existing summary", "Existing headline")
    conn.close()
    monkeypatch.setattr(summarizer, "classify_substance", lambda **kwargs: ("useful", "Recovered assessment."))
    resumed = backfill_recent_substance(apply=True, now=now)
    assert (resumed["eligible"], resumed["updated"]) == (1, 1)
    assert backfill_recent_substance(apply=True, now=now)["eligible"] == 0


def test_backfill_refuses_to_overwrite_a_new_classification_or_changed_transcript(tmp_path, monkeypatch):
    conn, now = _setup(tmp_path, monkeypatch)
    changed_path = _add(conn, tmp_path, now, "changed-transcript")
    _add(conn, tmp_path, now, "new-assessment")
    conn.close()

    def classify(**kwargs):
        if kwargs["project"] == "changed-transcript":
            changed_path.write_text("[user] Completely different work now\n[assistant] New findings.")
        else:
            other = db.get_connection()
            db.upsert_session(other, session_id="new-assessment", substance_band="substantial", substance_reason="Newer refresh won.")
            other.close()
        return "useful", "Stale inference."

    monkeypatch.setattr(summarizer, "classify_substance", classify)
    report = backfill_recent_substance(apply=True, now=now)
    assert (report["updated"], report["failed"], report["stale"]) == (0, 0, 2)
    conn = db.get_connection()
    assert db.get_session(conn, "changed-transcript")["substance_band"] is None
    assert db.get_session(conn, "new-assessment")["substance_reason"] == "Newer refresh won."
    conn.close()
