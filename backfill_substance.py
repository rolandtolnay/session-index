"""Classify unassessed, headlined Top-Level Sessions from the past week.

Preview: uv run backfill_substance.py
Apply:   uv run backfill_substance.py --apply

Uses existing Clean Transcripts; never reads source JSONL or regenerates summaries,
headlines, or artifacts. Safe to resume: atomically skips existing classifications
and skips observed changes to session metadata or transcript files. File validation
is best-effort, not locked against refreshes: a concurrent turn can leave an older
assessment until the next successful summary refresh, as with normal indexing.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import db
from parser import clean_user_messages
from recent_context import RECENT_DAYS


def _classify_snapshot(session: dict) -> tuple[tuple[str, str] | None, tuple[int, int] | None]:
    from summarizer import classify_substance

    path = Path(session["transcript_path"])
    try:
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    assessment = classify_substance(
        project=session.get("project") or "",
        branch=session.get("branch") or "",
        user_messages=clean_user_messages((session.get("user_messages") or "").split("\n---\n")),
        files_touched=[p.strip() for p in (session.get("files_touched") or "").split(",") if p.strip()],
        transcript_text=text,
    )
    return assessment, fingerprint


def backfill_recent_substance(*, apply: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=RECENT_DAYS)
    conn = db.get_connection() if apply else db._get_readonly_connection()
    try:
        if apply:
            db.init_db(conn)
        rows = conn.execute(f"""
            SELECT * FROM sessions
            WHERE julianday(started_at) BETWEEN julianday(?) AND julianday(?)
              AND {db.TOP_LEVEL_SESSION_PREDICATE}
              AND trim(COALESCE(headline, '')) != ''
              AND trim(COALESCE(transcript_path, '')) != ''
            ORDER BY julianday(started_at) DESC, session_id DESC
        """, (since.isoformat(), now.isoformat())).fetchall()
        sessions = [dict(row) for row in rows]
        candidates = [s for s in sessions if s.get("substance_band") is None and Path(s["transcript_path"]).is_file()]
        report = {"apply": apply, "eligible": len(candidates), "updated": 0, "failed": 0, "stale": 0, "results": []}
        if not apply:
            report["session_ids"] = [s["session_id"] for s in candidates]
            return report

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(_classify_snapshot, s): s for s in candidates}
            for future in as_completed(futures):
                session = futures[future]
                assessment, fingerprint = future.result()
                status = "failed"
                if assessment is not None:
                    try:
                        stat = Path(session["transcript_path"]).stat()
                        unchanged = (stat.st_mtime_ns, stat.st_size) == fingerprint
                    except OSError:
                        unchanged = False
                    status = "stale"
                    if unchanged:
                        band, reason = assessment
                        cursor = conn.execute("""
                            UPDATE sessions SET substance_band = ?, substance_reason = ?
                            WHERE session_id = ? AND substance_band IS NULL
                              AND transcript_path = ? AND ended_at IS ?
                              AND user_message_count IS ? AND assistant_message_count IS ?
                              AND assistant_char_count IS ?
                        """, (band, reason, session["session_id"], session["transcript_path"],
                              session.get("ended_at"), session.get("user_message_count"),
                              session.get("assistant_message_count"), session.get("assistant_char_count")))
                        conn.commit()
                        if cursor.rowcount:
                            status = "updated"
                report[status] += 1
                report["results"].append({"session_id": session["session_id"], "status": status})
        return report
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Run Luna and persist missing weekly classifications")
    args = parser.parse_args()
    report = backfill_recent_substance(apply=args.apply)
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
