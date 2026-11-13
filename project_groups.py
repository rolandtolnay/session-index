"""Project-group configuration and path matching shared by browsing and recents."""

from __future__ import annotations

import fnmatch
import json
import os
from typing import Any

PROJECT_CONTEXT_CONFIG_PATH = os.path.expanduser("~/.pi/agent/project-context.json")


def _glob_parts_match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern):
            result = path_index == len(path)
        elif pattern[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path) and match(pattern_index, path_index + 1)
            )
        else:
            result = path_index < len(path) and fnmatch.fnmatchcase(
                path[path_index], pattern[pattern_index]
            ) and match(pattern_index + 1, path_index + 1)
        memo[key] = result
        return result

    return match(0, 0)


def _selector_uses_unsupported_glob(selector: str) -> bool:
    """Reject Node-only glob forms that fnmatch would silently misinterpret."""
    return "{" in selector or "}" in selector or any(
        token in selector for token in ("@(", "+(", "?(", "*(", "!(")
    )


def selector_matches_path(selector: str, path: str) -> bool:
    """Match a configured root selector against path or any ancestor."""
    pattern = tuple(os.path.normpath(os.path.expanduser(selector)).split(os.sep))
    candidate = os.path.abspath(path)
    while True:
        if _glob_parts_match(pattern, tuple(candidate.split(os.sep))):
            return True
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return False
        candidate = parent


def load_project_groups(config_path: str | None = None) -> list[dict[str, Any]]:
    """Read validated groups from Pi's shared project-context configuration."""
    try:
        with open(config_path if config_path is not None else PROJECT_CONTEXT_CONFIG_PATH, encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except (OSError, ValueError):
        return []

    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("groups"), list):
        return []

    parsed: list[dict[str, Any]] = []
    names: set[str] = set()
    for candidate in raw["groups"]:
        if not isinstance(candidate, dict):
            return []
        name = candidate.get("name")
        projects = candidate.get("projects")
        files = candidate.get("files")
        if not isinstance(name, str) or not name.strip() or name.strip() in names:
            return []
        if not isinstance(projects, list) or not projects or not all(
            isinstance(selector, str) and selector.strip() for selector in projects
        ):
            return []
        if not isinstance(files, list) or not files or not all(
            isinstance(path, str) and path.strip() for path in files
        ):
            return []

        selectors = [selector.strip() for selector in projects]
        if any(
            selector != "~" and not selector.startswith("~/") and not os.path.isabs(selector)
            for selector in selectors
        ) or any(_selector_uses_unsupported_glob(selector) for selector in selectors):
            return []

        normalized_name = name.strip()
        names.add(normalized_name)
        parsed.append({"name": normalized_name, "selectors": selectors})

    return parsed
