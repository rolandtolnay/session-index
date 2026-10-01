# Historical project identity backfill

`backfill_projects.py` updates project metadata without reading Source Transcripts, regenerating summaries, or rewriting conversation artifacts. It resolves surviving Git checkouts and accepts a reviewed JSON object mapping exact historical worktree paths to canonical project paths. There is no prefix-matching rule in indexing or migration.

```json
{
  "/workspace/app-feature-payments": "/workspace/app",
  "/workspace/app/.claude/worktrees/fix-login": "/workspace/app"
}
```

Preview against a database copy first:

```sh
sqlite3 ~/.session-index/sessions.db '.backup /tmp/project-identity-copy.db'
uv run backfill_projects.py --db /tmp/project-identity-copy.db --mapping /path/to/reviewed-map.json
uv run backfill_projects.py --db /tmp/project-identity-copy.db --mapping /path/to/reviewed-map.json --apply
```

The default is read-only. `--apply` creates a SQLite backup in the database's sibling `backups/` directory before schema or data changes, then updates metadata transactionally. Its JSON receipt includes the backup path and each session's before/after metadata. A concurrent metadata change aborts the update rather than overwriting it. Repeating the same migration leaves already-corrected rows alone.

For the approved live cutover, use `--db ~/.session-index/sessions.db` with the same reviewed mapping and retain its JSON receipt. Only confirm mappings with clear ownership; matching names do not establish that independent clones or reference folders were worktrees. A mapping between surviving independent Git repositories is rejected.

Legacy `project_path` values supply the known historical location: Git-backed rows retain them in `worktree_path`, while non-Git locations remain their own canonical project paths. Historical `cwd` stays unknown unless already recorded; the migration does not invent a starting subdirectory. Re-indexing from an available source can supply more precise location metadata without undoing resolved project membership. In conventional Git layouts, the canonical project path is the main checkout; for bare or separate-Git-directory worktree layouts it is the shared Git directory when that directory is not named `.git`.
