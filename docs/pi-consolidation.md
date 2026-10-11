# One-time historical Pi consolidation

`consolidate_pi.py` removes independently indexed duplicates from verified historical worktree continuations. It is not topic-based deduplication and does not change normal indexing's rules for unmarked ancestry.

## Acceptance boundary

A candidate must be a linear cross-worktree copy chain with workflow markers, an indexed original owner, and at least one indexed duplicate. Indexed contributors must agree on project membership and visibility. The current selected conversation must preserve each ancestor's selected history as a prefix. Shared entries must match exactly; only later `Closed branch:` session-name entries may remain exclusively in an ancestor.

Fresh cook/prune conversations, branching or divergent chains, missing/malformed sources, uncertain downstream copies, registered continuations, and recently modified sources are excluded. The default idle cutoff is 24 hours; it is a conservative filter, not proof that another Pi process has no buffered work. Exit affected conversations before applying.

An isolated rehearsal additionally verifies:

- Saved Clean Transcripts remain prefixes of the continuing conversation.
- Existing tool, question, skill, subagent, mutation, and Side Chat facts remain intact at the same ordinals. Tool arguments/results and child transcript contents are checked, not just event counts.
- Retired canonical IDs resolve to the original owner without changing event ordinals. Chains requiring ordinal remapping are excluded.
- Deterministic re-indexing is unchanged on repeat, and unchanged ancestors cannot recreate duplicate rows.

These checks intentionally prefer exclusions over speculative repair. They establish preservation at migration time; ordinary later conversation growth retains the existing indexer's reference semantics.

## Plan and rehearse — no live writes

The existing index must already have the current schema; perform ordinary schema upgrades separately. Planning checks that prerequisite read-only. Application may create only the alias table introduced for this migration, inside its affected-state transaction.

From this repository:

```sh
uv run consolidate_pi.py plan --output /absolute/scratch/pi-consolidation
```

The output directory must not exist and must be physically outside indexed-data and raw-session storage, including through symlinks. It is created owner-only because the rehearsal contains private conversation data. Optional `--data-dir`, `--raw-root`, and `--idle-hours` select an isolated store or a different cutoff.

The command copies the database, regenerates deterministic evidence in the scratch store, and produces `plan.json`. Its `manifest.groups` contain approved candidates, before/after rows, source and artifact hashes, retired IDs, and publication paths. `manifest.excluded` explains refusals; it reports both edge-level and chain-level reasons, so its counts are not disjoint session totals. The printed digest identifies the exact reviewable plan.

`before/` contains the affected original generated artifacts. `after/` contains the rehearsed database and regenerated artifacts. Rejected rehearsals do not retain tentative ownership in that database. No description models, hooks, workers, or Linear operations run. The most advanced existing indexed summary/headline/classification is retained; fresh description generation remains the ordinary asynchronous indexer's responsibility.

Review the groups and exclusions before approving a live application. Do not edit a plan to force a rejected chain through; correct the condition and rehearse again.

## Apply — explicit approval

Use the exact digest printed by the reviewed rehearsal:

```sh
uv run consolidate_pi.py apply \
  --plan /absolute/scratch/pi-consolidation/plan.json \
  --approve <reviewed-digest>
```

Application refuses changed code, raw files, discovered children, eligibility, indexed evidence, generated artifacts, or rehearsal assets. Pi ownership and affected artifact locks serialize publication; SQLite updates are transactional. A full `backup.db` is made before schema/data writes, and `journal.json` records publication state. Retain the entire scratch directory until satisfied with the result.

Only approved owners and their facts/registry rows are changed. Raw files are never rewritten or deleted. Repeating a completed application is a no-op.

### Compatibility artifacts

Old generated files under retired canonical IDs remain immutable compatibility copies, not independent searchable sessions. Exact and unambiguous-prefix lookups through `find` and `inspect` resolve retired IDs to the surviving owner. `status --fix` recognizes their main files and child directories as owned compatibility artifacts rather than deleting them as orphans.

This migration reduces duplicate search results, not disk usage. Deleting the surviving indexed owner removes its alias lookups; remaining compatibility files then become eligible for ordinary orphan cleanup. Destructive management commands still require exact real session IDs, not aliases.

## Rollback and interruption

```sh
uv run consolidate_pi.py rollback \
  --plan /absolute/scratch/pi-consolidation/plan.json \
  --approve <reviewed-digest>
```

Rollback restores the affected prior rows and artifacts, including removing newly generated files. It does not replace the whole live database, so unrelated sessions indexed since application survive. It refuses intervening changes to affected evidence, sources, children, or artifacts rather than overwriting them.

A handled publication failure restores the pre-operation state. If the process dies mid-publication, the next apply refuses the unfinished journal; rollback can restore a known before/after file mixture while the database transaction remains wholly before or after. Unknown file contents are never overwritten. Repeating a completed rollback is a no-op.

This is a reversible, fail-closed migration—not a guarantee against every filesystem or hardware failure. Keep an independent backup and do not discard `before/`, `after/`, `backup.db`, or the manifest during recovery.
