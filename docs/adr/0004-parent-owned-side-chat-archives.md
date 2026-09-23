# Preserve Side Chats as parent-owned searchable artifacts

Pi persists completed Side Chat exchanges and their Focused Content in the originating session's Source Transcript as versioned, non-context custom entries. Session Index derives child transcripts and searchable child records from those entries; Side Chats are neither independent sessions nor Subagent Runs. This preserves discussions without importing exploration into the parent's model context or duplicating inherited parent history.

The parent Clean Transcript contains only child routing labels and paths. Each child receives an asynchronously generated headline scoped to its own conversation, with a first-question fallback; it receives no summary, Substance Band, or independent recent-context entry. Topic search returns the parent once and identifies matching children for explicit inspection.

Source ownership is captured at opening. Reading archive entries across branches preserves abandoned-branch discussions; matching the recorded native owner against the source header prevents clones from adopting inherited archives. Saving each completed turn limits crash loss to unfinished requests once Pi has persisted the parent. Before the parent's first assistant message, Pi keeps entries in memory; Side Chat warns that archival is pending, and subsequent parent indexing discovers the flushed records. Unsent drafts, provisional conclusions, thinking, and tool-result dumps are not archival conversation.

## Consequences

- Pi's shared Side Chat producer and Session Index share the `side-chat-archive` version 1 contract. Records have `kind` (`open`, `turn`, `close`), `sideChatId`, `ownerSessionId`, and an ISO `timestamp`. Open records add `openingLeafId` and `focusedContent` (`kind`, `label`, `text`); turn records add positive `sequence`, `question`, `answer`, and `model`.
- `side-chat:archived` notifications carry the captured `parentSessionId`, `parentSessionFile`, and `closed` flag. Detached indexing publishes artifacts while the main agent is idle; closure triggers child headline generation without refreshing parent descriptions. Full/summary indexing recovers missing headlines, including archives without a close marker. Deterministic-only backfills make no model calls.
- A separate headline lock coalesces generation while artifact writes and deletion retain the normal per-session lock. Results publish only if the child content hash is unchanged and its row still exists.
- Parent source files grow by the new child conversation only. Native persistent side sessions and raw child execution replay are deliberately excluded.
- Child artifacts count toward parent footprint and explicit deletion. Saved children block low-value pruning; source JSONL remains retained under the existing deletion policy.
- Manual Side Chat exports and explicit imports/command handoffs remain separate user actions. Previously discarded conversations cannot be recovered.
