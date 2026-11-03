"""Clean Transcript text as styled terminal rows for the session browser's reader.

User turns render as a shaded band, so a turn boundary is visible at a glance
even inside long Markdown answers. Assistant turns render Markdown the way Pi's
renderer does (pi-tui `components/markdown.js`): headings, lists, quotes,
fenced code, tables, rules, and inline emphasis, code and links. Rendering is
line by line, so every transcript line maps to the rows it produced; search
jumps and resize anchoring rely on that mapping.

Rows carry role names, never terminal attributes; the browser maps each role
to a style, so this module stays free of curses and easy to test.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

Span = tuple[str, str]  # (text, role); a role may add modifiers: "heading underline"

# Message headers the transcript renderers write, e.g. "[user] 12:28:37 ────";
# Subagent Run transcripts use [prompt]/[agent]/[error] and HH:MM. Prose that
# merely starts with "[user]" is content.
ROLE_DIVIDER = re.compile(r"\[(prompt|user|assistant|agent|error)\] (?:(\d{2}:\d{2}(?::\d{2})?) )?─+")
# A Claude assistant message's pointer to a Subagent Run it started.
SUBAGENT_MARKER = re.compile(r"\[subagent\] (\S+) ─+")
ROLE_LABELS = {"user": "You", "prompt": "Prompt", "assistant": "Assistant", "agent": "Agent", "error": "Tool error"}
# What the person (or the parent agent) wrote: a band, kept as typed, never Markdown.
BANDED = ("user", "prompt")
BAND_BAR = "▌"
# Horizontal strokes are ASCII, as in the browser's frames: macOS curses corrupts
# runs of a repeated Unicode glyph on terminals that advertise REP (Ghostty).
STROKE = "-"
QUESTION_TAG = re.compile(r"\[(?:question|answer)\] ")


class Line(NamedTuple):
    spans: tuple[Span, ...] = ()
    fill: str = ""  # Role painted across the full width behind the spans.

    @property
    def text(self) -> str:
        return "".join(text for text, _ in self.spans)


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


def merged(spans) -> tuple[Span, ...]:
    result: list[Span] = []
    for text, role in spans:
        if not text:
            continue
        if result and result[-1][1] == role:
            result[-1] = (result[-1][0] + text, role)
        else:
            result.append((text, role))
    return tuple(result)


def wrap(spans, width: int, prefix=(), hang=None) -> list[tuple[Span, ...]]:
    """Word-wrap styled spans into rows of at most `width` cells.

    `prefix` starts the first row and `hang` every later one (default: the
    prefix again, as for quote bars). Words longer than a row break by cells.
    """
    hang = prefix if hang is None else hang
    if sum(cells(text) for text, _ in prefix) >= width:
        prefix = ()
    if sum(cells(text) for text, _ in hang) >= width:
        hang = ()
    rows: list[tuple[Span, ...]] = []
    row, used, content = list(prefix), sum(cells(text) for text, _ in prefix), False

    def finish():
        while content and row and row[-1][0].isspace():
            row.pop()
        rows.append(merged(row))

    for text, role in spans:
        for piece in re.findall(r"\s+|\S+", text):
            size = cells(piece)
            if piece.isspace():
                if content and used + size < width:
                    row.append((piece, role))
                    used += size
                elif content:  # A space at the edge ends the row, so the next word cannot touch the last.
                    finish()
                    row, used, content = list(hang), sum(cells(t) for t, _ in hang), False
                continue
            if content and used + size > width:
                finish()
                row, used, content = list(hang), sum(cells(t) for t, _ in hang), False
            while used + cells(piece) > width:
                part = clipped(piece, width - used) or piece[0]
                row.append((part, role))
                content = True
                finish()
                row, used, content = list(hang), sum(cells(t) for t, _ in hang), False
                piece = piece[len(part):]
            if piece:
                row.append((piece, role))
                used += cells(piece)
                content = True
    finish()
    return rows


# --- Inline Markdown -------------------------------------------------------

INLINE = re.compile(r"""
    (?P<ticks>`+)(?P<code>.+?)(?<!`)(?P=ticks)(?!`)
  | \\(?P<escape>[!-/:-@\[-`{-~])
  | <(?P<autolink>https?://[^>\s]+)>
  | !?\[(?P<label>[^\]]+)\]\((?P<href>[^)\s]+)(?:\s+"[^"]*")?\)
  | \*\*(?P<bold>[^\s*](?:.*?[^\s])?)\*\*
  | (?<![\w])__(?P<bold2>\S(?:.*?\S)?)__(?![\w])
  | \*(?P<italic>[^\s*](?:.*?[^\s*])?)\*
  | (?<![\w])_(?P<italic2>[^\s_](?:.*?[^\s_])?)_(?![\w])
  | (?P<url>https?://[^\s<>]*[^\s<>.,;:!?'")\]])
""", re.VERBOSE)


def with_role(base: str, extra: str) -> str:
    return f"{base} {extra}".strip()


def inline(text: str, base: str = "") -> list[Span]:
    """Emphasis, code, links and escapes; unmatched markers stay literal text."""
    spans: list[Span] = []
    position = 0
    for found in INLINE.finditer(text):
        spans.append((text[position:found.start()], base))
        position = found.end()
        group = found.groupdict()
        if group["code"] is not None:
            code = group["code"]
            if len(code) > 2 and code.startswith(" ") and code.endswith(" "):
                code = code[1:-1]
            spans.append((code, with_role(base, "code")))
        elif group["escape"] is not None:
            spans.append((group["escape"], base))
        elif group["autolink"] or group["url"]:
            spans.append((group["autolink"] or group["url"], with_role(base, "link")))
        elif group["label"] is not None:
            spans += inline(group["label"], with_role(base, "link"))
            href = group["href"]
            if group["label"] not in (href, href.removeprefix("mailto:")):
                spans.append((f" ({href})", "muted"))
        elif (bold := group["bold"] or group["bold2"]) is not None:
            spans += inline(bold, with_role(base, "bold"))
        else:
            spans += inline(group["italic"] or group["italic2"], with_role(base, "italic"))
    spans.append((text[position:], base))
    return [span for span in spans if span[0]]


# --- Block Markdown --------------------------------------------------------

FENCE = re.compile(r"^( *)(`{3,}|~{3,})\s*([^`\s]*)")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
QUOTE = re.compile(r"^ {0,3}((?:>[ \t]?)+)(.*)$")
ITEM = re.compile(r"^( *)([-+*]|\d{1,9}[.)])(?:[ \t]+(\[[ xX]\][ \t]+)?(.*))?$")
TABLE_DELIMITER = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
# Kinds that sit together without a blank row; Pi spaces every other change of block.
FLOWING = {"paragraph", "item"}


def table_start(lines: list[str], i: int) -> bool:
    return i + 1 < len(lines) and "|" in lines[i] + lines[i + 1] and bool(TABLE_DELIMITER.match(lines[i + 1]))


def table_cells(line: str) -> list[str]:
    line = line.strip()
    line = line[1:] if line.startswith("|") else line
    line = line[:-1] if line.endswith("|") and not line.endswith("\\|") else line
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def render_table(source: list[str], width: int) -> list[list[Line]]:
    """Pi's box table: natural column widths, shrunk and wrapped to fit."""
    header = table_cells(source[0])
    count = len(header)
    rows = [(table_cells(line) + [""] * count)[:count] for line in source[2:]]
    overhead = 3 * count + 1
    available = width - overhead
    if available < count:  # Too narrow for a stable table: the Markdown as written.
        return [[Line(row) for row in wrap([(line.strip(), "")], width)] for line in source]
    rendered = [[inline(cell, "bold") for cell in header]] + [[inline(cell) for cell in row] for row in rows]
    natural, longest = [0] * count, [1] * count
    for row in rendered:
        for i, spans in enumerate(row):
            text = "".join(t for t, _ in spans)
            natural[i] = max(natural[i], cells(text))
            longest[i] = max(longest[i], min(30, max((cells(word) for word in text.split()), default=0)))
    minimum = longest
    if sum(minimum) > available:
        minimum = [1] * count
        remaining = available - count
        weights = [w - 1 for w in longest]
        total = sum(weights)
        for i in range(count):
            minimum[i] += remaining * weights[i] // total if total and remaining > 0 else 0
        for i in range(max(0, available - sum(minimum))):
            minimum[i % count] += 1
    if sum(natural) + overhead <= width:
        widths = [max(n, m) for n, m in zip(natural, minimum)]
    else:
        potential = sum(max(0, n - m) for n, m in zip(natural, minimum))
        extra = max(0, available - sum(minimum))
        widths = [m + (max(0, n - m) * extra // potential if potential else 0) for n, m in zip(natural, minimum)]
        remaining = available - sum(widths)
        while remaining > 0:
            grown = False
            for i in range(count):
                if remaining and widths[i] < natural[i]:
                    widths[i] += 1
                    remaining -= 1
                    grown = True
            if not grown:
                break

    def border(left, middle, right):
        return Line(((left + middle.join(STROKE * w for w in widths) + right, "border"),))

    def row_lines(row) -> list[Line]:
        wrapped = [wrap(spans, w) or [()] for spans, w in zip(row, widths)]
        result = []
        for index in range(max(len(cell) for cell in wrapped)):
            spans: list[Span] = [("│ ", "border")]
            for i, cell in enumerate(wrapped):
                part = cell[index] if index < len(cell) else ()
                spans += [*part, (" " * (widths[i] - sum(cells(t) for t, _ in part)), "")]
                spans.append((" │ " if i < count - 1 else " │", "border"))
            result.append(Line(merged(spans)))
        return result

    separator = border("├" + STROKE, STROKE + "┼" + STROKE, STROKE + "┤")
    bottom = border("└" + STROKE, STROKE + "┴" + STROKE, STROKE + "┘")
    output = [[border("┌" + STROKE, STROKE + "┬" + STROKE, STROKE + "┐"), *row_lines(rendered[0])], [separator]]
    for index, row in enumerate(rendered[1:]):
        last = index == len(rows) - 1
        output.append(row_lines(row) + [bottom if last else separator])
    if not rows:
        output[-1] = [bottom]
    return output


def render_markdown(source: list[str], width: int) -> list[list[Line]]:
    """One list of rows per source line, so callers can map lines to rows."""
    output: list[list[Line]] = []
    previous = "blank"
    fence = None  # (indent, marker) while inside a fenced code block
    i = 0
    while i < len(source):
        line = source[i].replace("\t", "   ")
        if fence:
            indent, marker = fence
            if line.strip().startswith(marker[0] * len(marker)) and not line.strip().strip(marker[0]):
                output.append([Line(((indent + "```", "border"),))])
                fence, previous = None, "code"
            else:
                code = line[len(indent):] if line.startswith(indent) else line.lstrip()
                pad = indent + "  " + code[:len(code) - len(code.lstrip())]
                rows = wrap([(code.strip(), "")], width, ((pad, ""),)) if code.strip() else [()]
                output.append([Line(row) for row in rows])
            i += 1
            continue
        kind, rows = "paragraph", []
        if not line.strip():
            kind, rows = "blank", [()]
        elif opening := FENCE.match(line):
            kind, fence = "code", (opening.group(1), opening.group(2))
            rows = wrap([("```" + opening.group(3), "border")], width, ((opening.group(1), ""),))
        elif table_start(source, i):
            end = i + 2
            while end < len(source) and source[end].strip() and "|" in source[end]:
                end += 1
            table = render_table(source[i:end], width)
            if previous not in ("blank", "table"):
                table[0].insert(0, Line())
            output += table
            previous, i = "table", end
            continue
        elif heading := HEADING.match(line):
            kind, level = "heading", len(heading.group(1))
            role = "heading underline" if level == 1 else "heading"
            marker = [(heading.group(1) + " ", role)] if level >= 3 else []
            rows = wrap(marker + inline(heading.group(2), role), width)
        elif RULE.match(line):
            kind, rows = "rule", [((STROKE * min(width, 80), "border"),)]
        elif quote := QUOTE.match(line):
            bars = ("│ " * quote.group(1).count(">"), "border")
            kind, rows = "quote", wrap(inline(quote.group(2), "quote"), width, (bars,))
        elif item := ITEM.match(line):
            indent = item.group(1)[:max(0, width // 2)]
            bullet = "- " if item.group(2) in "-+*" else item.group(2) + " "
            if item.group(3):
                bullet += "[x] " if "x" in item.group(3).lower() else "[ ] "
            hang = ((" " * (len(indent) + cells(bullet)), ""),)
            kind = "item"
            rows = wrap(inline(item.group(4) or ""), width, ((indent, ""), (bullet, "bullet")), hang)
        else:
            indent = line[:len(line) - len(line.lstrip(" "))]
            pad = ((indent, ""),) if len(indent) < width // 2 else ()
            rows = wrap(inline(line.strip()), width, pad)
        lines = [Line(row) for row in rows]
        if "blank" not in (kind, previous) and kind != previous and not {kind, previous} <= FLOWING:
            lines.insert(0, Line())
        output.append(lines)
        previous = kind
        i += 1
    return output


# --- Transcript layout -----------------------------------------------------

def band_rows(line: str, width: int) -> list[Line]:
    """A line of a user turn: kept as typed, behind the band's bar."""
    line = line.replace("\t", "    ")
    if not line.strip():
        return [Line(((BAND_BAR, "band_muted"),), "band")]
    indent = line[:len(line) - len(line.lstrip(" "))]
    bar = ((BAND_BAR + " " + (indent if len(indent) < width // 2 else ""), "band_muted"),)
    text = line.strip()
    tag = QUESTION_TAG.match(text)
    spans = [(tag.group(), "band_muted"), (text[tag.end():], "band")] if tag else [(text, "band")]
    return [Line(row, "band") for row in wrap(spans, width, bar)]


def plain_rows(line: str, width: int, role: str = "") -> list[Line]:
    if not line.strip():
        return [Line()]
    indent = line[:len(line) - len(line.lstrip(" "))]
    pad = ((indent, ""),) if len(indent) < width // 2 else ()
    return [Line(row) for row in wrap([(line.strip(), role)], width, pad)]


def assistant_rows(body: list[str], width: int) -> list[list[Line]]:
    """Markdown, except tool-call lines and Subagent Run pointers, which stay muted."""
    output: list[list[Line]] = []
    run: list[str] = []
    for line in body + [None]:
        marker = SUBAGENT_MARKER.fullmatch(line) if line is not None else None
        if line is not None and not marker and not line.startswith("→ "):
            run.append(line)
            continue
        output += render_markdown(run, width)
        run = []
        if marker:
            output.append(plain_rows("Subagent run · " + marker.group(1), width, "muted"))
        elif line is not None:
            output.append(plain_rows(line, width, "muted"))
    return output


def render_transcript(text: str, width: int) -> tuple[list[Line], list[int]]:
    """Rows for a transcript, and the index of each source line's first row."""
    source = text.splitlines()
    rows: list[Line] = []
    starts: list[int] = []
    previous = None
    i = 0
    while i < len(source):
        divider = ROLE_DIVIDER.fullmatch(source[i])
        if not divider:  # Text before the first message: shown as written.
            starts.append(len(rows))
            rows += plain_rows(source[i], width)
            i += 1
            continue
        role, time = divider.groups()
        end = next((j for j in range(i + 1, len(source)) if ROLE_DIVIDER.fullmatch(source[j])), len(source))
        body = source[i + 1:end]
        label = ROLE_LABELS[role] + (f" · {time}" if time else "")
        per_line: list[list[Line]]
        if role in BANDED:
            last = max((j for j, line in enumerate(body) if line.strip()), default=-1)
            per_line = [[Line(row, "band") for row in wrap([(label, "band_muted")], width, ((BAND_BAR + " ", "band_muted"),))]]
            per_line += [band_rows(line, width) if j <= last else [Line()] for j, line in enumerate(body)]
        elif role == "error":  # A failed tool's output: verbatim and muted, never Markdown.
            per_line = [plain_rows(label, width, "muted")] + [plain_rows(line, width, "muted") for line in body]
        else:
            # Consecutive assistant messages (split by tool calls) read as one turn.
            header = [] if previous == role else plain_rows(label, width, "muted")
            per_line = [header] + assistant_rows(body, width)
        for lines in per_line:
            starts.append(len(rows))
            rows += lines
        previous, i = role, end
    return rows, starts
