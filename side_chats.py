"""Pi Side Chat archives: parent-owned source records and derived child artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

CUSTOM_TYPE = "side-chat-archive"
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def parse_archives(path: str, owner_session_id: str) -> list[dict[str, Any]]:
    """Read owned records across ALL branches; clones cannot adopt copied records."""
    from pi_parser import _load_jsonl

    entries = _load_jsonl(path)
    if not entries or entries[0].get("type") != "session" or entries[0].get("id") != owner_session_id:
        return []
    children: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "custom" or entry.get("customType") != CUSTOM_TYPE:
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("ownerSessionId") != owner_session_id:
            continue
        child_id = data.get("sideChatId")
        timestamp = data.get("timestamp")
        if not isinstance(child_id, str) or not _ID.fullmatch(child_id) or not isinstance(timestamp, str):
            continue
        kind = data.get("kind")
        if kind == "open":
            focus = data.get("focusedContent")
            leaf = data.get("openingLeafId")
            if not isinstance(focus, dict) or not all(isinstance(focus.get(k), str) for k in ("kind", "label", "text")):
                continue
            if leaf is not None and not isinstance(leaf, str):
                continue
            children.setdefault(child_id, {
                "side_chat_id": child_id, "opening_leaf_id": leaf,
                "started_at": timestamp, "closed_at": None,
                "focused_content": {k: focus[k] for k in ("kind", "label", "text")}, "turns": {},
            })
        elif child_id in children:
            child = children[child_id]
            if kind == "turn":
                seq = data.get("sequence")
                if type(seq) is not int or seq < 1 or not all(isinstance(data.get(k), str) for k in ("question", "answer", "model")):
                    continue
                if not data["question"].strip() or not data["answer"].strip():
                    continue
                # A repeated delivery is idempotent; first durable value wins.
                child["turns"].setdefault(seq, {k: data[k] for k in ("sequence", "question", "answer", "model", "timestamp")})
            elif kind == "close":
                child["closed_at"] = timestamp
    result = []
    for child in children.values():
        if child["turns"]:
            child["turns"] = [turn for _, turn in sorted(child["turns"].items())]
            result.append(child)
    return result


def list_side_chats(conn, session_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM side_chats WHERE parent_session_id=? ORDER BY started_at, side_chat_id", (session_id,),
    )]


def label(row: dict) -> str:
    return " ".join((row.get("headline") or row["first_question"]).split())[:180]


def refs(rows: list[dict]) -> list[dict]:
    return [{"ref": f"sidechat/{row['parent_session_id']}/{row['side_chat_id']}",
             "headline": label(row), "opener": row["opener"], "started_at": row["started_at"]} for row in rows]


def build_rows(path: str, session, conn) -> list[dict]:
    import transcript

    previous = {row["side_chat_id"]: row for row in list_side_chats(conn, session.session_id)}
    rows = []
    for child in parse_archives(path, session.native_session_id):
        focus = child["focused_content"]
        turns = child["turns"]
        content = json.dumps({"focus": focus, "turns": turns}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(content.encode()).hexdigest()
        old = previous.get(child["side_chat_id"], {})
        rows.append({
            "parent_session_id": session.session_id, "side_chat_id": child["side_chat_id"],
            "opening_leaf_id": child["opening_leaf_id"], "opener": focus["kind"],
            "started_at": child["started_at"], "closed_at": child["closed_at"],
            "focused_content": json.dumps(focus, ensure_ascii=False),
            "turns_json": json.dumps(turns, ensure_ascii=False), "turn_count": len(turns),
            "first_question": turns[0]["question"],
            "headline": old.get("headline") if old.get("content_hash") == digest else None,
            "content_hash": digest,
            "search_text": "\n".join([focus["label"], focus["text"], *[t[k] for t in turns for k in ("question", "answer")]]),
            "transcript_path": os.path.join(transcript.TRANSCRIPT_DIR, session.session_id, f"side-chat-{child['side_chat_id']}.md"),
        })
    return rows


def replace_rows(conn, session_id: str, rows: list[dict]) -> None:
    from db import _replace_rows
    _replace_rows(conn, "side_chats", "parent_session_id", session_id, rows, commit=False)


def conversation_text(row: dict) -> str:
    """Child-only evidence; neither parent history nor the routing headline is input."""
    from transcript import render_transcript

    focus = json.loads(row["focused_content"])
    messages = [{"role": role, "content": turn[key], "timestamp": turn["timestamp"]}
                for turn in json.loads(row["turns_json"])
                for role, key in (("user", "question"), ("assistant", "answer"))]
    conversation = render_transcript(messages).partition("\n\n")[2]
    return f"[prompt] {'─' * 30}\nFocused Content: {focus['label']}\n{focus['text']}\n\n" + conversation


def atomic_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".side-chat-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_child(row: dict) -> None:
    import transcript
    from artifact_references import normalize_references

    parent = os.path.join(transcript.TRANSCRIPT_DIR, f"{row['parent_session_id']}.md")
    text = (f"# Side Chat: {label(row)}\n\nParent: `{parent}`\n"
            f"Ref: `sidechat/{row['parent_session_id']}/{row['side_chat_id']}`\n"
            f"Opener: {row['opener']} | Opened: {row['started_at']} | Opening leaf: {row['opening_leaf_id'] or 'none'}\n\n"
            + conversation_text(row))
    atomic_text(row["transcript_path"], normalize_references(text))


def refresh_headlines(session_id: str, *, recover_open: bool = False) -> None:
    """Generate outside the artifact lock, then publish only to an unchanged child.

    A separate per-parent lock coalesces concurrent close/recovery requests without
    delaying deterministic turn snapshots. Deletion cannot resurrect a session.
    """
    import db
    import transcript
    from indexing_lock import indexing_lock
    from summarizer import generate_headline

    with indexing_lock(os.path.dirname(db.DB_PATH), session_id + "-side-headlines"):
        conn = db.get_connection()
        try:
            rows = list_side_chats(conn, session_id)
        finally:
            conn.close()
        for row in rows:
            if row["headline"] or (not row["closed_at"] and not recover_open):
                continue
            headline = generate_headline(
                project="", branch="", user_messages=[t["question"] for t in json.loads(row["turns_json"])],
                files_touched=[], transcript_text=conversation_text(row), side_chat=True,
            )
            if not headline:
                continue
            with indexing_lock(os.path.dirname(db.DB_PATH), session_id):
                conn = db.get_connection()
                try:
                    updated = conn.execute(
                        "UPDATE side_chats SET headline=? WHERE parent_session_id=? AND side_chat_id=? AND content_hash=? AND headline IS NULL",
                        (headline, session_id, row["side_chat_id"], row["content_hash"]),
                    )
                    if not updated.rowcount:
                        continue
                    row["headline"] = headline
                    write_child(row)
                    children = list_side_chats(conn, session_id)
                    # Only the navigation is changed: do not reinterpret main history.
                    parent_path = Path(transcript.TRANSCRIPT_DIR) / f"{session_id}.md"
                    if parent_path.is_file():
                        text = parent_path.read_text()
                        start = text.find("## Related artifacts\n")
                        end = text.find("\n\n---\n", start)
                        if start >= 0 and end >= 0:
                            atomic_text(str(parent_path), text[:start] + transcript._artifact_navigation(session_id, children) + text[end:])
                    conn.commit()
                finally:
                    conn.close()


def matching_refs(conn, session_id: str, query: str) -> list[dict]:
    from db import build_fts_query
    rows = [dict(row) for row in conn.execute("""
        SELECT c.* FROM side_chats_fts f JOIN side_chats c ON c.rowid=f.rowid
        WHERE side_chats_fts MATCH ? AND c.parent_session_id=? ORDER BY rank, c.side_chat_id
    """, (build_fts_query(query), session_id))]
    return refs(rows)
