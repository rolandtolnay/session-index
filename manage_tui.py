"""Full-screen, dependency-free terminal UI for managing indexed sessions."""

from __future__ import annotations

import curses
from datetime import datetime
import locale
import os
import sqlite3
import textwrap
import unicodedata

from db import list_manage_sessions, set_hidden_from_recents


def plain(value: str | None) -> str:
    """Indexed text is data, never terminal escape sequences or markup."""
    return "".join(c for c in " ".join((value or "").split()) if c.isprintable())


def clipped(text: str, width: int) -> str:
    """Clip by terminal cells, not bytes or Unicode code-point count."""
    result = []
    used = 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrapped(text: str, width: int) -> list[str]:
    lines = []
    for line in textwrap.wrap(text, max(1, width)):
        while line:
            part = clipped(line, width)
            if not part:
                break
            lines.append(part)
            line = line[len(part):].lstrip()
    return lines


def display_date(value: str | None) -> str:
    try:
        date = datetime.fromisoformat(value or "").astimezone()
    except ValueError:
        return "Unknown date"
    days = (datetime.now().astimezone().date() - date.date()).days
    label = "Today" if days == 0 else "Yesterday" if days == 1 else date.strftime("%b %d, %Y")
    return f"{label} · {date:%H:%M}"


class SessionManager:
    def __init__(self, conn, delete_session):
        self.conn = conn
        self.delete_session = delete_session
        self.hidden_only = False
        self.offset = 0
        self.selected = 0
        self.sessions = []
        self.preview_scroll = 0
        self.preview_max = 0
        self.preview_height = 1
        self.status = "Hidden sessions remain searchable. Changes apply to future recent context."
        self.status_error = False
        self.delete_target = None
        self.confirmation = ""
        self.confirmation_error = False
        self.styles = {}
        self.reload()

    def reload(self):
        self.sessions = list_manage_sessions(self.conn, hidden_only=self.hidden_only, offset=self.offset)
        while not self.sessions and self.offset:
            self.offset = max(0, self.offset - 20)
            self.sessions = list_manage_sessions(self.conn, hidden_only=self.hidden_only, offset=self.offset)
        self.selected = min(self.selected, max(0, len(self.sessions) - 1))
        self.preview_scroll = 0

    def message(self, text, *, error=False):
        self.status, self.status_error = text, error

    def handle_key(self, key) -> bool:
        """Handle one terminal event; False exits without another redraw."""
        if self.delete_target is not None:
            if key in ("\x1b", "\x03"):
                self.delete_target = None
                self.message("Deletion cancelled. Nothing changed.")
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.confirmation = self.confirmation[:-1]
                self.confirmation_error = False
            elif key == "\x15":
                self.confirmation = ""
                self.confirmation_error = False
            elif key in ("\n", "\r", curses.KEY_ENTER):
                sid = self.delete_target["session_id"]
                if self.confirmation != sid:
                    self.confirmation_error = True
                else:
                    try:
                        result = self.delete_session(self.conn, sid)
                        kept = len(result["skipped_artifacts"])
                        self.message(f"Deleted {sid}." + (f" Kept {kept} shared/out-of-store artifact(s)." if kept else " Raw transcript preserved."))
                    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                        self.message(str(error), error=True)
                    self.delete_target = None
                    self.reload()
            elif isinstance(key, str) and key.isprintable() and len(self.confirmation) < 100:
                self.confirmation += key
                self.confirmation_error = False
            return True

        if key in ("q", "\x1b", "\x03"):
            return False
        if key in ("\t", "a", "f"):
            self.hidden_only = not self.hidden_only if key == "\t" else key == "f"
            self.offset, self.selected = 0, 0
            self.reload()
        elif key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
            delta = 1 if key in (curses.KEY_DOWN, "j") else -1
            self.selected = max(0, min(len(self.sessions) - 1, self.selected + delta))
            self.preview_scroll = 0
        elif key in ("n", curses.KEY_RIGHT):
            if list_manage_sessions(self.conn, hidden_only=self.hidden_only, offset=self.offset + 20):
                self.offset += 20
                self.selected = 0
                self.reload()
            else:
                self.message("You are on the last page.")
        elif key in ("p", curses.KEY_LEFT):
            self.offset = max(0, self.offset - 20)
            self.selected = 0
            self.reload()
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            step = max(1, self.preview_height - 1)
            delta = step if key == curses.KEY_NPAGE else -step
            self.preview_scroll = max(0, min(self.preview_max, self.preview_scroll + delta))
        elif key == "r":
            self.reload()
            self.message("Session list refreshed.")
        elif self.sessions and key in ("h", "u"):
            session = self.sessions[self.selected]
            hide = not session["hidden_from_recents"] if key == "h" else False
            try:
                set_hidden_from_recents(self.conn, session["session_id"], hide)
                self.message("Hidden from recents; still searchable." if hide else "Restored to recent context.")
                self.reload()
            except (ValueError, sqlite3.Error) as error:
                self.message(str(error), error=True)
        elif self.sessions and key == "d":
            self.delete_target = self.sessions[self.selected]
            self.confirmation = ""
            self.confirmation_error = False
        return True

    def configure_colors(self):
        self.styles = {"accent": curses.A_BOLD, "muted": curses.A_DIM,
                       "selected": curses.A_REVERSE | curses.A_BOLD,
                       "warning": curses.A_BOLD, "error": curses.A_BOLD}
        if not curses.has_colors() or "NO_COLOR" in os.environ:
            return
        curses.start_color()
        curses.use_default_colors()
        for number, (name, foreground, background) in enumerate([
            ("accent", curses.COLOR_CYAN, -1), ("muted", curses.COLOR_WHITE, -1),
            ("selected", curses.COLOR_WHITE, curses.COLOR_BLUE),
            ("warning", curses.COLOR_YELLOW, -1), ("error", curses.COLOR_RED, -1),
        ], 1):
            curses.init_pair(number, foreground, background)
            self.styles[name] = curses.color_pair(number)
        self.styles["accent"] |= curses.A_BOLD
        self.styles["muted"] |= curses.A_DIM
        self.styles["selected"] |= curses.A_BOLD

    def put(self, screen, y, x, text, style=0, width=None):
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        available = max(0, columns - x - 1)
        if width is not None:
            available = min(available, max(0, width))
        text = "".join(char if char.isprintable() else " " for char in text)
        if clipped(text, available) != text:
            text = clipped(text, available - 1) + "…" if available else ""
        try:
            screen.addstr(y, x, text, style)
        except curses.error:
            # A resize can invalidate coordinates between getmaxyx and addstr.
            pass

    def rule(self, screen, y, x, width, style, *, corners=""):
        """ASCII strokes avoid macOS curses' broken Unicode REP optimization."""
        height, columns = screen.getmaxyx()
        width = min(width, columns - x - 1)
        if not 0 <= y < height or x < 0 or width < 2:
            return
        inset = 1 if corners else 0
        try:
            screen.hline(y, x + inset, ord("-"), width - 2 * inset, style)
        except curses.error:
            pass  # A resize may invalidate the coordinates.
        if corners:
            self.put(screen, y, x, corners[0], style)
            self.put(screen, y, x + width - 1, corners[1], style)

    def draw_list(self, screen, y, x, height, width):
        self.put(screen, y, x, "SESSIONS", self.styles["muted"], width)
        if not self.sessions:
            self.put(screen, y + 2, x, "No hidden sessions." if self.hidden_only else "No indexed sessions.", width=width)
            return
        row_height = 3 if height >= 20 else 2
        capacity = max(1, (height - 2) // row_height)
        start = max(0, min(self.selected - capacity + 1, len(self.sessions) - capacity))
        for position, session in enumerate(self.sessions[start:start + capacity], start):
            line = y + 2 + (position - start) * row_height
            active = position == self.selected
            style = self.styles["selected"] if active else 0
            if active:
                for dy in range(2):
                    try:
                        screen.hline(line + dy, x, " ", width, style)
                    except curses.error:
                        pass
            title = plain(session.get("headline") or session.get("summary") or session.get("user_messages") or "Untitled session")
            marker = "›" if active else " "
            badge = "[hidden] " if session["hidden_from_recents"] else ""
            self.put(screen, line, x, f"{marker} {self.offset + position + 1:02}  {badge}{title}", style, width)
            project_width = max(8, min(20, width - 8 - len(session["session_id"])))
            project = clipped(plain(session.get("project") or "Unknown project"), project_width)
            metadata = f"{project} · {session['session_id']}"
            self.put(screen, line + 1, x + 5, metadata, style if active else self.styles["muted"], width - 5)
        self.put(screen, y + height, x, f"{self.selected + 1 if self.sessions else 0}/{len(self.sessions)} on page · ↑↓ browse", self.styles["muted"], width)

    def draw_preview(self, screen, y, x, height, width):
        self.put(screen, y, x, "PREVIEW", self.styles["accent"], width)
        if not self.sessions:
            self.put(screen, y + 2, x, "Tab returns to all sessions." if self.hidden_only else "Index a conversation to get started.", width=width)
            return
        session = self.sessions[self.selected]
        title = plain(session.get("headline") or "Session details")
        visibility = "HIDDEN FROM RECENTS · still searchable" if session["hidden_from_recents"] else "VISIBLE IN RECENTS"
        # Keep metadata fixed while only the summary scrolls.
        self.put(screen, y + 2, x, session.get("project") or "Unknown project", curses.A_BOLD, width)
        self.put(screen, y + 3, x, display_date(session.get("started_at")), self.styles["muted"], width)
        self.put(screen, y + 4, x, session["session_id"], self.styles["accent"], width)
        self.put(screen, y + 5, x, visibility, self.styles["warning"] if session["hidden_from_recents"] else self.styles["muted"], width)
        title_lines = wrapped(title, width)
        summary = plain(session.get("summary") or session.get("user_messages") or "No preview available yet.")
        lines = [(line, curses.A_BOLD) for line in title_lines] + [("", 0)]
        lines += [(line, 0) for line in wrapped(summary, width)]
        count = max(1, height - 8)
        self.preview_height = count
        self.preview_max = max(0, len(lines) - count)
        self.preview_scroll = min(self.preview_scroll, self.preview_max)
        for i, (line, style) in enumerate(lines[self.preview_scroll:self.preview_scroll + count]):
            self.put(screen, y + 7 + i, x, line, style, width)
        if self.preview_max:
            self.put(screen, y + height, x, "PgUp/PgDn scroll preview", self.styles["muted"], width)

    def draw_confirmation(self, screen):
        # Replace the browser so background rows and inactive shortcuts cannot
        # compete with this destructive decision, especially on narrow screens.
        screen.erase()
        height, columns = screen.getmaxyx()
        self.put(screen, 1, 2, "SESSION INDEX", self.styles["accent"])
        self.put(screen, 1, 19, " /  deletion confirmation", self.styles["muted"])
        width = min(76, columns - 6)
        x, y = (columns - width) // 2, max(3, (height - 17) // 2)
        self.rule(screen, y, x, width, self.styles["error"], corners="╭╮")
        for line in range(y + 1, y + 16):
            self.put(screen, line, x, "│", self.styles["error"])
            self.put(screen, line, x + width - 1, "│", self.styles["error"])
        self.put(screen, y + 1, x + 2, "DELETE SESSION?", self.styles["error"] | curses.A_BOLD, width - 4)
        session = self.delete_target
        self.put(screen, y + 3, x + 2, session.get("headline") or session.get("project") or "Selected session", curses.A_BOLD, width - 4)
        self.put(screen, y + 4, x + 2, session["session_id"], self.styles["accent"], width - 4)
        warning = "Removes indexed data and owned generated files. No undo. Raw transcripts remain; indexing can recreate this session."
        for i, line in enumerate(textwrap.wrap(warning, width - 4)):
            self.put(screen, y + 6 + i, x + 2, line, width=width - 4)
        self.put(screen, y + 10, x + 2, "Type the full session ID to confirm:", width=width - 4)
        self.put(screen, y + 11, x + 2, "> " + self.confirmation + "▏", self.styles["accent"], width - 4)
        if self.confirmation_error:
            self.put(screen, y + 13, x + 2, "ID does not match. Nothing has been deleted.", self.styles["error"], width - 4)
        self.put(screen, y + 15, x + 2, "Enter confirm   Esc cancel   Ctrl+U clear", self.styles["muted"], width - 4)
        self.rule(screen, y + 16, x, width, self.styles["error"], corners="╰╯")

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 24 or width < 60:
            self.put(screen, 1, 2, "Session Index · enlarge terminal to 60 × 24", curses.A_BOLD)
            self.put(screen, 3, 2, "Esc cancels deletion; q quits when not confirming.")
            screen.refresh()
            return
        self.put(screen, 1, 2, "SESSION INDEX", self.styles["accent"])
        self.put(screen, 1, 19, " /  session manager", self.styles["muted"])
        view = "HIDDEN" if self.hidden_only else "ALL SESSIONS"
        self.put(screen, 3, 2, f"{view}   ·   Page {self.offset // 20 + 1}   ·   Newest first", curses.A_BOLD)
        self.rule(screen, 4, 2, width - 4, self.styles["muted"])
        if width >= 112:
            split = width // 2
            for y in range(5, height - 4):
                self.put(screen, y, split, "│", self.styles["muted"])
            self.draw_list(screen, 6, 2, height - 12, split - 4)
            self.draw_preview(screen, 6, split + 3, height - 12, width - split - 5)
        else:
            list_height = max(7, (height - 12) // 2)
            self.draw_list(screen, 6, 2, list_height, width - 4)
            preview_y = 8 + list_height
            # Compact stacked preview reserves more space for the actual summary.
            if self.sessions:
                session = self.sessions[self.selected]
                self.put(screen, preview_y, 2, display_date(session.get("started_at")) + " · " + session["session_id"], self.styles["accent"], width - 4)
                summary = plain(session.get("summary") or session.get("user_messages") or "No preview available yet.")
                lines = wrapped(summary, width - 4)
                count = max(1, height - preview_y - 7)
                self.preview_height = count
                self.preview_max = max(0, len(lines) - count)
                self.preview_scroll = min(self.preview_scroll, self.preview_max)
                for i, line in enumerate(lines[self.preview_scroll:self.preview_scroll + count]):
                    self.put(screen, preview_y + 2 + i, 2, line, width=width - 4)
                if self.preview_max:
                    self.put(screen, height - 5, 2, "PgUp/PgDn scroll preview", self.styles["muted"], width - 4)
        self.rule(screen, height - 4, 2, width - 4, self.styles["muted"])
        self.put(screen, height - 3, 2, self.status, self.styles["error"] if self.status_error else self.styles["muted"], width - 4)
        visibility_key = "unhide" if self.sessions and self.sessions[self.selected]["hidden_from_recents"] else "hide"
        keys = f"↑↓ select  Tab view  h {visibility_key}  d delete  ←→ page  q quit"
        self.put(screen, height - 2, 2, keys, self.styles["accent"], width - 4)
        if self.delete_target is not None:
            self.draw_confirmation(screen)
        screen.refresh()

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        curses.set_escdelay(25)
        screen.keypad(True)
        self.configure_colors()
        while True:
            self.draw(screen)
            try:
                key = screen.get_wch()
            except KeyboardInterrupt:
                key = "\x03"
            height, width = screen.getmaxyx()
            if (height < 24 or width < 60) and key not in ("q", "\x1b", "\x03", curses.KEY_RESIZE):
                continue
            if not self.handle_key(key):
                return


def run_manager(conn, delete_session):
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(SessionManager(conn, delete_session).run)
