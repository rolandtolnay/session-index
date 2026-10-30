"""Side Chat navigation stays read-only and preserves the owning browser state."""

import curses
from dataclasses import replace
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from db import init_db, upsert_session
import manage_tui
from manage_tui import SessionManager


class Screen:
    def __init__(self, columns=60, rows=24):
        self.columns, self.rows = columns, rows
        self.lines = {}

    def getmaxyx(self):
        return self.rows, self.columns

    def erase(self):
        self.lines.clear()

    def addstr(self, y, x, text, style):
        self.lines.setdefault(y, []).append((x, text))

    def hline(self, *args):
        pass

    def refresh(self):
        pass

    def text(self):
        return "\n".join("".join(text for _, text in sorted(self.lines[y])) for y in sorted(self.lines))


@pytest.fixture
def browser(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    upsert_session(conn, session_id="parent", project="fixture", headline="Parent conversation",
                   summary=" ".join(f"summary{i}" for i in range(300)))
    upsert_session(conn, session_id="empty", project="other", headline="No children")
    for i in range(2):
        path = tmp_path / f"side-chat-{i}.md"
        path.write_text("# Side Chat header\n\n[prompt] ───\nFocused Content: example\n\n"
                        "[user] ───\nQuestion\n\n[assistant] ───\n"
                        "First paragraph\n\n    example_code()\n\n" +
                        "\n".join(f"exchange{i}_{j:03}" for j in range(80)) +
                        "\n[user] " + "quoted conversation content " * 20 + "UNTRUNCATED_TAIL\n")
        conn.execute("""INSERT INTO side_chats
            (parent_session_id, side_chat_id, opener, started_at, focused_content,
             turns_json, turn_count, first_question, headline, content_hash, search_text, transcript_path)
            VALUES ('parent', ?, 'selection', ?, '{}', '[]', 1, 'Question', ?, '', ?, ?)""",
                     (str(i), f"2026-01-0{i+1}", f"Child {i}", "needle" if i else "ordinary", str(path)))
    conn.commit()
    delete = Mock(side_effect=AssertionError("Side Chat navigation must not delete a session"))
    ui = SessionManager(conn, delete)
    ui.filters = replace(ui.filters, project="fixture")
    ui.sort = "oldest"
    ui.reset_position()
    ui.styles = dict.fromkeys(("strong", "muted", "accent", "border", "selected",
                              "selected_muted", "selected_accent", "state", "danger"), 0)
    yield ui
    conn.close()


def test_read_copy_and_back_preserve_parent_view_and_isolate_actions(browser, monkeypatch):
    ui, screen = browser, Screen()
    ui.draw(screen)
    ui.handle_key(curses.KEY_NPAGE)
    ui.draw(screen)
    before = (ui.index, ui.top, ui.preview_scroll, ui.filters, ui.sort)
    copied = Mock()
    monkeypatch.setattr(manage_tui, "copy_transcript_path", copied)
    ui.handle_key("\n")
    ui.handle_key("j")
    ui.handle_key("\n")
    ui.draw(screen)
    assert "Focused Content" in screen.text()
    assert ui.reader["headline"] == "Child 1"
    ui.handle_key("c")
    copied.assert_called_once_with(ui.reader["transcript_path"])
    assert ui.status and not ui.status_error
    for key in "hd/s":
        ui.handle_key(key)
    assert ui.delete_target is None and ui.panel is None
    assert ui.conn.execute("SELECT hidden_from_recents FROM sessions WHERE session_id='parent'").fetchone()[0] == 0
    ui.handle_key("\x1b")
    assert ui.reader is None and ui.side_chat_selected == 1
    ui.handle_key("\x1b")
    assert ui.side_chat_session is None
    assert (ui.index, ui.top, ui.preview_scroll, ui.filters, ui.sort) == before


@pytest.mark.parametrize("columns,rows", [(150, 40), (100, 34), (60, 24)])
def test_all_transcript_content_reachable_and_paragraphs_preserved(browser, columns, rows):
    ui, screen = browser, Screen(columns, rows)
    ui.handle_key("\n")
    ui.handle_key("\n")
    seen = set()
    for _ in range(200):
        ui.draw(screen)
        seen.update(screen.text().split())
        if ui.reader_scroll == ui.reader_max:
            break
        ui.handle_key(curses.KEY_NPAGE)
    assert {f"exchange0_{j:03}" for j in range(80)} <= seen
    assert "UNTRUNCATED_TAIL" in seen
    ui.handle_key(curses.KEY_HOME)
    ui.draw(screen)
    lines = [text for text, _ in ui.reader_lines[1]]
    paragraph = lines.index("First paragraph")
    assert lines[paragraph + 1] == ""
    assert lines[paragraph + 2] == "    example_code()"
    # Width changes must rewrap and keep the end of the transcript reachable.
    screen.columns = 60 if columns > 60 else 150
    ui.draw(screen)
    ui.handle_key(curses.KEY_END)
    ui.draw(screen)
    assert "UNTRUNCATED_TAIL" in screen.text()


def test_missing_transcript_can_copy_path_and_return(browser, monkeypatch):
    ui, screen = browser, Screen()
    path = ui.session["side_chats"][0]["transcript_path"]
    Path(path).unlink()
    ui.handle_key("\n")
    ui.handle_key("\n")
    ui.draw(screen)
    assert ui.reader_error and "missing" in screen.text().lower()
    copied = Mock(side_effect=OSError("Clipboard unavailable"))
    monkeypatch.setattr(manage_tui, "copy_transcript_path", copied)
    ui.handle_key("c")
    ui.draw(screen)
    assert ui.status_error and "Clipboard unavailable" in screen.text()
    copied.assert_called_once_with(path)
    ui.handle_key("q")
    ui.handle_key("q")
    assert ui.side_chat_session is None and ui.reader is None


def test_search_selects_matching_child_and_empty_session_is_inert(browser):
    ui = browser
    ui.filters = replace(ui.filters, query="needle")
    ui.reset_position()
    assert ui.total == 1
    ui.handle_key("\n")
    assert ui.side_chat_selected == 1
    ui.handle_key("\x1b")
    ui.filters = replace(ui.filters, project="other", query="")
    ui.reset_position()
    assert ui.session["session_id"] == "empty"
    ui.handle_key("\n")
    assert ui.side_chat_session is None
