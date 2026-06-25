# Session Index

Session Index preserves searchable records of agent conversations and exposes enough identity metadata to reconnect a running conversation with its indexed artifacts.

## Language

**Current Session**:
The agent conversation associated with the active agent/runtime process.
_Avoid_: Latest session, current terminal session, current project session

**Current Session Display**:
A user-visible presentation of Current Session identity and artifact locations for copying or inspection.
_Avoid_: Chat message, model context, transcript lookup

**Manual Current Session Indexing**:
An explicit user request from a Current Session Display to run the full indexing pass for the active Current Session before session shutdown.
_Avoid_: Refresh, backfill, transcript generation

**Canonical Session ID**:
The Session Index identifier for a session, including any provider namespace prefix required for uniqueness.
_Avoid_: Native ID, raw provider ID

Provider namespace examples:
Claude sessions normally use the native session id directly; Pi uses `pi:<uuid>`;
Codex uses `codex:<uuid>`.

**Clean Transcript**:
The generated markdown conversation artifact for a session, excluding detailed tool-call logs.
_Avoid_: Raw transcript, source transcript

**Source Transcript**:
The provider-owned raw session log consumed by Session Index.
_Avoid_: Clean transcript, markdown transcript

Codex Source Transcripts are the `rollout-*.jsonl` files under
`~/.codex/sessions/YYYY/MM/DD/` and `~/.codex/archived_sessions/`; Codex
metadata files such as `session_index.jsonl` and `state_5.sqlite` can enrich
rows but are not Source Transcripts.

**Tool Log**:
The generated markdown artifact containing detailed tool-call records for a session.
_Avoid_: Clean transcript, source transcript

**Tool Call**:
A provider-recorded assistant tool-use event that may have arguments, result text, and a corresponding Tool Log section.
_Avoid_: Skill Invocation, user request

**Skill Invocation**:
A request to load a named reusable prompt or workflow template, regardless of whether the provider presents it as a slash command, Pi skill envelope, provider Skill tool event, or exact `SKILL.md` read.
_Avoid_: Command Invocation, Skill result, Subagent Run, generic Tool Call

**Evidence Packet**:
A machine-readable bundle that connects one matched session fact or topic match to the transcript, tool-log, or subagent text needed to inspect what happened.
_Avoid_: Search result, query row, raw artifact

**Evidence Snippet**:
A bounded text selection from a Clean Transcript, Tool Log, or Subagent Run transcript included in an Evidence Packet.
_Avoid_: Excerpt, passage, broad transcript dump

**File Mutation**:
An attributed file path targeted by a successful write or edit tool action within a session.
_Avoid_: Changed File, Session Footprint, File Event

**Session-Collapsed Mutation Candidate**:
An Evidence Find result that groups matching File Mutations for one Canonical Session ID.
_Avoid_: Mutation event, changed-file list, raw mutation row

**Representative Mutation Path**:
A compact preview path selected only from File Mutations that matched the Evidence Find criteria for a Session-Collapsed Mutation Candidate.
_Avoid_: All changed files, files touched, session footprint

**Evidence Find**:
The primary retrieval action that turns topic or fact criteria into compact candidate results with inspection-ready references.
_Avoid_: Search, excerpt, evidence dump

**Evidence Inspect**:
The scoped retrieval action that turns an inspection-ready reference into bounded transcript, tool-log, or subagent evidence text.
_Avoid_: File read, broad excerpt, search

**Inspection Reference**:
A stable string returned by Evidence Find and accepted unchanged by Evidence Inspect to identify a specific session, skill invocation, tool call, or subagent run.
_Avoid_: File path, line number, raw query row

**LLM-Facing CLI Surface**:
The self-contained instruction layer formed by the installed skill documentation and CLI help output.
_Avoid_: README-dependent workflow, hidden maintainer knowledge

**Leaf ID**:
A provider-specific Pi branch identifier inside a session tree.
_Avoid_: Session ID, transcript ID

**Subagent Run**:
A child agent execution requested from a parent session.
_Avoid_: Agent transcript, subagent artifact

**Requested Agent Type**:
The agent name requested by the parent session for a Subagent Run, used as the canonical query label.
_Avoid_: Observed child type, artifact title

## Relationships

- A **Current Session** has exactly one **Canonical Session ID**.
- A **Current Session** has exactly one **Source Transcript** when the provider exposes a session file.
- A **Current Session** has one deterministic **Clean Transcript** path, even before that file exists.
- A **Current Session** has one deterministic **Tool Log** path, even before that file exists.
- A **Current Session** may expose a **Leaf ID** when the provider has branch-level identity.
- A **Current Session Display** presents metadata about exactly one **Current Session**.
- A **Current Session Display** may initiate **Manual Current Session Indexing** for its active **Current Session**.
- An **Evidence Find** produces zero or more compact candidate results and does not include evidence text.
- An **Evidence Find** result includes one or more **Inspection References** and may include candidate-specific metadata that directly helps choose or retrieve scoped context.
- An **Evidence Inspect** accepts an **Inspection Reference** produced by **Evidence Find**.
- A **Tool Log** contains sections for zero or more **Tool Calls**.
- A **Skill Invocation** belongs to exactly one **Canonical Session ID** and abstracts over provider-specific skill, slash-command, skill-envelope, or exact `SKILL.md` read encodings.
- An **Evidence Inspect** produces one or more **Evidence Packets**.
- An **Evidence Packet** may contain zero or more **Evidence Snippets**.
- A session may have zero or more **File Mutations**.
- A **File Mutation** is attributed to a tool-call sequence and may be inspected through the corresponding **Tool Log** section.
- A session may produce zero or more **Evidence Packets** when facts or topic matches point to inspectable artifact text.
- An **Evidence Packet** references one **Canonical Session ID** and may reference a **Clean Transcript**, **Tool Log**, or **Subagent Run** transcript.
- A session may request zero or more **Subagent Runs**.
- A **File Mutation** belongs to exactly one **Canonical Session ID**.
- A session may have zero or more **File Mutations**.
- A **Session-Collapsed Mutation Candidate** represents one **Canonical Session ID** and one or more matching **File Mutations**.
- A **Session-Collapsed Mutation Candidate** may include one or more **Representative Mutation Paths**.
- A **Representative Mutation Path** is selected from matching **File Mutations**, not from broad session file metadata.
- A **File Mutation** may be attributed to the main session or to a **Subagent Run** when the mutation occurred inside child-agent work.
- A **Subagent Run** has one **Requested Agent Type** when the parent session records the request.
- A **Subagent Run** may be known from the parent request even when its child Source Transcript or generated artifacts are missing.
- The **LLM-Facing CLI Surface** must be sufficient for an LLM to move from user prompt to relevant scoped context without reading the README.

## Example dialogue

> **Dev:** "Can the agent tell me where this conversation's transcript is?"
> **Domain expert:** "Yes — from inside the **Current Session**, it should return the deterministic **Clean Transcript** path, not guess the latest session for the project."

## Flagged ambiguities

- "current" can mean active agent process, focused terminal tab, or latest project session — resolved for v1: **Current Session** means the active agent/runtime process only.
- A missing **Current Session** must not be guessed from latest project or terminal state in v1; command callers should receive a clear failure.
- "session ID" can mean provider-native ID or Session Index ID — resolved for v1: default command output uses the **Canonical Session ID**.
- "find" can mean natural-language intent parsing or deterministic **Evidence Find** criteria — resolved: the CLI exposes structured criteria, and the calling LLM maps user language to those criteria using help text and the session-search skill.
- "skill invocation" was used as if every skill use were a row in `tool_calls` or separate from slash commands — resolved: **Skill Invocation** is the unified user-facing fact for reusable prompt or workflow template use, persisted in `skill_invocations`; provider-specific encodings are parser implementation details.
- **Evidence Packet** output can mean human-readable text, TOON, or JSON — resolved: JSON is the canonical and only planned output format for reliable, predictable LLM use.
- "excerpt" was legacy retrieval language — resolved: use **Evidence Snippet** for bounded evidence text inside an **Evidence Packet**.
- **Inspection Reference** syntax can be URI-like, JSON-shaped, or path-like — resolved: use slash-style strings such as `session/<session_id>`, `skill/<session_id>/<sequence>`, `tool/<session_id>/<sequence>`, and `subagent/<session_id>/<child_index>` so Pi session ids containing `:` remain easy to parse.
- Evidence locations can be persisted as indexed spans or derived from artifacts at inspection time — resolved for v1: derive from current artifacts at inspection time and return computed locators, avoiding a general artifact-span schema until the workflow proves it needs one.
- Artifact paths can be broad artifact inventories or candidate-specific handles — resolved: **Evidence Find** avoids broad artifact inventories, but candidate-specific artifact pointers are acceptable when they directly help an LLM choose or retrieve scoped context.
- CLI compactness can mean fewer fields or faster path-to-context — resolved: optimize for the LLM reaching the most relevant scoped context quickly, not for minimal JSON or ceremony for its own sake.
- CLI usage guidance can live in README, docs, skill docs, or help text — resolved: the **LLM-Facing CLI Surface** is the installed skill documentation plus CLI help; README is for adopters and maintainers, not required LLM operating context.
- "changed files" can mean read/search references, write/edit targets, git dirty state, or net filesystem delta — resolved: **File Mutation** means successful write/edit tool targets only and is distinct from Pi's **Session Footprint** concept.
- `find --mutated` can mean event-level audit rows or session-level discovery candidates — resolved: **Session-Collapsed Mutation Candidate** is the default discovery result; event-level rows remain an explicit detail mode.
