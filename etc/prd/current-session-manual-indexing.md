# Current Session Manual Indexing

## Problem Statement

The Current Session Display can show deterministic Clean Transcript and Tool Log paths before those generated artifacts exist. For an active or resumed Pi session, this leaves the user looking at correct but unsatisfying `[missing]` statuses even though the Source Transcript exists and the normal session-shutdown full indexing pass could generate the artifacts.

The user needs a deliberate, user-only way to run full indexing for the active Current Session from the display itself, then see the display update when generated artifacts now exist.

## Solution

Add focused-only **Manual Current Session Indexing** to the Current Session Display. While the display is open, `Ctrl+R` starts the same full Pi indexing pass normally run on session shutdown. The display advertises this as `Ctrl+R index current snapshot`, shows progress/result state, refreshes exact Current Session metadata when possible, and updates Clean Transcript and Tool Log existence statuses without appending anything to the active conversation or model context.

Existing generated artifacts should show compact local last-written timestamps on their own rows. These timestamps come from artifact file metadata and must be labeled/presented as artifact last-written times, not as an authoritative database `last indexed` field.

## User Stories

1. As a Pi user viewing the Current Session Display, I want to press `Ctrl+R` to index the current snapshot, so that missing generated artifacts can be created before I end the session.
2. As a Pi user, I want the display to show indexing progress after I press `Ctrl+R`, so that I know the hotkey was accepted and work is in flight.
3. As a Pi user, I want the display to refresh artifact statuses after indexing finishes while the display is still open, so that `[missing]` can become `[exists]` without rerunning the command.
4. As a Pi user, I want existing Clean Transcript and Tool Log rows to show compact last-written timestamps, so that resumed sessions reveal when those artifacts were previously generated.
5. As a Pi user, I want missing artifacts to remain truthfully marked `[missing]` after indexing, so that sessions with no tool calls or insufficient content are not misrepresented.
6. As a Pi user, I want closing the display after starting indexing to leave the indexing job running, so that a committed manual indexing request is not accidentally cancelled.
7. As a Pi user, I want repeated `Ctrl+R` presses while indexing is already running to be ignored, so that duplicate full indexing jobs and model calls are not started.
8. As a Pi user, I want Manual Current Session Indexing to remain user-only and model-invisible, so that it does not mutate the active chat transcript or become part of model context.
9. As a Pi user, I want the display to stop waiting after a generous timeout while allowing the indexing job to continue in the background, so that a slow summarizer does not trap the focused UI indefinitely.

## Implementation Decisions

- Manual Current Session Indexing runs the existing full Pi indexing pass, not a transcript-only shortcut. This includes metadata, summary, Clean Transcript, Subagent transcript, Tool Log, and database/search updates according to the existing full indexing pipeline.
- The hotkey is `Ctrl+R` and only applies while the Current Session Display is focused. There is no global Pi keybinding in scope.
- Pressing `Ctrl+R` starts indexing immediately. No second confirmation prompt is shown because the modified key and visible footer hint make the action deliberate.
- The display footer/action hint should advertise `Ctrl+R index current snapshot` alongside the close keys. Existing display copy that says there is no indexing or artifact generation must be replaced with copy that accurately describes the user-only indexing side effect.
- Before starting indexing, the command refreshes exact Pi runtime identity from the active session manager and overlays Session Index environment. It must preserve the existing no-guessing Current Session contract.
- Manual indexing is single-flight per display/session. While a job is running, additional `Ctrl+R` presses do not start or queue another job.
- The manual runner should wait for the full indexing child process when the display is open, but it must not use the detached fire-and-forget lifecycle helper directly because the UI needs completion/timeout feedback.
- The manual runner has a generous UI-level wait timeout above the summarizer’s normal timeout. If the wait timeout is reached, the display reports that indexing is still running or timed out from the UI’s perspective, refreshes factual artifact metadata if possible, and leaves the child process running in the background.
- Closing the display after indexing starts does not cancel the indexing job. If the display is closed before completion, the job finishes silently; the UI is not reopened and no conversation entry is appended.
- If the display remains open and indexing completes before the UI timeout, the command re-runs current-session metadata resolution and updates the display in place.
- Completion copy stays simple and factual, such as `Indexed snapshot at <local time>`. Detailed per-stage indexing status is out of scope; artifact rows remain the source of truth.
- Current-session metadata should expose optional last-written timestamps for generated artifacts when Clean Transcript or Tool Log files exist. These are filesystem artifact timestamps, not database index timestamps.
- Artifact timestamps are shown per generated artifact row, using compact local time. The Source Transcript row does not show a last-modified timestamp because that would conflate raw session mutation with generated artifact indexing.
- The active Pi conversation must not receive a visible notice, custom session entry, or model-context addition for this action.
- Documentation should explicitly distinguish display-only metadata inspection from the new explicit Manual Current Session Indexing hotkey.

## Testing Decisions

- Tests should focus on externally visible behavior: JSON metadata fields, rendered display text/states, key handling, and command orchestration outcomes. Avoid testing private helper structure.
- Cover current-session metadata behavior for generated artifact last-written timestamps: present when artifact files exist, absent when missing, and not produced for Source Transcript as an indexing timestamp.
- Cover display rendering for:
  - `Ctrl+R index current snapshot` footer hint
  - existing artifact timestamps on Clean Transcript and Tool Log rows
  - indexing progress state
  - completed state with refreshed statuses
  - factual missing statuses after completion
- Cover focused key behavior:
  - `Ctrl+R` starts indexing
  - dismissal keys still close the display
  - repeated `Ctrl+R` while running does not start another job
- Cover command orchestration with mocked child processes and current-session metadata resolution:
  - missing Pi identity still fails clearly without ambient fallback
  - full indexing is invoked with refreshed Session Index env
  - completion refreshes metadata while open
  - timeout stops waiting but does not kill the child
- Prior test patterns already exist for current-session display formatting, extension command registration, Session Index env construction, staged indexing, and current-session resolver behavior; extend those patterns rather than adding brittle full end-to-end terminal tests.

## Out of Scope

- A global Pi hotkey outside the Current Session Display.
- A transcript-only indexing mode for this UI action.
- A new authoritative database `last_full_indexed_at` or schema migration.
- Detailed per-stage progress or structured indexing diagnostics in the focused UI.
- Cancelling manual indexing from the display after it starts.
- Reopening the display or sending notifications after completion if the user closed it.
- Changing how Pi selects or parses the active branch inside a Source Transcript.
- Changing Clean Transcript or Tool Log content format.

## Further Notes

- Full indexing may invoke summarization through the configured model path, so the action should be documented as potentially slower and resource-consuming.
- Generated artifacts represent a snapshot of an active session. If the conversation continues afterward, the user can press `Ctrl+R` again after the previous job completes to index a newer snapshot.
- Artifact last-written timestamps help with resumed sessions but should not be described as authoritative indexing history.
