"""Generate sample PDFs for the tests and for trying Acrimonious Reader out.

    python3 tests/sample.py OUTPUT.pdf
"""

import math
import sys

import cairo

LOREM = (
    "Acrimonious Reader renders every page with Poppler on a worker thread, so scrolling stays "
    "smooth even for heavy documents. The quick brown fox jumps over the lazy dog. Pack my box with five "
    "dozen liquor jugs. Sphinx of black quartz, judge my vow. How vexingly quick daft zebras jump."
)


def _wrap(cr, text, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if cr.text_extents(trial).x_advance > width and line:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line]


def _paragraphs(cr, x, y, width, count):
    cr.set_font_size(11)
    for _ in range(count):
        for line in _wrap(cr, LOREM, width):
            cr.move_to(x, y)
            cr.show_text(line)
            y += 15
        y += 9
    return y


def make_sample(path, chapters=6):
    a4 = (595.28, 841.89)
    surface = cairo.PDFSurface(path, *a4)
    surface.set_metadata(cairo.PDF_METADATA_TITLE, "Acrimonious Reader Sample Document")
    surface.set_metadata(cairo.PDF_METADATA_AUTHOR, "Acrimonious Reader test suite")
    cr = cairo.Context(surface)
    cr.select_font_face("DejaVu Sans")

    # Title page with a table of contents that links to every chapter.
    cr.set_font_size(28)
    cr.move_to(72, 140)
    cr.show_text("Sample Document")
    cr.set_font_size(14)
    cr.move_to(72, 200)
    cr.show_text("Contents")
    cr.set_font_size(12)
    for n in range(1, chapters + 1):
        cr.tag_begin(cairo.TAG_LINK, f"dest='chapter{n}'")
        cr.move_to(90, 220 + 22 * n)
        cr.show_text(f"Chapter {n}: page {n + 1}")
        cr.tag_end(cairo.TAG_LINK)
    cr.tag_begin(cairo.TAG_LINK, "uri='https://www.gnome.org'")
    cr.move_to(72, 420)
    cr.set_source_rgb(0.1, 0.3, 0.8)
    cr.show_text("Visit gnome.org")
    cr.tag_end(cairo.TAG_LINK)
    cr.set_source_rgb(0, 0, 0)
    cr.show_page()

    root = 0
    for n in range(1, chapters + 1):
        landscape = n == 3
        if landscape:
            surface.set_size(a4[1], a4[0])
        else:
            surface.set_size(*a4)
        width = (a4[1] if landscape else a4[0]) - 144
        cr.tag_begin(cairo.TAG_DEST, f"name='chapter{n}'")
        cr.set_font_size(22)
        cr.move_to(72, 100)
        cr.show_text(f"Chapter {n}")
        cr.tag_end(cairo.TAG_DEST)
        chapter_id = surface.add_outline(root, f"Chapter {n}", f"dest='chapter{n}'", 0)
        surface.add_outline(chapter_id, f"Section {n}.1", f"page={n + 1} pos=[72 300]", 0)
        y = _paragraphs(cr, 72, 140, width, 3)
        # A coloured figure, to see rendering and night mode at work.
        cx, cy = 72 + width / 2, y + 120
        for k in range(12):
            cr.set_source_rgb(0.5 + 0.5 * math.cos(k), 0.5 + 0.5 * math.sin(k), 0.6)
            cr.arc(cx + 60 * math.cos(k * math.pi / 6), cy + 60 * math.sin(k * math.pi / 6), 30, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgb(0, 0, 0)
        cr.tag_begin(cairo.TAG_LINK, "dest='chapter1'")
        cr.move_to(72, cy + 140)
        cr.set_font_size(11)
        cr.show_text("Back to chapter 1")
        cr.tag_end(cairo.TAG_LINK)
        cr.show_page()
    surface.finish()


if __name__ == "__main__":
    make_sample(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
