"""Full-screen, dependency-free terminal UI for managing indexed sessions."""

from __future__ import annotations

import curses
from dataclasses import replace
from datetime import date, datetime, timedelta
import locale
import os
import sqlite3
import textwrap
import unicodedata

from db import set_hidden_from_recents, TOP_LEVEL_SESSION_PREDICATE
from manage_query import ManageFilters, query_manage_sessions


SORT_LABELS = {
    "auto": "Automatic", "newest": "Newest first", "oldest": "Oldest first",
    "relevance": "Best match", "substance": "Substance first",
}
BAND_LABELS = {None: "Any substance", "substantial": "Substantial", "useful": "Useful",
               "unknown": "Unknown", "low_value": "Low-value"}
FILTER_CATEGORIES = ("project", "dates", "source", "visibility", "substance")
FILTER_LABELS = ("Project", "Date", "Provider", "Visibility", "More")
HELP_LINES = [
    "/          Search; Enter applies, Esc cancels",
    "f          Filter; type a project, Enter applies",
    "Tab        Next filter category (Shift+Tab back)",
    "Ctrl+R     Clear filters inside the picker",
    "s          Sort order; * marks the current choice",
    "c          Reset search, filters, and sort",
    "↑↓ / j k   Select a session",
    "←→ / p n   Previous / next page",
    "PgUp/PgDn  Scroll selected preview",
    "Tab        In the list: toggle all / hidden",
    "h          Hide / unhide selected session",
    "d          Delete (full session ID required)",
    "r          Refresh, keeping search and filters",
    "q / Esc    Quit (Esc first dismisses a panel)",
    "", "SEARCH",
    "Every word must match; partial words work.",
    "Typos use near matches only if exact matches fail.",
    "Search covers titles, summaries, user prompts,",
    "projects, files, and IDs—not raw or assistant text.",
    "", "FILTERS & SORTING",
    "Each filter choice applies immediately on Enter.",
    "Choose All / Any to clear just that category.",
    "Custom dates are inclusive local start dates.",
    "More contains Substance Band: reference value,",
    "not length. Unknown means not yet assessed.",
    "Automatic sort: newest browsing, best match searching.",
    "Substance sort: substantial, useful, unknown,",
    "then low-value; newest first within each band.",
]


def edit_input(text: str, cursor: int, key) -> tuple[str, int]:
    """Single-line editing shared by search, project lookup, and date inputs."""
    if key in (curses.KEY_LEFT, "\x02"):
        cursor = max(0, cursor - 1)
    elif key in (curses.KEY_RIGHT, "\x06"):
        cursor = min(len(text), cursor + 1)
    elif key in (curses.KEY_HOME, "\x01"):
        cursor = 0
    elif key in (curses.KEY_END, "\x05"):
        cursor = len(text)
    elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        text, cursor = text[:max(0, cursor - 1)] + text[cursor:], max(0, cursor - 1)
    elif key == curses.KEY_DC:
        text = text[:cursor] + text[cursor + 1:]
    elif key == "\x15":
        text, cursor = "", 0
    elif isinstance(key, str) and key.isprintable() and len(text) < 500:
        text, cursor = text[:cursor] + key + text[cursor:], cursor + len(key)
    return text, cursor


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
        self.filters = ManageFilters()
        self.sort = "auto"
        self.total = 0
        self.approximate = False
        self.panel = None
        self.panel_selected = 0
        self.help_scroll = 0
        self.input = ""
        self.cursor = 0
        self.panel_error = ""
        self.projects = []
        self.date_inputs = ["", ""]
        self.date_field = 0
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

    @property
    def hidden_only(self):
        return self.filters.visibility == "hidden"

    @property
    def sort_label(self):
        order = self.sort
        if order == "auto" or (order == "relevance" and not self.filters.query):
            order = "relevance" if self.filters.query else "newest"
        return SORT_LABELS[order]

    def reload(self):
        page = query_manage_sessions(self.conn, self.filters, sort=self.sort, offset=self.offset)
        if self.offset and not page.sessions:
            self.offset = max(0, (page.total - 1) // 20 * 20)
            page = query_manage_sessions(self.conn, self.filters, sort=self.sort, offset=self.offset)
        self.sessions, self.total, self.approximate = page.sessions, page.total, page.approximate
        self.selected = min(self.selected, max(0, len(self.sessions) - 1))
        self.preview_scroll = 0

    def reset_position(self):
        self.offset, self.selected = 0, 0
        self.reload()

    def open_panel(self, panel):
        self.panel, self.panel_selected, self.panel_error = panel, 0, ""
        if panel == "search":
            self.input = self.filters.query
            self.cursor = len(self.input)
        elif panel == "filters":
            self.input, self.cursor = "", 0
            self.projects = [row[0] for row in self.conn.execute(f"""
                SELECT DISTINCT COALESCE(project, '') FROM sessions
                WHERE {TOP_LEVEL_SESSION_PREDICATE}
                ORDER BY COALESCE(project, '') COLLATE NOCASE, COALESCE(project, '')
            """)]
            self.select_category("project")
        elif panel == "range":
            self.date_inputs = [self.filters.since, self.filters.until]
            self.date_field = 0
            self.cursor = len(self.date_inputs[0])
        elif panel == "sort":
            self.select_current_option()
        elif panel == "help":
            self.help_scroll = 0

    def date_presets(self):
        today = date.today()
        return {"any": ("", ""), "today": (today.isoformat(), today.isoformat()),
                **{str(days): ((today - timedelta(days=days - 1)).isoformat(), today.isoformat())
                   for days in (7, 30, 90)}}

    def current_option(self):
        if self.panel == "sort":
            return self.sort
        if self.panel == "dates":
            bounds = (self.filters.since, self.filters.until)
            return next((key for key, value in self.date_presets().items() if value == bounds), "custom")
        return getattr(self.filters, self.panel)

    def select_current_option(self):
        current = self.current_option()
        self.panel_selected = next((i for i, (key, _) in enumerate(self.panel_options()) if key == current), 0)

    def select_category(self, category):
        self.panel, self.panel_error = category, ""
        if category == "project":
            self.cursor = len(self.input)
        self.select_current_option()

    @staticmethod
    def date_label(filters):
        if filters.since and filters.until:
            return filters.since if filters.since == filters.until else f"{filters.since} – {filters.until}"
        if filters.since:
            return f"From {filters.since}"
        if filters.until:
            return f"Through {filters.until}"
        return "Any date"

    def panel_options(self):
        if self.panel == "project":
            options = [(p, p or "Unknown project") for p in self.projects
                       if self.input.casefold() in (p or "Unknown project").casefold()]
            return options if self.input else [(None, "All projects")] + options
        if self.panel == "dates":
            return [("any", "Any date"), ("today", "Today"), ("7", "Past 7 days"),
                    ("30", "Past 30 days"), ("90", "Past 90 days"), ("custom", "Custom range…")]
        if self.panel == "source":
            return [(None, "All providers"), ("claude", "Claude"), ("pi", "Pi"), ("codex", "Codex"), ("", "Unknown")]
        if self.panel == "visibility":
            return [("all", "All sessions"), ("visible", "Visible in recents"), ("hidden", "Hidden from recents")]
        if self.panel == "substance":
            return list(BAND_LABELS.items())
        if self.panel == "sort":
            return [(key, label) for key, label in SORT_LABELS.items() if key != "relevance" or self.filters.query]
        return []

    def apply_filter(self, **changes):
        self.filters = replace(self.filters, **changes)
        self.panel = None
        self.reset_position()
        self.message("Filter applied. f adds or changes a filter · c resets the view.")

    def handle_panel_key(self, key):
        if key in ("\x1b", "\x03"):
            if self.panel == "range":
                self.select_category("dates")
            else:
                self.panel = None
            return
        enter = key in ("\n", "\r", curses.KEY_ENTER)
        if self.panel == "help":
            if key in ("?", "q") or enter:
                self.panel = None
            elif key in (curses.KEY_DOWN, "j", curses.KEY_NPAGE):
                self.help_scroll = min(len(HELP_LINES) - 1, self.help_scroll + (8 if key == curses.KEY_NPAGE else 1))
            elif key in (curses.KEY_UP, "k", curses.KEY_PPAGE):
                self.help_scroll = max(0, self.help_scroll - (8 if key == curses.KEY_PPAGE else 1))
            return
        if self.panel in FILTER_CATEGORIES:
            if key in ("\t", curses.KEY_BTAB):
                delta = 1 if key == "\t" else -1
                self.select_category(FILTER_CATEGORIES[(FILTER_CATEGORIES.index(self.panel) + delta) % len(FILTER_CATEGORIES)])
                return
            if key == "\x12":
                self.filters = ManageFilters(query=self.filters.query)
                self.panel = None
                self.reset_position()
                self.message("Filters cleared. Search and sort kept.")
                return
        if self.panel == "search":
            if enter:
                self.filters = replace(self.filters, query=self.input.strip())
                self.panel = None
                self.reset_position()
                self.message("Near matches shown; no exact matches in this scope." if self.approximate else "Search updated. / edit · c reset all")
            else:
                self.input, self.cursor = edit_input(self.input, self.cursor, key)
            return
        if self.panel == "range":
            if enter and self.date_field == 1:
                try:
                    since, until = (value.strip() for value in self.date_inputs)
                    for value in (since, until):
                        if value and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                            raise ValueError()
                    if since and until and since > until:
                        raise ValueError()
                except ValueError:
                    self.panel_error = "Use YYYY-MM-DD; From must not be after Through."
                else:
                    self.apply_filter(since=since, until=until)
            elif enter or key in ("\t", curses.KEY_UP, curses.KEY_DOWN, curses.KEY_BTAB):
                self.date_field = 1 - self.date_field
                self.cursor = len(self.date_inputs[self.date_field])
            else:
                self.date_inputs[self.date_field], self.cursor = edit_input(self.date_inputs[self.date_field], self.cursor, key)
                self.panel_error = ""
            return
        options = self.panel_options()
        if key in (curses.KEY_UP, curses.KEY_DOWN, "\t", curses.KEY_BTAB) or (self.panel != "project" and key in ("j", "k")):
            delta = -1 if key in (curses.KEY_UP, curses.KEY_BTAB, "k") else 1
            if options:
                self.panel_selected = (self.panel_selected + delta) % len(options)
        elif enter and options:
            value = options[self.panel_selected][0]
            if self.panel == "sort":
                self.sort, self.panel = value, None
                self.reset_position()
                self.message("Sort updated.")
            elif self.panel == "dates":
                if value == "custom":
                    self.open_panel("range")
                else:
                    since, until = self.date_presets()[value]
                    self.apply_filter(since=since, until=until)
            else:
                self.apply_filter(**{self.panel: value})
        elif self.panel == "project":
            previous_input = self.input
            self.input, self.cursor = edit_input(self.input, self.cursor, key)
            if self.input != previous_input:
                self.panel_selected = 0

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

        if self.panel is not None:
            self.handle_panel_key(key)
            return True
        if key in ("q", "\x1b", "\x03"):
            return False
        if key in ("/", "f", "s", "?"):
            self.open_panel({"/": "search", "f": "filters", "s": "sort", "?": "help"}[key])
        elif key == "c":
            self.filters, self.sort = ManageFilters(), "auto"
            self.reset_position()
            self.message("Search, filters, and sort reset.")
        elif key in ("\t", "a"):
            self.filters = replace(self.filters, visibility="hidden" if key == "\t" and not self.hidden_only else "all")
            self.reset_position()
        elif key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
            delta = 1 if key in (curses.KEY_DOWN, "j") else -1
            self.selected = max(0, min(len(self.sessions) - 1, self.selected + delta))
            self.preview_scroll = 0
        elif key in ("n", curses.KEY_RIGHT):
            if self.offset + 20 < self.total:
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
            self.put(screen, y + 2, x, "No matching sessions.", curses.A_BOLD, width)
            self.put(screen, y + 4, x, "/ edit search · f filters · c reset", self.styles["muted"], width)
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
            short_date = display_date(session.get("started_at")).split(" · ")[0]
            provider = {"claude": "Claude", "pi": "Pi", "codex": "Codex"}.get(session.get("source"), "?")
            metadata = f"{project} · {short_date} · {provider}"
            self.put(screen, line + 1, x + 5, metadata, style if active else self.styles["muted"], width - 5)
        self.put(screen, y + height, x, f"{self.selected + 1 if self.sessions else 0}/{len(self.sessions)} on page · ↑↓ browse", self.styles["muted"], width)

    def draw_preview(self, screen, y, x, height, width):
        self.put(screen, y, x, "PREVIEW", self.styles["accent"], width)
        if not self.sessions:
            self.put(screen, y + 2, x, "Try fewer words or a wider date range.", width=width)
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
        if session.get("match_excerpt"):
            lines += [(line, self.styles["accent"]) for line in wrapped(plain(session["match_excerpt"]), width)] + [("", 0)]
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

    def draw_input(self, screen, y, x, text, cursor, width):
        # Keep the insertion point visible when editing long queries or paths.
        before = text[:cursor]
        while before and len(clipped(before, max(1, width - 3))) < len(before):
            before = before[1:]
        self.put(screen, y, x, before + "▏" + text[cursor:], self.styles["accent"], width)

    def draw_panel(self, screen):
        screen.erase()
        height, columns = screen.getmaxyx()
        width = min(84, columns - 6)
        x = (columns - width) // 2
        top = max(3, (height - 24) // 2)
        bottom = min(height - 4, top + 20)
        left, content_width = x + 2, width - 4
        filtering = self.panel in FILTER_CATEGORIES
        title = "Filter conversations" if filtering else {
            "search": "Search conversations", "sort": "Sort conversations",
            "range": "Filter · Custom dates", "help": "Keyboard & search guide",
        }[self.panel]
        self.put(screen, 1, 2, "SESSION INDEX", self.styles["accent"])
        self.rule(screen, top, x, width, self.styles["muted"], corners="╭╮")
        self.put(screen, top + 1, left, title, curses.A_BOLD, content_width)
        self.rule(screen, bottom, x, width, self.styles["muted"], corners="╰╯")
        hint, secondary_hint = "Enter apply · Esc cancel", ""

        if filtering:
            tab_x = left
            for category, label in zip(FILTER_CATEGORIES, FILTER_LABELS):
                active = self.panel == category
                # Brackets and the More separator retain hierarchy without color.
                tab = f"[{label}]" if active else label
                if category == "substance":
                    self.put(screen, top + 3, tab_x, "| ", self.styles["muted"])
                    tab_x += 2
                self.put(screen, top + 3, tab_x, tab,
                         self.styles["accent"] if active else self.styles["muted"])
                tab_x += len(tab) + 2
            hint = "Tab category · ↑↓ choose · Enter set · Esc cancel"
            secondary_hint = "Shift+Tab back · Ctrl+R clear filters"

        if self.panel == "search":
            self.put(screen, top + 3, left, "WORDS / FILE / SESSION ID", self.styles["muted"], content_width)
            self.draw_input(screen, top + 4, left, self.input, self.cursor, content_width)
            self.rule(screen, top + 5, left, content_width, self.styles["muted"])
            self.put(screen, top + 7, left, "All words · Partial matches · Typo fallback", self.styles["muted"], content_width)
            self.put(screen, top + 9, left, "WITHIN", self.styles["muted"], content_width)
            for i, line in enumerate(wrapped(self.scope_label(), content_width)[:3]):
                self.put(screen, top + 10 + i, left, line, width=content_width)
            hint = "Enter search · Esc cancel · Ctrl+U clear"
        elif self.panel == "range":
            self.put(screen, top + 3, left, "LOCAL START DATES · YYYY-MM-DD", self.styles["muted"], content_width)
            for i, label in enumerate(("FROM", "THROUGH · inclusive")):
                y = top + 5 + i * 4
                self.put(screen, y, left, label, self.styles["muted"], content_width)
                if i == self.date_field:
                    self.draw_input(screen, y + 1, left, self.date_inputs[i], self.cursor, content_width)
                else:
                    self.put(screen, y + 1, left, self.date_inputs[i] or "Any date", width=content_width)
                self.rule(screen, y + 2, left, content_width, self.styles["muted"])
            hint = "Tab field · Enter next/set · Esc back"
            secondary_hint = "Blank = unbounded · Ctrl+U clears this field"
        elif self.panel == "help":
            start_y = top + 3
            capacity = bottom - start_y - 1
            self.help_scroll = min(self.help_scroll, max(0, len(HELP_LINES) - capacity))
            for i, line in enumerate(HELP_LINES[self.help_scroll:self.help_scroll + capacity]):
                style = self.styles["accent"] if line.isupper() else 0
                self.put(screen, start_y + i, left, line, style, content_width)
            self.put(screen, bottom - 1, left, f"{self.help_scroll + 1}–{min(len(HELP_LINES), self.help_scroll + capacity)} / {len(HELP_LINES)}", self.styles["muted"], content_width)
            hint = "↑↓ / PgUp/PgDn scroll · Enter / Esc close"
        else:
            options = self.panel_options()
            if self.panel == "project":
                self.put(screen, top + 5, left, "NARROW PROJECTS", self.styles["muted"], content_width)
                self.draw_input(screen, top + 6, left, self.input, self.cursor, content_width)
                if not self.input:
                    self.put(screen, top + 6, left + 2, "Type a project name…", self.styles["muted"], content_width - 2)
                self.rule(screen, top + 7, left, content_width, self.styles["muted"])
                caption = "PROJECTS"
            else:
                value = self.date_label(self.filters) if self.panel == "dates" else next(
                    (label for key, label in options if key == self.current_option()), "")
                self.put(screen, top + 5, left, "CURRENT", self.styles["muted"], content_width)
                self.put(screen, top + 6, left, value, self.styles["accent"], content_width)
                caption = {"dates": "STARTED", "source": "PROVIDER", "visibility": "VISIBILITY",
                           "substance": "SUBSTANCE · REFERENCE VALUE", "sort": "ORDER"}[self.panel]
            self.put(screen, top + 8, left, caption, self.styles["muted"], content_width - 12)
            self.put(screen, top + 8, left + content_width - 9, "* current", self.styles["muted"], 9)
            start_y = top + 9
            capacity = max(1, bottom - start_y - 1)
            start = max(0, min(self.panel_selected - capacity + 1, len(options) - capacity))
            current = self.current_option()
            for i, (value, label) in enumerate(options[start:start + capacity], start):
                active = i == self.panel_selected
                style = self.styles["selected"] if active else 0
                y = start_y + i - start
                if active:
                    try:
                        screen.hline(y, left, " ", content_width, style)
                    except curses.error:
                        pass
                prefix = ("› " if active else "  ") + ("* " if value == current else "  ")
                self.put(screen, y, left, prefix + plain(label), style, content_width)
            if not options:
                self.put(screen, start_y, left, "No matching projects.", width=content_width)
                self.put(screen, start_y + 2, left, "Ctrl+U clears the name; Esc cancels.", self.styles["muted"], content_width)
            elif len(options) > capacity:
                self.put(screen, bottom - 1, left, f"{self.panel_selected + 1}/{len(options)} · ↑↓ scroll", self.styles["muted"], content_width)
        if self.panel_error:
            self.put(screen, bottom - 2, left, self.panel_error, self.styles["error"], content_width)
        self.put(screen, bottom + 1, x, hint, self.styles["accent"], width)
        self.put(screen, bottom + 2, x, secondary_hint, self.styles["muted"], width)

    def scope_label(self):
        filters = self.filters
        labels = []
        if filters.project is not None:
            labels.append(filters.project or "Unknown project")
        if filters.since or filters.until:
            labels.append(self.date_label(filters))
        if filters.source is not None:
            labels.append({"claude": "Claude", "pi": "Pi", "codex": "Codex", "": "Unknown provider"}.get(filters.source, filters.source))
        if filters.visibility != "all":
            labels.append(filters.visibility.capitalize())
        if filters.substance is not None:
            labels.append(BAND_LABELS[filters.substance])
        return " · ".join(labels) if labels else "All projects · Any date · All providers · All visibility"

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
        search = f"/ {self.filters.query}" if self.filters.query else "/ Search conversations"
        self.put(screen, 2, 2, search, self.styles["accent"], width - 4)
        self.put(screen, 3, 2, "f " + self.scope_label(), self.styles["muted"], width - 4)
        count = f"{self.total} {'near matches' if self.approximate else 'sessions'}"
        pages = max(1, (self.total + 19) // 20)
        self.put(screen, 4, 2, f"{count} · {self.offset // 20 + 1}/{pages} pages · {self.sort_label}", curses.A_BOLD, width - 4)
        self.rule(screen, 5, 2, width - 4, self.styles["muted"])
        if width >= 112:
            split = width // 2
            for y in range(6, height - 4):
                self.put(screen, y, split, "│", self.styles["muted"])
            self.draw_list(screen, 7, 2, height - 13, split - 4)
            self.draw_preview(screen, 7, split + 3, height - 13, width - split - 5)
        else:
            list_height = max(5, (height - 13) // 2)
            self.draw_list(screen, 7, 2, list_height, width - 4)
            preview_y = 9 + list_height
            # Compact stacked preview reserves more space for the actual summary.
            if self.sessions:
                session = self.sessions[self.selected]
                self.put(screen, preview_y, 2, display_date(session.get("started_at")) + " · " + session["session_id"], self.styles["accent"], width - 4)
                summary = plain(session.get("summary") or session.get("user_messages") or "No preview available yet.")
                excerpt = plain(session.get("match_excerpt"))
                lines = (wrapped(excerpt, width - 4) + [""] if excerpt else []) + wrapped(summary, width - 4)
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
        keys = "/ search  f filter  s sort  c reset  ? help  q quit" if width < 90 else f"/ search  f filter  s sort  c reset  h {visibility_key}  d delete  ? help  q quit"
        self.put(screen, height - 2, 2, keys, self.styles["accent"], width - 4)
        if self.delete_target is not None:
            self.draw_confirmation(screen)
        elif self.panel is not None:
            self.draw_panel(screen)
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
