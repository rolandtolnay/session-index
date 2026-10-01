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
PAGE_SIZE = 20
# Excerpt fields the preview already shows in full; repeating them is noise.
PREVIEWED_MATCH_FIELDS = ("Headline:", "Summary:", "Project:", "Session ID:")
PROVIDER_LABELS = {"claude": "Claude", "pi": "Pi", "codex": "Codex"}
# Pi's claude-code-dark theme (~/.pi/agent/themes/claude-code-dark.json):
# claude (accent), inactive (muted), subtle (border), userMessageBg (selection).
PALETTE = {"text": "#FFFFFF", "accent": "#D77757", "muted": "#999999", "border": "#505050",
           "selected_bg": "#373737", "state": "#FFC107", "danger": "#FF6B80"}
# (key, description) rows render as key hints; uppercase strings are captions.
HELP_LINES = [
    ("↑↓ j k", "Select a session, across pages"),
    ("←→ p n", "Jump to the previous / next page"),
    ("PgUp PgDn", "Scroll the preview"),
    ("/", "Search; Enter applies, Esc cancels"),
    ("f", "Filter; type a project, Enter applies"),
    ("s", "Sort order"),
    ("c", "Reset search, filters, and sort"),
    ("Tab", "Toggle all / hidden-only sessions"),
    ("h", "Hide / unhide selected session"),
    ("d", "Delete selected session; y confirms"),
    ("r", "Refresh, keeping search and filters"),
    ("q Esc", "Quit (Esc first dismisses a panel)"),
    "", "IN PICKERS",
    ("Tab", "Next filter category (Shift+Tab back)"),
    ("Ctrl+R", "Clear all filters, keep search and sort"),
    ("Ctrl+U", "Clear the text input"),
    "", "SEARCH",
    "Every word must match; partial words work.",
    "Typos use near matches only if exact matches fail.",
    "Search covers titles, summaries, user prompts,",
    "projects, files, IDs, and Side Chat conversations.",
    "Main assistant text and raw logs are not searched.",
    "", "FILTERS & SORTING",
    "Each filter choice applies immediately on Enter.",
    "Choose All / Any to clear just that category.",
    "Custom dates are inclusive local start dates.",
    "More contains Substance Band: reference value,",
    "not length. Unknown means not yet assessed.",
    "Automatic sort: newest browsing, best match searching.",
    "Substance sort: substantial, useful, unknown,",
    "then low-value; newest first within each band.",
    "", "HIDE & DELETE",
    "Hidden sessions stay searchable; hiding only keeps",
    "them out of future recent context.",
    "Delete removes indexed data and generated files.",
    "Raw transcripts remain, so re-indexing can restore it.",
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


def cell_width(char: str) -> int:
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1


def cells(text: str) -> int:
    return sum(cell_width(char) for char in text)


def clipped(text: str, width: int) -> str:
    """Clip by terminal cells, not bytes or Unicode code-point count."""
    result = []
    used = 0
    for char in text:
        size = cell_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def ellipsized(text: str, width: int) -> str:
    """Never let a clipped value read as complete, e.g. a worktree project name."""
    if cells(text) <= width:
        return text
    return clipped(text, width - 1) + "…" if width > 0 else ""


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


def xterm256(hex_color: str) -> int:
    """Nearest xterm-256 index; redefining terminal colors would leak past exit."""
    target = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    levels = (0, 95, 135, 175, 215, 255)
    candidates = {16 + 36 * r + 6 * g + b: (levels[r], levels[g], levels[b])
                  for r in range(6) for g in range(6) for b in range(6)}
    candidates.update({232 + i: (8 + 10 * i,) * 3 for i in range(24)})
    return min(candidates, key=lambda index: sum((a - b) ** 2 for a, b in zip(candidates[index], target)))


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
        self.status = ""
        self.status_error = False
        self.delete_target = None
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

    @property
    def customized(self):
        return self.filters != ManageFilters() or self.sort != "auto"

    @property
    def session(self):
        return self.sessions[self.selected] if self.sessions else None

    def session_actions(self):
        """Keys acting on the selected session; add new per-session actions here."""
        if self.session is None:
            return []
        return [("h", "unhide" if self.session["hidden_from_recents"] else "hide"), ("d", "delete")]

    def reload(self):
        page = query_manage_sessions(self.conn, self.filters, sort=self.sort, offset=self.offset)
        if self.offset and not page.sessions:
            self.offset = max(0, (page.total - 1) // PAGE_SIZE * PAGE_SIZE)
            page = query_manage_sessions(self.conn, self.filters, sort=self.sort, offset=self.offset)
        self.sessions, self.total, self.approximate = page.sessions, page.total, page.approximate
        self.selected = min(self.selected, max(0, len(self.sessions) - 1))
        self.preview_scroll = 0

    def reset_position(self):
        self.offset, self.selected = 0, 0
        self.reload()

    def move_selection(self, delta):
        """Pages are a query detail; browsing flows across their boundaries."""
        target = self.selected + delta
        if 0 <= target < len(self.sessions):
            self.selected = target
        elif target >= len(self.sessions) and self.offset + PAGE_SIZE < self.total:
            self.offset += PAGE_SIZE
            self.selected = 0
            self.reload()
        elif target < 0 and self.offset:
            self.offset = max(0, self.offset - PAGE_SIZE)
            self.selected = PAGE_SIZE - 1
            self.reload()
        self.preview_scroll = 0

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

    def date_label(self, filters):
        bounds = (filters.since, filters.until)
        preset = next((key for key, value in self.date_presets().items() if value == bounds), None)
        if preset:
            return dict(self.panel_options_for("dates"))[preset]
        if filters.since and filters.until:
            return filters.since if filters.since == filters.until else f"{filters.since} – {filters.until}"
        if filters.since:
            return f"From {filters.since}"
        if filters.until:
            return f"Through {filters.until}"
        return "Any date"

    def panel_options(self):
        return self.panel_options_for(self.panel)

    def panel_options_for(self, panel):
        if panel == "project":
            options = [(p, p or "Unknown project") for p in self.projects
                       if self.input.casefold() in (p or "Unknown project").casefold()]
            return options if self.input else [(None, "All projects")] + options
        if panel == "dates":
            return [("any", "Any date"), ("today", "Today"), ("7", "Past 7 days"),
                    ("30", "Past 30 days"), ("90", "Past 90 days"), ("custom", "Custom range…")]
        if panel == "source":
            return [(None, "All providers"), ("claude", "Claude"), ("pi", "Pi"), ("codex", "Codex"), ("", "Unknown")]
        if panel == "visibility":
            return [("all", "All sessions"), ("visible", "Visible in recents"), ("hidden", "Hidden from recents")]
        if panel == "substance":
            return list(BAND_LABELS.items())
        if panel == "sort":
            return [(key, label) for key, label in SORT_LABELS.items() if key != "relevance" or self.filters.query]
        return []

    def apply_filter(self, **changes):
        self.filters = replace(self.filters, **changes)
        self.panel = None
        self.reset_position()
        self.message("")

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
                self.message("")
                return
        if self.panel == "search":
            if enter:
                self.filters = replace(self.filters, query=self.input.strip())
                self.panel = None
                self.reset_position()
                self.message("No exact matches in this scope; showing near matches." if self.approximate else "")
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
                self.message("")
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
            # Only an explicit y deletes; every other key is inert until Esc/n.
            if key in ("\x1b", "\x03", "n", "N"):
                self.delete_target = None
                self.message("Deletion cancelled. Nothing changed.")
            elif key in ("y", "Y"):
                sid = self.delete_target["session_id"]
                try:
                    result = self.delete_session(self.conn, sid)
                    kept = len(result["skipped_artifacts"])
                    self.message(f"Deleted {sid}." + (f" Kept {kept} shared/out-of-store artifact(s)." if kept else " Raw transcript preserved."))
                except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                    self.message(str(error), error=True)
                self.delete_target = None
                self.reload()
            return True

        if self.panel is not None:
            self.handle_panel_key(key)
            return True
        if key in ("q", "\x1b", "\x03"):
            return False
        if key != curses.KEY_RESIZE:
            self.message("")  # Status reports the previous action only.
        if key in ("/", "f", "s", "?"):
            self.open_panel({"/": "search", "f": "filters", "s": "sort", "?": "help"}[key])
        elif key == "c":
            self.filters, self.sort = ManageFilters(), "auto"
            self.reset_position()
        elif key in ("\t", "a"):
            self.filters = replace(self.filters, visibility="hidden" if key == "\t" and not self.hidden_only else "all")
            self.reset_position()
        elif key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
            self.move_selection(1 if key in (curses.KEY_DOWN, "j") else -1)
        elif key in ("n", curses.KEY_RIGHT):
            if self.offset + PAGE_SIZE < self.total:
                self.offset += PAGE_SIZE
                self.selected = 0
                self.reload()
            else:
                self.message("You are on the last page.")
        elif key in ("p", curses.KEY_LEFT):
            self.offset = max(0, self.offset - PAGE_SIZE)
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
        return True

    def configure_colors(self):
        """Semantic roles; draw code names a role, never a raw color or attribute.

        strong: primary content    muted: secondary metadata, captions, defaults
        accent: keys and focus     border: rules and frames
        selected: the focused row (selected_muted / selected_accent inside it)
        state: non-default session state (hidden)    danger: destructive or failed
        """
        self.styles = {"strong": curses.A_BOLD, "muted": curses.A_DIM, "accent": curses.A_BOLD,
                       "border": curses.A_DIM, "selected": curses.A_REVERSE | curses.A_BOLD,
                       "selected_muted": curses.A_REVERSE, "selected_accent": curses.A_REVERSE | curses.A_BOLD,
                       "state": curses.A_BOLD, "danger": curses.A_BOLD}
        if not curses.has_colors() or "NO_COLOR" in os.environ:
            return
        curses.start_color()
        curses.use_default_colors()
        if curses.COLORS >= 256:
            color = {name: xterm256(value) for name, value in PALETTE.items()}
        else:  # Nearest basic colors; 8-color terminals cannot show the Pi palette.
            color = {"text": curses.COLOR_WHITE, "accent": curses.COLOR_RED, "muted": curses.COLOR_WHITE,
                     "border": curses.COLOR_WHITE, "selected_bg": curses.COLOR_BLACK,
                     "state": curses.COLOR_YELLOW, "danger": curses.COLOR_RED}
        for number, (name, foreground, background, attribute) in enumerate([
            ("strong", "text", None, curses.A_BOLD), ("accent", "accent", None, curses.A_BOLD),
            ("muted", "muted", None, 0), ("border", "border", None, 0),
            ("selected", "text", "selected_bg", curses.A_BOLD),
            ("selected_muted", "muted", "selected_bg", 0),
            ("selected_accent", "accent", "selected_bg", curses.A_BOLD),
            ("state", "state", None, 0), ("danger", "danger", None, 0),
        ], 1):
            curses.init_pair(number, color[foreground], color[background] if background else -1)
            self.styles[name] = curses.color_pair(number) | attribute
        if curses.COLORS < 256:
            self.styles["muted"] |= curses.A_DIM
            self.styles["border"] |= curses.A_DIM

    def put(self, screen, y, x, text, style=0, width=None):
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        available = max(0, columns - x - 1)
        if width is not None:
            available = min(available, max(0, width))
        text = "".join(char if char.isprintable() else " " for char in text)
        text = ellipsized(text, available)
        try:
            screen.addstr(y, x, text, style)
        except curses.error:
            # A resize can invalidate coordinates between getmaxyx and addstr.
            pass

    def put_spans(self, screen, y, x, spans, width):
        """Draw (text, style) runs left to right, clipping the whole line to width."""
        end = x + width
        for text, style in spans:
            if x >= end:
                break
            self.put(screen, y, x, text, style, end - x)
            x += cells(text)
        return x

    def draw_keys(self, screen, y, x, keys, width):
        """The one key-hint format: accent key, muted label, two-space gaps."""
        spans = []
        for item in keys:
            key, label, *style = item
            spans += [(key, style[0] if style else self.styles["accent"]),
                      (" " + label + "  ", self.styles["muted"])]
        return self.put_spans(screen, y, x, spans, width)

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

    def fill(self, screen, y, x, width, style):
        try:
            screen.hline(y, x, " ", width, style)
        except curses.error:
            pass

    def pane_heading(self, screen, y, x, width, label, detail=""):
        self.put(screen, y, x, label, self.styles["muted"], width)
        if detail and cells(label) + cells(detail) + 2 <= width:
            self.put(screen, y, x + width - cells(detail), detail, self.styles["muted"])

    def position_label(self):
        noun = "near matches" if self.approximate else "sessions"
        if not self.sessions:
            return f"0 {noun}"
        return f"{self.offset + self.selected + 1:,} of {self.total:,}" + (" near matches" if self.approximate else "")

    def draw_list(self, screen, y, x, height, width):
        self.pane_heading(screen, y, x, width, "SESSIONS", self.position_label())
        if not self.sessions:
            self.put(screen, y + 2, x, "No matching sessions.", self.styles["strong"], width)
            self.draw_keys(screen, y + 4, x, [("/", "edit search"), ("f", "filters"), ("c", "reset")], width)
            return
        row_height = 3 if height >= 20 else 2
        capacity = max(1, (height - 2) // row_height)
        start = max(0, min(self.selected - capacity + 1, len(self.sessions) - capacity))
        for position, session in enumerate(self.sessions[start:start + capacity], start):
            line = y + 2 + (position - start) * row_height
            active = position == self.selected
            row = self.styles["selected"] if active else 0
            if active:
                for dy in range(2):
                    self.fill(screen, line + dy, x, width, row)
            title = plain(session.get("headline") or session.get("summary") or session.get("user_messages") or "Untitled session")
            marker = ("›", self.styles["selected_accent"]) if active else (" ", 0)
            self.put_spans(screen, line, x, [marker, (" " + title, row)], width)
            project = ellipsized(plain(session.get("project") or "Unknown project"), max(8, min(28, width // 3)))
            short_date = display_date(session.get("started_at")).split(" · ")[0]
            provider = PROVIDER_LABELS.get(session.get("source"), "?")
            meta = self.styles["selected_muted"] if active else self.styles["muted"]
            spans = [(f"{project} · {short_date} · {provider}", meta)]
            if session["hidden_from_recents"]:
                spans += [(" · ", meta), ("hidden", row if active else self.styles["state"])]
            self.put_spans(screen, line + 1, x + 2, spans, width - 2)

    def draw_preview(self, screen, y, x, height, width, *, compact=False):
        """Fixed identity block, then a scrollable body.

        Compact (stacked) mode drops the heading and what the list row already
        shows; its muted metadata line doubles as the pane's heading.
        """
        if not compact:
            self.pane_heading(screen, y, x, width, "PREVIEW")
            y, height = y + 2, height - 2
        session = self.session
        if session is None:
            self.put(screen, y, x, "Try fewer words or a wider date range.", width=width)
            return
        provider = PROVIDER_LABELS.get(session.get("source"), "Unknown provider")
        # Without a headline the summary leads, as it does in the list row.
        fixed = [] if compact else [(line, self.styles["strong"]) for line in wrapped(
            plain(session.get("headline")), width)[:3]]
        if not compact:
            fixed.append((plain(session.get("project") or "Unknown project"), self.styles["muted"]))
        fixed.append((f"{display_date(session.get('started_at'))} · {provider} · {session['session_id']}", self.styles["muted"]))
        if session["hidden_from_recents"]:
            fixed.append(("Hidden from recents · still searchable", self.styles["state"]))
        for i, (line, style) in enumerate(fixed):
            self.put(screen, y + i, x, line, style, width)

        body = []
        matches = [part for part in plain(session.get("match_excerpt")).split(" | ")
                   if part and not part.startswith(PREVIEWED_MATCH_FIELDS)]
        if matches:
            body += [("MATCHED IN", self.styles["muted"])]
            body += [(line, 0) for part in matches for line in wrapped(part, width)] + [("", 0)]
        summary = plain(session.get("summary") or session.get("user_messages") or "No preview available yet.")
        body += [(line, 0) for line in wrapped(summary, width)]
        if session.get("side_chats"):
            body += [("", 0), ("SIDE CHATS", self.styles["muted"])]
            for child in session["side_chats"]:
                marker = "matched · " if child.get("matched") else ""
                body += [(line, 0) for line in wrapped(plain(marker + child["headline"]), width)]
                body += [(line, self.styles["muted"]) for line in wrapped(plain(child["transcript_path"]), width)]
        top = y + 1 + len(fixed)
        count = max(1, y + height - top - 1)  # The last row is kept for the scroll hint.
        self.preview_height = count
        self.preview_max = max(0, len(body) - count)
        self.preview_scroll = min(self.preview_scroll, self.preview_max)
        for i, (line, style) in enumerate(body[self.preview_scroll:self.preview_scroll + count]):
            self.put(screen, top + i, x, line, style, width)
        if self.preview_max:
            where = f"{self.preview_scroll * 100 // self.preview_max}%"
            self.draw_keys(screen, y + height - 1, x, [("PgUp/PgDn", f"scroll · {where}")], width)

    def draw_brand(self, screen, context=""):
        self.put(screen, 1, 2, "SESSION INDEX", self.styles["strong"])
        if context:
            self.put(screen, 1, 16, "· " + context, self.styles["muted"])

    def draw_confirmation(self, screen):
        # Replace the browser so background rows and inactive shortcuts cannot
        # compete with this destructive decision, especially on narrow screens.
        screen.erase()
        height, columns = screen.getmaxyx()
        self.draw_brand(screen, "delete session")
        width = min(76, columns - 6)
        x, y = (columns - width) // 2, max(3, (height - 13) // 2)
        left, inner = x + 2, width - 4
        self.rule(screen, y, x, width, self.styles["danger"], corners="╭╮")
        for line in range(y + 1, y + 12):
            self.put(screen, line, x, "│", self.styles["danger"])
            self.put(screen, line, x + width - 1, "│", self.styles["danger"])
        session = self.delete_target
        self.put(screen, y + 1, left, "DELETE SESSION?", self.styles["danger"] | curses.A_BOLD, inner)
        self.put(screen, y + 3, left, plain(session.get("headline") or "Untitled session"), self.styles["strong"], inner)
        self.put(screen, y + 4, left, f"{plain(session.get('project') or 'Unknown project')} · {session['session_id']}", self.styles["muted"], inner)
        warning = "Removes indexed data and generated files for this session. No undo. Raw transcripts remain, so re-indexing can recreate it."
        for i, line in enumerate(wrapped(warning, inner)[:3]):
            self.put(screen, y + 6 + i, left, line, width=inner)
        self.draw_keys(screen, y + 10, left, [("y", "delete", self.styles["danger"]), ("Esc", "cancel")], inner)
        self.rule(screen, y + 12, x, width, self.styles["danger"], corners="╰╯")

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
        self.draw_brand(screen)
        self.rule(screen, top, x, width, self.styles["border"], corners="╭╮")
        self.put(screen, top + 1, left, title, self.styles["strong"], content_width)
        self.rule(screen, bottom, x, width, self.styles["border"], corners="╰╯")
        hint, secondary_hint = [("Enter", "apply"), ("Esc", "cancel")], []

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
            hint = [("Tab", "category"), ("↑↓", "choose"), ("Enter", "set"), ("Esc", "cancel")]
            secondary_hint = [("Shift+Tab", "back"), ("Ctrl+R", "clear filters")]

        if self.panel == "search":
            self.put(screen, top + 3, left, "WORDS / FILE / SESSION ID", self.styles["muted"], content_width)
            self.draw_input(screen, top + 4, left, self.input, self.cursor, content_width)
            self.rule(screen, top + 5, left, content_width, self.styles["border"])
            self.put(screen, top + 7, left, "All words · Partial matches · Typo fallback", self.styles["muted"], content_width)
            self.put(screen, top + 9, left, "WITHIN", self.styles["muted"], content_width)
            for i, line in enumerate(wrapped(self.scope_label() or "All sessions", content_width)[:3]):
                self.put(screen, top + 10 + i, left, line, width=content_width)
            hint = [("Enter", "search"), ("Esc", "cancel"), ("Ctrl+U", "clear")]
        elif self.panel == "range":
            self.put(screen, top + 3, left, "LOCAL START DATES · YYYY-MM-DD", self.styles["muted"], content_width)
            for i, label in enumerate(("FROM", "THROUGH · inclusive")):
                y = top + 5 + i * 4
                self.put(screen, y, left, label, self.styles["muted"], content_width)
                if i == self.date_field:
                    self.draw_input(screen, y + 1, left, self.date_inputs[i], self.cursor, content_width)
                else:
                    self.put(screen, y + 1, left, self.date_inputs[i] or "Any date", width=content_width)
                self.rule(screen, y + 2, left, content_width, self.styles["border"])
            hint = [("Tab", "field"), ("Enter", "next/set"), ("Esc", "back")]
            secondary_hint = [("Ctrl+U", "clear field"), ("blank", "unbounded")]
        elif self.panel == "help":
            start_y = top + 3
            capacity = bottom - start_y - 1
            self.help_scroll = min(self.help_scroll, max(0, len(HELP_LINES) - capacity))
            for i, line in enumerate(HELP_LINES[self.help_scroll:self.help_scroll + capacity]):
                if isinstance(line, tuple):
                    self.put(screen, start_y + i, left, line[0], self.styles["accent"], 11)
                    self.put(screen, start_y + i, left + 11, line[1], width=content_width - 11)
                else:
                    self.put(screen, start_y + i, left, line, self.styles["muted"] if line.isupper() else 0, content_width)
            self.put(screen, bottom - 1, left, f"{self.help_scroll + 1}–{min(len(HELP_LINES), self.help_scroll + capacity)} / {len(HELP_LINES)}", self.styles["muted"], content_width)
            hint = [("↑↓ PgUp PgDn", "scroll"), ("Enter/Esc", "close")]
        else:
            options = self.panel_options()
            if self.panel == "project":
                self.put(screen, top + 5, left, "NARROW PROJECTS", self.styles["muted"], content_width)
                self.draw_input(screen, top + 6, left, self.input, self.cursor, content_width)
                if not self.input:
                    self.put(screen, top + 6, left + 2, "Type a project name…", self.styles["muted"], content_width - 2)
                self.rule(screen, top + 7, left, content_width, self.styles["border"])
                caption = "PROJECTS"
            else:
                value = self.date_label(self.filters) if self.panel == "dates" else next(
                    (label for key, label in options if key == self.current_option()), "")
                self.put(screen, top + 5, left, "CURRENT", self.styles["muted"], content_width)
                self.put(screen, top + 6, left, value, self.styles["strong"], content_width)
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
                    self.fill(screen, y, left, content_width, style)
                marker = ("› ", self.styles["selected_accent"]) if active else ("  ", 0)
                label = ("* " if value == current else "  ") + plain(label)
                self.put_spans(screen, y, left, [marker, (label, style)], content_width)
            if not options:
                self.put(screen, start_y, left, "No matching projects.", width=content_width)
                self.draw_keys(screen, start_y + 2, left, [("Ctrl+U", "clear the name"), ("Esc", "cancel")], content_width)
            elif len(options) > capacity:
                self.put(screen, bottom - 1, left, f"{self.panel_selected + 1}/{len(options)} · ↑↓ scroll", self.styles["muted"], content_width)
        if self.panel_error:
            self.put(screen, bottom - 2, left, self.panel_error, self.styles["danger"], content_width)
        self.draw_keys(screen, bottom + 1, x, hint, width)
        self.draw_keys(screen, bottom + 2, x, secondary_hint, width)

    def scope_label(self):
        """Active filters only; callers choose the wording for 'none'."""
        filters = self.filters
        labels = []
        if filters.project is not None:
            labels.append(filters.project or "Unknown project")
        if filters.since or filters.until:
            labels.append(self.date_label(filters))
        if filters.source is not None:
            labels.append(PROVIDER_LABELS.get(filters.source, "Unknown provider"))
        if filters.visibility != "all":
            labels.append(f"{filters.visibility.capitalize()} only")
        if filters.substance is not None:
            labels.append(BAND_LABELS[filters.substance])
        return " · ".join(labels)

    def draw_header(self, screen, width):
        """View controls: key, then value; user-set values are strong, defaults muted."""
        self.draw_brand(screen)
        query, scope = self.filters.query, self.scope_label()
        strong, muted = self.styles["strong"], self.styles["muted"]
        self.put_spans(screen, 2, 2, [("/", self.styles["accent"]), (" ", 0),
                                      (query, strong) if query else ("Search conversations", muted)], width)
        # Sort and reset keep their room; only a long filter value is shortened.
        tail = [("   s", self.styles["accent"]), (" ", 0), (self.sort_label, strong if self.sort != "auto" else muted)]
        if self.customized:
            tail += [("   c", self.styles["accent"]), (" reset", muted)]
        scope_width = max(8, width - 2 - sum(cells(text) for text, _ in tail))
        x = self.put_spans(screen, 3, 2, [("f", self.styles["accent"]), (" ", 0),
                                          (ellipsized(scope, scope_width), strong) if scope else ("No filters", muted)], width)
        self.put_spans(screen, 3, x, tail, 2 + width - x)

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 24 or width < 60:
            self.put(screen, 1, 2, "Session Index · enlarge terminal to 60 × 24", self.styles["strong"])
            self.put(screen, 3, 2, "Esc cancels deletion; q quits when not confirming.")
            screen.refresh()
            return
        self.draw_header(screen, width - 4)
        self.rule(screen, 4, 2, width - 4, self.styles["border"])
        # Panes span rows 6 .. height-6; the footer owns the last four rows.
        body_top, body_height = 6, height - 11
        if width >= 112:
            split = width // 2
            for y in range(5, height - 4):
                self.put(screen, y, split, "│", self.styles["border"])
            self.draw_list(screen, body_top, 2, body_height, split - 4)
            self.draw_preview(screen, body_top, split + 3, body_height, width - split - 5)
        else:
            list_height = max(6, body_height * 11 // 20)
            self.draw_list(screen, body_top, 2, list_height - 1, width - 4)
            self.draw_preview(screen, body_top + list_height, 2, body_height - list_height, width - 4, compact=True)
        self.rule(screen, height - 4, 2, width - 4, self.styles["border"])
        self.put(screen, height - 3, 2, self.status, self.styles["danger"] if self.status_error else 0, width - 4)
        self.draw_keys(screen, height - 2, 2, self.session_actions() + [("?", "help"), ("q", "quit")], width - 4)
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
