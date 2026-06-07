# Skill Invocation Audits

## Problem Statement

Session Index does not reliably answer the user's real audit question: "where was this skill invoked?" The current implementation treats skill usage as a property of Tool Calls, so provider formats that encode reusable prompt/workflow templates as slash commands, skill envelopes, or file reads are missed. This makes skill behavior audits incomplete, especially for Pi sessions where visible skill use is often present in the Clean Transcript but absent from `tool_calls`.

The result is that `find --skill review` and SQL aggregates over skill usage can return empty or misleading results even when the transcript clearly shows the skill was invoked.

## Solution

Introduce **Skill Invocation** as the canonical user-facing fact for reusable prompt or workflow template use. Session Index should normalize provider-specific encodings into one `skill_invocations` audit surface, without exposing whether the invocation came from a slash command, skill envelope, tool event, or exact `SKILL.md` read.

`find --skill NAME` should return Skill Invocation candidates with `skill/<session_id>/<sequence>` Inspection References. `inspect skill/...` should prioritize a straight path to the complete relevant transcript by returning primary transcript artifact metadata, plus invocation metadata and locator/preview information. The full transcript and Tool Log artifacts remain the canonical evidence store; fact tables must not store full skill envelopes or large prompt bodies.

## User Stories

1. As a Session Index user, I want `find --skill review` to find every session where `review` was invoked, so that skill behavior audits are complete across Pi and Claude history.

2. As a Session Index user, I want a Skill Invocation result to hide provider encoding details, so that I can reason about skills instead of command/tool implementation differences.

3. As a Session Index user, I want `inspect skill/<session_id>/<sequence>` to return the Clean Transcript path or subagent transcript path where the invocation happened, so that an agent can immediately read the full context.

4. As a Session Index user, I want SQL counts over `skill_invocations`, so that aggregate audits use a trustworthy canonical table instead of misleading `tool_calls` fields.

5. As a Session Index user, I want lifecycle/runtime slash commands excluded from Skill Invocation audits, so that `/clear`, `/help`, `/model`, and similar controls do not pollute prompt-template usage results.

6. As a Session Index user, I want subagent skill usage detected when a subagent reads an exact `SKILL.md`, so that skill audits include child-agent behavior and point to the subagent transcript when that is the best evidence artifact.

7. As a maintainer, I want deterministic reindex/backfill to populate historical Skill Invocations, so that the fix applies to existing indexed sessions without regenerating summaries.

## Implementation Decisions

- **Skill Invocation is the canonical concept.** Reusable slash commands, skill envelopes, provider Skill tool events, and exact `SKILL.md` reads all normalize to Skill Invocations. Provider-specific detection mechanisms are implementation details and should not appear as user-facing `kind` or `source` distinctions in find/inspect output.

- **Use a dedicated `skill_invocations` fact table.** This table is the canonical SQL audit surface for skill usage. Existing `tool_calls.skill_name` should be removed because it presents an incomplete and misleading audit surface.

- **Store metadata, not evidence bodies.** Skill facts should store normalized skill name, per-session skill sequence, timestamp when available, compact invocation preview/arguments when bounded, and locator data needed for inspect. They must not store full skill envelopes, expanded prompt bodies, or transcript dumps.

- **Use canonical name normalization.** Skill names are lowercased and stripped of leading slash and `skill:` prefix, while preserving meaningful separators such as hyphens and colons. Matching for `find --skill` is based on this canonical name.

- **Use `skill/<session_id>/<sequence>` Inspection References.** The sequence is the ordinal among Skill Invocations in the session, in source order. This keeps skill refs event-level and avoids leaking provider encoding into the ref contract.

- **Evidence Find remains compact.** `find --skill` returns compact candidates with the primary `skill/...` ref, session summary metadata, and match metadata such as skill name, sequence, timestamp, and bounded preview/arguments. It does not return artifact inventories or evidence text.

- **Evidence Inspect is artifact-first.** `inspect skill/...` returns primary transcript artifact metadata first. For parent-session invocations, the primary artifact is the Clean Transcript. For subagent-scope invocations, the primary artifact is the subagent transcript, with parent Clean Transcript available as context when present. Inspect may include a small invocation locator/preview, but it does not inline the full transcript.

- **Extraction is transcript-evidence based, not registry based.** Historical Source Transcripts are the source of truth. Extraction must not depend on the current installed skill registry because skills may have moved, changed, or disappeared since older sessions were recorded.

- **V1 extraction formats.** The first implementation must normalize Pi skill envelopes, slash commands including `/skill:*`, all non-lifecycle slash commands, Claude provider Skill tool events, and exact reads of installed `SKILL.md` paths. Known lifecycle/runtime commands are filtered out.

- **Subagent support uses transcript locality.** When a Skill Invocation is discovered inside a subagent scope, inspect should map it to the subagent transcript as the primary artifact instead of forcing users back to the parent Clean Transcript.

- **Backfill uses deterministic reindex.** Existing indexed sessions should be repaired through the established deterministic backfill path that regenerates transcripts, Tool Logs, and fact tables without LLM summaries. Transcript-only migrations are not sufficient because parser metadata and subagent context matter.

- **Documentation is part of the change.** The session-search skill docs, query reference, README, debugging docs, glossary, and ADR must teach the new Skill Invocation model, `skill/...` refs, canonical SQL table, and backfill guidance.

## Testing Decisions

- Tests should verify observable audit behavior, not parser implementation details. The key behavior is that heterogeneous provider encodings all become indistinguishable Skill Invocations from the user's perspective.

- Extraction tests are required for Pi skill envelopes, slash commands, `/skill:*` commands, Claude Skill tool events, exact `SKILL.md` reads, subagent-scope `SKILL.md` reads, name normalization, and lifecycle-command filtering.

- Evidence Find/Inspect tests are required for `find --skill` returning `skill/...` refs and compact match metadata, and for `inspect skill/...` returning the expected primary transcript artifact path, existence metadata, and locator/preview without inlining full transcripts.

- Tests should include both parent-session and subagent-scope Skill Invocations because primary artifact selection differs between those cases.

- SQL/query behavior should be smoke-validated through `skill_invocations` counts for known historical names after deterministic backfill, but the PRD's explicit test focus is extraction plus find/inspect behavior.

## Out of Scope

- Inferring intent from arbitrary file reads that do not target an exact `SKILL.md` path.

- Resolving historical invocations against the current installed skill registry.

- Storing full skill envelopes or expanded prompt bodies in SQLite.

- Inlining complete transcripts in `inspect skill/...` responses.

- Exposing provider detection source as part of normal find/inspect output.

- Reworking Subagent Run semantics; subagent skill usage should integrate with existing subagent transcript artifacts without redefining Subagent Runs.

## Further Notes

The key product principle is that the audit reader cares that a named Skill Invocation happened, not how the provider encoded it. Parser and fact extraction code should absorb provider variation so the LLM-facing CLI and SQL surface stay simple.

The implementation should preserve the existing Evidence Find / Evidence Inspect split: find selects compact candidates and inspect provides paths/locators to evidence artifacts. Full context remains in generated transcripts and Tool Logs.
