"""Exercise curses output through a real terminal, including terminfo REP."""

from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import uuid

import pytest


@pytest.mark.parametrize("columns,rows", [(80, 28), (150, 42)])
def test_confirmation_borders_with_repeat_capable_terminal(tmp_path, columns, rows):
    if not shutil.which("tmux") or not shutil.which("tic"):
        pytest.skip("terminal rendering regression requires tmux and tic")

    # macOS curses mishandles repeated Unicode glyphs when this capability is
    # advertised (as it is by xterm-ghostty). Avoid depending on a Ghostty install.
    term = "session-index-rep"
    source = tmp_path / "terminal.src"
    source.write_text(
        f"{term}|Repeat-capable terminal fixture,\n"
        "\trep=%p1%c\\E[%p2%{1}%-%db,\n\tuse=xterm-256color,\n"
    )
    subprocess.run(["tic", "-x", "-o", str(tmp_path / "terminfo"), str(source)], check=True)
    socket = f"session-index-render-{uuid.uuid4().hex}"
    base = ["tmux", "-L", socket]

    def tmux(*args):
        return subprocess.check_output([*base, *args], text=True, timeout=10)

    repo = str(Path(__file__).resolve().parents[1])
    script = tmp_path / "render.py"
    script.write_text(textwrap.dedent(f"""
        import curses, locale, sqlite3, subprocess, sys
        sys.path.insert(0, {repo!r})
        from db import init_db, upsert_session
        from manage_tui import SessionManager
        locale.setlocale(locale.LC_ALL, '')
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for i in range(20):
            upsert_session(conn, session_id=f'fixture-{{i}}', project='fixture',
                           headline='Background session content must not leak into the border',
                           summary='Disposable terminal rendering fixture.')
        ui = SessionManager(conn, lambda *_: None)
        def run(screen):
            curses.curs_set(0)
            screen.keypad(True)
            ui.configure_colors()
            while True:
                ui.draw(screen)
                subprocess.run({base!r} + ['wait-for', '-S', 'painted'], check=True)
                if not ui.handle_key(screen.get_wch()):
                    break
        curses.wrapper(run)
    """))
    # Pass the fixture environment directly, without relying on shell quoting.
    try:
        subprocess.run([*base, "new-session", "-d", "-s", "fixture", "-x", str(columns), "-y", str(rows),
                        "env", f"TERM={term}", f"TERMINFO={tmp_path / 'terminfo'}",
                        sys.executable, str(script)], check=True)
        tmux("wait-for", "painted")
        tmux("send-keys", "-t", "fixture", "d")
        tmux("wait-for", "painted")
        rendered = tmux("capture-pane", "-p", "-t", "fixture")
        border_width = min(76, columns - 6)
        lines = [line.strip() for line in rendered.splitlines()]
        # Require uninterrupted borders at their full terminal-cell width, with
        # no old list text or divider left on either row. Accept either glyph.
        for left, right in (("╭", "╮"), ("╰", "╯")):
            borders = [line for line in lines if line.startswith(left)]
            assert len(borders) == 1, rendered
            border = borders[0]
            assert len(border) == border_width and border.endswith(right), rendered
            assert set(border[1:-1]) <= {"─", "-"}, rendered
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True)
