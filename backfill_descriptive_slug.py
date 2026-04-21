"""One-off: upgrade historical rows' slug to the descriptive slug emitted when
a plan is accepted (custom-title / agent-name JSONL entries).

Only touches the `slug` column — no summary regeneration, no transcript rewrite,
no Ollama calls. Safe to re-run.
"""
import glob
import json
import os
import re
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/.session-index/sessions.db")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def extract_descriptive_slug(jsonl_path: str) -> str | None:
    """Scan a JSONL for custom-title/agent-name entries. Return first match."""
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line or '"custom-title"' not in line and '"agent-name"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type", "")
                candidate = ""
                if etype == "custom-title":
                    candidate = entry.get("customTitle", "")
                elif etype == "agent-name":
                    candidate = entry.get("agentName", "")
                if candidate and _SLUG_RE.match(candidate):
                    return candidate
    except OSError:
        return None
    return None


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT session_id, slug FROM sessions").fetchall()
    total = len(rows)
    print(f"Scanning {total} sessions...")

    updated = 0
    unchanged = 0
    no_jsonl = 0
    no_slug = 0

    for i, row in enumerate(rows, 1):
        sid = row["session_id"]
        current_slug = row["slug"]

        matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{sid}.jsonl"))
        if not matches:
            no_jsonl += 1
            continue

        descriptive = extract_descriptive_slug(matches[0])
        if not descriptive:
            no_slug += 1
            continue

        if descriptive == current_slug:
            unchanged += 1
            continue

        conn.execute(
            "UPDATE sessions SET slug = ? WHERE session_id = ?",
            (descriptive, sid),
        )
        updated += 1
        print(f"[{i}/{total}] {sid[:12]}  {current_slug!r} -> {descriptive!r}")

    conn.commit()
    conn.close()

    print()
    print(f"Updated:        {updated}")
    print(f"Already current:{unchanged}")
    print(f"No descriptive: {no_slug}")
    print(f"JSONL missing:  {no_jsonl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
