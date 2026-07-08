# Session Footprint Eval

Acceptance cases for reducing generated artifact storage and adding safe pruning.
Uncertain keep/delete cases default to keep.

| # | Case | Fixture shape | Expected outcome |
|---|------|---------------|------------------|
| 1 | Pi branch/tree navigation session | Pi JSONL with fork/tree/resume noise, one user turn and one assistant answer | Indexed if message threshold is met; navigation-only content does not make a prune candidate by itself. |
| 2 | Claude coding session with summary | Claude JSONL with user/assistant text, summary, Read/Edit/Bash tools | Summary, FTS topic recall, file mutation facts, and Inspect refs still work after compact Tool Log generation. |
| 3 | Codex apply_patch session | Codex rollout with `apply_patch` changes and patch output | File mutation facts include changed paths; Tool Log keeps patch/update evidence and inspectable section text. |
| 4 | Pi subagent transcript | Parent Pi session with nested subagent session | Parent has subagent refs; child transcript is generated; prune deletes both only when parent session ID is confirmed. |
| 5 | Claude subagent transcript | Claude parent with `Agent` request and sidechain JSONL | Parent clean transcript links to child; skill/subagent recall stays queryable; child generated artifact is owned by parent. |
| 6 | Huge read-only tool output | Tool calls for `Read`, `read`, `Grep`, `rg`, or shell `cat/sed/rg` with large result text | Tool Log stores arguments/status plus compact head/tail excerpt and omitted-byte notice, materially smaller than old 20k read-result cap. |
| 7 | Write/Edit/Update evidence | `Write`, `Edit`, `apply_patch`, or update-like tools with useful result output | Tool Log uses the larger audit cap; mutation path facts remain queryable; inspect returns enough result context for audit. |
| 8 | Error output | Failed Bash/read/edit tool call with long error text | Error result keeps a larger bounded excerpt than read-only success output so failure diagnosis remains inspectable. |
| 9 | Ambiguous low-value summary | Summary contains weak phrases such as "no changes" but session has mutations, subagents, questions, or skills | Audit marks `keep`/not eligible; prune never deletes from a heuristic alone. |
| 10 | Confirmed low-value session | Session has no mutations, subagents, skills, questions, or summary signal and its explicit ID is passed with `--confirm` | Prune deletes DB session row, all owned fact rows, clean transcript, Tool Log, and subagent dir, but not source JSONL. |
| 11 | Missing/dangling generated paths | DB row points to missing transcript/tool/subagent paths | Audit reports missing bytes safely; prune treats missing owned artifacts as already absent and still removes DB/facts only when confirmed. |
| 12 | Unrelated generated artifact nearby | Transcript directory contains another session file/dir and orphaned artifact paths | Confirmed prune deletes only paths owned by the target session ID; unrelated sessions and raw source JSONL are untouched. |
| 13 | Resume/fork parent metadata | Pi/Codex session has parent native/session path metadata | Parent metadata stays in DB; prune of child does not infer parent deletion. |
| 14 | Source JSONL exists | Indexed session has `source_path` pointing to a real JSONL | Prune reports source path as retained and never deletes or mutates it. |

Representative run requirements:

- Reindex a fixture set containing read-heavy and mutation-heavy tools and compare generated artifact byte totals before/after compact Tool Log rendering.
- Run `find`, `inspect`, `query --schema`, query facts for skills/subagents/file mutations/questions, status checks, and the full test suite.
- Exercise `prune --dry-run` and confirmed prune with explicit session IDs; verify unrelated sessions and source JSONL survive.
