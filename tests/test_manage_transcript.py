"""Clean Transcripts render as role-separated turns with Pi-style Markdown."""

import pytest

from manage_transcript import cells, render_markdown, render_transcript

TRANSCRIPT = """[user] 10:00:00 ──────
Use **this** exactly
[question] Scope
[answer] All of it

[assistant] 10:00:05 ──────
## Plan
Read *the* `snake_case_name` docs at [Pi](https://pi.dev), then 2 * 3 * 4.
1. A first step long enough to wrap onto a second row under its own text
   - nested **bold**
> quoted
---
| Name | Value |
|------|------:|
| alpha | a long value that has to wrap inside a narrow table cell |

```python
x = **not_bold**  # `kept`
```
→ bash: ls **not markdown**
[subagent] general-purpose ──────
Research it
→ see: agent-a1.md

[assistant] 10:01:00 ──────
Done.
"""


def roles(rows, text):
    return {role for row in rows for span, role in row.spans if span == text}


@pytest.mark.parametrize("width", [20, 33, 60, 150])
def test_every_row_fits_and_every_source_line_maps_to_its_rows(width):
    rows, starts = render_transcript(TRANSCRIPT, width)
    assert len(starts) == len(TRANSCRIPT.splitlines())
    assert starts == sorted(starts) and starts[-1] <= len(rows)
    assert all(cells(row.text) <= width for row in rows)


def test_turns_are_banded_or_rendered_as_markdown():
    rows, starts = render_transcript(TRANSCRIPT, 80)
    texts = [row.text for row in rows]
    # The user's turn is a band kept as typed, ending before the blank separator.
    user = [row for row in rows if row.fill == "band"]
    assert [row.text for row in user] == ["▌ You · 10:00:00", "▌ Use **this** exactly",
                                          "▌ [question] Scope", "▌ [answer] All of it"]
    assert rows[len(user)].fill == ""
    # Consecutive assistant messages read as one turn under one header.
    assert texts.count("Assistant · 10:00:05") == 1 and not any("10:01:00" in text for text in texts)
    assert roles(rows, "Plan") == {"heading"}
    prose = next(text for text in texts if text.startswith("Read"))
    assert prose == "Read the snake_case_name docs at Pi (https://pi.dev), then 2 * 3 * 4."
    assert roles(rows, "the") == {"italic"} and roles(rows, "snake_case_name") == {"code"}
    assert roles(rows, "Pi") == {"link"}
    assert "   - nested bold" in texts and roles(rows, "bold") == {"bold"}
    assert "│ quoted" in texts and roles(rows, "quoted") == {"quote"}
    # Code is verbatim; tool calls and Subagent Run pointers stay muted, not Markdown.
    assert "  x = **not_bold**  # `kept`" in texts
    assert roles(rows, "→ bash: ls **not markdown**") == {"muted"}
    assert roles(rows, "Subagent run · general-purpose") == {"muted"}
    assert any(text.startswith("┌") for text in texts) and any(text.startswith("└") for text in texts)


def test_unclosed_markers_and_narrow_tables_stay_readable():
    assert render_markdown(["a **b and `c"], 40)[0][0].text == "a **b and `c"
    # A space at the row's edge breaks the row instead of vanishing between two words.
    assert [row.text for row in render_markdown(["bounds and a definition"], 10)[0]] == ["bounds and", "a", "definition"]
    table = ["| A | B | C |", "|---|---|---|", "| 1 | 2 | 3 |"]
    assert render_markdown(table, 12)[0][0].text.startswith("| A | B |")  # No room for boxes: as written.
    assert render_markdown(table, 13)[0][0].text == "┌---┬---┬---┐"
