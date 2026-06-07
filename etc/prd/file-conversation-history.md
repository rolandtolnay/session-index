# File Conversation History

## Problem Statement

Evidence Find currently treats File Mutation discovery as an event-level audit trail. When an LLM asks which past conversations changed a file or area, `find --mutated` returns repeated tool-level rows from the same Canonical Session ID instead of a compact file history by session. This makes it harder to answer the practical question: “which LLM conversations changed this file, and which Clean Transcript should I inspect to understand why?”

Topic-based Evidence Find also fails too easily when the caller remembers a session with adjacent wording instead of exact indexed terms. A failed exact topic match can prevent discovery entirely, including topic-scoped drill-downs into tools, File Mutations, or Subagent Runs.

## Solution

Make `find --mutated` a file conversation history by default. It should return one Session-Collapsed Mutation Candidate per matching Canonical Session ID, ordered like a file history with most recent sessions first. Each candidate should use the session Inspection Reference as the primary handle, include compact mutation metadata for choosing relevant sessions, and include a bounded set of related tool Inspection References for exact mutation drill-down. Event-level File Mutation rows remain available through an explicit mutation mode.

Add deterministic fuzzy topic fallback for any Evidence Find query that uses `--topic`. Exact topic search remains primary. If exact topic scoping returns zero sessions, RapidFuzz ranks the same filtered candidate session set using already-indexed data, then the existing find criterion continues from that fuzzy session scope. Fuzzy fallback should improve recall without adding model calls, embeddings, vector databases, or synonym registries.

## User Stories

1. As an LLM investigating a specific file, I want `find --mutated <path>` to return unique sessions that mutated that path, so that I can inspect the relevant Clean Transcripts like a conversation-level equivalent of file history.

2. As an LLM choosing between matching sessions, I want each Session-Collapsed Mutation Candidate to include mutation counts and representative matching paths, so that I can tell why the session was returned without loading evidence text.

3. As an LLM needing exact mutation evidence, I want collapsed mutation candidates to include a bounded list of related tool Inspection References, so that I can drill into relevant Tool Log sections after choosing a session.

4. As an LLM performing an audit, I want an explicit event-level mutation mode, so that I can still retrieve individual File Mutation events when the exact sequence-level trail matters.

5. As an LLM searching by remembered topic wording, I want topic fallback to recover likely sessions when exact topic search finds nothing, so that I do not need several manual query rewrites.

6. As an LLM combining topic with another criterion, I want fuzzy fallback to improve topic scoping when exact topic scoping is empty, so that `--topic` remains useful while drilling into tools, File Mutations, and Subagent Runs.

7. As a maintainer, I want fuzzy fallback to be deterministic, bounded, and visibly marked in match metadata, so that retrieval remains testable and explainable.

## Implementation Decisions

- Preserve exact File Mutation facts as event-level source data. File Mutation extraction and persistence should continue storing one row per successful write/edit mutation path. Session collapse happens in Evidence Find candidate retrieval, not during indexing.

- `find --mutated` defaults to session-collapsed discovery. Omitting mutation mode is equivalent to selecting session mode.

- Add an explicit mutation mode with two values: session and event. Session mode is the default file conversation history view; event mode preserves the current row-by-row File Mutation behavior.

- A Session-Collapsed Mutation Candidate uses the session Inspection Reference as its primary reference. The result is about the Canonical Session ID that changed the file, not about a single tool event.

- Keep the existing candidate envelope shape with top-level `ref`, `inspect_refs`, `session`, and `match`. Some duplication is acceptable because the shape remains predictable across session-level and event-level candidates.

- Do not include Clean Transcript paths directly in Evidence Find results. The idiomatic path to Clean Transcript metadata remains inspecting the session Inspection Reference. Evidence Find stays compact and does not return Evidence Snippets or broad artifact inventories.

- Collapsed mutation match metadata includes total matching File Mutation row count and distinct matching path count. Row count communicates activity density; distinct path count communicates breadth.

- Collapsed mutation match metadata includes up to five Representative Mutation Paths. Representative Mutation Paths are selected only from File Mutations that matched the `--mutated` criterion, never from broad session file metadata. Rank representative paths by matching row frequency descending, then first mutation sequence, then path for deterministic ties.

- Collapsed mutation candidates include up to five related tool Inspection References for matching File Mutations. These refs support exact Tool Log drill-down without reintroducing event-level noise into the default discovery result.

- Order session-collapsed mutation candidates by session recency by default. This matches the file-history use case: the user is looking for the sequence of LLM conversations that changed a file or area.

- Keep event-level mutation output available for audit/detail use. Event mode should continue to return tool-level Inspection References and per-event File Mutation metadata.

- Add a small fuzzy topic retrieval module with a simple, testable interface that can score candidate sessions using RapidFuzz. This module should encapsulate token/blob construction, scoring, thresholding, and deterministic ordering.

- Fuzzy fallback uses RapidFuzz as a deliberate dependency exception, captured in the project ADR. It should rank already-indexed session data only; it must not introduce LLM calls, embeddings, vector search, or project-specific synonym registries.

- Fuzzy candidate blobs include session summary, project and branch, File Mutation paths, tool names, and Subagent Run names/task previews where cheaply available from indexed facts.

- Exact topic search remains primary. Fuzzy fallback runs only when exact topic scoping returns zero sessions. It does not blend with non-empty exact results in this version.

- Fuzzy fallback applies to any Evidence Find use of `--topic`, including topic-scoped tool, mutation, skill, question, and subagent discovery. When exact topic scoping is empty, fuzzy topic scoping supplies the candidate sessions, and the remaining criterion still applies exactly.

- Fuzzy fallback strictly honors structured filters such as project, date range, and explicit session. It must not expand beyond caller-supplied constraints.

- Fuzzy fallback results use the normal topic match shape with metadata identifying `match_mode` as fuzzy fallback and including a numeric score. Exact topic results should remain identifiable as exact/default topic matches. When fuzzy fallback scopes a non-topic result, keep the primary match kind and expose the fallback details under `match.topic_scope`.

- Use a conservative initial fuzzy threshold. Prefer returning no fallback candidates over flooding Evidence Find with weak broad matches. Do not expose a threshold flag in the first version.

- Update the LLM-Facing CLI Surface so callers know that `find --mutated` is session-collapsed by default, that event mode exists for exact audit rows, and that topic search has empty-only fuzzy fallback.

## Testing Decisions

- Test external Evidence Find behavior rather than SQL implementation details. The important contract is candidate shape, ordering, scoping, references, and match metadata.

- Add tests for default session-collapsed mutation discovery: one result per Canonical Session ID, recent-first ordering, session primary refs, bounded related tool refs, match count, distinct path count, and top-five Representative Mutation Paths selected only from matching File Mutations.

- Add tests for explicit event mutation mode preserving current event-level behavior: tool primary refs, individual File Mutation match metadata, tool filtering, and inspectability of returned refs.

- Add tests for fuzzy topic fallback: fallback runs when exact topic scoping is empty, does not run when exact topic results exist, honors project/date/session filters, returns normal Evidence Find candidates with fuzzy metadata and score, and applies to topic-scoped non-topic criteria while keeping those criteria exact.

- Add threshold tests so weak fuzzy matches do not flood results.

- Use existing Evidence Find tests as prior art for compact JSON candidates, filter composition, topic-scoped event searches, and no evidence text in find results.

- File Mutation extraction tests should remain regression tests only unless implementation changes the extractor. This work should not redefine what counts as a File Mutation.

## Out of Scope

- Embeddings, vector databases, RAG, or external model calls inside Evidence Find.

- LLM-based query rewriting inside the CLI.

- Broad synonym maps, project-scoped alias registries, or automatic alias mining.

- Returning Clean Transcript text, Tool Log text, Subagent Run transcript text, or Evidence Snippets from Evidence Find.

- Returning all mutated paths or all related tool refs in session-collapsed mutation mode.

- Removing event-level File Mutation discovery.

- Changing the canonical Inspection Reference syntax or adding a new mutation-session reference kind.

- Reworking the entire Evidence Find candidate envelope to remove existing redundancy.

- Fuzzy augmentation when exact topic results are non-empty. That may be revisited later if real usage shows exact results can be non-empty but still poor.

## Further Notes

This work reframes File Mutation discovery around the user's file-history mental model. The default answer to `find --mutated <path>` should be “these are the LLM conversations that changed that file or area,” not “these are every write/edit event that matched.” Exact events remain available, but they are a drill-down or audit mode.

The fuzzy fallback rollout should be broad enough that topic filtering becomes more reliable wherever `--topic` is used, but conservative enough that exact matches retain priority and result ranking stays explainable.
