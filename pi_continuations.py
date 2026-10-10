"""Explicit Pi handoff ownership. Raw sessions are never modified here."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass

from subagent_parser import SubagentInfo

import db
from indexing_lock import indexing_lock
from pi_parser import _load_jsonl, _resolve_parent_session_path, discover_pi_subagents
from session_identity import canonical_session_id


def exact_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


@contextmanager
def ownership_lock():
    """Serialize ownership decisions and publication, never model generation."""
    with indexing_lock(os.path.dirname(db.DB_PATH), "pi-conversation-ownership"):
        yield


def _validate_header(entries: list[dict]) -> None:
    if not entries or not isinstance(entries[0], dict) or entries[0].get("type") != "session":
        raise ValueError("Pi source is missing its native session header")
    native = entries[0].get("id")
    if not isinstance(native, str):
        raise ValueError("Pi source is missing its native session identity")
    canonical_session_id("pi", native)


def _raw(path: str) -> list[dict]:
    entries = _load_jsonl(path)
    _validate_header(entries)
    return entries


def _activity(entries: list[dict]) -> list[str]:
    # All branches: reopening an unchanged ancestor is not a fork, but new raw
    # user/assistant activity is, even if cleaning removes its text.
    return [e["id"] for e in entries if e.get("type") == "message"
            and isinstance(e.get("message"), dict)
            and e["message"].get("role") in {"user", "assistant"}
            and isinstance(e.get("id"), str)]


def _has_registry(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='pi_continuation_sources'").fetchone() is not None


def resolve(conn, native_session_id: str, source_path: str, entries: list[dict] | None = None) -> tuple[str, str, bool, bool]:
    """Return owner, immutable collision key, publishable, registered."""
    path = exact_path(source_path)
    default = canonical_session_id("pi", native_session_id)
    if not _has_registry(conn):
        return default, native_session_id, True, False
    rows = conn.execute("SELECT * FROM pi_continuation_sources WHERE source_path=? OR native_session_id=?",
                        (path, native_session_id)).fetchall()
    if not rows:
        return default, native_session_id, True, False
    if len(rows) != 1 or rows[0]["source_path"] != path or rows[0]["native_session_id"] != native_session_id:
        raise ValueError("Pi registered source path/native identity mismatch")
    row = rows[0]
    owner = conn.execute("SELECT * FROM pi_conversation_owners WHERE session_id=?", (row["owner_id"],)).fetchone()
    if owner is None:
        raise ValueError("Pi continuation owner is missing")
    if entries is None:
        try:
            entries = _raw(path)
        except FileNotFoundError:
            if owner["active_path"] != path:
                return owner["session_id"], owner["identity_key"], False, True
            raise
    _validate_header(entries)
    if entries[0]["id"] != native_session_id:
        raise ValueError("Pi source native identity mismatch")
    if owner["active_path"] == path:
        return owner["session_id"], owner["identity_key"], True, True
    checkpoint = json.loads(row["checkpoint_json"] or "[]")
    if set(_activity(entries)) <= set(checkpoint):
        return owner["session_id"], owner["identity_key"], False, True
    key = "pi-fork:" + json.dumps([native_session_id, path, row["owner_id"], checkpoint], separators=(",", ":"))
    sid = "pi:" + hashlib.sha256(key.encode()).hexdigest()[:16]
    fork = conn.execute("SELECT * FROM pi_conversation_owners WHERE session_id=?", (sid,)).fetchone()
    if fork and fork["identity_key"] != key:
        raise ValueError("Session identity conflict")
    return sid, key, not fork or fork["active_path"] == path, True


def resolve_identity(native_session_id: str, source_path: str, *, db_path: str | None = None) -> str:
    """Read-only identity lookup, including deterministic unregistered forks."""
    path = db_path or db.DB_PATH
    if not os.path.isfile(path):
        return canonical_session_id("pi", native_session_id)
    conn = db._get_readonly_connection(path)
    try:
        return resolve(conn, native_session_id, source_path)[0]
    finally:
        conn.close()


def refresh_paths(source_path: str) -> list[str]:
    """Refresh child owners when evidence finishes in a retired raw source."""
    path = exact_path(source_path)
    paths = {path}
    if not os.path.isfile(db.DB_PATH):
        return [path]
    conn = db._get_readonly_connection(db.DB_PATH)
    try:
        if _has_registry(conn):
            paths.update(row[0] for row in conn.execute(
                "SELECT DISTINCT o.active_path FROM pi_continuation_children c "
                "JOIN pi_conversation_owners o ON o.session_id=c.owner_id WHERE c.source_path=?",
                (path,),
            ))
        return sorted(paths)
    finally:
        conn.close()


def _assert_owner(conn, sid: str, key: str, native: str, path: str, *, header: dict | None = None) -> None:
    db.assert_session_identity(conn, sid, "pi", native, identity_key=key)
    rows = conn.execute("SELECT session_id, identity_key FROM pi_conversation_owners WHERE session_id=? OR identity_key=?", (sid, key)).fetchall()
    if any(tuple(r) != (sid, key) for r in rows):
        raise ValueError("Session identity conflict")
    existing = conn.execute("SELECT started_at FROM sessions WHERE session_id=?", (sid,)).fetchone()
    started_at = existing[0] if existing and existing[0] else (header if header is not None else _raw(path)[0]).get("timestamp")
    conn.execute("INSERT OR IGNORE INTO pi_conversation_owners (session_id, identity_key, active_path, active_native_id, started_at) VALUES (?, ?, ?, ?, ?)",
                 (sid, key, path, native, started_at))


def record_transfer(source_path: str, destination_path: str, *, source_entries: list[dict]) -> str:
    """Register a successful handoff from [getHeader(), *getEntries()].

    The captured live snapshot is authoritative, including wholly unflushed
    sessions. No source file is created or rewritten; no indexing or LLM runs.
    """
    source_path, destination_path = exact_path(source_path), exact_path(destination_path)
    with ownership_lock():
        destination = _raw(destination_path)
        try:
            persisted_source = _raw(source_path)
        except FileNotFoundError:
            persisted_source = None
        source = source_entries
        _validate_header(source)
        if persisted_source is not None and persisted_source[0]["id"] != source[0]["id"]:
            raise ValueError("Live Pi header does not match the source native identity")
        native, dest_native = source[0]["id"], destination[0]["id"]
        parent = destination[0].get("parentSession")
        if source_path == destination_path or native == dest_native or not isinstance(parent, str) or _resolve_parent_session_path(destination_path, parent) != source_path:
            raise ValueError("Invalid Pi transfer lineage")
        checkpoint = source[1:]
        if any(not isinstance(e, dict) or e.get("type") == "session" or not isinstance(e.get("id"), str) or not e["id"] for e in checkpoint):
            raise ValueError("Invalid live Pi entries")
        conn = db.get_connection()
        try:
            db.init_db(conn)
            existing = conn.execute("SELECT * FROM pi_transfers WHERE destination_path=?", (destination_path,)).fetchone()
            if existing:
                if existing["source_path"] != source_path:
                    raise ValueError("Pi destination already belongs to another transfer")
                # Repeated delivery never advances an old destination again.
                # Check both exact native origins, even after a source forks.
                resolve(conn, native, source_path, source)
                resolve(conn, dest_native, destination_path, destination)
                return existing["owner_id"]
            # Full-copy contract: destination can append a branch marker, but
            # must retain every live source entry unchanged, on all branches.
            copied = {e.get("id"): e for e in destination[1:]}
            if len({e["id"] for e in checkpoint}) != len(checkpoint) or any(copied.get(e["id"]) != e for e in checkpoint):
                raise ValueError("Pi destination does not preserve the live source tree")
            live = {e["id"]: e for e in checkpoint}
            # Teardown may append archival records after the live capture.
            # Only conflicting captured entries invalidate the source; later
            # activity must not move the handoff checkpoint forward.
            if persisted_source is not None and any(
                e.get("id") in live and live[e["id"]] != e for e in persisted_source[1:]
            ):
                raise ValueError("Live Pi checkpoint does not belong to the source tree")
            sid, key, publishable, _ = resolve(conn, native, source_path, [source[0], *checkpoint])
            if not publishable:
                raise ValueError("Cannot transfer an unchanged retired Pi source")
            _assert_owner(conn, sid, key, native, source_path, header=source[0])
            # Refuse to steal a previously independent destination or registered native.
            registered = conn.execute("SELECT 1 FROM pi_continuation_sources WHERE source_path=? OR native_session_id=?", (destination_path, dest_native)).fetchone()
            indexed = conn.execute("SELECT 1 FROM sessions WHERE source='pi' AND native_session_id=?", (dest_native,)).fetchone()
            if registered or indexed:
                raise ValueError("Pi destination already has independent ownership")
            children(conn, sid, source_path, native, capture=True, entries=source)
            conn.execute("INSERT INTO pi_continuation_sources VALUES (?, ?, ?, ?) ON CONFLICT(source_path) DO UPDATE SET owner_id=excluded.owner_id, checkpoint_json=excluded.checkpoint_json",
                         (source_path, native, sid, json.dumps(_activity(checkpoint))))
            conn.execute("INSERT INTO pi_continuation_sources VALUES (?, ?, ?, NULL)", (destination_path, dest_native, sid))
            conn.execute("UPDATE pi_conversation_owners SET active_path=?, active_native_id=? WHERE session_id=?", (destination_path, dest_native, sid))
            conn.execute("INSERT INTO pi_transfers VALUES (?, ?, ?)", (destination_path, source_path, sid))
            conn.commit()
            return sid
        finally:
            conn.close()


def contributor_archives(
    conn, owner_id: str, source_path: str, native: str, *, raw_sources: dict[str, list[dict]],
) -> list[dict]:
    """Read explicit child evidence from its source and successful raw copies.

    Copies may contain live archives never flushed to the source. Source entries
    appended after transfer (such as finishing an open Side Chat) also survive.
    The caller filters this result to captured child IDs; ordinary clones never
    use this native-header substitution.
    """
    from side_chats import parse_archives

    def read(path: str) -> list[dict]:
        if path not in raw_sources:
            try:
                raw_sources[path] = _raw(path)
            except FileNotFoundError:
                raw_sources[path] = []
        return raw_sources[path]

    merged: dict[str, dict] = {}
    copies = conn.execute(
        "SELECT t.destination_path, s.native_session_id FROM pi_transfers t "
        "JOIN pi_continuation_sources s ON s.source_path=t.destination_path "
        "WHERE t.source_path=? AND t.owner_id=? ORDER BY t.rowid",
        (source_path, owner_id),
    ).fetchall()
    for copy_path, copy_native in copies:
        raw = read(copy_path)
        if not raw:
            continue
        if raw[0]["id"] != copy_native:
            raise ValueError("Pi contributor copy native identity mismatch")
        for entry in raw[1:]:
            if isinstance(entry.get("id"), str):
                merged.setdefault(entry["id"], entry)
    raw = read(source_path)
    if raw:
        if raw[0]["id"] != native:
            raise ValueError("Pi contributor native identity mismatch")
        for entry in raw[1:]:
            if isinstance(entry.get("id"), str):
                merged[entry["id"]] = entry
    # This is an explicit original-native view of retained records, not a new
    # raw session or permission for clones to adopt inherited archives.
    return parse_archives(source_path, native, entries=[{"type": "session", "id": native}, *merged.values()])


@dataclass(frozen=True)
class OwnedChildren:
    archives: list[dict]
    subagent_infos: list[SubagentInfo]


def publication_children(
    conn, owner_id: str, active_path: str, native: str, *, entries: list[dict],
) -> OwnedChildren:
    """Prepare current contributor evidence under the publication locks.

    Registry locators select ownership; contributor copies only supply evidence
    for those selected children, never permission to adopt inherited archives.
    """
    locators, infos = children(conn, owner_id, active_path, native, entries=entries)
    owned_ids: dict[tuple[str, str], set[str]] = {}
    for source_path, source_native, child_id in locators:
        owned_ids.setdefault((source_path, source_native), set()).add(child_id)
    archives_by_id: dict[str, dict] = {}
    raw_sources = {exact_path(active_path): entries}
    for (source_path, source_native), child_ids in owned_ids.items():
        for child in contributor_archives(conn, owner_id, source_path, source_native, raw_sources=raw_sources):
            if child["side_chat_id"] in child_ids:
                archives_by_id[child["side_chat_id"]] = child
    return OwnedChildren(archives=list(archives_by_id.values()), subagent_infos=infos)


def children(
    conn, owner_id: str, active_path: str, native: str, *, capture: bool = False,
    entries: list[dict] | None = None,
) -> tuple[list[tuple[str, str, str]], list[SubagentInfo]]:
    """Owned Side Chat locators and SubagentInfo, including frozen contributors."""
    from side_chats import parse_archives

    path = exact_path(active_path)
    current = [("sidechat", path, c["side_chat_id"]) for c in parse_archives(path, native, include_empty=capture, entries=entries)]
    infos = {i.jsonl_path: i for i in discover_pi_subagents(path)}
    current += [("subagent", path, p) for p in infos]
    for kind, source, child in current:
        if capture:
            conn.execute("INSERT OR IGNORE INTO pi_continuation_children VALUES (?, ?, ?, ?)", (kind, source, child, owner_id))
    saved = [tuple(r) for r in conn.execute("SELECT kind, source_path, child_id FROM pi_continuation_children WHERE owner_id=? ORDER BY source_path, child_id", (owner_id,))]
    for item in current:
        claimed = conn.execute("SELECT owner_id FROM pi_continuation_children WHERE kind=? AND source_path=? AND child_id=?", item).fetchone()
        if (not claimed or claimed[0] == owner_id) and item not in saved:
            saved.append(item)
    sides, subs = [], []
    owner = conn.execute("SELECT identity_key FROM pi_conversation_owners WHERE session_id=?", (owner_id,)).fetchone()
    for kind, source, child in saved:
        if kind == "sidechat":
            registered = conn.execute("SELECT native_session_id FROM pi_continuation_sources WHERE source_path=?", (source,)).fetchone()
            source_native = registered[0] if registered else native
            sides.append((source, source_native, child))
        else:
            info = infos.get(child)
            if info is None:
                info = next((i for i in discover_pi_subagents(source) if i.jsonl_path == child), None)
            if info:
                # Identical run-directory names across contributors must not
                # overwrite each other's child artifacts or fact scopes.
                agent_id = info.agent_id
                # Preserve the original owner's existing child artifact paths;
                # continuation/fork sources get stable, path-specific namespaces.
                registered = conn.execute("SELECT native_session_id FROM pi_continuation_sources WHERE source_path=?", (source,)).fetchone()
                source_native = registered[0] if registered else native
                if not owner or owner[0] != source_native:
                    agent_id = hashlib.sha256(child.encode()).hexdigest()[:16] + "-" + agent_id
                info = SubagentInfo(jsonl_path=info.jsonl_path, meta_path=info.meta_path,
                                    agent_id=agent_id, agent_type=info.agent_type)
                subs.append(info)
    return sides, subs
