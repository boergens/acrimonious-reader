"""Text selection on top of Poppler's text layout: lines, hit tests and highlight rectangles.

Positions in the text are cursor indices: 0 is before the first character, len(text) after the last.
All coordinates are page points with the origin at the top left of the unrotated page.
"""

import re
from dataclasses import dataclass

END = 1 << 62  # a cursor index past the end of any page's text
MIN_FORM_SIZE, MAX_FORM_SIZE = 4.0, 72.0


@dataclass(frozen=True)
class FormLine:
    """A run of underscores meant to be written on, as in 'Name: ________'."""

    x1: float
    y1: float
    x2: float
    y2: float
    size: float  # the underscores' font size in points, so that writing can match it
    start: int  # cursor indices of the run
    end: int


def find_form_lines(text, rects, attributes=()):
    """The form lines in a page's text: two or more underscores standing on their own, so that
    the ones inside words like `__init__` do not count. `attributes` gives Poppler's font sizes as
    (first index, last index, size) runs; without them the size is estimated from the line height."""
    lines = []
    for match in re.finditer(r"_{2,}", text):
        start, end = match.span()
        if (start > 0 and text[start - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
            continue
        box = rects[start:end]
        if len(box) < end - start:
            continue
        y1, y2 = min(r[1] for r in box), max(r[3] for r in box)
        size = next((size for first, last, size in attributes if first <= start <= last), None)
        if size is None:
            size = (y2 - y1) * 0.9  # the line box is about 1.1 font sizes tall in common fonts
        size = min(max(round(size * 2) / 2, MIN_FORM_SIZE), MAX_FORM_SIZE)
        lines.append(FormLine(min(r[0] for r in box), y1, max(r[2] for r in box), y2, size, start, end))
    return lines


def _is_word(ch):
    return ch.isalnum() or ch == "_"


class PageText:
    """The text of one page with the rectangle of every character."""

    def __init__(self, text, rects, attributes=()):
        n = min(len(text), len(rects))
        self.text = text[:n]
        self.rects = rects[:n]
        self.form_lines = find_form_lines(self.text, self.rects, attributes)
        self.lines = []  # (x1, y1, x2, y2, start, end) with end exclusive
        start = 0
        for i in range(n + 1):
            if i == n or self.text[i] == "\n":
                if i > start:
                    box = self.rects[start:i]
                    self.lines.append((
                        min(r[0] for r in box),
                        min(r[1] for r in box),
                        max(r[2] for r in box),
                        max(r[3] for r in box),
                        start,
                        i,
                    ))
                start = i + 1

    def is_over_text(self, x, y, pad=2.0):
        return any(
            x1 - pad <= x <= x2 + pad and y1 - pad <= y <= y2 + pad
            for x1, y1, x2, y2, _, _ in self.lines
        )

    def index_at(self, x, y):
        """The cursor index nearest to a point."""
        if not self.lines:
            return 0

        def distance(line):
            x1, y1, x2, y2 = line[:4]
            dy = 0 if y1 <= y <= y2 else min(abs(y - y1), abs(y - y2))
            dx = 0 if x1 <= x <= x2 else min(abs(x - x1), abs(x - x2))
            return dy, dx

        line = min(self.lines, key=distance)
        for i in range(line[4], line[5]):
            x1, _, x2, _ = self.rects[i]
            if x < (x1 + x2) / 2:
                return i
        return line[5]

    def word_range(self, i):
        """The word at cursor index i, or the single character there if it is not a word."""
        text, n = self.text, len(self.text)
        if n == 0:
            return 0, 0
        j = min(i, n - 1)
        if not _is_word(text[j]) and j > 0 and _is_word(text[j - 1]):
            j -= 1  # the cursor sits just after a word
        if not _is_word(text[j]):
            return j, j + 1
        start, end = j, j + 1
        while start > 0 and _is_word(text[start - 1]):
            start -= 1
        while end < n and _is_word(text[end]):
            end += 1
        return start, end

    def line_range(self, i):
        for line in self.lines:
            if line[4] <= i <= line[5]:
                return line[4], line[5]
        return i, i

    def highlight(self, start, end):
        """One rectangle per line for the text between two cursor indices."""
        rects = []
        for _, _, _, _, line_start, line_end in self.lines:
            a, b = max(start, line_start), min(end, line_end)
            if a < b:
                chars = self.rects[a:b]
                rects.append((
                    min(r[0] for r in chars),
                    min(r[1] for r in chars),
                    max(r[2] for r in chars),
                    max(r[3] for r in chars),
                ))
        return rects


@dataclass(frozen=True)
class Selection:
    anchor: tuple  # (page, index) where the selection started
    focus: tuple  # (page, index) where it ends; may come before the anchor
    mode: str = "char"  # "char", "word" or "line", from a single, double or triple click

    def with_focus(self, focus):
        return Selection(self.anchor, focus, self.mode)

    @property
    def is_empty(self):
        return self.mode == "char" and self.anchor == self.focus
