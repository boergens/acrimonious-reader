from acrimonious_reader.selection import END, PageText, Selection


def make_text():
    # Two lines of 10-point-wide characters: "hello world" at y 10..20, "foo bar" at y 30..40.
    lines = [("hello world", 10), ("foo bar", 30)]
    text, rects = "", []
    for i, (line, y) in enumerate(lines):
        for n, _ in enumerate(line):
            rects.append((10 + 10 * n, y, 20 + 10 * n, y + 10))
        text += line
        if i < len(lines) - 1:
            text += "\n"
            rects.append(rects[-1])  # Poppler gives the newline a rectangle too
    return PageText(text, rects)


def test_lines():
    page = make_text()
    assert [(start, end) for *_, start, end in page.lines] == [(0, 11), (12, 19)]
    assert page.lines[0][:4] == (10, 10, 120, 20)


def test_index_at():
    page = make_text()
    assert page.index_at(11, 15) == 0  # left half of "h"
    assert page.index_at(19, 15) == 1  # right half of "h"
    assert page.index_at(500, 15) == 11  # past the end of the line
    assert page.index_at(12, 34) == 12  # start of the second line
    assert page.index_at(12, 200) == 12  # below everything: nearest line
    assert PageText("", []).index_at(5, 5) == 0


def test_word_and_line_ranges():
    page = make_text()
    assert page.word_range(2) == (0, 5)  # inside "hello"
    assert page.word_range(5) == (0, 5)  # just after "hello"
    assert page.word_range(8) == (6, 11)  # inside "world"
    assert page.line_range(14) == (12, 19)


def test_highlight_spans_lines():
    page = make_text()
    assert page.highlight(6, 15) == [(70, 10, 120, 20), (10, 30, 40, 40)]
    assert page.highlight(0, END) == [(10, 10, 120, 20), (10, 30, 80, 40)]
    assert page.highlight(3, 3) == []


def test_selection_emptiness():
    assert Selection((0, 3), (0, 3)).is_empty
    assert not Selection((0, 3), (0, 3), "word").is_empty
    assert Selection((0, 3), (0, 3)).with_focus((1, 0)).focus == (1, 0)
