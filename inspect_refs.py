"""Inspection Reference parsing/formatting for Evidence Find/Inspect."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


class InspectionRefError(ValueError):
    """Raised when an Inspection Reference is malformed."""


def _validate_session_id(session_id: str) -> None:
    if not session_id:
        raise InspectionRefError("inspection ref requires session_id")


def _validate_non_negative(value: int, label: str) -> None:
    if value < 0:
        raise InspectionRefError(f"Invalid {label} in inspection ref: must be non-negative")


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    kind: Literal["session"] = field(init=False, default="session")

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)


@dataclass(frozen=True)
class ToolRef:
    session_id: str
    sequence: int
    kind: Literal["tool"] = field(init=False, default="tool")

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_non_negative(self.sequence, "sequence")


@dataclass(frozen=True)
class QuestionRef:
    session_id: str
    sequence: int
    question_index: int
    kind: Literal["question"] = field(init=False, default="question")

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_non_negative(self.sequence, "sequence")
        _validate_non_negative(self.question_index, "question_index")


@dataclass(frozen=True)
class SubagentRef:
    session_id: str
    child_index: int
    kind: Literal["subagent"] = field(init=False, default="subagent")

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_non_negative(self.child_index, "child_index")


InspectionRef: TypeAlias = SessionRef | ToolRef | QuestionRef | SubagentRef


def _parse_int(value: str, label: str) -> int:
    if value == "":
        raise InspectionRefError(f"Missing {label} in inspection ref")
    try:
        parsed = int(value)
    except ValueError as e:
        raise InspectionRefError(f"Invalid {label} in inspection ref: {value!r}") from e
    _validate_non_negative(parsed, label)
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
        return SessionRef(session_id=_session_id(parts))
    if kind == "tool":
        if len(parts) < 3:
            raise InspectionRefError("Expected tool/<session_id>/<sequence>")
        return ToolRef(
            session_id=_join_session_id(parts, 1, -1),
            sequence=_parse_int(parts[-1], "sequence"),
        )
    if kind == "question":
        if len(parts) < 4:
            raise InspectionRefError("Expected question/<session_id>/<sequence>/<question_index>")
        return QuestionRef(
            session_id=_join_session_id(parts, 1, -2),
            sequence=_parse_int(parts[-2], "sequence"),
            question_index=_parse_int(parts[-1], "question_index"),
        )
    if kind == "subagent":
        if len(parts) < 3:
            raise InspectionRefError("Expected subagent/<session_id>/<child_index>")
        return SubagentRef(
            session_id=_join_session_id(parts, 1, -1),
            child_index=_parse_int(parts[-1], "child_index"),
        )

    raise InspectionRefError(f"Unknown inspection ref kind: {kind!r}")


def format_ref(ref: InspectionRef) -> str:
    """Format an InspectionRef as the slash-style CLI contract string."""
    if isinstance(ref, SessionRef):
        return f"session/{ref.session_id}"
    if isinstance(ref, ToolRef):
        return f"tool/{ref.session_id}/{ref.sequence}"
    if isinstance(ref, QuestionRef):
        return f"question/{ref.session_id}/{ref.sequence}/{ref.question_index}"
    if isinstance(ref, SubagentRef):
        return f"subagent/{ref.session_id}/{ref.child_index}"
    raise InspectionRefError(f"Unknown inspection ref type: {type(ref).__name__}")
