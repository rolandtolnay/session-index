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

**Clean Transcript**:
The generated markdown conversation artifact for a session, excluding detailed tool-call logs.
_Avoid_: Raw transcript, source transcript

**Source Transcript**:
The provider-owned raw session log consumed by Session Index.
_Avoid_: Clean transcript, markdown transcript

**Tool Log**:
The generated markdown artifact containing detailed tool-call records for a session.
_Avoid_: Clean transcript, source transcript

**File Mutation**:
A successful write or edit tool event targeting a file path during a session.
_Avoid_: Files touched, file reference, read file

**Evidence Packet**:
A machine-readable bundle that connects one matched session fact or topic match to the transcript, tool-log, or subagent text needed to inspect what happened.
_Avoid_: Search result, query row, raw artifact

**File Mutation**:
An attributed file path targeted by a successful write or edit tool action within a session.
_Avoid_: Changed File, Session Footprint, File Event

**Evidence Find**:
The primary retrieval action that turns topic or fact criteria into compact candidate results with inspection-ready references.
_Avoid_: Search, excerpt, evidence dump

**Evidence Inspect**:
The scoped retrieval action that turns an inspection-ready reference into bounded transcript, tool-log, or subagent evidence text.
_Avoid_: File read, broad excerpt, search

**Inspection Reference**:
A stable string returned by Evidence Find and accepted unchanged by Evidence Inspect to identify a specific session, tool call, or subagent run.
_Avoid_: File path, line number, raw query row

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
- An **Evidence Find** produces zero or more compact candidate results and does not include transcript, tool-log, or subagent evidence text.
- An **Evidence Find** result includes one or more **Inspection References**.
- An **Evidence Inspect** accepts an **Inspection Reference** produced by **Evidence Find**.
- An **Evidence Inspect** produces one or more **Evidence Packets**.
- A session may have zero or more **File Mutations**.
- A **File Mutation** is attributed to a tool-call sequence and may be inspected through the corresponding **Tool Log** section.
- A session may produce zero or more **Evidence Packets** when facts or topic matches point to inspectable artifact text.
- An **Evidence Packet** references one **Canonical Session ID** and may reference a **Clean Transcript**, **Tool Log**, or **Subagent Run** transcript.
- A session may request zero or more **Subagent Runs**.
- A **File Mutation** belongs to exactly one **Canonical Session ID**.
- A session may have zero or more **File Mutations**.
- A **File Mutation** may be attributed to the main session or to a **Subagent Run** when the mutation occurred inside child-agent work.
- A **Subagent Run** has one **Requested Agent Type** when the parent session records the request.
- A **Subagent Run** may be known from the parent request even when its child Source Transcript or generated artifacts are missing.

## Example dialogue

> **Dev:** "Can the agent tell me where this conversation's transcript is?"
> **Domain expert:** "Yes — from inside the **Current Session**, it should return the deterministic **Clean Transcript** path, not guess the latest session for the project."

## Flagged ambiguities

- "current" can mean active agent process, focused terminal tab, or latest project session — resolved for v1: **Current Session** means the active agent/runtime process only.
- A missing **Current Session** must not be guessed from latest project or terminal state in v1; command callers should receive a clear failure.
- "session ID" can mean provider-native ID or Session Index ID — resolved for v1: default command output uses the **Canonical Session ID**.
- "find" can mean natural-language intent parsing or deterministic **Evidence Find** criteria — resolved: the CLI exposes structured criteria, and the calling LLM maps user language to those criteria using help text and the session-search skill.
- **Evidence Packet** output can mean human-readable text, TOON, or JSON — resolved: JSON is the canonical and only planned output format for reliable, predictable LLM use.
- **Inspection Reference** syntax can be URI-like, JSON-shaped, or path-like — resolved: use slash-style strings such as `session/<session_id>`, `tool/<session_id>/<sequence>`, and `subagent/<session_id>/<child_index>` so Pi session ids containing `:` remain easy to parse.
- Evidence locations can be persisted as indexed spans or derived from artifacts at inspection time — resolved for v1: derive from current artifacts at inspection time and return computed locators, avoiding a general artifact-span schema until the workflow proves it needs one.
- "changed files" can mean read/search references, write/edit targets, git dirty state, or net filesystem delta — resolved: **File Mutation** means successful write/edit tool targets only and is distinct from Pi's **Session Footprint** concept.
