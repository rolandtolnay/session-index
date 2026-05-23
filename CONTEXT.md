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
- A session may request zero or more **Subagent Runs**.
- A **Subagent Run** has one **Requested Agent Type** when the parent session records the request.
- A **Subagent Run** may be known from the parent request even when its child Source Transcript or generated artifacts are missing.

## Example dialogue

> **Dev:** "Can the agent tell me where this conversation's transcript is?"
> **Domain expert:** "Yes — from inside the **Current Session**, it should return the deterministic **Clean Transcript** path, not guess the latest session for the project."

## Flagged ambiguities

- "current" can mean active agent process, focused terminal tab, or latest project session — resolved for v1: **Current Session** means the active agent/runtime process only.
- A missing **Current Session** must not be guessed from latest project or terminal state in v1; command callers should receive a clear failure.
- "session ID" can mean provider-native ID or Session Index ID — resolved for v1: default command output uses the **Canonical Session ID**.
