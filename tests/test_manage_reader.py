"""The transcript reader stays read-only and preserves the browser state underneath."""

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


def transcript(tag: str, opener: str = "[user] 10:00:00 ───") -> str:
    return (f"fixture | main | 2026-01-01\n---\n\n## Related artifacts\n- ARTIFACT_LIST_{tag}\n\n---\n\n"
            f"{opener}\nQuestion {tag}\n\n[assistant] 10:00:05 ───\nFirst paragraph\n\n    example_code()\n\n" +
            "rewrapped prose " * 40 + "\n" +
            "\n".join(f"{tag}_{j:03}" for j in range(80)) + f"\nOnly Zebra here {tag}\nZebra and Giraffe {tag}" +
            "\n[user] " + "quoted conversation content " * 20 + f"TAIL_{tag}\n")


@pytest.fixture
def browser(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    main = tmp_path / "parent.md"
    main.write_text(transcript("main"))
    upsert_session(conn, session_id="parent", project="fixture", headline="Parent conversation",
                   summary="zebra giraffe " + " ".join(f"summary{i}" for i in range(300)), transcript_path=str(main),
                   substance_band="substantial")
    upsert_session(conn, session_id="empty", project="other", headline="No children",
                   transcript_path=str(tmp_path / "never-generated.md"))
    for i in range(2):
        path = tmp_path / f"side-chat-{i}.md"
        path.write_text(transcript(f"child{i}", "[prompt] ───"))
        conn.execute("""INSERT INTO side_chats
            (parent_session_id, side_chat_id, opener, started_at, focused_content,
             turns_json, turn_count, first_question, headline, content_hash, search_text, transcript_path)
            VALUES ('parent', ?, 'selection', ?, '{}', '[]', 1, 'Question', ?, '', ?, ?)""",
                     (str(i), f"2026-01-0{i+1}", f"Child {i}", "needle" if i else "ordinary", str(path)))
    runs = [("code-reviewer", "subagent", "First task"), ("Agent", "general-purpose", "Second task"),
            ("scout", "subagent", "Never written")]
    for i, (requested, observed, task) in enumerate(runs):
        path = tmp_path / f"agent-{i}.md"
        path.write_text(f"# subagent — 2026-01-01 10:00\nParent: parent\n---\n\n[prompt] 10:00 ──────\n{task}\n\n"
                        f"[agent] 10:01 ──────\n## Findings {i}\n- **Bold** finding\n")
        conn.execute("""INSERT INTO subagent_runs (parent_session_id, requested_agent_type, observed_agent_type,
            task_preview, started_at, transcript_path) VALUES ('parent', ?, ?, ?, ?, ?)""",
                     (requested, observed, task, f"2026-01-01T1{i}:00:00Z", str(path) if i < 2 else None))
    conn.commit()
    delete = Mock(side_effect=AssertionError("Reading must not delete a session"))
    ui = SessionManager(conn, delete)
    ui.filters = replace(ui.filters, project="fixture")
    ui.sort = "oldest"
    ui.reset_position()
    ui.styles = dict.fromkeys(manage_tui.STYLE_ROLES, 0)
    yield ui
    conn.close()


def test_open_copy_side_chat_and_back_preserve_views_and_isolate_actions(browser, monkeypatch):
    ui, screen = browser, Screen()
    ui.draw(screen)
    ui.handle_key(curses.KEY_NPAGE)
    ui.draw(screen)
    before = (ui.index, ui.top, ui.preview_scroll, ui.filters, ui.sort)
    copied = Mock()
    monkeypatch.setattr(manage_tui, "copy_transcript_path", copied)

    ui.handle_key("\n")
    ui.draw(screen)
    text = screen.text()
    assert "Question main" in text and "ARTIFACT_LIST" not in text
    assert ui.reader.path in text.replace("\n", "")  # Absolute and whole, as copied.
    assert "2 side chats" in text
    ui.handle_key("c")
    copied.assert_called_with(ui.session["transcript_path"])
    ui.handle_key(curses.KEY_NPAGE)
    main_scroll = ui.reader.scroll
    assert main_scroll

    ui.handle_key("s")
    ui.handle_key("j")
    ui.handle_key("\n")
    ui.draw(screen)
    assert ui.reader.child["headline"] == "Child 1"
    assert "Question child1" in screen.text() and "From session: Parent conversation" in screen.text()
    ui.handle_key("c")
    copied.assert_called_with(ui.reader.child["transcript_path"])
    # Switching Side Chats replaces the open one, so Esc still lands on the session.
    ui.handle_key("s")
    assert ui.panel_selected == 1
    ui.handle_key("k")
    ui.handle_key("\n")
    assert [reader.child and reader.child["headline"] for reader in ui.readers] == [None, "Child 0"]

    for key in "hd/f":
        ui.handle_key(key)
    assert ui.delete_target is None and ui.panel is None
    assert ui.conn.execute("SELECT hidden_from_recents FROM sessions WHERE session_id='parent'").fetchone()[0] == 0
    ui.handle_key("\x1b")
    assert ui.reader.child is None and ui.reader.scroll == main_scroll
    ui.handle_key("\x1b")
    assert not ui.readers
    assert (ui.index, ui.top, ui.preview_scroll, ui.filters, ui.sort) == before


@pytest.mark.parametrize("columns,rows", [(150, 40), (100, 34), (60, 24)])
def test_all_transcript_content_reachable_and_paragraphs_preserved(browser, columns, rows):
    ui, screen = browser, Screen(columns, rows)
    ui.handle_key("\n")
    seen = set()
    for _ in range(200):
        ui.draw(screen)
        seen.update(screen.text().split())
        if ui.reader.scroll == ui.reader.max:
            break
        ui.handle_key(curses.KEY_NPAGE)
    assert {f"main_{j:03}" for j in range(80)} <= seen
    assert "TAIL_main" in seen
    ui.handle_key(curses.KEY_HOME)
    ui.draw(screen)
    lines = [line.text for line in ui.reader.lines[1]]
    paragraph = lines.index("First paragraph")
    assert lines[paragraph + 1] == ""
    assert lines[paragraph + 2] == "    example_code()"
    # Width changes rewrap, keep the line at the top in place, and keep the end reachable.
    ui.reader.scroll = lines.index("main_040")
    ui.draw(screen)
    screen.columns = 60 if columns > 60 else 150
    ui.draw(screen)
    assert ui.reader.lines[1][ui.reader.scroll].text == "main_040"
    ui.handle_key(curses.KEY_END)
    ui.draw(screen)
    assert "TAIL_main" in screen.text()


def test_missing_transcript_still_copies_path_and_returns(browser, monkeypatch):
    ui, screen = browser, Screen()
    ui.filters = replace(ui.filters, project="other")
    ui.reset_position()
    ui.handle_key("\n")
    ui.draw(screen)
    assert ui.reader.error and "missing" in screen.text().lower()
    assert "side chat" not in screen.text().lower()
    ui.handle_key("s")
    assert ui.panel is None
    copied = Mock(side_effect=OSError("Clipboard unavailable"))
    monkeypatch.setattr(manage_tui, "copy_transcript_path", copied)
    ui.handle_key("c")
    ui.draw(screen)
    assert ui.status_error and "Clipboard unavailable" in screen.text()
    copied.assert_called_once_with(ui.reader.path)
    ui.handle_key("q")
    assert not ui.readers and ui.handle_key("q") is False


def test_side_chat_picker_starts_on_the_search_match(browser):
    ui = browser
    ui.filters = replace(ui.filters, query="needle")
    ui.reset_position()
    assert ui.total == 1
    ui.handle_key("\n")
    ui.handle_key("s")
    assert ui.panel == "side_chats" and ui.panel_selected == 1
    ui.handle_key("\x1b")
    assert ui.panel is None and ui.reader.child is None


def test_search_opens_transcripts_at_the_first_line_matching_every_word(browser):
    ui, screen = browser, Screen()
    ui.filters = replace(ui.filters, query="GIRAFFE zebra")
    ui.reset_position()
    ui.handle_key("\n")
    ui.draw(screen)
    lines = screen.text().splitlines()
    body = lines.index(next(line for line in lines if "main_079" in line))
    assert lines[body + 2].strip() == "Zebra and Giraffe main"  # Context above, every word preferred.
    ui.handle_key("s")
    ui.handle_key("\n")
    ui.draw(screen)
    assert "Zebra and Giraffe child0" in screen.text() and ui.reader.scroll
    ui.handle_key("\x1b")
    assert ui.reader.scroll  # Returning keeps the place the jump chose.


@pytest.mark.parametrize("columns", [150, 100])
def test_substance_band_heads_the_preview_and_the_transcript_but_unknown_is_omitted(browser, columns):
    ui, screen = browser, Screen(columns, 34)
    ui.draw(screen)
    assert screen.text().count("Substantial") == 1
    ui.handle_key("\n")
    ui.draw(screen)
    assert screen.text().splitlines()[0].rstrip().endswith("Substantial")
    ui.handle_key("\x1b")
    ui.filters = replace(ui.filters, project="other")
    ui.reset_position()
    ui.draw(screen)
    assert not any(line.rstrip().endswith("Unknown") for line in screen.text().splitlines())


def test_subagent_runs_open_from_the_transcript_and_return_to_it(browser):
    ui, screen = browser, Screen(100, 34)
    while ui.preview_scroll < ui.preview_max or not ui.preview_max:
        ui.draw(screen)
        ui.handle_key(curses.KEY_NPAGE)
    ui.draw(screen)
    assert "code-reviewer · general-purpose" in screen.text()  # Runs without a transcript are left out.
    ui.handle_key("\n")
    ui.handle_key(curses.KEY_NPAGE)
    scroll = ui.reader.scroll
    ui.draw(screen)
    assert "a 2 subagent runs" in screen.text()

    ui.handle_key("a")
    ui.draw(screen)
    assert ui.panel == "subagent_runs" and "general-purpose · Second task" in screen.text()
    ui.handle_key("j")
    ui.handle_key("\n")
    ui.draw(screen)
    text = screen.text()
    assert "general-purpose · " in text and "From session: Parent conversation" in text
    assert "Second task" in text
    assert "Agent · 10:01" in text and "Findings 1" in text and "- Bold finding" in text
    assert "[agent]" not in text and "**" not in text
    ui.handle_key("a")
    assert ui.panel_selected == 1  # The run being read.
    ui.handle_key("\x1b")
    ui.handle_key("\x1b")
    assert ui.reader.child is None and ui.reader.scroll == scroll
