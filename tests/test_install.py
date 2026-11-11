"""Installer coverage using temporary homes, never live runtime configuration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


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


def test_pi_install_manages_only_search_skill(tmp_path):
    pi_dir = tmp_path / ".pi" / "agent"
    skill = pi_dir / "skills" / "session-search"
    manifest_path = pi_dir / "session-index" / ".manifest.json"

    for _ in range(2):
        installed = _run_installer(tmp_path, "--target", "pi")
        assert installed.returncode == 0, installed.stderr or installed.stdout
        assert skill.is_symlink()
        assert skill.resolve() == REPO_ROOT / "skills" / "session-search"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["target"] == "pi"
        assert manifest["skill"] == "session-search"
        assert "extension" not in manifest
        assert not (pi_dir / "extensions").exists()
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".codex").exists()

    removed = _run_installer(tmp_path, "--uninstall", "--target", "pi")
    assert removed.returncode == 0, removed.stderr or removed.stdout
    assert not skill.is_symlink()
    assert not manifest_path.exists()


@pytest.mark.parametrize("extension_kind", ["directory", "symlink", "legacy-symlink", "broken-symlink"])
@pytest.mark.parametrize("reinstall", [False, True], ids=["legacy-uninstall", "install-reinstall-uninstall"])
def test_pi_preserves_extension_even_with_legacy_manifest(tmp_path, extension_kind, reinstall):
    pi_dir = tmp_path / ".pi" / "agent"
    extension = pi_dir / "extensions" / "session-index"
    extension.parent.mkdir(parents=True)
    legacy_root = tmp_path / "previous-session-index"
    if extension_kind == "directory":
        extension.mkdir()
        target = extension
    else:
        target = legacy_root / "pi-extension" if extension_kind == "legacy-symlink" else tmp_path / "pi-owned-extension"
        if extension_kind != "broken-symlink":
            target.mkdir(parents=True)
        extension.symlink_to(target, target_is_directory=True)
    if extension_kind != "broken-symlink":
        (target / "index.ts").write_text("// independently owned Pi adapter\n")

    skill = pi_dir / "skills" / "session-search"
    skill.parent.mkdir(parents=True)
    legacy_skill = legacy_root / "skills" / "session-search"
    legacy_skill.mkdir(parents=True)
    skill.symlink_to(legacy_skill, target_is_directory=True)
    manifest_path = pi_dir / "session-index" / ".manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps({
        "version": "1.0.0", "target": "pi", "repoRoot": str(legacy_root),
        "skill": "session-search", "extension": "session-index",
    }))

    # A normal reinstall owns the current skill, not an unrelated old skill link.
    if reinstall:
        skill.unlink()

    actions = [[], [], ["--uninstall"]] if reinstall else [["--uninstall"]]
    for args in actions:
        result = _run_installer(tmp_path, "--target", "pi", *args)
        assert result.returncode == 0, result.stderr or result.stdout
        if extension_kind == "directory":
            assert extension.is_dir() and not extension.is_symlink()
        else:
            assert extension.is_symlink()
            assert extension.readlink() == target
        if extension_kind == "broken-symlink":
            assert not target.exists()
        else:
            assert (target / "index.ts").read_text() == "// independently owned Pi adapter\n"

    assert not skill.is_symlink()
    assert not manifest_path.exists()


def test_claude_install_is_idempotent_and_uninstall_preserves_unrelated_hooks(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    original = {
        "custom": {"keep": True},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/other/stop"}]}]},
    }
    settings_path.write_text(json.dumps(original))

    for _ in range(2):
        installed = _run_installer(tmp_path, "--target", "claude")
        assert installed.returncode == 0, installed.stderr or installed.stdout
    settings = json.loads(settings_path.read_text())
    for event in ("SessionStart", "Stop", "SessionEnd"):
        ours = [entry for entry in settings["hooks"][event]
                if any(str(REPO_ROOT / "hooks") in h.get("command", "") for h in entry["hooks"])]
        assert len(ours) == 1
    skill = claude_dir / "skills" / "session-search"
    assert skill.is_symlink()
    assert skill.resolve() == REPO_ROOT / "skills" / "session-search"

    removed = _run_installer(tmp_path, "--uninstall", "--target", "claude")
    assert removed.returncode == 0, removed.stderr or removed.stdout
    assert not skill.is_symlink()
    assert not (claude_dir / "session-index" / ".manifest.json").exists()
    assert json.loads(settings_path.read_text()) == original


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
    assert document["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    for event in ("Stop", "Interrupt", "SessionEnd", "SessionStart", "SubagentStop"):
        handlers = [h for g in document["hooks"][event] for h in g.get("hooks", [])
                    if "codex_stop.py" in h.get("command", "")]
        assert len(handlers) == 1
        assert handlers[0]["timeout"] <= (3 if event in {"Interrupt", "SessionEnd"} else 5)

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
