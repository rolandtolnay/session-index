# Cheap Fuzzy Evidence Find Improvements

## Problem Statement

Evidence Find works well when the caller's wording overlaps with indexed session summaries and structured facts. It is weaker when a user describes a remembered conversation with adjacent wording that does not appear verbatim in the indexed text.

A recent lookup for a past `.pi` conversation about redesigning how subagent output is rendered in the TUI illustrates the gap. An exact-ish topic query for "subagent output rendered TUI rendering redesigned" returned no candidates. Broader topic queries such as "subagent render output TUI" found the relevant cluster, and `inspect` then worked very well. File Mutation lookup confirmed the right implementation session, but with low density because it returned many event-level rows from the same session.

The desired improvement is not a full semantic search system. The goal is cheap, deterministic, low-risk improvement to recall and signal-to-noise without embeddings, RAG, project-specific synonym registries, or LLM-driven query rewriting inside the CLI.

## Solution

Add two low-hanging-fruit improvements:

1. Use a popular deterministic fuzzy matching library as a fallback for `find --topic`.
2. Add a session-collapsed discovery mode for noisy File Mutation searches.

For Python, prefer `rapidfuzz`. It is widely used, fast, deterministic, and simple to integrate. Keep the existing topic search as the primary path. If normal topic search returns no results or weak results, run a RapidFuzz fallback over filtered candidate sessions.

The fallback should score the user query against existing indexed candidate text, not a new semantic corpus. A practical candidate blob can include:

- session summary
- project and branch
- mutated file paths
- tool names
- requested subagent names
- possibly generated transcript headings or path tokens if already available cheaply

Use fuzzy ranking to recover likely candidates with partial lexical overlap. This will not solve true synonym gaps such as `output` versus `result` when there is little other overlap, but it should catch many cheap near misses, word-order differences, morphology differences, and path/name overlaps across many projects without domain-specific setup.

## User Stories

1. As an LLM finding a vaguely remembered session, I want `find --topic` to recover likely candidates when exact topic search returns no results, so that I do not need several manual query rewrites.

2. As an LLM using Session Index across many projects, I want fuzzy fallback to work without per-project synonym configuration, so that cheap retrieval improvements scale across domains.

3. As an LLM comparing topic candidates, I want fuzzy fallback results to use the normal Evidence Find JSON shape, so that I can inspect selected candidates the same way as exact topic results.

4. As an LLM auditing File Mutations, I want an optional session-collapsed view that groups repeated mutation events by session with matching file counts and representative paths, so that discovery has a better signal-to-noise ratio.

5. As a maintainer, I want these improvements to stay deterministic and bounded, so that repeated queries are stable and the CLI does not depend on model calls or semantic infrastructure.

## Implementation Decisions

- Do not add embeddings, vector databases, RAG, or LLM calls.

- Do not add a broad synonym registry or project-specific alias system for this brief. That is too much machinery for the observed problem.

- Keep natural-language intent parsing outside the CLI. The calling LLM still maps user requests to structured `find` flags.

- Use `rapidfuzz` for deterministic fuzzy scoring in Python.

- Keep existing exact/FTS topic search as the primary retrieval path.

- Run fuzzy fallback only when topic search returns zero results or all results are below an agreed minimum quality threshold. If there is no existing quality score, start with fallback-on-empty only.

- Restrict fuzzy fallback to the same filters already supplied to `find`, such as project, date range, and session filters. Do not fuzzy-rank the entire corpus when the user supplied narrowing filters.

- Build one searchable text blob per candidate session from already-indexed data. Start with session summary, project, branch, File Mutation paths, tool names, and requested subagent names.

- Use simple weighted RapidFuzz scores. A starting point:

```python
from rapidfuzz import fuzz

score = max(
    fuzz.token_set_ratio(query, session_summary),
    0.8 * fuzz.token_set_ratio(query, searchable_blob),
    0.7 * fuzz.partial_token_sort_ratio(query, searchable_blob),
)
```

- Return only candidates above a conservative threshold. Tune the threshold from tests and a small set of real lookup examples.

- Mark fuzzy fallback matches in `match` metadata, for example:

```json
{
  "kind": "topic",
  "topic": "redesigned how subagent output is rendered in the TUI",
  "match_mode": "fuzzy_fallback",
  "score": 82.5
}
```

- Keep Evidence Find compact. Fuzzy fallback should improve candidate selection, not return Evidence Snippets.

- Add an optional session-collapsed mode for event-heavy criteria such as `--mutated`. Candidate rows should group by Canonical Session ID and include:
  - match count
  - representative mutated paths
  - session summary
  - primary session Inspection Reference
  - a small bounded list of related tool Inspection References

- Preserve event-level `find --mutated` as the default where exact audit trails matter. Session-collapsed mode is for discovery and ranking, not a replacement for exact event inspection.

## Testing Decisions

- Add a regression test where exact topic search misses a query similar to "redesigned how subagent output is rendered in the TUI", but fuzzy fallback returns the expected session candidate when its summary/path blob contains overlapping terms such as `subagent`, `render`, `TUI`, `result`, or `card`.

- Add tests for fuzzy fallback triggering:
  - fallback runs when exact topic results are empty
  - fallback does not run when exact topic results are sufficient, unless an explicit flag is later added
  - project/date/session filters constrain the fuzzy candidate set

- Add tests for fuzzy result shape: normal Evidence Find JSON, compact candidate data, Inspection References, `match_mode: "fuzzy_fallback"`, and score metadata.

- Add tests for threshold behavior so weak broad matches do not flood the result set.

- Add tests for session-collapsed File Mutation discovery: repeated mutations in one session collapse to one session candidate with counts and representative paths, while default event-level mode still returns individual mutation events.

- Add regression tests that Evidence Find still does not include transcript, Tool Log, or Subagent Run evidence text during candidate discovery.

## Out of Scope

- Embedding search, vector databases, RAG, or external model calls.

- LLM-based query rewriting inside Session Index.

- Broad synonym maps, project-scoped alias registries, or automatic alias mining from docs.

- Replacing Evidence Inspect or returning evidence text during candidate discovery.

- Full semantic synonym coverage for arbitrary English.

- Changing the canonical JSON-only output contract for Evidence Find and Evidence Inspect.

- Removing event-level File Mutation results.

## Further Notes

This brief intentionally applies the Pareto principle. The observed audit had two practical problems: exact topic search was brittle to wording, and File Mutation search returned too many repeated rows from one session. RapidFuzz fallback and session-collapsed mutation discovery address those directly with minimal new product surface.

The expected benefit is better first-pass retrieval for near-miss wording across many projects, not true semantic synonym matching. If this later proves insufficient, a separate brief can revisit domain aliases or richer search infrastructure with evidence from more failed lookups.
