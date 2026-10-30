"""Read-only artifact access and the Session Manager's macOS clipboard action."""

from pathlib import Path
import subprocess
import sys


def _absolute_path(path: str) -> Path:
    if not path or "\x00" in path or not Path(path).is_absolute():
        raise ValueError("Transcript path is missing or is not absolute.")
    return Path(path)


def read_transcript(path: str) -> str:
    """Load readable text without allowing indexed content to control the terminal."""
    artifact = _absolute_path(path)
    try:
        text = artifact.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise ValueError("Transcript is not valid UTF-8 text.") from error
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


def copy_transcript_path(path: str) -> None:
    """Copy the recorded absolute path verbatim; never invoke a shell."""
    _absolute_path(path)
    if sys.platform != "darwin":
        raise OSError("Clipboard copying is supported on macOS only.")
    try:
        result = subprocess.run(
            ["pbcopy"], input=path.encode("utf-8"), capture_output=True, timeout=3,
        )
    except FileNotFoundError as error:
        raise OSError("Clipboard unavailable: pbcopy was not found.") from error
    except subprocess.TimeoutExpired as error:
        raise OSError("Clipboard copy timed out. Try again.") from error
    if result.returncode:
        raise OSError("Clipboard copy failed. Check that the macOS clipboard is available.")
