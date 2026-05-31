# Evidence Find Inspect CLI

## Problem Statement

Session Index can already identify candidate sessions through topic search and structured SQL, and it can generate Clean Transcripts, Tool Logs, and Subagent Run transcripts. The problem is the handoff between those pieces: an LLM can find sessions or fact rows, but it must manually decide which artifact to read, derive paths, search Tool Logs for sequence headings, inspect subagent transcripts, and keep context noise under control.

The user's primary workflow is not just counting sessions or listing rows. The user asks an LLM to audit past agent behavior, tool behavior, skill results, subagent outputs, file mutation trails, and rule/flow usage, then extract lessons from the relevant transcripts and logs. The current split between search and excerpt solved an important noise problem: vague candidate discovery should return compact summaries first, and actual transcript/log text should only be retrieved after the LLM has narrowed to specific likely sessions. The new surface must preserve that two-stage retrieval discipline while making structured tool/skill/subagent/question/file-mutation evidence easier to inspect.

## Solution

Replace the user-facing `search` + `excerpt` workflow with a deterministic `find` + `inspect` workflow, while keeping `query` as the raw read-only SQL escape hatch.

`find` is compact candidate discovery. It accepts structured criteria, returns JSON only, and includes inspection-ready references. It does not include Clean Transcript, Tool Log, or Subagent Run transcript evidence text. Its job is to help the LLM identify likely sessions or events without polluting context.

`inspect` is scoped evidence retrieval. It accepts Inspection References returned by `find` and returns bounded JSON Evidence Packets containing artifact metadata, computed locators, and relevant text snippets from Clean Transcripts, Tool Logs, or Subagent Run transcripts.

`query` remains for counts, aggregates, custom grouping, schema discovery, and unusual questions that are better expressed as SQL.

The LLM-facing decision tree is:

1. Use `query` for counts, aggregations, rankings, or custom SQL analysis.
2. Otherwise use `find` first to discover compact candidate sessions/events.
3. Read the JSON summaries, previews, and match metadata from `find`.
4. Copy selected Inspection References into `inspect`.
5. Use `inspect` only on scoped refs to retrieve bounded evidence text.
6. If evidence is insufficient, inspect a related context ref or run a narrower `find`.

## User Stories

1. As an LLM auditing past sessions, I want to run a deterministic `find --topic` command that returns compact JSON session candidates with summaries and inspection refs, so that I can narrow vague user memories before loading transcript text.

2. As an LLM auditing tool behavior, I want to run `find --tool <name>` and receive event-level results with Tool Log inspection refs, so that I can identify specific tool calls without manually querying SQL and constructing log locations.

3. As an LLM auditing skill behavior, I want to run `find --skill <name>` and receive event-level results with the matching skill call metadata and Tool Log inspection refs, so that I can inspect skill arguments/results only for likely relevant invocations.

4. As an LLM auditing file changes, I want to run `find --mutated <path-fragment>` and receive event-level File Mutation results with the stored path, tool attribution, and Tool Log inspection refs, so that I can inspect the exact write/edit event without relying on broad files-touched metadata.

5. As an LLM auditing subagent behavior, I want to run `find --subagent <name>` and receive event-level results with Subagent Run previews, match confidence, parent-call refs, and subagent inspection refs, so that I can choose which child run to inspect.

6. As an LLM auditing question-tool answers, I want to run `find --tool question --question-recommended true|false` and receive question-answer candidates with question text, selected label, and inspection refs, so that I can inspect exact prompts where recommended or non-recommended options were chosen.

7. As an LLM combining topic and behavioral criteria, I want `find` filters such as topic, tool, skill, mutated file, subagent, project, session, and date range to compose predictably, so that I can narrow candidates without post-processing large result sets manually.

8. As an LLM inspecting a selected candidate, I want to pass an Inspection Reference from `find` directly to `inspect`, so that I do not have to derive file paths, line ranges, sequence headings, or subagent transcript paths myself.

9. As an LLM inspecting a Tool Log event, I want `inspect` to resolve a tool-call reference into the exact Tool Log section and return bounded text plus locator metadata, so that I can audit arguments, status, result, and any associated File Mutation rows without reading the entire log.

10. As an LLM inspecting a session/topic candidate, I want `inspect` to return focused Clean Transcript excerpts using a provided query string, so that I can retrieve relevant conversation content after selecting a likely session.

11. As an LLM inspecting a Subagent Run, I want `inspect` to resolve the subagent reference to the correct transcript and return the task/prompt area by default or query-focused excerpts when requested, so that I can audit child-agent behavior without scanning all agent transcripts.

12. As a user of the CLI, I want `find`, `inspect`, and `query` help text to explain when to use each command, so that an LLM can learn the workflow from the CLI and the installed skill documentation.

13. As a maintainer, I want legacy search/excerpt surfaces removed or made non-primary once `find` and `inspect` cover their jobs, so that the LLM has fewer overlapping commands to choose from and does not accidentally use outdated workflows.

14. As a maintainer, I want evidence locations derived from current artifacts at inspection time rather than persisted in a general span table for v1, so that implementation stays simple and artifact regeneration after backfill remains straightforward.

## Implementation Decisions

- The primary user-facing retrieval surface is `find`, `inspect`, and `query`.

- `find` replaces candidate discovery currently covered by full-text search and common structured fact queries. It returns JSON only. It must not include transcript, Tool Log, or subagent transcript evidence text.

- `inspect` replaces scoped excerpt retrieval and Tool Log/subagent artifact lookup. It returns JSON Evidence Packets containing bounded evidence text.

- `query` remains a read-only SQL escape hatch. It is the preferred command for counts, rankings, aggregates, custom grouping, and schema discovery.

- Natural-language intent parsing does not belong in the CLI. The calling LLM maps user language to structured flags using CLI help and the session-search skill documentation.

- `find` supports composable deterministic filters:
  - topic text
  - tool name
  - skill name
  - requested subagent type
  - mutated file path fragment
  - question recommended status
  - project
  - date range
  - session id
  - result limit

- `find` may support a focus query for ranking/previews, but it must remain candidate discovery. It must not become a broad evidence dump.

- `find` results are event-level by default. A Tool Call, skill invocation, File Mutation, question answer, Subagent Run, or topic match should produce its own result rather than being hidden inside a session-level bundle.

- Topic matches are represented as session-level candidate events because there is no structured fact row behind them.

- File Mutation matches are represented using the stored path exactly as supplied to the mutation tool. `find --mutated <path-fragment>` should match against that stored path without treating broad `sessions.files_touched` metadata as evidence of mutation.

- Each `find` result includes one primary Inspection Reference and may include related refs. For example, a subagent result can include both the child-run ref and the parent tool-call ref.

- File Mutation results use the related `tool/<session_id>/<sequence>` Inspection Reference as their primary ref in v1. A separate mutation ref is not needed because the evidence lives in the Tool Log section, and mutation paths can contain slashes. The result match metadata carries the mutated path.

- Inspection References use slash-style strings so provider session ids containing colons remain easy to parse:
  - `session/<session_id>`
  - `tool/<session_id>/<sequence>`
  - `subagent/<session_id>/<child_index>`
  - `question/<session_id>/<sequence>/<question_index>`

- Inspection References are the contract between `find` and `inspect`. File paths and locators may be included for transparency, but LLMs should be able to copy refs unchanged from `find` into `inspect`.

- `inspect` resolves references using current database facts and generated artifacts at runtime.

- v1 does not add a general persisted artifact-span table. `inspect` computes locators such as artifact path, Tool Log heading, sequence, and line range when practical.

- Tool-call inspection resolves the session Tool Log and extracts the matching markdown section by sequence. When File Mutation facts exist for that session and sequence, the Evidence Packet includes those stored mutation paths as structured metadata.

- Question inspection resolves the question fact, includes structured question-answer fields, and includes the related Tool Log section.

- Subagent inspection resolves the Subagent Run, includes run metadata such as requested type, task preview, status, match confidence, and transcript path, then returns either the beginning/task area or query-focused transcript excerpts.

- Session inspection uses Clean Transcript excerpting and may include relevant subagent transcript hits only when scoped by the inspected session and query.

- Transcript excerpt extraction should return structured excerpt objects instead of only formatted text, so `inspect` can emit JSON with artifact type, path, optional line range, and text.

- Tool Log extraction should become a small isolated module that can return a section by sequence, including heading, status/result text, and computed line range when possible.

- Inspection Reference parsing should become a small isolated module with clear validation errors.

- CLI help and session-search skill docs are part of the product surface. They must encode the usage decision tree: use `query` for aggregates, `find` for compact candidates, and `inspect` for scoped evidence text.

- Legacy `search` and `excerpt` should be removed from the primary CLI/help/skill workflow when the replacement is complete. Backwards compatibility is not a requirement.

- Backfill after implementation should regenerate artifacts and fact tables so the corpus is consistent with the new inspect-time expectations.

## Testing Decisions

- Tests should cover externally observable behavior, not internal implementation details. The important contract is that deterministic CLI inputs produce predictable JSON outputs, stable refs, and scoped evidence retrieval.

- Add focused unit tests for Inspection Reference parsing, including Pi-style session ids with colons, invalid ref kinds, missing parts, non-integer sequence/child indexes, and round-trip strings emitted by `find`.

- Add focused unit tests for Tool Log section extraction by sequence, including missing files, missing sequences, multi-digit sequence formatting, line range computation when implemented, and section boundaries before the next heading.

- Add focused unit tests for structured transcript excerpt objects, reusing the existing excerpt behavior expectations while verifying JSON-ready fields such as artifact type, path, locator, and text.

- Add CLI behavior tests for `find --topic`, `find --tool`, `find --skill`, `find --mutated`, `find --subagent`, and question recommended filters. Tests should verify JSON shape, compactness, event-level result grain, and presence of inspection refs.

- Add CLI behavior tests for `inspect --ref session/...`, `inspect --ref tool/...`, `inspect --ref question/...`, and `inspect --ref subagent/...`. Tests should verify scoped evidence text, artifact metadata, associated File Mutation metadata for tool refs, and clear errors for missing artifacts or stale refs.

- Add tests for the intended LLM handoff: a ref returned by `find` can be passed unchanged to `inspect` and resolves to the expected evidence.

- Add tests that `find` does not include transcript/Tool Log/subagent evidence text, preserving compact candidate discovery.

- Add tests for help/schema/documentation-sensitive behavior where practical: command help should describe the decision tree and examples clearly enough for the session-search skill to mirror.

- Update existing CLI tests that assume `search`/`excerpt` are primary commands. Remove or revise legacy assertions once those commands are retired.

- Run the full existing test suite after implementation because command routing, database access, artifact generation, and transcript extraction are all touched.

## Out of Scope

- Natural-language parsing inside the CLI.

- Multiple output formats such as TOON, Markdown, or human-readable tables for `find`/`inspect`. JSON is canonical for this work.

- Persisted general-purpose artifact span or offset tables in v1.

- Semantic collapsing of tool families beyond existing normalized tool names and dedicated fact tables.

- Redesigning multi-select question-answer storage. Multi-select recommendation attribution remains ambiguous unless addressed by separate work.

- Source JSONL inspection as the primary evidence path. Generated Clean Transcripts, Tool Logs, and Subagent Run transcripts remain the preferred artifacts.

- LLM-generated summaries during `inspect`. `inspect` retrieves bounded evidence text; interpretation and lesson extraction belong to the calling LLM.

- A graphical or TUI surface for evidence retrieval.

## Further Notes

This PRD intentionally preserves the lesson from the previous search/excerpt design: vague discovery and text inspection must remain separate phases. The improvement is not to return more text earlier; it is to make the handoff from compact candidate discovery to scoped evidence retrieval mechanical and reliable.

The feature should be implemented before the user's planned full backfill, so the regenerated corpus has consistent Tool Logs, fact rows, Subagent Run transcripts, and inspect-time artifact expectations.

The session-search skill should be updated as part of this work because it is the main instruction layer that teaches an LLM how to map natural user questions onto deterministic CLI parameters.
