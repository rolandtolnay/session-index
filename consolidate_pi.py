"""One-time, fail-closed consolidation of historical Pi worktree continuations.

plan creates a read-only inventory and rehearses eligible chains in an isolated
store. apply requires the exact plan digest; rollback refuses subsequent changes.
Raw files are never written. Retired generated files remain compatibility copies.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

import db
import indexer
import pi_continuations as pc
import tool_log
import transcript
from pi_parser import discover_pi_subagents, select_pi_branch
from session_identity import canonical_session_id

FACTS = {
    "sessions": "session_id", "tool_calls": "session_id", "file_mutations": "session_id",
    "question_answers": "session_id", "skill_invocations": "session_id",
    "subagent_runs": "parent_session_id", "side_chats": "parent_session_id",
}
REGISTRY = {
    "pi_conversation_owners": "session_id", "pi_continuation_sources": "owner_id",
    "pi_continuation_children": "owner_id", "pi_transfers": "owner_id",
    "pi_session_aliases": "owner_id",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".consolidating-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic_bytes(path, json.dumps(value, indent=2).encode() + b"\n")


def file_hash(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Symlinks are not migration inputs: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def read_raw(path):
    entries = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    pc._validate_header(entries)
    if len(entries) < 2:
        raise ValueError("Empty source tree")
    if any(not isinstance(e, dict) or (e.get("type") == "message" and not isinstance(e.get("message"), dict)) for e in entries[1:]):
        raise ValueError("Malformed source entry")
    select_pi_branch(entries, entries[-1]["id"])
    ids = {e["id"] for e in entries[1:]}
    if any(e.get("parentId") is not None and e["parentId"] not in ids for e in entries[1:]):
        raise ValueError("Broken source tree")
    return entries


def activity(entries):
    return [e for e in entries if e.get("type") == "message" and e.get("message", {}).get("role") in {"user", "assistant"}]


def headers(raw_root, rows):
    paths = set(Path(raw_root).glob("*/*.jsonl")) | {Path(r["source_path"]) for r in rows if r.get("source_path")}
    found = {}
    for path in sorted(paths):
        try:
            with path.open() as handle:
                header = json.loads(next(handle))
            if header.get("type") == "session":
                found[str(path.absolute())] = header
        except (OSError, ValueError, StopIteration):
            continue
    return found


def parent_path(path, header):
    value = header.get("parentSession")
    if not isinstance(value, str) or not value:
        return None
    return pc._resolve_parent_session_path(path, value)


def discover(conn, raw_root, *, idle_seconds):
    """Only copied cross-worktree chains are candidates; other lineage is a boundary."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE source='pi'")]
    indexed = {r["source_path"]: r for r in rows}
    hs = headers(raw_root, rows)
    edges, rejected, blocked = {}, [], set()
    now = time.time()
    active = os.environ.get("SESSION_INDEX_SOURCE_PATH")
    for child, h in hs.items():
        parent = parent_path(child, h)
        if not parent:
            continue
        try:
            if parent not in hs:
                raise ValueError("parent raw file missing")
            if h.get("cwd") == hs[parent].get("cwd"):
                raise ValueError("same-directory lineage is not a worktree transfer")
            # An unreadable cross-worktree descendant cannot be silently
            # dropped in favor of an earlier terminal. Only a proven fresh
            # conversation establishes an independent boundary.
            edges[child] = parent
            a, b = read_raw(parent), read_raw(child)
            by_b = {e["id"]: e for e in b[1:]}
            shared = {e["id"] for e in activity(a)} & {e["id"] for e in activity(b)}
            if not shared:
                del edges[child]
                raise ValueError("fresh conversation, not copied history")
            if active in {parent, child} or any(now - Path(p).stat().st_mtime < idle_seconds for p in (parent, child)):
                raise ValueError("active or too recent")
            copied = [e for e in a[1:] if e["id"] in by_b]
            if any(by_b[e["id"]] != e for e in copied):
                raise ValueError("shared entry content differs")
            missing = [e for e in a[1:] if e["id"] not in by_b]
            if any(e["type"] != "session_info" or not e.get("name", "").startswith("Closed branch: ")
                   or not e.get("timestamp") or e["timestamp"] < h.get("timestamp", "") for e in missing):
                raise ValueError("parent has uncopied evidence or divergent activity")
            selected_a = activity(select_pi_branch(a, a[-1]["id"]).branch)
            selected_b = activity(select_pi_branch(b, b[-1]["id"]).branch)
            if selected_b[:len(selected_a)] != selected_a:
                raise ValueError("selected history is not preserved as a prefix")
            new = [e for e in b[1:] if e["id"] not in {v["id"] for v in a[1:]}]
            names = [e.get("name", "") for e in new if e["type"] == "session_info"]
            branch = any(name.startswith("Branch: ") for name in names)
            returning = (hs.get(parent_path(parent, hs[parent]), {}).get("cwd") == h.get("cwd")
                         and bool(names))
            if not branch and not returning:
                raise ValueError("no worktree workflow marker")
        except (OSError, ValueError, KeyError, TypeError) as error:
            rejected.append({"source": parent, "destination": child, "reason": str(error)})
            if child in edges:
                blocked.update((parent, child))
    adjacent = defaultdict(set)
    for child, parent in edges.items():
        adjacent[parent].add(child)
        adjacent[child].add(parent)
    groups, seen = [], set()
    for start in sorted(adjacent):
        if start in seen:
            continue
        component, pending = set(), [start]
        while pending:
            p = pending.pop()
            if p in component:
                continue
            component.add(p)
            pending.extend(adjacent[p] - component)
        seen.update(component)
        roots = component - edges.keys()
        reason = None
        if component & blocked:
            reason = "chain contains an uncertain transfer"
        elif len(roots) != 1 or any(sum(parent == p for parent in edges.values()) > 1 for p in component):
            reason = "branching or cyclic transfer chain"
        if reason:
            rejected.append({"paths": sorted(component), "reason": reason})
            continue
        root = next(iter(roots))
        chain = [root]
        while child := next((c for c,p in edges.items() if p == chain[-1]), None):
            chain.append(child)
        existing = [indexed[p] for p in chain if p in indexed]
        owner = canonical_session_id("pi", hs[root]["id"])
        if (root not in indexed or indexed[root]["session_id"] != owner
                or len(existing) < 2):
            rejected.append({"paths": chain, "reason": "no indexed duplicate with its original owner"})
            continue
        if len({r["project_id"] for r in existing}) != 1 or not existing[0]["project_id"]:
            rejected.append({"paths": chain, "reason": "project membership is not established"})
            continue
        if len({r["hidden_from_recents"] for r in existing}) != 1:
            rejected.append({"paths": chain, "reason": "visibility differs"})
            continue
        ids = [canonical_session_id("pi", hs[p]["id"]) for p in chain]
        placeholders = ",".join("?" for _ in ids)
        if conn.execute(f"SELECT 1 FROM pi_continuation_sources WHERE native_session_id IN ({placeholders}) OR owner_id IN ({placeholders})",
                        [hs[p]["id"] for p in chain] + ids).fetchone():
            rejected.append({"paths": chain, "reason": "already registered; not a historical migration candidate"})
            continue
        groups.append({"owner": owner, "paths": chain, "ids": ids,
                       "retired": [r["session_id"] for r in existing if r["session_id"] != owner]})
    return groups, rejected


def require_schema(conn):
    """Inspect the established schema; never run global maintenance on live data."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(db.SCHEMA)
        for table in FACTS | REGISTRY:
            actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if table == "pi_session_aliases" and not actual:
                continue  # The only new table this migration may create.
            expected = {r[1] for r in reference.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise ValueError(f"Upgrade the index schema separately before consolidation: {table}")
    finally:
        reference.close()


def snapshot(conn, ids):
    placeholders = ",".join("?" for _ in ids)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {table: sorted([dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {column} IN ({placeholders})", ids)], key=encoded)
            if table in tables else [] for table, column in (FACTS | REGISTRY).items()}


def restore(conn, ids, state):
    placeholders = ",".join("?" for _ in ids)
    for table, column in (FACTS | REGISTRY).items():
        conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids)
        for row in state[table]:
            columns = ",".join(row)
            values = ",".join("?" for _ in row)
            conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", tuple(row.values()))


@contextmanager
def storage(root):
    """The standalone migration runs one isolated indexer at a time."""
    bindings = [(db, "DATA_DIR", str(root)), (db, "DB_PATH", str(root / "sessions.db")),
                (transcript, "TRANSCRIPT_DIR", str(root / "transcripts")),
                (tool_log, "TRANSCRIPT_DIR", str(root / "transcripts"))]
    old = [(module, name, getattr(module, name)) for module,name,_ in bindings]
    try:
        for module,name,value in bindings:
            setattr(module, name, value)
        yield
    finally:
        for module,name,value in old:
            setattr(module, name, value)


def raw_inputs(group):
    paths = set(group["paths"])
    for source in group["paths"]:
        for info in discover_pi_subagents(source):
            paths.add(info.jsonl_path)
            if info.meta_path:
                paths.add(info.meta_path)
    return {p: file_hash(p) for p in sorted(paths)}


def artifact_paths(state):
    paths = set()
    for row in state["sessions"]:
        paths.update(row[k] for k in ("transcript_path", "tool_log_path") if row[k])
        paths.update(p.strip() for p in (row["subagent_transcripts"] or "").split(",") if p.strip())
    for table in ("subagent_runs", "side_chats"):
        paths.update(r["transcript_path"] for r in state[table] if r["transcript_path"])
    return paths


def relative_artifact(path, root):
    path, root = Path(path), Path(root)
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError(f"Non-canonical artifact path: {path}")
    rel = path.relative_to(root / "transcripts")
    if path.suffix != ".md":
        raise ValueError(f"Unexpected artifact: {path}")
    return str(Path("transcripts") / rel)


def tool_sections(text):
    lines = text.splitlines(keepends=True)
    headings = tool_log._tool_headings(lines)
    return {seq: "".join(lines[start+1:(headings[i+1][0] if i+1<len(headings) else len(lines))]).strip()
            for i,(start,seq) in enumerate(headings)}


def main_body(text):
    lines = text.splitlines()
    start = next((i for i,l in enumerate(lines) if l.startswith(("[user] ", "[assistant] "))), len(lines))
    return "\n".join(lines[start:]).strip()


def verify_evidence(before, after, source_root, archive, staged):
    """Verify final publication bytes, not a pre-relocation intermediate."""
    def publication(path):
        return staged / relative_artifact(path, source_root)

    for session in before["sessions"]:
        sid = session["session_id"]
        path_map, scope_map, agent_map = {}, {}, {}
        new_subs = {r["child_index"]: r for r in after["subagent_runs"] if r["child_index"] is not None}
        for old in before["subagent_runs"]:
            if old["parent_session_id"] != sid or old["child_index"] is None:
                continue
            new = new_subs.get(old["child_index"])
            if not new:
                raise ValueError("existing subagent reference would disappear")
            agent_map[old["agent_id"]] = new["agent_id"]
            scope_map["agent-" + (old["agent_id"] or "unknown")] = "agent-" + (new["agent_id"] or "unknown")
            if old["transcript_path"]:
                if not new["transcript_path"]:
                    raise ValueError("existing child transcript would disappear")
                path_map[old["transcript_path"]] = new["transcript_path"]
                a = (archive / relative_artifact(old["transcript_path"], source_root)).read_text()
                b = publication(new["transcript_path"]).read_text()
                a = "\n".join(l for l in a.splitlines() if not l.startswith("Parent: "))
                b = "\n".join(l for l in b.splitlines() if not l.startswith("Parent: "))
                if a != b:
                    raise ValueError("child transcript evidence changed")
        for table, column in FACTS.items():
            if table == "sessions":
                continue
            def normalized(row, old=False):
                row = dict(row)
                row.pop(column)
                if table == "side_chats":
                    row.pop("headline")
                    row.pop("transcript_path")
                if old:
                    if "scope" in row:
                        row["scope"] = scope_map.get(row["scope"], row["scope"])
                    if "agent_id" in row:
                        row["agent_id"] = agent_map.get(row["agent_id"], row["agent_id"])
                    for key in ("transcript_path", "subagent_transcript_path"):
                        if key in row:
                            row[key] = path_map.get(row[key], row[key])
                return encoded(row)
            new_rows = Counter(normalized(r) for r in after[table])
            old_rows = Counter(normalized(r, True) for r in before[table] if r[column] == sid)
            if old_rows - new_rows:
                raise ValueError(f"{table}: existing evidence or reference ordinals would change ({sid})")
        if session["tool_log_path"]:
            old = tool_sections((archive / relative_artifact(session["tool_log_path"], source_root)).read_text())
            new = tool_sections(publication(after["sessions"][0]["tool_log_path"]).read_text())
            if any(new.get(seq) != body for seq,body in old.items()):
                raise ValueError("tool arguments/results at an existing ordinal changed")
        if session["transcript_path"]:
            old = main_body((archive / relative_artifact(session["transcript_path"], source_root)).read_text())
            for a,b in agent_map.items():
                old = old.replace(f"→ see: agent-{a}.md", f"→ see: agent-{b}.md")
            new = main_body(publication(after["sessions"][0]["transcript_path"]).read_text())
            if not old or not new.startswith(old):
                raise ValueError("existing Clean Transcript is not a preserved prefix")


def stage_group(group, staged, before):
    with storage(staged):
        conn = db.get_connection()
        try:
            db.init_db(conn)
            db.delete_sessions(conn, group["retired"])
            # Retain the most advanced indexed description, not the short opener.
            by_path = {r["source_path"]: r for r in before["sessions"]}
            latest = next(by_path[p] for p in reversed(group["paths"]) if p in by_path)
            fields = ("summary", "headline", "substance_band", "substance_reason")
            conn.execute("UPDATE sessions SET " + ",".join(f"{f}=COALESCE(?,{f})" for f in fields) + " WHERE session_id=?",
                         [latest[f] for f in fields] + [group["owner"]])
            for row in before["side_chats"]:
                row = dict(row, parent_session_id=group["owner"])
                conn.execute("INSERT OR IGNORE INTO side_chats (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
            conn.commit()
            for source,destination in zip(group["paths"],group["paths"][1:]):
                a,b = read_raw(source),read_raw(destination)
                copied = {e["id"] for e in b[1:]}
                captured = [a[0], *(e for e in a[1:] if e["id"] in copied)]
                if pc.record_transfer(source,destination,source_entries=captured) != group["owner"]:
                    raise ValueError("unexpected indexed owner")
            result = indexer.index_source_transcript("pi", group["paths"][-1], indexer.NO_SUMMARY_INDEX_OPTIONS)
            if result.skipped_reason:
                raise ValueError(result.skipped_reason)
            for sid in group["retired"]:
                conn.execute("INSERT INTO pi_session_aliases VALUES (?,?)", (sid,group["owner"]))
            conn.commit()
            first = snapshot(conn, group["ids"])
            hashes = {str(p):file_hash(p) for p in (staged/"transcripts").rglob("*.md")}
            for ancestor in group["paths"][:-1]:
                if indexer.index_source_transcript("pi", ancestor, indexer.NO_SUMMARY_INDEX_OPTIONS).skipped_reason != "unchanged transferred Pi ancestor":
                    raise ValueError("ancestor would be indexed again")
            indexer.index_source_transcript("pi", group["paths"][-1], indexer.NO_SUMMARY_INDEX_OPTIONS)
            if snapshot(conn,group["ids"]) != first or any(file_hash(p)!=h for p,h in hashes.items()):
                raise ValueError("deterministic re-indexing is not idempotent")
            return first
        finally:
            conn.close()


def code_hash():
    return digest({p.name:file_hash(p) for p in sorted(Path(__file__).resolve().parent.glob("*.py"))})


def relocate_rows(state, staged, root):
    """Only persisted artifact locators change; evidence strings are untouched."""
    locators = {"sessions": ("transcript_path", "tool_log_path"),
                "subagent_runs": ("transcript_path",), "side_chats": ("transcript_path",),
                "skill_invocations": ("subagent_transcript_path",)}
    result = {table: [dict(row) for row in rows] for table, rows in state.items()}
    def relocate(path):
        return str(root / Path(path).relative_to(staged)) if path else path
    for table, fields in locators.items():
        for row in result[table]:
            for field in fields:
                row[field] = relocate(row[field])
            if table == "sessions" and row["subagent_transcripts"]:
                row["subagent_transcripts"] = ", ".join(relocate(p.strip()) for p in row["subagent_transcripts"].split(","))
    return result


def relocate_navigation(path, owner, staged, root):
    """Relocate producer-owned navigation only, never quoted paths in evidence."""
    text = path.read_text()
    old, new = str(staged / "transcripts"), str(root / "transcripts")
    if path.name == owner + ".md":
        start = text.find("## Related artifacts\n")
        end = text.find("\n\n---\n", start)
        if start >= 0 and end >= 0:
            text = text[:start] + text[start:end].replace(old, new) + text[end:]
    elif path.name.startswith("side-chat-"):
        lines = text.splitlines(keepends=True)
        if len(lines) > 2 and lines[2].startswith("Parent: `" + old + "/"):
            lines[2] = lines[2].replace(old, new, 1)
            text = "".join(lines)
    path.write_text(text)


def plan(root, raw_root, output, *, idle_seconds=86400):
    root, raw_root, output = (Path(p).expanduser().resolve() for p in (root,raw_root,output))
    if output == root or root in output.parents or output == raw_root or raw_root in output.parents:
        raise ValueError("Rehearsal output must be outside live index and raw storage")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    staged, archive = output/"after", output/"before"
    staged.mkdir(); archive.mkdir()
    source = db._get_readonly_connection(str(root/"sessions.db"))
    try:
        require_schema(source)
        with sqlite3.connect(staged/"sessions.db") as dest:
            source.backup(dest)
        with storage(staged):
            conn = db.get_connection()
            try:
                db.init_db(conn)
                groups,rejected = discover(conn,raw_root,idle_seconds=idle_seconds)
            finally:
                conn.close()
        approved = []
        for group in groups:
            conn = db._get_readonly_connection(str(staged/"sessions.db"))
            before = snapshot(conn,group["ids"]); conn.close()
            try:
                inputs = raw_inputs(group)
                originals = {}
                for path in sorted(artifact_paths(before)):
                    rel = relative_artifact(path,root)
                    content = Path(path).read_bytes()
                    destination = archive/rel
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(content)
                    originals[rel] = hashlib.sha256(content).hexdigest()
                after = stage_group(group,staged,before)
                if raw_inputs(group)!=inputs or snapshot(source,group["ids"])!=before:
                    raise ValueError("inputs changed during rehearsal")
                if any(file_hash(root/rel)!=h for rel,h in originals.items()):
                    raise ValueError("artifacts changed during rehearsal")
                after = relocate_rows(after, staged, root)
                publications = {}
                for path in sorted((staged/"transcripts").glob(group["owner"]+"*")):
                    files = path.rglob("*.md") if path.is_dir() else [path]
                    for generated in files:
                        rel = str(generated.relative_to(staged))
                        relocate_navigation(generated, group["owner"], staged, root)
                        publications[rel] = file_hash(generated)
                        if rel not in originals:
                            originals[rel] = file_hash(root/rel)
                            if originals[rel] is not None:
                                dest = archive/rel; dest.parent.mkdir(parents=True,exist_ok=True)
                                shutil.copyfile(root/rel,dest)
                verify_evidence(before,after,root,archive,staged)
                approved.append(dict(group,before=before,after=after,inputs=inputs,
                                     originals=originals,publications=publications))
            except (OSError, ValueError, KeyError, sqlite3.Error) as error:
                # A failed rehearsal must not leave its tentative ownership in
                # the final scratch index beside the approved groups.
                with sqlite3.connect(staged/"sessions.db") as conn:
                    restore(conn,group["ids"],before)
                for path in (staged/"transcripts").glob(group["owner"]+"*"):
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                rejected.append({"paths":group["paths"],"reason":str(error)})
        manifest = {"version":1,"root":str(root),"raw_root":str(raw_root),"code":code_hash(),
                    "idle_seconds":idle_seconds,"groups":approved,"excluded":rejected}
        receipt = {"digest":digest(manifest),"manifest":manifest}
        save_json(output/"plan.json", receipt)
        return {"plan":str(output/"plan.json"),"digest":receipt["digest"],"groups":len(approved),
                "duplicate_rows_removed":sum(len(g["retired"]) for g in approved),
                "excluded_reasons":dict(Counter(e["reason"] for e in rejected))}
    finally:
        source.close()


def check_raw(groups):
    if any(raw_inputs(g) != g["inputs"] for g in groups):
        raise ValueError("Raw inputs or child inventory changed; refusing mutation")


def check_files(root, expected):
    if any(file_hash(root/rel)!=value for rel,value in expected.items()):
        raise ValueError("Generated artifacts changed; refusing overwrite")


def publish_files(root, copies, expected):
    for rel,value in expected.items():
        target = root/rel
        if value is None:
            target.unlink(missing_ok=True)
        else:
            atomic_bytes(target, (copies/rel).read_bytes())


def apply(plan_path, approval, *, rollback=False):
    plan_path = Path(plan_path).absolute()
    receipt = json.loads(plan_path.read_text()); manifest = receipt["manifest"]
    if digest(manifest)!=receipt["digest"] or approval!=receipt["digest"]:
        raise ValueError("Exact reviewed plan digest required")
    if manifest["code"]!=code_hash():
        raise ValueError("Migration/indexing code changed; rehearse again")
    root, output = Path(manifest["root"]), plan_path.parent
    groups = manifest["groups"]
    if not groups:
        return {"status":"nothing to apply"}
    ids = sorted({sid for g in groups for sid in g["ids"]})
    journal = output/"journal.json"
    with storage(root), pc.ownership_lock(), ExitStack() as locks:
        from indexing_lock import indexing_lock
        for sid in ids:
            locks.enter_context(indexing_lock(root,sid))
        conn = db.get_connection()
        try:
            state = json.loads(journal.read_text()) if journal.exists() else None
            desired = "rolled_back" if rollback else "applied"
            if state and state["status"]==desired:
                return {"status":desired,"already_complete":True}
            if rollback and not state:
                raise ValueError("No application to roll back")
            interrupted = state and state["status"]=="applying"
            if interrupted and not rollback:
                raise ValueError("Interrupted application; run rollback with this approved plan first")
            require_schema(conn)
            check_raw(groups)
            if not rollback:
                eligible,_ = discover(conn,manifest["raw_root"],idle_seconds=manifest["idle_seconds"])
                paths = {tuple(g["paths"]) for g in eligible}
                if any(tuple(g["paths"]) not in paths or raw_inputs(g)!=g["inputs"] for g in groups):
                    raise ValueError("Raw inputs, child inventory, or eligibility changed; rehearse again")
            expected_key, target_key = ("after","before") if rollback else ("before","after")
            # Full SQLite backup precedes writes; rollback restores only affected
            # rows, so unrelated sessions indexed since application are retained.
            if not rollback and not (output/"backup.db").exists():
                with sqlite3.connect(output/"backup.db") as backup:
                    conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS pi_session_aliases (alias_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL)")
            for g in groups:
                current = snapshot(conn,g["ids"])
                if current!=g[expected_key] and not (interrupted and current==g["before"]):
                    raise ValueError("Indexed evidence changed; refusing overwrite")
                expected = dict(g["originals"])
                if rollback:
                    expected.update(g["publications"])
                if interrupted:
                    if any(file_hash(root/rel) not in {value,g["originals"].get(rel)} for rel,value in expected.items()):
                        raise ValueError("Interrupted files contain unknown changes; refusing overwrite")
                else:
                    check_files(root,expected)
                check_files(output/"before",{r:h for r,h in g["originals"].items() if h is not None})
                check_files(output/"after",g["publications"])
            check_raw(groups)
            save_json(journal, {"status":"applying","direction":desired})
            try:
                for g in groups:
                    files = ({rel:g["originals"][rel] for rel in g["publications"]}
                             if rollback else g["publications"])
                    publish_files(root,output/("before" if rollback else "after"),files)
                    restore(conn,g["ids"],g[target_key])
                check_raw(groups)
                conn.commit()
            except BaseException:
                conn.rollback()
                for g in groups:
                    # SQLite rollback normally already restored the rows (and
                    # may have undone creation of the alias table). Compensate
                    # rows explicitly only if commit completed before failure.
                    if snapshot(conn,g["ids"]) != g[expected_key]:
                        restore(conn,g["ids"],g[expected_key])
                    files = (g["publications"] if rollback else {r:g["originals"][r] for r in g["publications"]})
                    publish_files(root,output/("after" if rollback else "before"),files)
                conn.commit()
                save_json(journal, {"status":"applied" if rollback else "rolled_back"})
                raise
            save_json(journal, {"status":desired,"at":datetime.now(timezone.utc).isoformat()})
            return {"status":desired,"groups":len(groups),"backup":str(output/"backup.db")}
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command",required=True)
    p = commands.add_parser("plan")
    p.add_argument("--data-dir",default=str(Path.home()/".session-index"))
    p.add_argument("--raw-root",default=str(Path.home()/".pi/agent/sessions"))
    p.add_argument("--output",required=True)
    p.add_argument("--idle-hours",type=float,default=24)
    for command in ("apply","rollback"):
        p = commands.add_parser(command)
        p.add_argument("--plan",required=True)
        p.add_argument("--approve",required=True,help="Exact digest printed by plan")
    args = parser.parse_args()
    try:
        result = (plan(args.data_dir,args.raw_root,args.output,idle_seconds=max(0,args.idle_hours*3600))
                  if args.command=="plan" else apply(args.plan,args.approve,rollback=args.command=="rollback"))
        print(json.dumps(result,indent=2))
    except (OSError,ValueError,sqlite3.Error) as error:
        parser.exit(1,f"Consolidation refused: {error}\n")


if __name__ == "__main__":
    main()
