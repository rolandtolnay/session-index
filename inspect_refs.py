"""Inspection Reference parsing/formatting for Evidence Find/Inspect."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InspectionRef:
    kind: str
    session_id: str
    sequence: int | None = None
    question_index: int | None = None
    child_index: int | None = None


class InspectionRefError(ValueError):
    """Raised when an Inspection Reference is malformed."""


def _parse_int(value: str, label: str) -> int:
    if value == "":
        raise InspectionRefError(f"Missing {label} in inspection ref")
    try:
        parsed = int(value)
    except ValueError as e:
        raise InspectionRefError(f"Invalid {label} in inspection ref: {value!r}") from e
    if parsed < 0:
        raise InspectionRefError(f"Invalid {label} in inspection ref: must be non-negative")
    return parsed


def _join_session_id(parts: list[str], start: int, end: int) -> str:
    session_id = "/".join(parts[start:end])
    if not session_id:
        raise InspectionRefError("Missing session_id in inspection ref")
    return session_id


def _session_id(parts: list[str]) -> str:
    if len(parts) < 2:
        raise InspectionRefError("Expected session/<session_id>")
    return _join_session_id(parts, 1, len(parts))


def parse_ref(value: str) -> InspectionRef:
    """Parse a slash-style Inspection Reference.

    Supported forms:
      - session/<session_id>
      - tool/<session_id>/<sequence>
      - question/<session_id>/<sequence>/<question_index>
      - subagent/<session_id>/<child_index>
    """
    if not value or not isinstance(value, str):
        raise InspectionRefError("Inspection ref must be a non-empty string")
    parts = value.split("/")
    kind = parts[0]

    if kind == "session":
        return InspectionRef(kind="session", session_id=_session_id(parts))
    if kind == "tool":
        if len(parts) < 3:
            raise InspectionRefError("Expected tool/<session_id>/<sequence>")
        return InspectionRef(
            kind="tool",
            session_id=_join_session_id(parts, 1, -1),
            sequence=_parse_int(parts[-1], "sequence"),
        )
    if kind == "question":
        if len(parts) < 4:
            raise InspectionRefError("Expected question/<session_id>/<sequence>/<question_index>")
        return InspectionRef(
            kind="question",
            session_id=_join_session_id(parts, 1, -2),
            sequence=_parse_int(parts[-2], "sequence"),
            question_index=_parse_int(parts[-1], "question_index"),
        )
    if kind == "subagent":
        if len(parts) < 3:
            raise InspectionRefError("Expected subagent/<session_id>/<child_index>")
        return InspectionRef(
            kind="subagent",
            session_id=_join_session_id(parts, 1, -1),
            child_index=_parse_int(parts[-1], "child_index"),
        )

    raise InspectionRefError(f"Unknown inspection ref kind: {kind!r}")


def format_ref(ref: InspectionRef) -> str:
    """Format an InspectionRef as the slash-style CLI contract string."""
    if ref.kind == "session":
        if not ref.session_id:
            raise InspectionRefError("session ref requires session_id")
        return f"session/{ref.session_id}"
    if ref.kind == "tool":
        if ref.sequence is None:
            raise InspectionRefError("tool ref requires sequence")
        return f"tool/{ref.session_id}/{ref.sequence}"
    if ref.kind == "question":
        if ref.sequence is None or ref.question_index is None:
            raise InspectionRefError("question ref requires sequence and question_index")
        return f"question/{ref.session_id}/{ref.sequence}/{ref.question_index}"
    if ref.kind == "subagent":
        if ref.child_index is None:
            raise InspectionRefError("subagent ref requires child_index")
        return f"subagent/{ref.session_id}/{ref.child_index}"
    raise InspectionRefError(f"Unknown inspection ref kind: {ref.kind!r}")
