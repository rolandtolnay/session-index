# Consolidate Pi Worktree Conversations

## Problem

Moving a Pi conversation into a dedicated Git worktree and back creates separate raw Pi sessions with inherited history. Session Index currently treats those native identities as separate indexed sessions once each contains a new user-and-assistant exchange. One piece of work can therefore produce overlapping Clean Transcripts, repeated indexed facts, and several search or recent-context entries.

The current inherited-only clone filter avoids some duplicate indexing but also skips a returned conversation until another exchange occurs. Closing a branch should complete the indexed continuation without requiring the user to keep chatting or update a Linear ticket.

Raw Pi session duplication is acceptable. The problem is duplicated or incomplete representation in Session Index.

## Outcome

A conversation transferred from A in the main worktree, to B in a dedicated worktree, and back to C is represented by one Session Index conversation. It keeps its original public identity and generated artifact paths, contains the selected continuing history without repetitions introduced by the transfers, and owns all associated Side Chats, Subagent Runs, and indexed facts.

Pi retains its original raw sessions and native identities. Explicit forks and clones remain independent indexed conversations. If the user resumes a transferred ancestor and adds new conversation activity, that continuation becomes a separate indexed fork without replacing the consolidated conversation.

The change spans Session Index and the Pi worktree-handoff producer in `~/.pi`. It applies to newly recorded transfers, not retrospective discovery or consolidation of historical chains.

## User Stories

1. As a Pi user moving work between worktrees, I want one stable searchable conversation throughout those transfers, so that search, recent context, and the Session Manager TUI do not present repeated versions of the same work.
2. As a user closing a branch and immediately leaving Pi, I want the indexed conversation to reflect the successful continuation without another chat exchange, so that the returned session is not excluded merely because it contains inherited conversation history.
3. As a user retrieving earlier evidence, I want the consolidated conversation to retain all its Side Chats, Subagent Runs, Tool Calls, questions, and File Mutations, so that consolidation neither loses evidence nor counts inherited copies as new events.
4. As a user resuming an earlier raw Pi session after transferring away from it, I want new conversation activity there to become a separate indexed fork, so that independent work cannot overwrite the ongoing consolidated conversation.

## Decisions

These are the user-confirmed requirements.

1. **Explicit worktree continuation is one indexed conversation.** Successful conversation transfers through the Branch Workflow and Close Branch Workflow advance the same indexed conversation. Ordinary Pi forks and clones remain independent. Parent ancestry alone is insufficient evidence to consolidate them; these actions can share inherited history while representing different work.

2. **The original public identity remains stable.** During A → B → C, the consolidated conversation retains its original Session Index ID and generated artifact paths. Current-session artifact lookup, search, recents, and inspection must agree on that identity. Native Pi identities and raw paths remain accurate for resume and exact-source operations; consolidation must not impersonate a different raw session.

3. **One owner and one representation of inherited evidence.** The consolidated session owns one Clean Transcript, one Tool Log, one current summary/headline set, and the associated indexed facts and child artifacts. Inherited copies do not become additional conversation turns, Tool Calls, questions, File Mutations, Side Chats, or Subagent Runs. The continuing selected history determines the main conversation; consolidation is not concatenation of overlapping transcripts.

4. **All contributing children remain accessible.** Side Chats and Subagent Runs originating in A or B remain discoverable and inspectable from the consolidated session after transfer to C. Their physical raw locations may remain unchanged. Consolidation changes indexed ownership, not Pi's raw storage or the separation between the main conversation and explicitly readable child transcripts.

5. **Completion does not depend on Linear or further conversation.** A successful transfer must become indexable without a new user-and-assistant exchange. A completed Close Branch Workflow must refresh the applicable indexed conversation whether or not it transfers sessions or updates a Linear ticket. The absence of a Linear update cannot exclude the continuation. A cancelled or failed session switch must not advance indexed ownership to an unused destination copy.

6. **Transferred ancestors become forks only when continued.** Merely reopening or re-indexing A or B after transfer does not create a new indexed session. New conversation activity added to such a source establishes a separate indexed fork, while the existing consolidated conversation continues following its current continuation. Delayed indexing of an unchanged ancestor must not regress that conversation or recreate duplicate indexed records.

7. **New transfers only.** Existing historical chains are not discovered or consolidated by this work. An already-existing source session may participate in a newly recorded explicit transfer; this is not authorization to infer earlier continuation relationships. Raw Pi session files and native IDs are not rewritten, moved, or deleted for deduplication.

## Assumptions

These are proposed defaults, approved for inclusion as assumptions rather than additional fixed requirements. Verify them against the implementation; conflicts resolve toward observed behavior without reopening the decisions above.

- Reuse asynchronous indexing: a successful handoff schedules deterministic artifact refresh without blocking branch cleanup or waiting for a summarizing model. Summary and headline refresh follow the existing background cadence. Immediate exit must not discard the refresh request.
- A fork created by continuing an old transferred source contains its selected inherited conversation plus its new activity. Previously owned child artifacts stay with the consolidated conversation; new children created in that resumed source belong to the fork. Inherited evidence in a genuine fork is not the unwanted duplication caused by a transfer.
- The consolidated record retains its original start time and visibility preference while reflecting the latest continuation's activity and location. Original source locations remain available for exact-source operations and evidence provenance.
- Exact-source snapshots, including commit-intent capture, continue using the actual invoking raw session and selected Pi leaf rather than substituting the consolidated generated transcript.
- Existing transcript branch selection and cleaning remain the default. This work does not merge abandoned conversation branches into one linear history.
- A completely empty conversation still follows the ordinary minimum-content requirement. An already qualifying conversation does not need to qualify again after each transfer.

## Conventions

- **Pi handoff boundary:** `~/.pi/agent/extensions/branch-command/session-handoff.ts` preserves the live session tree and selected leaf, then switches sessions. `close-branch/session-return.ts` reuses it or continues in place. Match those existing success and cancellation boundaries. The close-branch continuation precedent is commit `0f1896a`; full-session handoff is `292c20e`. Do not replace the raw copy behavior or broaden this into changes to unrelated session-shaping commands.
- **Canonical indexing pipeline:** use `indexer.py`, `hooks/session_refresh.py`, `hooks/_session_refresh_worker.py`, and `indexing_lock.py` as the existing staged-refresh and serialized-publication foundation. The indexing serialization precedent is `0580250`. Extend that ownership model rather than adding an independent transcript generator. Do not carry forward the assumption that each native ID necessarily owns a different cleaned conversation when explicit continuation says otherwise.
- **Child evidence:** `side_chats.py`, `pi_parser.py:discover_pi_subagents`, and the indexer's child-artifact stages own archive parsing and discovery. Side Chat ownership was introduced in `03b4182`; ADR 0004 explains why copied archives must not be adopted by ordinary clones. Preserve that protection while allowing the explicitly consolidated owner to retain children from its contributing sources.
- **Identity and current-session surfaces:** `session_identity.py`, `current_session.py`, `snapshot.py`, `evidence_inspect.py`, and `pi-extension/session-index-env.ts` currently connect native identity, canonical identity, raw origin, and generated paths. Preserve exact native-origin validation. Update directly affected CLI, display, skill, and inspection contracts consistently rather than overloading a native ID with logical ownership.
- **Project membership:** follow `project_identity.py` and ADR 0005. Linked worktrees already share Project identity; Project grouping alone must not consolidate unrelated conversations.
- **Domain documentation:** `CONTEXT.md` and ADRs 0002/0003 currently describe the native/canonical identity relationship; ADR 0004 defines parent-owned Side Chats. Distinguish the consolidated indexed conversation from the raw Current Session in directly affected documentation. Record any intentional change to an existing architectural decision with a linked superseding ADR rather than silently contradicting it. Preserve the existing canonical-ID format and collision safety where they still apply.

## Verification

Prove these properties through public indexing, identity-resolution, and handoff boundaries, using temporary raw fixtures and databases rather than the user's live data.

1. **Consolidation preserves evidence and counts it once.** A qualifying conversation transferred A → B → C produces one stable searchable record and artifact set. The continuing selected history is complete without transfer-induced repetition; facts and all contributing child artifacts remain discoverable with correct ownership. Relevant precedents: `tests/test_indexer.py`, `tests/test_side_chats.py`, and Pi's `agent/extensions/branch-command/session-handoff.test.ts`.

2. **Ownership cannot regress through asynchronous work.** Pending or in-flight refreshes from transferred sources cannot overwrite the newer continuation, recreate superseded records, or lose child evidence. Repeated processing is idempotent. Relevant precedents: `tests/test_session_refresh.py`, `tests/test_indexing_lock.py`, and the stale-child-publication tests in `tests/test_side_chats.py`.

3. **The workflow itself is sufficient to refresh.** Successful transfer and completed close cause the applicable indexed conversation to refresh without subsequent conversation or a Linear update, including immediate Pi exit. An in-place close does not create another conversation; an unused destination from a cancelled or failed switch does not take ownership. Use Pi handoff/session-return tests and `tests/test_pi_extension_env.py` as lifecycle precedents.

4. **Continuation and divergence stay distinct.** Ordinary forks/clones remain independent. Reopening or re-indexing an unchanged transferred ancestor does not split it; adding new conversation activity creates a separate indexed fork without changing the consolidated continuation. Prove child ownership across that divergence as part of the same behavior. Existing clone qualification cases in `tests/test_indexer.py` provide the starting fixtures.

5. **Stable cleaned identity does not corrupt raw origin.** Across transfers, Current Session artifact lookup and user-facing retrieval resolve the stable indexed conversation, while resume metadata and exact-source snapshots identify the actual raw session and selected leaf. Wrong native identity, wrong worktree, and missing Pi leaf remain rejected where required. Follow `tests/test_snapshot.py`, current-session tests, and extension environment tests.

6. **Scope and existing behavior remain intact.** Historical unmarked chains are not inferred as continuations; raw files remain unchanged by consolidation. Existing Claude/Codex indexing, child separation, and genuine Pi fork behavior remain green. Run the Session Index full suite for indexing/database/hook changes (`uv run --with pytest -m pytest tests/`) and the affected Pi branch-command, close-branch, and Session Index extension suites. Inspect the Session Manager TUI if its presentation changes.

No real branch merge, Linear mutation, or historical/live-data backfill is needed to verify these properties. Changes to live indexed data require explicit user authorization and a backup under the repository's existing maintenance rules.

## Out of Scope

- Historical-chain discovery, automatic historical consolidation, or a user-facing historical merge tool.
- Changing Pi's native session identity, raw-session storage, `/resume` behavior, or full-history worktree copying.
- Automatically consolidating ordinary forks, clones, unrelated same-project conversations, pruning-created sessions, or cross-provider conversations.
- A general conversation-graph browser, arbitrary transcript merge editor, or content-similarity deduplication.
- Changing the Close Branch Workflow's merge approvals, Git cleanup, Linear ticket-selection policy, or Linear mutation behavior.
- Unrelated custom-entry/message parsing improvements, including the existing Linear audit-record parser gap. Those records do not determine whether consolidation occurs.
- Synchronous summary/headline generation as a prerequisite for completing a branch command.

## Further Notes

The previous close-branch implementation intentionally preserved conversation content by copying the raw session rather than resuming an unrelated main-worktree conversation. That behavior remains appropriate here; this work changes how Session Index represents its continuity.

Current Session Index code suppresses inherited-only Pi clones, then indexes their complete selected history after new user-and-assistant activity. Pi subagent discovery is rooted beside the source JSONL, while Side Chat archives carry native owner identities. These observations explain why selecting C alone is insufficient for child ownership, even though its inherited history normally supplies the main transcript.
