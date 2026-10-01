"""One-time project metadata backfill; explicit reviewed mappings, no prefix rules.

uv run backfill_projects.py --db /path/to/sessions.db --mapping /path/to/map.json
Add --apply to write. Every apply makes a SQLite backup before schema/data writes.
The mapping JSON is an object of exact old indexed paths to canonical projects.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from db import init_db
from project_identity import resolve_project

FIELDS = ("project_id", "project_path", "project", "cwd", "worktree_path")


def plan_backfill(conn: sqlite3.Connection, mapping: dict[str, str]) -> list[dict]:
    """Inspect metadata only; historical source logs and generated text stay intact."""
    targets = {}
    for old, target in mapping.items():
        if not os.path.isabs(old) or not os.path.isabs(target):
            raise ValueError("Mapping paths must be absolute")
        identity = resolve_project(target)
        if not identity.project_id.startswith("git:"):
            raise ValueError(f"Mapping target is not an accessible Git repository: {target}")
        observed = resolve_project(old)
        if observed.project_id.startswith("git:") and observed.project_id != identity.project_id:
            raise ValueError(f"Mapping would merge independent Git repositories: {old} -> {target}")
        targets[old] = identity

    plan = []
    resolved = {}
    cursor = conn.execute("SELECT * FROM sessions ORDER BY session_id")
    columns = [item[0] for item in cursor.description]
    for values in cursor:
        row = dict(zip(columns, values))
        before = {field: row.get(field) for field in FIELDS}
        location = row.get("worktree_path") or row.get("project_path")
        if not location:
            continue
        # Existing identities need no filesystem reinterpretation. Explicit
        # mappings may still correct a previously recorded directory fallback.
        if row.get("project_id") and location not in targets:
            continue
        if location not in resolved:
            resolved[location] = resolve_project(location)
        identity = targets.get(location) or resolved[location]
        after = dict(before, project_id=identity.project_id,
                     project_path=identity.project_path, project=identity.project)
        # The legacy path was a checkout root, not necessarily the starting cwd.
        # Never invent a historical cwd from it; preserve the known path instead.
        if identity.project_id.startswith("git:"):
            after["worktree_path"] = before["worktree_path"] or location
        if before != after:
            plan.append({"session_id": row["session_id"], "before": before, "after": after,
                         "mapping": location in targets})
    return plan


def apply_backfill(conn: sqlite3.Connection, plan: list[dict]) -> None:
    """All metadata changes commit together; concurrent changes fail closed."""
    assignments = ", ".join(f"{field} = :new_{field}" for field in FIELDS)
    expected = " AND ".join(f"{field} IS :old_{field}" for field in FIELDS)
    with conn:
        for item in plan:
            params = {"session_id": item["session_id"]}
            params.update({f"old_{field}": value for field, value in item["before"].items()})
            params.update({f"new_{field}": value for field, value in item["after"].items()})
            result = conn.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = :session_id AND {expected}", params,
            )
            if result.rowcount != 1:
                raise ValueError(f"Session changed since planning: {item['session_id']}; backfill rolled back")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Database to inspect or update")
    parser.add_argument("--mapping", required=True, help="Reviewed JSON mapping of exact old paths to canonical project paths")
    parser.add_argument("--apply", action="store_true", help="Back up, then apply metadata changes (default: read-only plan)")
    args = parser.parse_args()
    path = Path(args.db).expanduser().resolve(strict=True)
    mapping = json.loads(Path(args.mapping).read_text())
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        parser.error("mapping must be a JSON object of old path -> target path")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    plan = plan_backfill(conn, mapping)
    backup = None
    if args.apply:
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}-project-identity.db"
        with sqlite3.connect(backup) as dest:
            conn.backup(dest)
        conn.close()
        conn = sqlite3.connect(path)
        init_db(conn)
        apply_backfill(conn, plan)
    conn.close()
    print(json.dumps({"applied": args.apply, "backup": str(backup) if backup else None,
                      "sessions_updated": len(plan),
                      "sessions_regrouped": sum(item["before"]["project_path"] != item["after"]["project_path"] for item in plan),
                      "changes": plan}, indent=2))


if __name__ == "__main__":
    main()
