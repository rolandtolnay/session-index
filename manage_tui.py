"""Full-screen, dependency-free terminal UI for managing indexed sessions."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import curses
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import locale
import os
import re
import sqlite3
import textwrap

from db import project_options, set_hidden_from_recents
from manage_artifacts import copy_transcript_path, read_transcript
from manage_query import ManageFilters, query_manage_sessions
from manage_transcript import BAND_BAR, ROLE_DIVIDER, cells, clipped, render_transcript


SORT_LABELS = {
    "auto": "Automatic", "newest": "Newest first", "oldest": "Oldest first",
    "relevance": "Best match", "substance": "Substance first",
}
BAND_LABELS = {None: "Any substance", "substantial": "Substantial", "useful": "Useful",
               "unknown": "Unknown", "low_value": "Low-value"}
FILTER_CATEGORIES = ("project", "dates", "source", "visibility", "substance")
FILTER_LABELS = ("Project", "Date", "Provider", "Visibility", "More")
# Sessions fetched per query; the list scrolls through them as one continuous list.
CHUNK_SIZE = 60
# Excerpt fields the preview already shows in full; repeating them is noise.
PREVIEWED_MATCH_FIELDS = ("Headline:", "Summary:", "Project:", "Session ID:")
# The first message divider ends a transcript's own header (date line, Related
# artifacts), which the reader's identity block and child pickers replace.
FIRST_DIVIDER = re.compile(rf"^{ROLE_DIVIDER.pattern}$", re.MULTILINE)
PROVIDER_LABELS = {"claude": "Claude", "pi": "Pi", "codex": "Codex"}
# A session's children, read from its transcript: picker panel (also the
# session's key for them) -> (reader key, picker title, singular noun).
CHILD_PANELS = {"side_chats": ("s", "Side chats", "side chat"),
                "subagent_runs": ("a", "Subagent runs", "subagent run")}
# Pi's claude-code-dark theme (~/.pi/agent/themes/claude-code-dark.json):
# claude (accent), inactive (muted), subtle (border and Pi's dim), userMessageBg
# (selection and user turns), accentTeal (mdCode), suggestion (mdLink).
PALETTE = {"text": "#FFFFFF", "accent": "#D77757", "muted": "#999999", "dim": "#505050",
           "border": "#505050", "selected_bg": "#373737", "state": "#FFC107", "danger": "#FF6B80",
           "code": "#00CCCC", "link": "#B1B9F9", "inverse": "#000000"}
# Every role draw code may name; transcript rows add modifiers ("heading underline").
STYLE_ROLES = ("strong", "muted", "dim", "accent", "border", "selected", "selected_muted", "selected_accent",
               "state", "danger", "heading", "bullet", "code", "link", "quote", "band", "band_muted", "match")
STYLE_MODIFIERS = {"bold": curses.A_BOLD, "italic": getattr(curses, "A_ITALIC", 0), "underline": curses.A_UNDERLINE}
# Transcript roles that hold conversation text, so search matches are marked in them.
SEARCHABLE_ROLES = {"heading", "code", "link", "quote", "band", *STYLE_MODIFIERS}
# (key, description) rows render as key hints; uppercase strings are captions.
HELP_LINES = [
    ("↑↓ j k", "Select the previous / next session"),
    ("⇧↑↓", "Jump one screen of sessions"),
    ("← → p n", "Same; option/ctrl+↑↓ work too"),
    ("pgup pgdn", "Scroll the preview"),
    ("enter", "Open the selected session's transcript"),
    ("/", "Search; enter applies, esc cancels"),
    ("f", "Filter; type a project, enter applies"),
    ("s", "Sort order"),
    ("c", "Reset search, filters, and sort"),
    ("tab", "Toggle all / hidden-only sessions"),
    ("h", "Hide / unhide selected session"),
    ("d", "Delete selected session; y confirms"),
    ("r", "Refresh, keeping search and filters"),
    ("q esc", "Quit (esc first dismisses a panel)"),
    "", "TRANSCRIPT",
    ("↑↓ j k", "Scroll a line"),
    ("⇧↑↓ space", "Scroll a screen; pgup pgdn, ← → too"),
    ("home end", "Jump to the start / end; g G too"),
    ("c", "Copy the transcript's absolute path"),
    ("s", "Pick one of the session's Side Chats"),
    ("a", "Pick one of the session's Subagent Runs"),
    ("esc q", "Back: Side Chat or run, transcript, list"),
    "", "IN PICKERS",
    ("tab", "Next filter category (shift+tab back)"),
    ("ctrl+r", "Clear all filters, keep search and sort"),
    ("ctrl+u", "Clear the text input"),
    "", "SEARCH",
    "Every word must match; partial words work.",
    "Typos use near matches only if exact matches fail.",
    "Search covers titles, summaries, user prompts,",
    "projects, files, IDs, and Side Chat conversations.",
    "Main assistant text and raw logs are not searched.",
    "Transcripts open at the first match, highlighted.",
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


def screen_jump(key) -> int:
    """-1 / 1 for keys that jump a screen of sessions, else 0.

    Shift+arrows arrive as KEY_SR / KEY_SF; Option and Ctrl variants arrive as
    terminfo extended keys (kUP3, kDN5, ...), which only have a keyname.
    Terminfo entries without them (tmux-256color) leave the raw CSI sequence.
    """
    if isinstance(key, str) and key.startswith("\x1b[1;"):
        return {"A": -1, "B": 1}.get(key[-1], 0)
    if key in ("p", curses.KEY_LEFT, curses.KEY_SR):
        return -1
    if key in ("n", curses.KEY_RIGHT, curses.KEY_SF):
        return 1
    if not isinstance(key, int):
        return 0
    try:
        name = curses.keyname(key)
    except (ValueError, curses.error):
        return 0
    if name.startswith(b"kUP"):
        return -1
    if name.startswith(b"kDN"):
        return 1
    return 0


def escape_sequence(screen) -> str:
    """Esc plus any sequence curses did not decode, so it never reads as a bare Esc."""
    sequence = "\x1b"
    screen.nodelay(True)
    try:
        while len(sequence) < 16:
            sequence += screen.get_wch()
    except curses.error:
        pass  # Nothing more is buffered.
    finally:
        screen.nodelay(False)
    return sequence


def plain(value: str | None) -> str:
    """Indexed text is data, never terminal escape sequences or markup."""
    return "".join(c for c in " ".join((value or "").split()) if c.isprintable())


def ellipsized(text: str, width: int) -> str:
    """Never let a clipped value read as complete, e.g. a worktree project name."""
    if cells(text) <= width:
        return text
    return clipped(text, width - 1) + "…" if width > 0 else ""


def message_count(value: int | None) -> str:
    """Unknown counts read as unknown, never as an empty session."""
    return "?" if value is None else f"{value:,}"


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


def chunked(text: str, width: int) -> list[str]:
    """Split by cells with no word breaks, for paths that have no spaces."""
    lines = []
    while text:
        part = clipped(text, max(2, width))
        lines.append(part)
        text = text[len(part):]
    return lines


def clamped(text: str, width: int, count: int) -> list[str]:
    """At most count wrapped lines, the last ellipsized when text runs over."""
    lines = wrapped(text, width)
    if len(lines) > count:
        lines = lines[:count - 1] + [ellipsized(" ".join(lines[count - 1:]), width)]
    return lines


def xterm256(hex_color: str) -> int:
    """Nearest xterm-256 index; redefining terminal colors would leak past exit."""
    target = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    levels = (0, 95, 135, 175, 215, 255)
    candidates = {16 + 36 * r + 6 * g + b: (levels[r], levels[g], levels[b])
                  for r in range(6) for g in range(6) for b in range(6)}
    candidates.update({232 + i: (8 + 10 * i,) * 3 for i in range(24)})
    return min(candidates, key=lambda index: sum((a - b) ** 2 for a, b in zip(candidates[index], target)))


def hints_width(keys) -> int:
    """Cells draw_keys needs for these hints."""
    return sum(cells(key) + (1 + cells(label) if label else 0) for key, label, *_ in keys) + 3 * (len(keys) - 1)


def substance_label(session: dict) -> str:
    """The session's Substance Band; an unassessed session shows nothing, not "Unknown"."""
    band = session.get("substance_band")
    return BAND_LABELS[band] if band in ("substantial", "useful", "low_value") else ""


def display_date(value: str | None) -> str:
    try:
        date = datetime.fromisoformat(value or "").astimezone()
    except ValueError:
        return "Unknown date"
    days = (datetime.now().astimezone().date() - date.date()).days
    label = "Today" if days == 0 else "Yesterday" if days == 1 else date.strftime("%b %d, %Y")
    return f"{label} · {date:%H:%M}"


@dataclass
class Reader:
    """A transcript open full-screen: a session's Clean Transcript, or one of its
    children (a Side Chat or Subagent Run, named by its CHILD_PANELS key)."""
    session: dict
    child: dict | None
    kind: str | None
    path: str
    text: str = ""
    error: str = ""
    # (width, styled lines wrapped for it, index of each transcript line's first wrapped line)
    lines: tuple = (0, [], [])
    scroll: int = 0
    max: int = 0
    height: int = 1
    # Opened from a search: matches are highlighted, and the first draw scrolls
    # to the transcript line holding the first match.
    terms: re.Pattern | None = None
    match: int | None = None


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
        # Positions are absolute in the result set; `sessions` is the fetched
        # chunk starting at `offset`, and `top` is the first row on screen.
        self.offset = 0
        self.index = 0
        self.top = 0
        self.rows = 10  # Rows the list shows; draw_list updates it.
        self.sessions = []
        self.preview_scroll = 0
        self.preview_max = 0
        self.preview_height = 1
        self.status = ""
        self.status_error = False
        self.delete_target = None
        # Open transcripts: the session's, then optionally one of its Side Chats.
        # The last one is on screen; Esc pops back toward the browser underneath.
        self.readers: list[Reader] = []
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
        local = self.index - self.offset
        return self.sessions[local] if 0 <= local < len(self.sessions) else None

    @property
    def reader(self):
        return self.readers[-1] if self.readers else None

    def session_actions(self):
        """Keys acting on the selected session; add new per-session actions here."""
        if self.session is None:
            return []
        return [("enter", "open", self.styles["accent"]),
                ("h", "unhide" if self.session["hidden_from_recents"] else "hide"), ("d", "delete")]

    def fetch(self):
        """Fetch the chunk around the viewport; chunks are a query detail."""
        limit = max(CHUNK_SIZE, self.rows * 3)
        self.offset = max(0, self.top - (limit - self.rows) // 2)
        page = query_manage_sessions(self.conn, self.filters, sort=self.sort, offset=self.offset, limit=limit)
        self.sessions, self.total, self.approximate = page.sessions, page.total, page.approximate

    def reload(self):
        self.projects = project_options(self.conn)
        self.project_labels = {value: label for value, label, _count in self.projects}
        self.fetch()
        self.index = min(self.index, max(0, self.total - 1))
        self.scroll_into_view()
        self.preview_scroll = 0

    def reset_position(self):
        self.index, self.top = 0, 0
        self.reload()

    def scroll_into_view(self):
        """Scroll as little as possible to show the selection, then fetch if needed."""
        self.top = min(self.top, self.index, max(0, self.total - self.rows))
        self.top = max(self.top, self.index - self.rows + 1, 0)
        end = min(self.total, self.top + self.rows)
        if self.top < self.offset or end > self.offset + len(self.sessions):
            self.fetch()

    def move_selection(self, delta, *, scroll=False):
        """Select relative to the current session; scroll also shifts the view by delta."""
        if not self.total:
            return
        self.index = max(0, min(self.total - 1, self.index + delta))
        if scroll:
            self.top += delta
        self.scroll_into_view()
        self.preview_scroll = 0

    def open_panel(self, panel):
        self.panel, self.panel_selected, self.panel_error = panel, 0, ""
        if panel == "search":
            self.input = self.filters.query
            self.cursor = len(self.input)
        elif panel == "filters":
            self.input, self.cursor = "", 0
            self.projects = project_options(self.conn)
            self.project_labels = {value: label for value, label, _count in self.projects}
            self.select_category("project")
        elif panel == "range":
            self.date_inputs = [self.filters.since, self.filters.until]
            self.date_field = 0
            self.cursor = len(self.date_inputs[0])
        elif panel == "sort":
            self.select_current_option()
        elif panel in CHILD_PANELS:
            # The child being read, else the first one the search matched.
            current = self.current_option()
            self.panel_selected = current if current is not None else next(
                (i for i, child in enumerate(self.children(panel)) if child.get("matched")), 0)
        elif panel == "help":  # From the reader, start at the keys that work there.
            self.help_scroll = HELP_LINES.index("TRANSCRIPT") if self.readers else 0

    def date_presets(self):
        today = date.today()
        return {"any": ("", ""), "today": (today.isoformat(), today.isoformat()),
                **{str(days): ((today - timedelta(days=days - 1)).isoformat(), today.isoformat())
                   for days in (7, 30, 90)}}

    def children(self, kind):
        """Side Chats or Subagent Runs of the session whose transcript is open."""
        return self.readers[0].session.get(kind) or [] if self.readers else []

    def current_option(self):
        if self.panel == "sort":
            return self.sort
        if self.panel in CHILD_PANELS:
            child = self.reader.child
            return next((i for i, other in enumerate(self.children(self.panel)) if other is child), None)
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
            options = [(value, label) for value, label, _count in self.projects
                       if self.input.casefold() in label.casefold() or self.input.casefold() in value.casefold()]
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
        if panel == "side_chats":
            return [(i, ("matched · " if child.get("matched") else "") + child["headline"])
                    for i, child in enumerate(self.children(panel))]
        if panel == "subagent_runs":
            return [(i, f"{child['agent']} · {child['headline']}") for i, child in enumerate(self.children(panel))]
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
            elif self.panel in CHILD_PANELS:
                kind, self.panel = self.panel, None
                self.open_transcript(self.readers[0].session, self.children(kind)[value], kind)
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
        if self.readers:
            self.handle_reader_key(key)
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
        elif jump := screen_jump(key):
            self.move_selection(jump * self.rows, scroll=True)
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            step = max(1, self.preview_height - 1)
            delta = step if key == curses.KEY_NPAGE else -step
            self.preview_scroll = max(0, min(self.preview_max, self.preview_scroll + delta))
        elif key == "r":
            self.reload()
            self.message("Session list refreshed.")
        elif self.session and key in ("h", "u"):
            session = self.session
            hide = not session["hidden_from_recents"] if key == "h" else False
            try:
                set_hidden_from_recents(self.conn, session["session_id"], hide)
                self.message("Hidden from recents; still searchable." if hide else "Restored to recent context.")
                self.reload()
            except (ValueError, sqlite3.Error) as error:
                self.message(str(error), error=True)
        elif self.session and key == "d":
            self.delete_target = self.session
        elif self.session and key in ("\n", "\r", curses.KEY_ENTER):
            self.open_transcript(self.session)
        return True

    def open_transcript(self, session, child=None, kind=None):
        """Read a transcript over the browser, whose state stays untouched underneath.

        A child (Side Chat or Subagent Run) opens above its session's transcript,
        replacing any other child, so Esc always returns to the conversation it
        belongs to.
        """
        reader = Reader(session, child, kind, (child or session).get("transcript_path") or "")
        try:
            text = read_transcript(reader.path)
        except (OSError, ValueError) as error:
            reader.error = ("The transcript file is missing." if isinstance(error, FileNotFoundError)
                            else str(error.strerror or error) if isinstance(error, OSError) else str(error))
        else:
            start = FIRST_DIVIDER.search(text)
            reader.text = text[start.start():] if start else text
            self.find_first_match(reader)
        self.readers = self.readers[:1] + [reader] if child else [reader]

    def find_first_match(self, reader):
        """The search's partial, case-insensitive matching, on one line: all words, else any."""
        terms = sorted(set(self.filters.query.casefold().split()), key=len, reverse=True)
        if not terms:
            return
        # Message dividers are structure; "user" or "assistant" must not stop on them.
        lines = ["" if ROLE_DIVIDER.fullmatch(line) else line.casefold() for line in reader.text.splitlines()]
        for test in (all, any):
            reader.match = next((i for i, line in enumerate(lines) if test(term in line for term in terms)), None)
            if reader.match is not None:
                reader.terms = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE)
                return

    def handle_reader_key(self, key):
        reader = self.reader
        if key != curses.KEY_RESIZE:
            self.message("")
        if key in ("\x1b", "\x03", "q", curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.readers.pop()
        elif key == "c":
            try:
                copy_transcript_path(reader.path)
                self.message("Copied transcript path.")
            except (OSError, ValueError) as error:
                self.message(str(error) or "Could not copy the transcript path.", error=True)
        elif kind := next((name for name, (child_key, *_) in CHILD_PANELS.items()
                           if key == child_key and self.children(name)), None):
            self.open_panel(kind)
        elif key == "?":
            self.open_panel("help")
        elif key in (curses.KEY_HOME, "g"):
            reader.scroll = 0
        elif key in (curses.KEY_END, "G"):
            reader.scroll = reader.max
        else:
            page = max(1, reader.height - 1)
            delta = {curses.KEY_DOWN: 1, "j": 1, curses.KEY_UP: -1, "k": -1, " ": page,
                     curses.KEY_NPAGE: page, curses.KEY_PPAGE: -page}.get(key) or screen_jump(key) * page
            reader.scroll = max(0, min(reader.max, reader.scroll + delta))

    def configure_colors(self):
        """Semantic roles; draw code names a role, never a raw color or attribute.

        strong: primary content    muted: secondary metadata, captions, defaults, hint actions
        dim: hint keys             accent: view state, focus, and the next action
        border: rules and frames
        selected: the focused row (selected_muted / selected_accent inside it)
        state: non-default session state (hidden)    danger: destructive or failed
        heading, bullet, code, link, quote: transcript Markdown, as Pi renders it
        band / band_muted: user turns    match: search matches in a transcript
        """
        italic = STYLE_MODIFIERS["italic"]
        self.styles = {"strong": curses.A_BOLD, "muted": curses.A_DIM, "dim": curses.A_DIM,
                       "accent": curses.A_BOLD, "border": curses.A_DIM, "selected": curses.A_REVERSE | curses.A_BOLD,
                       "selected_muted": curses.A_REVERSE, "selected_accent": curses.A_REVERSE | curses.A_BOLD,
                       "state": curses.A_BOLD, "danger": curses.A_BOLD, "heading": curses.A_BOLD, "bullet": 0,
                       "code": 0, "link": curses.A_UNDERLINE, "quote": curses.A_DIM | italic, "band": 0,
                       "band_muted": curses.A_DIM, "match": curses.A_REVERSE}
        if not curses.has_colors() or "NO_COLOR" in os.environ:
            return
        curses.start_color()
        curses.use_default_colors()
        if curses.COLORS >= 256:
            color = {name: xterm256(value) for name, value in PALETTE.items()}
        else:  # Nearest basic colors; 8-color terminals cannot show the Pi palette.
            color = {"text": curses.COLOR_WHITE, "accent": curses.COLOR_RED, "muted": curses.COLOR_WHITE,
                     "dim": curses.COLOR_WHITE, "border": curses.COLOR_WHITE, "selected_bg": curses.COLOR_BLACK,
                     "state": curses.COLOR_YELLOW, "danger": curses.COLOR_RED, "code": curses.COLOR_CYAN,
                     "link": curses.COLOR_BLUE, "inverse": curses.COLOR_BLACK}
        for number, (name, foreground, background, attribute) in enumerate([
            ("strong", "text", None, curses.A_BOLD), ("accent", "accent", None, curses.A_BOLD),
            ("muted", "muted", None, 0), ("dim", "dim", None, 0), ("border", "border", None, 0),
            ("selected", "text", "selected_bg", curses.A_BOLD),
            ("selected_muted", "muted", "selected_bg", 0),
            ("selected_accent", "accent", "selected_bg", curses.A_BOLD),
            ("state", "state", None, 0), ("danger", "danger", None, 0),
            # Transcript Markdown, as Pi's mdHeading, mdListBullet, mdCode, mdLink and mdQuote.
            ("heading", "accent", None, curses.A_BOLD), ("bullet", "accent", None, 0),
            ("code", "code", None, 0), ("link", "link", None, curses.A_UNDERLINE),
            ("quote", "muted", None, italic),
            ("band", "text", "selected_bg", 0), ("band_muted", "muted", "selected_bg", 0),
            ("match", "inverse", "accent", 0),
        ], 1):
            curses.init_pair(number, color[foreground], color[background] if background else -1)
            self.styles[name] = curses.color_pair(number) | attribute
        if curses.COLORS < 256:
            for name in ("muted", "dim", "border", "quote", "band_muted"):
                self.styles[name] |= curses.A_DIM

    def style(self, role):
        """A transcript role: its last STYLE_ROLES color plus any modifiers ("heading underline")."""
        color = extra = 0
        for name in role.split():
            if name in STYLE_MODIFIERS:
                extra |= STYLE_MODIFIERS[name]
            else:
                color = self.styles[name]
        return color | extra

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
        """Pi's key-hint format: lowercase dim key, muted action, muted " · " between hints.

        An optional third item replaces the key's role: accent for the one next
        action in the row, danger for a destructive one.
        """
        spans = []
        for item in keys:
            key, label, *style = item
            if spans:
                spans.append((" · ", self.styles["muted"]))
            spans.append((key.lower(), style[0] if style else self.styles["dim"]))
            if label:
                spans.append((" " + label, self.styles["muted"]))
        return self.put_spans(screen, y, x, spans, width)

    def put_right(self, screen, y, x, width, text, used=0):
        """Muted text against the right edge, if it fits beside `used` cells."""
        if text and used + cells(text) + 2 <= width:
            self.put(screen, y, x + width - cells(text), text, self.styles["muted"])

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
        self.put_right(screen, y, x, width, detail, cells(label))

    def position_label(self):
        noun = "near matches" if self.approximate else "sessions"
        if not self.total:
            return f"0 {noun}"
        return f"{self.index + 1:,} of {self.total:,}" + (" near matches" if self.approximate else "")

    def draw_list(self, screen, y, x, height, width):
        self.pane_heading(screen, y, x, width, "SESSIONS", self.position_label())
        if not self.total:
            self.put(screen, y + 2, x, "No matching sessions.", self.styles["strong"], width)
            self.draw_keys(screen, y + 4, x, [("/", "edit search", self.styles["accent"]), ("f", "filters"), ("c", "reset")], width)
            return
        row_height = 3 if height >= 20 else 2
        capacity = max(1, (height - 2) // row_height)
        if capacity != self.rows:  # A resize changes how far a screen jump goes.
            self.rows = capacity
            self.scroll_into_view()
        start = self.top - self.offset
        for position, session in enumerate(self.sessions[start:start + capacity], self.top):
            line = y + 2 + (position - self.top) * row_height
            active = position == self.index
            row = self.styles["selected"] if active else 0
            if active:
                for dy in range(2):
                    self.fill(screen, line + dy, x, width, row)
            title = plain(session.get("headline") or session.get("summary") or session.get("user_messages") or "Untitled session")
            marker = ("›", self.styles["selected_accent"]) if active else (" ", 0)
            self.put_spans(screen, line, x, [marker, (" " + title, row)], width)
            short_date = display_date(session.get("started_at")).split(" · ")[0]
            provider = PROVIDER_LABELS.get(session.get("source"), "?")
            users = message_count(session.get("user_message_count"))
            replies = message_count(session.get("assistant_message_count"))
            budget = width - 2 - (9 if session["hidden_from_recents"] else 0)
            details = f" · {short_date} · {provider} · {users} user / {replies} asst"
            if cells(details) - 3 > budget:
                # Drop the year (the preview keeps it); Today, Yesterday and Unknown date have none.
                details = details.replace(short_date, short_date.split(", ")[0], 1)
            # The project yields width first so date, provider, counts and state stay visible.
            spare = budget - cells(details)
            # Too little room for a readable fragment drops the project and its separator.
            project = ellipsized(plain(self.project_label(session)), min(28, spare)) if spare >= 4 else ""
            meta = self.styles["selected_muted"] if active else self.styles["muted"]
            spans = [(project + details if project else details[3:], meta)]
            if session["hidden_from_recents"]:
                spans += [(" · ", meta), ("hidden", row if active else self.styles["state"])]
            self.put_spans(screen, line + 1, x + 2, spans, width - 2)

    def draw_preview(self, screen, y, x, height, width, *, compact=False):
        """Fixed identity block, then a scrollable body.

        Compact (stacked) mode drops the heading and what the list row already
        shows; its muted metadata line doubles as the pane's heading. Either
        heading carries the Substance Band on its right.
        """
        session = self.session
        if not compact:
            self.pane_heading(screen, y, x, width, "PREVIEW", substance_label(session) if session else "")
            y, height = y + 2, height - 2
        if session is None:
            self.put(screen, y, x, "Try fewer words or a wider date range.", width=width)
            return
        provider = PROVIDER_LABELS.get(session.get("source"), "Unknown provider")
        # Without a headline the summary leads, as it does in the list row.
        fixed = [] if compact else [(line, self.styles["strong"]) for line in wrapped(
            plain(session.get("headline")), width)[:3]]
        if not compact:
            fixed.append((plain(self.project_label(session)), self.styles["muted"]))
        fixed.append((f"{display_date(session.get('started_at'))} · {provider} · {session['session_id']}", self.styles["muted"]))
        if session["hidden_from_recents"]:
            fixed.append(("Hidden from recents · still searchable", self.styles["state"]))
        for i, (line, style) in enumerate(fixed):
            self.put(screen, y + i, x, line, style, width)
        if compact:
            self.put_right(screen, y, x, width, substance_label(session), cells(fixed[0][0]))

        # The first and last two indexed user messages, each capped at three lines
        # and drawn as the transcript draws user turns: a band with its bar, numbered.
        prompts = [text for part in (session.get("user_messages") or "").split("\n---\n")
                   if (text := plain(part))]
        numbered = list(enumerate(prompts, 1))
        shown = numbered if len(prompts) <= 4 else numbered[:2] + [None] + numbered[-2:]
        digits = len(str(len(prompts)))
        indent = 2 + digits + 2  # Bar, space, ordinal, two spaces.
        messages, visible = [], []
        for item in shown:
            if messages:
                messages.append(([], 0))
            if item is None:
                omitted = len(prompts) - 4
                messages.append(([(" " * indent + f"{omitted:,} message{'s' * (omitted != 1)} omitted", self.styles["muted"])], 0))
                continue
            number, text = item
            lines = clamped(text, width - indent, 3)
            for i, line in enumerate(lines):
                label = f"{number:>{digits}}" if i == 0 else " " * digits
                messages.append(([(f"{BAND_BAR} {label}  ", self.styles["band_muted"]), (line, self.styles["band"])], self.styles["band"]))
            visible.append(" ".join(lines))

        def on_screen(part):
            """A user-message excerpt adds nothing when its text is already shown."""
            if not part.startswith("User messages:"):
                return False
            pieces = re.split(r"(?:^| )---(?: |$)", part.removeprefix("User messages:").strip(" …"))
            return all(any(piece in text for text in visible) for piece in pieces if piece)

        def row(line, style=0):
            return ([(line, style)], 0)

        # Rows are (spans, fill); a band row paints its fill across the pane first.
        body = []
        matches = [part for part in plain(session.get("match_excerpt")).split(" | ")
                   if part and not part.startswith(PREVIEWED_MATCH_FIELDS) and not on_screen(part)]
        if matches:
            body += [row("MATCHED IN", self.styles["muted"])]
            body += [row(line) for part in matches for line in wrapped(part, width)] + [row("")]
        if summary := plain(session.get("summary")) or ("" if messages else "No preview available yet."):
            body += [row(line) for line in wrapped(summary, width)]
        if messages:
            body += ([row("")] if summary else []) + [row("USER MESSAGES", self.styles["muted"])] + messages
        if session.get("side_chats"):
            body += [row(""), row("SIDE CHATS", self.styles["muted"])]
            for child in session["side_chats"]:
                marker = "matched · " if child.get("matched") else ""
                lines = wrapped(plain(marker + child["headline"]), width - 2)
                body += [row(("· " if i == 0 else "  ") + line) for i, line in enumerate(lines)]
        if runs := session.get("subagent_runs"):
            agents = Counter(run["agent"] for run in runs)
            body += [row(""), row("SUBAGENT RUNS", self.styles["muted"])]
            body += [row(line) for line in wrapped(" · ".join(
                f"{agent} ×{count}" if count > 1 else agent for agent, count in agents.most_common()), width)]
        top = y + 1 + len(fixed)
        count = max(1, y + height - top - 1)  # The last row is kept for the scroll hint.
        self.preview_height = count
        self.preview_max = max(0, len(body) - count)
        self.preview_scroll = min(self.preview_scroll, self.preview_max)
        span = min(width, screen.getmaxyx()[1] - x - 1)  # Fill without put's clipping ellipsis.
        for i, (spans, fill) in enumerate(body[self.preview_scroll:self.preview_scroll + count]):
            if fill:
                self.put(screen, top + i, x, " " * span, fill, width)
            self.put_spans(screen, top + i, x, spans, width)
        if self.preview_max:
            end = self.draw_keys(screen, y + height - 1, x, [("pgup/pgdn", "scroll")], width)
            self.put_right(screen, y + height - 1, x, width, f"{self.preview_scroll * 100 // self.preview_max}%", end - x)

    def draw_brand(self, screen, context=""):
        self.put(screen, 1, 2, "SESSION INDEX", self.styles["strong"])
        if context:
            self.put(screen, 1, 16, "· " + context, self.styles["muted"])

    def draw_reader(self, screen):
        """Full-screen transcript, framed like the browser so it reads as one tool.

        The identity block names the conversation and its absolute path, which
        `c` copies; Side Chats and Subagent Runs are one key away rather than
        separate screens.
        """
        screen.erase()
        height, columns = screen.getmaxyx()
        width = columns - 4
        reader = self.reader
        session, child = reader.session, reader.child
        strong, muted = self.styles["strong"], self.styles["muted"]
        parent = plain(session.get("headline") or session.get("summary") or "Untitled session")
        self.draw_brand(screen, CHILD_PANELS[reader.kind][2] if child else "transcript")
        if not child:  # Children are never assessed; the band describes the session.
            self.put_right(screen, 1, 2, width, substance_label(session), cells("SESSION INDEX · transcript"))
        if reader.kind == "subagent_runs":  # The task opens the transcript, so the agent names the run.
            identity = [[(plain(child["agent"]), strong), (" · " + display_date(child.get("started_at")), muted)]]
        elif child:
            identity = [[(line, strong)] for line in clamped(plain(child["headline"]), width, 2)]
        else:
            provider = PROVIDER_LABELS.get(session.get("source"), "Unknown provider")
            identity = [[(line, strong)] for line in clamped(parent, width, 2)]
            identity.append([(f"{plain(self.project_label(session))} · {display_date(session.get('started_at'))} · {provider}", muted)])
        if child:
            identity.append([(ellipsized("From session: " + parent, width), muted)])
        path = chunked(reader.path, width)
        if len(path) > 3:
            path = path[:2] + [ellipsized("".join(path[2:]), width)]
        identity += [[(line, muted)] for line in path]
        for i, spans in enumerate(identity):
            self.put_spans(screen, 2 + i, 2, spans, width)
        rule = 3 + len(identity)
        self.rule(screen, rule, 2, width, self.styles["border"])
        top = rule + 2
        self.draw_transcript(screen, top, height - 5 - top, width)  # Body rows end above the footer rule.
        self.rule(screen, height - 4, 2, width, self.styles["border"])
        position = f"{reader.scroll * 100 // reader.max}%" if reader.max else ""
        status_width = width - (cells(position) + 2 if position else 0)
        self.put(screen, height - 3, 2, self.status, self.styles["danger"] if self.status_error else 0, status_width)
        self.put_right(screen, height - 3, 2, width, position)
        keys = [("↑↓/⇧↑↓", "scroll")] if reader.max else []
        keys.append(("c", "copy path"))
        children = [(key, f"{count} {noun}{'s' if count > 1 else ''}")
                    for kind, (key, _, noun) in CHILD_PANELS.items() if (count := len(self.children(kind)))]
        if children:  # The first kind of child is the next action.
            children[0] += (self.styles["accent"],)
        keys += children + [("esc", "back")]
        # Narrow terminals drop the self-evident scroll hint, then shorten copy.
        while hints_width(keys) > width and keys[0][0] != "c":
            keys.pop(0)
        if hints_width(keys) > width:
            keys[0] = ("c", "copy")
        self.draw_keys(screen, height - 2, 2, keys, width)

    def draw_transcript(self, screen, y, height, width):
        reader = self.reader
        reader.height = max(1, height)
        if reader.error:
            reader.max = 0
            self.put(screen, y, 2, "Could not read this transcript.", self.styles["danger"], width)
            for i, line in enumerate(wrapped(plain(reader.error), width)[:max(0, height - 2)]):
                self.put(screen, y + 2 + i, 2, line, width=width)
            return
        if reader.lines[0] != width:
            if reader.lines[0]:  # A resize keeps the transcript line at the top in place.
                anchor, context = bisect_right(reader.lines[2], reader.scroll) - 1, 0
            else:  # The first draw opens at the search match, two lines of context above.
                anchor, context = reader.match, 2
            reader.lines = (width, *render_transcript(reader.text, width))
            starts = reader.lines[2]
            if anchor is not None and starts:
                reader.scroll = max(0, starts[anchor] - context)
        lines = reader.lines[1]
        reader.max = max(0, len(lines) - reader.height)
        reader.scroll = min(reader.scroll, reader.max)
        for i, line in enumerate(lines[reader.scroll:reader.scroll + reader.height]):
            if line.fill:
                self.fill(screen, y + i, 2, width, self.style(line.fill))
            spans = []
            for text, role in line.spans:
                end = 0
                if reader.terms and not set(role.split()) - SEARCHABLE_ROLES:
                    for found in reader.terms.finditer(text):
                        spans += [(text[end:found.start()], self.style(role)), (found.group(), self.styles["match"])]
                        end = found.end()
                spans.append((text[end:], self.style(role)))
            self.put_spans(screen, y + i, 2, spans, width)
        if not lines:
            self.put(screen, y, 2, "This transcript has no exchanges yet.", self.styles["muted"], width)

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
        self.put(screen, y + 4, left, f"{plain(self.project_label(session))} · {session['session_id']}", self.styles["muted"], inner)
        warning = "Removes indexed data and generated files for this session. No undo. Raw transcripts remain, so re-indexing can recreate it."
        for i, line in enumerate(wrapped(warning, inner)[:3]):
            self.put(screen, y + 6 + i, left, line, width=inner)
        self.draw_keys(screen, y + 10, left, [("y", "delete", self.styles["danger"]), ("esc", "cancel")], inner)
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
            **{kind: title for kind, (_, title, _) in CHILD_PANELS.items()},
        }[self.panel]
        self.draw_brand(screen)
        self.rule(screen, top, x, width, self.styles["border"], corners="╭╮")
        self.put(screen, top + 1, left, title, self.styles["strong"], content_width)
        self.rule(screen, bottom, x, width, self.styles["border"], corners="╰╯")
        accent = self.styles["accent"]
        hint, secondary_hint = [("enter", "apply", accent), ("esc", "cancel")], []

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
            hint = [("tab", "category"), ("↑↓", "choose"), ("enter", "set", accent), ("esc", "cancel")]
            secondary_hint = [("shift+tab", "back"), ("ctrl+r", "clear filters")]

        if self.panel == "search":
            self.put(screen, top + 3, left, "WORDS / FILE / SESSION ID", self.styles["muted"], content_width)
            self.draw_input(screen, top + 4, left, self.input, self.cursor, content_width)
            self.rule(screen, top + 5, left, content_width, self.styles["border"])
            self.put(screen, top + 7, left, "All words · Partial matches · Typo fallback", self.styles["muted"], content_width)
            self.put(screen, top + 9, left, "WITHIN", self.styles["muted"], content_width)
            for i, line in enumerate(wrapped(self.scope_label() or "All sessions", content_width)[:3]):
                self.put(screen, top + 10 + i, left, line, width=content_width)
            hint = [("enter", "search", accent), ("esc", "cancel"), ("ctrl+u", "clear")]
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
            hint = [("tab", "field"), ("enter", "next/set", accent), ("esc", "back")]
            secondary_hint = [("ctrl+u", "clear field")]
        elif self.panel == "help":
            start_y = top + 3
            capacity = bottom - start_y - 1
            self.help_scroll = min(self.help_scroll, max(0, len(HELP_LINES) - capacity))
            for i, line in enumerate(HELP_LINES[self.help_scroll:self.help_scroll + capacity]):
                if isinstance(line, tuple):  # One step brighter than a hint: muted key, plain text.
                    self.put(screen, start_y + i, left, line[0], self.styles["muted"], 11)
                    self.put(screen, start_y + i, left + 11, line[1], width=content_width - 11)
                else:
                    self.put(screen, start_y + i, left, line, self.styles["muted"] if line.isupper() else 0, content_width)
            self.put(screen, bottom - 1, left, f"{self.help_scroll + 1}–{min(len(HELP_LINES), self.help_scroll + capacity)} / {len(HELP_LINES)}", self.styles["muted"], content_width)
            hint = [("↑↓ pgup/pgdn", "scroll"), ("enter/esc", "close")]
        else:
            options = self.panel_options()
            caption_y = top + 8
            if self.panel in CHILD_PANELS:
                parent = self.readers[0].session
                parent = plain(parent.get("headline") or parent.get("summary") or "Untitled session")
                self.put(screen, top + 3, left, ellipsized("From session: " + parent, content_width), self.styles["muted"])
                caption, caption_y = "", top + 5
                hint = [("↑↓", "choose"), ("enter", "read", accent), ("esc", "back")]
            elif self.panel == "project":
                self.put(screen, top + 5, left, "NARROW PROJECTS", self.styles["muted"], content_width)
                self.draw_input(screen, top + 6, left, self.input, self.cursor, content_width)
                if not self.input:
                    self.put(screen, top + 6, left + 2, "Type a project name or path…", self.styles["muted"], content_width - 2)
                self.rule(screen, top + 7, left, content_width, self.styles["border"])
                caption = "PROJECTS"
            else:
                value = self.date_label(self.filters) if self.panel == "dates" else next(
                    (label for key, label in options if key == self.current_option()), "")
                self.put(screen, top + 5, left, "CURRENT", self.styles["muted"], content_width)
                self.put(screen, top + 6, left, value, self.styles["strong"], content_width)
                caption = {"dates": "STARTED", "source": "PROVIDER", "visibility": "VISIBILITY",
                           "substance": "SUBSTANCE · REFERENCE VALUE", "sort": "ORDER"}[self.panel]
            current = self.current_option()
            self.put(screen, caption_y, left, caption, self.styles["muted"], content_width - 12)
            if self.panel not in CHILD_PANELS or current is not None:
                self.put(screen, caption_y, left + content_width - 9, "* current", self.styles["muted"], 9)
            start_y = caption_y + 1
            capacity = max(1, bottom - start_y - 1)
            start = max(0, min(self.panel_selected - capacity + 1, len(options) - capacity))
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
                self.draw_keys(screen, start_y + 2, left, [("ctrl+u", "clear the name"), ("esc", "cancel")], content_width)
            elif len(options) > capacity:
                self.put(screen, bottom - 1, left, f"{self.panel_selected + 1}/{len(options)} · ↑↓ scroll", self.styles["muted"], content_width)
        if self.panel_error:
            self.put(screen, bottom - 2, left, self.panel_error, self.styles["danger"], content_width)
        self.draw_keys(screen, bottom + 1, x, hint, width)
        self.draw_keys(screen, bottom + 2, x, secondary_hint, width)

    def project_label(self, session):
        value = session.get("project_identity") or session.get("project_id") or session.get("project_path") or session.get("project") or ""
        return self.project_labels.get(value, session.get("project") or "Unknown project")

    def scope_label(self):
        """Active filters only; callers choose the wording for 'none'."""
        filters = self.filters
        labels = []
        if filters.project is not None:
            labels.append(self.project_labels.get(filters.project, filters.project or "Unknown project"))
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
        """View controls: dim key, then value; user-set values are accent state, defaults muted."""
        self.draw_brand(screen)
        query, scope = self.filters.query, self.scope_label()
        accent, muted, dim = self.styles["accent"], self.styles["muted"], self.styles["dim"]
        self.put_spans(screen, 2, 2, [("/", dim), (" ", 0),
                                      (query, accent) if query else ("Search conversations", muted)], width)
        # Sort and reset keep their room; only a long filter value is shortened.
        tail = [("   s", dim), (" ", 0), (self.sort_label, accent if self.sort != "auto" else muted)]
        if self.customized:
            tail += [("   c", dim), (" reset", muted)]
        scope_width = max(8, width - 2 - sum(cells(text) for text, _ in tail))
        x = self.put_spans(screen, 3, 2, [("f", dim), (" ", 0),
                                          (ellipsized(scope, scope_width), accent) if scope else ("No filters", muted)], width)
        self.put_spans(screen, 3, x, tail, 2 + width - x)

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 24 or width < 60:
            self.put(screen, 1, 2, "Session Index · enlarge terminal to 60 × 24", self.styles["strong"])
            self.put(screen, 3, 2, "Esc cancels deletion; q quits when not confirming.")
            screen.refresh()
            return
        if self.delete_target is not None:
            self.draw_confirmation(screen)
        elif self.panel is not None:
            self.draw_panel(screen)  # Panels replace the browser or the reader they opened from.
        elif self.readers:
            self.draw_reader(screen)
        else:
            self.draw_browser(screen, height, width)
        screen.refresh()

    def draw_browser(self, screen, height, width):
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
                if key == "\x1b":
                    key = escape_sequence(screen)
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
