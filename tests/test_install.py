"""Installer coverage for the user-level Codex integration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_installer(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        ["node", "install.js", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_codex_install_is_idempotent_and_uninstall_preserves_unrelated_hooks(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    original = {
        "custom": {"keep": True},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "/other/start"}]}],
            "Stop": [{
                "matcher": "ignored-by-stop",
                "hooks": [{"type": "command", "command": "/other/stop"}],
            }],
        },
    }
    hooks_path.write_text(json.dumps(original))

    first = _run_installer(tmp_path, "--target", "codex")
    second = _run_installer(tmp_path, "--target", "codex")

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout
    document = json.loads(hooks_path.read_text())
    commands = [
        handler.get("command", "")
        for group in document["hooks"]["Stop"]
        for handler in group.get("hooks", [])
    ]
    assert commands.count("/other/stop") == 1
    ours = [command for command in commands if command.endswith('hooks/codex_stop.py"')]
    assert len(ours) == 1
    assert "uv run --quiet" in ours[0]
    assert document["custom"] == {"keep": True}
    assert document["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]

    search_skill = codex_dir / "skills" / "session-search"
    current_skill = codex_dir / "skills" / "current-session"
    assert search_skill.is_symlink()
    assert search_skill.resolve() == REPO_ROOT / "skills" / "session-search"
    assert current_skill.is_symlink()
    assert current_skill.resolve() == REPO_ROOT / "skills" / "current-session"
    manifest = json.loads((codex_dir / "session-index" / ".manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert manifest["skills"] == ["session-search", "current-session"]
    assert manifest["hooksFileCreated"] is False
    assert "review/trust" in first.stdout

    removed = _run_installer(tmp_path, "--uninstall", "--target", "codex")

    assert removed.returncode == 0, removed.stderr or removed.stdout
    assert not search_skill.exists()
    assert not current_skill.exists()
    assert not (codex_dir / "session-index" / ".manifest.json").exists()
    assert json.loads(hooks_path.read_text()) == original


def test_codex_uninstall_removes_hooks_file_created_by_installer(tmp_path):
    installed = _run_installer(tmp_path, "--target", "codex")
    assert installed.returncode == 0, installed.stderr or installed.stdout
    hooks_path = tmp_path / ".codex" / "hooks.json"
    assert hooks_path.exists()

    removed = _run_installer(tmp_path, "--uninstall", "--target", "codex")

    assert removed.returncode == 0, removed.stderr or removed.stdout
    assert not hooks_path.exists()
