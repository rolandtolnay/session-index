# Session File Mutations PRD

## Problem Statement

Session Index can currently show files broadly touched during a session, but that broad list includes files that were only read, listed, or searched. When a user wants to recover the concrete files an agent wrote or updated during a session, they must inspect generated tool logs or infer from over-inclusive metadata. That is not first-class, not easy to query, and not precise enough for workflows that need the actual mutation footprint of a session without adopting Pi's broader Session Footprint semantics.

## Solution

Add **File Mutations** as a first-class structured fact in Session Index. A File Mutation is an attributed file path targeted by a successful write or edit tool action within a session. File Mutations are stored as one row per successful mutation event, including minimal attribution metadata so callers can retrieve either the exact file list or the underlying event trail.

Users will retrieve File Mutations through the existing read-only SQL query surface for now. The CLI command surface is intentionally out of scope because it is being reworked separately.

## User Stories

1. As a developer reviewing a past session, I want to query the files successfully written or edited by that session, so that I can reconstruct what the agent changed without reading the full tool log.
2. As a developer using Session Index for audit/debugging, I want File Mutations to exclude read/search/list activity, so that the result does not overstate which files were changed.
3. As a developer reviewing delegated work, I want File Mutations from subagent runs included with attribution, so that a parent session's changed-file list includes successful child-agent mutations.
4. As a developer querying historical sessions, I want File Mutations populated by the existing raw-log backfill flow, so that older sessions can gain the same first-class facts when source transcripts are still available.
5. As an agent or tool author, I want the exact path argument supplied to the mutation tool preserved, so that query results match the evidence in the original tool call rather than an inferred filesystem resolution.

## Implementation Decisions

- Add a new structured fact table for File Mutations. The table represents successful write/edit mutation events only, not failed attempts and not non-mutating file references.
- Store one row per successful mutation event rather than one deduplicated row per path. Consumers that need an exact file list can select distinct paths, while event rows preserve sequence, scope, and tool attribution.
- Use a minimal attributed row shape: session identity, provider source, scope, sequence, timestamp, raw tool name, normalized tool name, and path.
- Extract File Mutations from parsed tool-call arguments, not from generated Markdown tool logs.
- Support provider-specific path arguments:
  - Claude-style write/edit tools provide a file path argument.
  - Pi-style write/edit tools provide a path argument and may provide nested edit paths.
- Include write/edit calls nested inside wrapper tools such as batched parallel tool invocations when the wrapper arguments expose nested write/edit calls.
- Exclude bash mutations entirely. Mutating-looking shell commands remain outside this feature.
- Exclude read, grep/search, find/list, and other non-mutating tools entirely.
- Include File Mutations from subagent runs by using the existing combined parent/subagent tool-call stream and preserving scope attribution.
- Store paths exactly as supplied to the tool call. Do not resolve relative paths to absolute paths and do not add project-relative normalization in this PRD.
- Populate File Mutations during the existing tool-log/fact indexing stage. Current sessions receive rows after the existing full/manual/shutdown indexing paths run.
- Historical coverage comes from existing raw source transcript backfill. If a source transcript is gone, this PRD does not require reconstructing File Mutations from generated tool logs.
- Leave the existing broad files-touched metadata unchanged. It remains useful for search and summary context; File Mutations provide the precise write/edit fact.
- Expose the table through the existing read-only SQL/schema discovery surface and update user-facing documentation with example queries. Do not add a dedicated changed-files CLI command in this PRD.

## Testing Decisions

- Test the File Mutation extraction module as a pure behavior boundary. Good tests assert which rows are produced for representative parsed tool calls, not internal helper implementation details.
- Extraction tests must cover successful Claude-style write and edit calls, successful Pi-style write and edit calls, nested edit paths, nested wrapper calls, subagent scope preservation, failed mutation exclusion, and exclusion of read/search/list/bash calls.
- Database tests must cover schema creation, replace/idempotency behavior, deleting owned rows when sessions are deleted, and inclusion in the fact-table schema reference used by query discovery.
- Indexer integration tests must prove full/no-summary indexing populates File Mutations through the existing fact persistence path and remains idempotent on re-index.
- Documentation/query-surface tests should be updated where existing tests assert schema output or documented fact tables.
- Prior art: follow the existing fact-table tests for tool calls, subagent runs, and question answers, and the existing indexer tests for staged fact persistence.

## Out of Scope

- Net filesystem delta, baseline snapshots, line counts, or revert detection.
- Pi Session Footprint footer behavior or live active-turn mutation tracking.
- Bash mutation attribution.
- Parsing existing Markdown tool logs as a migration source.
- Changing the meaning of existing files-touched metadata.
- Adding a dedicated CLI command for changed files.
- Path canonicalization or project-relative path derivation.
- Persisting file contents, diffs, or tool result payloads.

## Further Notes

The expected common query for an exact file list is a distinct path selection over File Mutations for a session. The event-level representation intentionally preserves attribution while keeping storage small because it stores only compact metadata and the path, not arguments, results, contents, or diffs.
