"""Structured logger with monthly rotation.

Logs to ~/.session-index/logs/session-index.log
Format: HH:MM:SS.mmm [sid_6] hook_name  | message
Silent on all failures.
"""

import datetime
import os

DATA_DIR = os.path.expanduser("~/.session-index")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "session-index.log")
_MONTH_MARKER = os.path.join(LOG_DIR, ".last-month")
LOG_PREV_FILE = os.path.join(LOG_DIR, "session-index.prev.log")


def _rotate_on_new_month() -> None:
    """Archive current log on month change. Keeps current + previous month."""
    month = datetime.date.today().strftime("%Y-%m")
    try:
        last = open(_MONTH_MARKER).read().strip()
    except OSError:
        last = ""
    if last != month:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if os.path.exists(LOG_FILE):
                os.replace(LOG_FILE, LOG_PREV_FILE)
            with open(LOG_FILE, "w") as f:
                f.write(f"=== {month} ===\n")
            with open(_MONTH_MARKER, "w") as f:
                f.write(month)
        except OSError:
            pass


def log(session_id: str, hook: str, message: str) -> None:
    """Append a timestamped log line. Silent on failure."""
    try:
        _rotate_on_new_month()
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        sid = (session_id or "")[-6:] or "??????"
        with open(LOG_FILE, "a") as f:
            f.write(f"{ts} [{sid}] {hook:<18} | {message}\n")
    except Exception:
        pass
