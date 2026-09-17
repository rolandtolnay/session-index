"""Offline, backed-up migration to short Canonical Session IDs.

Dry run: uv run migrate_session_ids.py
Apply/resume: uv run migrate_session_ids.py --apply

Stop provider sessions and detached refresh workers first. An interrupted run keeps
indexing paused. Re-running --apply resumes the same manifest; it never rebuilds
from provider transcripts. Backups are retained under backups/short-ids-*/.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

from artifact_references import (
    LEGACY_ID, NATIVE_LEGACY_ID, REFERENCE_IDS_FILE, TEXT_COLUMNS, load_reference_ids,
    normalize_references, referenced_session_ids, short_legacy_id,
)
from session_identity import (
    PREFIXES, PREVIOUS_SHORT_ID, canonical_session_id, is_canonical_session_id, refresh_session_id,
)

OWNERS = {"sessions": "session_id", "tool_calls": "session_id", "skill_invocations": "session_id",
          "file_mutations": "session_id", "subagent_runs": "parent_session_id", "question_answers": "session_id"}
COMPONENTS = (REFERENCE_IDS_FILE, "transcripts", "refresh-jobs", "sessions.db")


def reference_bytes(mapping: dict) -> bytes:
    return (json.dumps(mapping, sort_keys=True, indent=2) + "\n").encode()


def historical_reference_ids(root: Path, sessions: list[dict]) -> dict[str, str]:
    """Recover irreversible 12-hex references from native identities, never guess.

    The preceding UUID migration's backups also preserve native identities for
    orphan artifacts, refresh jobs, and references quoted in generated text.
    Those backups are read only during the 12-to-16 cutover, not normal rendering.
    """
    mapping = load_reference_ids(root)

    def remember(old: str, new: str) -> None:
        if not re.fullmatch(PREVIOUS_SHORT_ID, old) or not new.startswith(old):
            raise ValueError(f"Invalid historical short identity: {old}")
        previous = mapping.setdefault(old, new)
        if previous != new:
            raise ValueError(f"Ambiguous historical short identity: {old}")

    def remember_native(old: str) -> None:
        if re.fullmatch(NATIVE_LEGACY_ID, old):
            new = short_legacy_id(old, {})
            remember(new[:-4], new)

    upgrading = False
    for row in sessions:
        if re.fullmatch(PREVIOUS_SHORT_ID, row["session_id"]):
            upgrading = True
            remember(row["session_id"], canonical_session_id(row["source"], row["native_session_id"]))
    if upgrading:
        for path in sorted((root / "backups").glob("short-ids-*/manifest.json")):
            previous = json.loads(path.read_text())
            if previous.get("root") != str(root):
                continue
            for old in previous.get("mapping", {}):
                remember_native(old)
            for file in previous.get("files", []):
                if file["component"] == "transcripts":
                    remember_native(file["old"].split("/")[0].removesuffix(".md").removesuffix(".tools"))
                elif file["component"] == "refresh-jobs":
                    parts = file["old"].split("/")
                    if len(parts) >= 2 and parts[0] in PREFIXES:
                        source, name = parts[:2]
                        native = name.removeprefix(source + "-")
                        remember_native(native if source == "claude" else f"{source}:{native}")
            for artifact in (path.parent / "backup" / "transcripts").rglob("*.md"):
                for old in referenced_session_ids(artifact.read_text()):
                    remember_native(old)
    return mapping


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def connect(path: Path, *, readonly: bool = False):
    conn = sqlite3.connect(f"file:{path}?mode={'ro' if readonly else 'rw'}", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def normalize_text(text: str, root: Path, reference_ids: dict) -> str:
    # Also support a relocated local store (including migration test fixtures).
    pattern = re.escape(str(root / "transcripts")) + rf"/({LEGACY_ID})(?=[/.]|$)"
    text = re.sub(pattern, lambda m: str(root / "transcripts") + "/" + short_legacy_id(m[1], reference_ids), text)
    return normalize_references(text, reference_ids)


def artifact_name(relative: str, reference_ids: dict) -> str:
    parts = relative.split("/")
    stem = parts[0]
    for suffix in (".tools.md", ".md"):
        if stem.endswith(suffix):
            parts[0] = short_legacy_id(stem[:-len(suffix)], reference_ids) + suffix
            break
    else:
        parts[0] = short_legacy_id(stem, reference_ids)
    return "/".join(parts)


def refresh_name(relative: str, reference_ids: dict) -> str:
    parts = relative.split("/")
    if len(parts) < 2 or parts[0] not in PREFIXES:
        return relative
    source, name = parts[:2]
    prefix = PREFIXES[source]
    if re.fullmatch(rf"{prefix}-[0-9a-f]{{16}}", name):
        return relative
    if re.fullmatch(rf"{prefix}-[0-9a-f]{{12}}", name):
        old = name.replace("-", ":", 1)
        if old not in reference_ids:
            raise ValueError(f"Cannot recover native identity for refresh job: {old}")
        parts[1] = reference_ids[old].replace(":", "-")
    else:
        native = name.removeprefix(source + "-")
        parts[1] = canonical_session_id(source, native).replace(":", "-")
    return "/".join(parts)


def normalized_row(table: str, row: dict, mapping: dict, root: Path, reference_ids: dict) -> dict:
    result = dict(row)
    owner = OWNERS[table]
    if row[owner] not in mapping:
        raise ValueError(f"Orphan fact owner in {table}: {row[owner]}")
    result[owner] = mapping[row[owner]]
    for key in TEXT_COLUMNS.get(table, ()):
        if isinstance(result.get(key), str):
            result[key] = normalize_text(result[key], root, reference_ids)
    return result


def row_digest(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_plan(root: Path) -> dict:
    with connect(root / "sessions.db", readonly=True) as conn:
        sessions = [dict(row) for row in conn.execute("SELECT * FROM sessions ORDER BY rowid")]
        reference_ids = historical_reference_ids(root, sessions)
        mapping, targets, identities = {}, {}, set()
        for row in sessions:
            source, native, old = row["source"], row["native_session_id"], row["session_id"]
            new = canonical_session_id(source, native)
            if new in targets and targets[new] != old:
                raise ValueError(f"Short ID collision: {new}")
            if (source, native) in identities:
                raise ValueError("Duplicate native identity")
            if old not in {new, new[:-4], native if source == "claude" else f"{source}:{native}"}:
                raise ValueError(f"Unexpected existing canonical identity: {old}")
            mapping[old], targets[new] = new, old
            identities.add((source, native))
        tables = {}
        missing_pointers = set()
        pointer_columns = {"transcript_path", "tool_log_path", "subagent_transcripts", "subagent_transcript_path"}
        for table in OWNERS:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            expected = [normalized_row(table, row, mapping, root, reference_ids) for row in rows]
            for row in rows:
                for column in pointer_columns & row.keys():
                    for path in (row[column] or "").split(", "):
                        if path.startswith(str(root / "transcripts") + "/") and not Path(path).is_file():
                            missing_pointers.add(normalize_text(path, root, reference_ids))
            tables[table] = {"count": len(rows), "before": row_digest(rows), "after": row_digest(expected)}

    files = []
    for component in ("transcripts", "refresh-jobs"):
        directory = root / component
        if not directory.exists():
            continue
        destinations = set()
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Unexpected symlink: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            if component == "refresh-jobs" and path.name in {"worker.pid", "dispatch.lock", "worker.lock"}:
                # Process coordination files are not portable state; workers must be stopped.
                files.append({"component": component, "old": relative, "new": None,
                              "before": file_digest(path), "after": None})
                continue
            new = artifact_name(relative, reference_ids) if component == "transcripts" else refresh_name(relative, reference_ids)
            if component == "transcripts":
                owner = new.split("/")[0].removesuffix(".md").removesuffix(".tools")
                if re.fullmatch(PREVIOUS_SHORT_ID, owner):
                    raise ValueError(f"Cannot recover native identity for artifact owner: {owner}")
            if new in destinations:
                raise ValueError(f"Artifact collision: {component}/{new}")
            destinations.add(new)
            data = transformed_file(path, component, root, reference_ids=reference_ids)
            files.append({"component": component, "old": relative, "new": new,
                          "before": file_digest(path), "after": hashlib.sha256(data).hexdigest()})
    # Include orphan artifact owners in collision detection, not just database rows.
    artifact_owners = dict(targets)
    for file in files:
        if file["component"] != "transcripts":
            continue
        old = file["old"].split("/")[0].removesuffix(".md").removesuffix(".tools")
        new = short_legacy_id(old, reference_ids)
        if is_canonical_session_id(new):
            previous = artifact_owners.setdefault(new, old)
            if previous != old:
                raise ValueError(f"Artifact/database identity collision: {new}")
    changed = sum(k != v for k, v in mapping.items())
    changes = changed or any(t["before"] != t["after"] for t in tables.values()) or any(
        f["new"] is not None and (f["old"] != f["new"] or f["before"] != f["after"]) for f in files)
    reference_path = root / REFERENCE_IDS_FILE
    reference_before = file_digest(reference_path) if reference_path.exists() else None
    reference_after = hashlib.sha256(reference_bytes(reference_ids)).hexdigest()
    changes = changes or (reference_before != reference_after and bool(reference_ids or reference_before))
    return {"version": 2, "root": str(root), "mapping": mapping, "tables": tables, "files": files,
            "reference_ids": reference_ids, "reference_before": reference_before, "reference_after": reference_after,
            "changed_sessions": changed, "changes": bool(changes), "missing_pointers": sorted(missing_pointers)}


def transformed_file(path: Path, component: str, root: Path, *, reference_ids: dict, relative: str | None = None) -> bytes:
    data = path.read_bytes()
    if component == "transcripts":
        if path.suffix != ".md":
            raise ValueError(f"Unexpected artifact format: {path}")
        return normalize_text(data.decode("utf-8"), root, reference_ids).encode("utf-8")
    if path.suffix == ".json":
        payload = json.loads(data)
        if isinstance(payload, dict) and "session_id" in payload:
            source = payload.get("source") or (relative.split("/")[0] if relative else path.relative_to(root / "refresh-jobs").parts[0])
            payload["session_id"] = refresh_session_id(source, reference_ids.get(payload["session_id"], payload["session_id"]))
            return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return data


def assert_no_workers() -> None:
    output = subprocess.check_output(["ps", "-axo", "pid,args"], text=True)
    for line in output.splitlines():
        # Match actual executable argv, not a shell command mentioning a worker.
        if re.search(r"\bpython\S*\s+\S*/hooks/(?:_session_refresh_worker|_session_end_worker)\.py\b", line, re.I):
            raise RuntimeError("Detached indexing workers are still active; stop them before migration")


def backup_and_stage(root: Path, run: Path, plan: dict) -> None:
    original, staged = run / "backup", run / "staged"
    original.mkdir(exist_ok=True)
    staged.mkdir(exist_ok=True)
    original_refs = original / REFERENCE_IDS_FILE
    if not original_refs.exists():
        temporary_refs = original_refs.with_suffix(".tmp")
        if plan["reference_before"] is None:
            temporary_refs.write_bytes(reference_bytes({}))
        else:
            live = root / REFERENCE_IDS_FILE
            if file_digest(live) != plan["reference_before"]:
                raise RuntimeError("Reference mapping changed before backup")
            shutil.copy2(live, temporary_refs)
        os.replace(temporary_refs, original_refs)
    expected_before = plan["reference_before"] or hashlib.sha256(reference_bytes({})).hexdigest()
    if file_digest(original_refs) != expected_before:
        raise RuntimeError("Reference mapping backup differs from manifest")
    (staged / REFERENCE_IDS_FILE).write_bytes(reference_bytes(plan["reference_ids"]))
    if not (original / "sessions.db").exists():
        temporary = original / "database.tmp"
        with connect(root / "sessions.db", readonly=True) as source, closing(sqlite3.connect(temporary)) as backup:
            source.backup(backup)
        os.replace(temporary, original / "sessions.db")
    for component in ("transcripts", "refresh-jobs"):
        (original / component).mkdir(exist_ok=True)
        (staged / component).mkdir(exist_ok=True)
    with connect(original / "sessions.db", readonly=True) as source, closing(sqlite3.connect(staged / "sessions.db")) as dest:
        source.backup(dest)
    for file in plan["files"]:
        source = original / file["component"] / file["old"]
        if not source.exists():
            live = root / file["component"] / file["old"]
            if file_digest(live) != file["before"]:
                raise RuntimeError("Store changed during backup; migration stopped")
            source.parent.mkdir(parents=True, exist_ok=True)
            temporary = source.with_name(source.name + ".backup-tmp")
            shutil.copy2(live, temporary)
            os.replace(temporary, source)
        if file_digest(source) != file["before"]:
            raise RuntimeError("Backup differs from manifest; migration stopped")
        if file["new"] is None:
            continue
        dest = staged / file["component"] / file["new"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = transformed_file(source, file["component"], root, reference_ids=plan["reference_ids"], relative=file["old"])
        if hashlib.sha256(data).hexdigest() != file["after"]:
            raise RuntimeError("Store changed during staging; migration stopped")
        dest.write_bytes(data)
    with connect(staged / "sessions.db") as conn:
        for table in OWNERS:
            rows = conn.execute(f"SELECT rowid AS _rowid, * FROM {table} ORDER BY rowid").fetchall()
            before = [{k: row[k] for k in row.keys() if k != "_rowid"} for row in rows]
            if row_digest(before) != plan["tables"][table]["before"]:
                raise RuntimeError("Database changed during backup; migration stopped")
            for row, data in zip(rows, before):
                updated = normalized_row(table, data, plan["mapping"], root, plan["reference_ids"])
                changes = {k: v for k, v in updated.items() if v != data[k]}
                if changes:
                    columns = ", ".join(f'"{key}" = ?' for key in changes)
                    conn.execute(f"UPDATE {table} SET {columns} WHERE rowid = ?", (*changes.values(), row["_rowid"]))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    validate(staged, plan)


def validate(store: Path, plan: dict) -> None:
    if file_digest(store / REFERENCE_IDS_FILE) != plan["reference_after"]:
        raise RuntimeError("Missing or incorrect historical reference mapping")
    with connect(store / "sessions.db", readonly=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        for table, expected in plan["tables"].items():
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            if len(rows) != expected["count"] or row_digest(rows) != expected["after"]:
                raise RuntimeError(f"Migrated data differs from expected values: {table}")
    for file in plan["files"]:
        if file["new"] is None:
            continue
        path = store / file["component"] / file["new"]
        if not path.is_file() or file_digest(path) != file["after"]:
            raise RuntimeError(f"Missing or incorrect migrated artifact: {path}")
    # Require every previously existing managed pointer to keep resolving.
    root = Path(plan["root"])
    paths = {str(root / f["component"] / f["new"]) for f in plan["files"] if f["new"] is not None}
    allowed_missing = set(plan["missing_pointers"])
    with connect(store / "sessions.db", readonly=True) as conn:
        for table, columns in (("sessions", ("transcript_path", "tool_log_path", "subagent_transcripts")),
                               ("subagent_runs", ("transcript_path",)),
                               ("skill_invocations", ("subagent_transcript_path",))):
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                for value in row:
                    for path in (value or "").split(", "):
                        if path.startswith(str(root / "transcripts") + "/") and path not in paths:
                            # Don't fabricate artifacts absent before migration.
                            if path not in allowed_missing:
                                raise RuntimeError(f"Broken migrated pointer: {path}")


def cutover(root: Path, run: Path, plan: dict) -> None:
    displaced = run / "displaced"
    displaced.mkdir(exist_ok=True)
    staged = run / "staged"
    for component in COMPONENTS:
        if not (staged / component).exists():
            if not (root / component).exists():
                raise RuntimeError(f"Neither staged nor installed component exists: {component}")
            continue
        if component == "sessions.db" and (root / component).exists():
            with connect(root / component) as conn:
                result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result[0]:
                    raise RuntimeError("SQLite WAL is busy; refusing cutover")
        if (root / component).exists():
            if (displaced / component).exists():
                raise RuntimeError(f"Unexpected concurrent write during cutover: {component}")
            os.replace(root / component, displaced / component)
        os.replace(staged / component, root / component)
    validate(root, plan)


def migrate(root: Path, *, apply: bool = False) -> dict:
    root = root.expanduser().resolve()
    marker = root / "identity-migration.json"
    if not apply:
        plan = build_plan(root)
        return {"changed_sessions": plan["changed_sessions"], "files": len(plan["files"]),
                "changes": plan["changes"], "tables": {k: v["count"] for k, v in plan["tables"].items()}}
    with (root / "indexing.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert_no_workers()
        state = json.loads(marker.read_text()) if marker.exists() else {}
        if state.get("run"):
            run = Path(state["run"])
            plan = json.loads((run / "manifest.json").read_text())
            if plan.get("version") != 2 or plan["root"] != str(root):
                raise RuntimeError("Migration manifest belongs to a different store or ID format")
        else:
            plan = build_plan(root)
            if not plan["changes"]:
                return {"changed_sessions": 0, "changes": False}
            required = sum(p.stat().st_size for name in COMPONENTS for p in
                           ((root / name).rglob("*") if (root / name).is_dir() else [root / name]) if p.is_file()) * 3
            if shutil.disk_usage(root).free < required:
                raise RuntimeError("Insufficient disk space for backup and staging")
            run = root / "backups" / f"short-ids-{time.time_ns()}"
            run.mkdir(parents=True)
            atomic_json(run / "manifest.json", plan)
            state = {"run": str(run), "status": "preparing"}
            atomic_json(marker, state)
        if state["status"] == "preparing":
            backup_and_stage(root, run, plan)
            state["status"] = "prepared"
            atomic_json(marker, state)
        cutover(root, run, plan)
        atomic_json(run / "result.json", {"status": "complete", "changed_sessions": plan["changed_sessions"]})
        marker.unlink()
        return {"changed_sessions": plan["changed_sessions"], "changes": True, "backup": str(run / "backup")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".session-index")
    parser.add_argument("--apply", action="store_true", help="Back up and migrate offline, or resume a prepared cutover")
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_dir, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
