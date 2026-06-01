# LLM-Facing CLI Surface

## Problem Statement

Session Index’s canonical Evidence Find / Evidence Inspect workflow is meant to let an LLM move from a user’s vague request about past work to the most relevant scoped context quickly. The current surface still carries legacy search/excerpt vocabulary and command wrappers, broad artifact inventories in candidate results, and schema/help output that is not optimized as a standalone LLM reference.

The result is unnecessary noise: an LLM can learn outdated commands, read broad artifact paths instead of following Inspection References, or spend context on session-level paths that do not help choose the next scoped evidence packet. At the same time, over-minimizing JSON for purity would also hurt the primary goal if it removes candidate-specific data that helps the LLM find the right context faster.

## Solution

Make the installed skill documentation and CLI help the standalone LLM-facing CLI surface. Remove legacy user-facing search/excerpt command surfaces and align product vocabulary around Evidence Find, Evidence Inspect, Inspection References, and Evidence Snippets.

Evidence Find remains compact candidate discovery. It should not include evidence text or broad artifact inventories, but it may include candidate-specific metadata when that directly helps an LLM choose or retrieve scoped context. Evidence Inspect becomes the canonical place for artifact metadata, locators, and bounded Evidence Snippets. Session inspection without a query becomes valid so an LLM can retrieve generated artifact metadata and subagent Inspection References without loading transcript text.

`query --schema` becomes a curated LLM-oriented SQL reference instead of raw DDL, because LLMs using SQL need table semantics, ref-construction guidance, and copyable examples more than implementation DDL.

## User Stories

1. As an LLM using Session Index, I want the installed skill documentation and CLI help to fully explain the workflow, so that I can use the CLI effectively without reading the README.

2. As an LLM looking for past work, I want Evidence Find results to contain compact candidate-selection data and Inspection References, so that I can compare likely matches without loading evidence text too early.

3. As an LLM inspecting a selected candidate, I want Evidence Inspect to return artifact paths, locators, and bounded Evidence Snippets, so that I can cite or reason from scoped context without reading whole artifacts.

4. As an LLM inspecting a session, I want `inspect` on a session reference to work even without a query, so that I can retrieve generated artifact metadata and subagent Inspection References without needing transcript snippets.

5. As an LLM inspecting a session with a query, I want the same session metadata and artifact metadata plus query-focused Evidence Snippets, so that the response shape stays predictable whether or not I request text.

6. As an LLM choosing between subagent runs, I want session inspection to expose subagent Inspection References with requested agent type and task preview, so that I can choose the relevant child run before loading its transcript.

7. As an LLM finding subagent runs, I want candidate-specific subagent metadata, including the subagent transcript path, when it directly helps me retrieve scoped context, so that useful data is not removed for ceremony.

8. As an LLM finding tool calls, questions, or file mutations, I do not want repeated broad session artifact inventories in every candidate, so that candidate discovery stays focused on refs, match metadata, and session summaries.

9. As an LLM writing SQL against Session Index, I want `query --schema` to provide a curated reference with table purposes, key columns, semantics, and examples, so that I can produce correct read-only queries quickly.

10. As a maintainer, I want obsolete search/excerpt command wrappers and CLI functions removed, so that the available surface area reinforces the canonical Evidence Find / Evidence Inspect workflow.

11. As a maintainer, I want README and debugging docs to point briefly to the canonical CLI surface rather than duplicating the operating guide, so that user-facing guidance does not drift.

## Implementation Decisions

- The north star for the CLI is fastest reliable path from user prompt to relevant scoped context. Compactness means reducing noise that slows that path, not minimizing JSON fields for its own sake.

- The LLM-facing CLI surface is the installed skill documentation plus CLI help output. These must be sufficient for an LLM to use the system without README access.

- README remains adopter/maintainer documentation. It should provide a concise overview and point toward the installed skill and CLI help rather than duplicating the full LLM operating guide.

- Debugging documentation should describe troubleshooting through canonical commands and should not teach legacy command workflows.

- Legacy user-facing search/excerpt wrappers and command implementations should be deleted, not deprecated. The canonical API is Evidence Find, Evidence Inspect, and Query.

- Internal production names should stop teaching legacy vocabulary:
  - session candidate lookup should use Evidence Find terminology
  - structured transcript text selection should use Evidence Snippet terminology
  - text-only helpers should use Evidence Text terminology where they remain useful internally

- Evidence Find candidates should retain `session.summary` by default because it is high-signal candidate-selection metadata.

- Evidence Find candidates should remove broad top-level artifact inventories. In particular, session-level Clean Transcript, Tool Log, and subagent transcript path lists should not be repeated across every candidate.

- Evidence Find may include candidate-specific artifact pointers when they directly help an LLM choose or retrieve scoped context. The subagent run transcript path qualifies because it points to the exact matched child run. A broad Tool Log path on every tool/question/mutation candidate does not qualify because the useful scoped unit is the inspected Tool Log section.

- Evidence Inspect should be the canonical source for artifact metadata, locators, and Evidence Snippets.

- Session inspection without query should return a successful Evidence Packet with no evidence snippets:

```json
{
  "ref": "session/<session_id>",
  "session": { "session_id": "...", "project": "...", "started_at": "..." },
  "match": { "kind": "session" },
  "artifacts": {
    "clean_transcript": { "path": "...", "exists": true },
    "tool_log": { "path": "...", "exists": true },
    "subagent_transcripts": { "count": 2 }
  },
  "inspect_refs": {
    "subagents": [
      {
        "ref": "subagent/<session_id>/0",
        "requested_agent_type": "scout",
        "task_preview": "Map the current CLI/docs surface..."
      }
    ]
  },
  "evidence": []
}
```

- Session inspection with query should return the same artifact metadata and subagent refs, plus query-focused Evidence Snippets.

- Generated artifact metadata should include both `path` and `exists` for deterministic artifacts, because paths can be known before generated files exist.

- Session inspection should not include raw Source Transcript paths. Generated artifacts remain the normal evidence path.

- Session inspection should not list every subagent transcript path. It should expose subagent Inspection References with selection metadata; exact child transcript paths come from inspecting a specific subagent reference or from candidate-specific subagent find results.

- `inspect_refs` may contain structured arrays when that materially helps LLM navigation. The session-to-subagents relationship is the accepted case.

- Product-facing “excerpt” vocabulary should become “snippet” vocabulary. Evidence Snippet is the canonical term for bounded text returned in an Evidence Packet.

- `query --schema` should print a curated reference only, not raw CREATE TABLE DDL. The reference should cover table purpose, important columns, semantics, ref construction, and copyable examples.

- Local evaluation scripts should be updated only as needed to keep imports working after production renames. Broad cleanup of historical eval terminology is out of scope unless it touches product-facing guidance.

## Testing Decisions

- Tests should assert externally observable CLI and JSON behavior, not private helper structure.

- Evidence Find tests should verify:
  - no broad top-level artifact inventory is returned
  - `session.summary` remains present
  - no evidence text is returned
  - candidate-specific subagent transcript path remains available
  - refs returned by find can still be passed unchanged to inspect

- Evidence Inspect tests should verify:
  - session inspect without query succeeds and returns artifact metadata, subagent refs, and empty evidence
  - session inspect with query returns the same metadata plus Evidence Snippets
  - generated artifact metadata includes deterministic paths and existence booleans
  - subagent refs include requested agent type and task preview
  - tool/question/subagent inspections still return scoped evidence paths and locators
  - snippet locators use snippet terminology rather than excerpt terminology

- Query tests should verify `query --schema` emits a curated reference with table semantics and examples, without raw DDL fragments or partial SQL comments.

- CLI tests should verify legacy search/excerpt commands and wrappers are gone from the installed skill script surface and are not registered as primary commands.

- Documentation tests, if present or practical, should focus on the SKILL and CLI help containing enough standalone guidance for LLM use.

- Existing integration tests that exercise FTS-backed candidate lookup should be updated to the renamed internal function while preserving behavioral assertions.

## Out of Scope

- Adding new CLI flags to toggle artifact verbosity.

- Natural-language parsing inside the CLI.

- Returning raw Source Transcript paths as part of the normal Evidence Inspect workflow.

- Listing every subagent transcript path in session-level inspect metadata.

- Reworking the underlying database schema.

- Redesigning ranking, FTS behavior, or snippet selection strategy beyond terminology and response-shape cleanup.

- Full cleanup of historical benchmark/evaluation vocabulary unrelated to product-facing CLI use.

- README as a required LLM operating manual.

## Further Notes

This work supersedes the remaining legacy search/excerpt surface left after the Evidence Find / Evidence Inspect migration. The guiding principle is not “less JSON is always better”; it is “every field should help the LLM reach relevant scoped context faster.” Broad inventories and duplicated paths should be removed when they distract from candidate selection. Candidate-specific handles should remain when they shorten the path to useful context.
