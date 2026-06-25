"""Extraction of queryable tool-call facts from parsed tool calls.

Pure functions over `ParsedToolCall` (provider-agnostic — both the Claude and Pi
parsers produce the same shape). The indexer persists the returned row dicts into
the `tool_calls`, `subagent_runs`, and `question_answers` fact tables.
"""

from __future__ import annotations

from typing import Any

from parser import ParsedToolCall
from subagent_runs import ParsedSubagentRun
from tool_events import iter_tool_use_candidates

_QUESTION_TOOLS = {"askuserquestion", "question"}
_FILE_MUTATION_TOOLS = {"write", "edit", "apply_patch"}
_RECOMMENDED_MARKER = "(Recommended)"


def normalize_tool_name(raw: str) -> str:
    """Lexically normalize a raw tool name: namespace-stripped + lowercased.

    Semantic families are intentionally not collapsed here: the raw `tool_name`
    stays available, and higher-level tables such as `question_answers` and
    `subagent_runs` expose provider-independent facts for those domains.
    """
    return (raw or "").rsplit(".", 1)[-1].lower()


def build_tool_call_rows(
    session_id: str, source: str, combined_calls: list[ParsedToolCall],
) -> list[dict[str, Any]]:
    """One row per tool call (main + subagent scope)."""
    rows: list[dict[str, Any]] = []
    for call in combined_calls:
        rows.append({
            "session_id": session_id,
            "source": source,
            "scope": call.scope or "main",
            "sequence": call.sequence or None,
            "timestamp": call.timestamp or None,
            "tool_name": call.tool_name or "",
            "tool": normalize_tool_name(call.tool_name),
            "is_error": 1 if call.is_error else 0,
        })
    return rows


def _top_level_paths(arguments: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _edit_batch_paths(arguments: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    edits = arguments.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("path"), str) and edit["path"]:
                paths.append(edit["path"])
    return paths


def _apply_patch_paths(arguments: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    changes = arguments.get("changes")
    if isinstance(changes, dict):
        for path, change in changes.items():
            if isinstance(path, str) and path:
                paths.append(path)
            if isinstance(change, dict) and isinstance(change.get("move_path"), str) and change["move_path"]:
                paths.append(change["move_path"])
    elif isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            move_path = change.get("move_path")
            if isinstance(path, str) and path:
                paths.append(path)
            if isinstance(move_path, str) and move_path:
                paths.append(move_path)
    return paths


def _unique_in_order(paths: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _mutation_paths(tool: str, arguments: dict[str, Any]) -> list[str]:
    if tool not in _FILE_MUTATION_TOOLS:
        return []
    if tool == "apply_patch":
        return _unique_in_order(_apply_patch_paths(arguments))

    paths = _top_level_paths(arguments)
    if tool == "edit":
        paths.extend(_edit_batch_paths(arguments))
    return _unique_in_order(paths)


def _append_file_mutation_rows(
    rows: list[dict[str, Any]],
    *,
    session_id: str,
    source: str,
    call: ParsedToolCall,
    tool_name: str,
    tool: str,
    paths: list[str],
) -> None:
    for path in paths:
        rows.append({
            "session_id": session_id,
            "source": source,
            "scope": call.scope or "main",
            "sequence": call.sequence or None,
            "timestamp": call.timestamp or None,
            "tool_name": tool_name or "",
            "tool": tool,
            "path": path,
        })


def build_file_mutation_rows(
    session_id: str, source: str, combined_calls: list[ParsedToolCall],
) -> list[dict[str, Any]]:
    """One row per successful write/edit file mutation event."""
    rows: list[dict[str, Any]] = []
    for call in combined_calls:
        if call.is_error:
            continue

        for candidate in iter_tool_use_candidates(call):
            tool = normalize_tool_name(candidate.tool_name)
            _append_file_mutation_rows(
                rows,
                session_id=session_id,
                source=source,
                call=call,
                tool_name=candidate.tool_name,
                tool=tool,
                paths=_mutation_paths(tool, candidate.arguments),
            )
    return rows


def build_subagent_run_rows(runs: list[ParsedSubagentRun]) -> list[dict[str, Any]]:
    """Map normalized ParsedSubagentRun facts to subagent_runs rows."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        rows.append({
            "parent_session_id": run.parent_session_id,
            "source": run.source,
            "requested_agent_type": run.requested_agent_type or None,
            "observed_agent_type": run.observed_agent_type or None,
            "call_tool": run.call_tool or None,
            "call_sequence": run.call_sequence,
            "call_tool_id": run.call_tool_id or None,
            "child_index": run.child_index,
            "agent_id": run.agent_id or None,
            "status": run.status or None,
            "started_at": run.started_at or None,
            "ended_at": run.ended_at or None,
            "duration_seconds": run.duration_seconds or None,
            "tool_call_count": run.tool_call_count or None,
            "transcript_path": run.transcript_path or None,
            "task_preview": run.task_preview or None,
            "match_confidence": run.match_confidence or None,
        })
    return rows


# ── Question outcomes ──────────────────────────────────────────────────────

def _option_labels(options: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict) and isinstance(opt.get("label"), str):
                labels.append(opt["label"])
    return labels


def _recommended_labels(options: Any) -> set[str]:
    """Labels flagged `(Recommended)` — Claude marks the option label or its
    description; Pi marks the label. Either location counts."""
    rec: set[str] = set()
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            label = opt.get("label")
            if not isinstance(label, str):
                continue
            description = opt.get("description") or ""
            if _RECOMMENDED_MARKER in label or _RECOMMENDED_MARKER in description:
                rec.add(label)
    return rec


def _selected_from_question_outcome(call: ParsedToolCall, question_index: int, question_text: str) -> list[str] | None:
    """Resolve selected option(s) from parser-normalized question outcomes."""
    if call.question_cancelled:
        return []

    selection = None
    for candidate in call.question_selections:
        if candidate.question == question_text:
            selection = candidate
            break
    if selection is None and 0 <= question_index < len(call.question_selections):
        selection = call.question_selections[question_index]

    if selection and selection.selected_labels:
        return selection.selected_labels
    return None


def _selected_from_text(result_text: str, question_text: str) -> list[str] | None:
    """Resolve the picked option from the result text.

    Handles Claude's `"<question>"="<label>"` echo and Pi's `- <question> -> <answer>`
    line form. Returns None when neither matches.
    """
    if not result_text or not question_text:
        return None

    # Claude: ..."<question>"="<label>", ...
    needle = f'"{question_text}"="'
    idx = result_text.find(needle)
    if idx != -1:
        start = idx + len(needle)
        end = result_text.find('"', start)
        label = (result_text[start:] if end == -1 else result_text[start:end]).strip()
        return [label] if label else None

    # Pi: "- <question> -> <answer>"
    needle = f"{question_text} -> "
    idx = result_text.find(needle)
    if idx != -1:
        start = idx + len(needle)
        end = result_text.find("\n", start)
        answer = (result_text[start:] if end == -1 else result_text[start:end]).strip()
        return [answer] if answer else None

    return None


def build_question_rows(
    session_id: str, source: str, combined_calls: list[ParsedToolCall],
) -> list[dict[str, Any]]:
    """One row per asked question across all question-tool calls.

    Parser-normalized question selections are authoritative; result text is the
    fallback (covers Claude and legacy Pi text-only echoes). Unanswered/cancelled
    questions are stored with NULL selected_label/was_recommended (still counted
    as "asked"). MultiSelect rows store joined labels with was_recommended NULL
    (ambiguous by design).
    """
    rows: list[dict[str, Any]] = []
    for call in combined_calls:
        if normalize_tool_name(call.tool_name) not in _QUESTION_TOOLS:
            continue
        args = call.arguments if isinstance(call.arguments, dict) else {}
        questions = args.get("questions")
        if not isinstance(questions, list):
            continue
        result_text = call.result or ""

        for question_index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            options = question.get("options", [])
            option_labels = set(_option_labels(options))
            recommended = _recommended_labels(options)
            multi_select = bool(question.get("multiSelect"))
            question_text = question.get("question") or ""
            header = question.get("header") or ""

            selected = _selected_from_question_outcome(call, question_index, question_text)
            if selected is None:
                selected = _selected_from_text(result_text, question_text)
            answered = bool(selected)

            if not answered:
                selected_label: str | None = None
                was_recommended: int | None = None
                is_other: int | None = None
            elif multi_select:
                selected_label = ", ".join(selected)
                was_recommended = None
                is_other = 1 if any(s not in option_labels for s in selected) else 0
            else:
                selected_label = selected[0]
                is_other = 0 if selected_label in option_labels else 1
                if recommended:
                    was_recommended = 1 if selected_label in recommended else 0
                else:
                    was_recommended = None

            rows.append({
                "session_id": session_id,
                "source": source,
                "sequence": call.sequence or None,
                "question_index": question_index,
                "header": header or None,
                "question": question_text or None,
                "selected_label": selected_label,
                "was_recommended": was_recommended,
                "is_other": is_other,
                "option_count": len(options) if isinstance(options, list) else 0,
                "multi_select": 1 if multi_select else 0,
            })
    return rows
