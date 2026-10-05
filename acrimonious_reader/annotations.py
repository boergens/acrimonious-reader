"""Text boxes and freehand ink that the user adds to pages.

Items are immutable: editing one replaces it with a changed copy, which keeps undo simple. They draw
themselves with cairo, and the same drawing code produces both the view on screen and the
appearance saved into the PDF (see pdfwrite.py), so what you see is what other viewers show.

Coordinates are page points with the origin at the top left of the page as Poppler shows it.
"""

import functools
import math
import uuid
from dataclasses import asdict, dataclass, field, replace

import cairo
from gi.repository import GObject, Pango, PangoCairo

FONT_FAMILY = "Liberation Sans,Arial,Helvetica,Sans"
ASCENT = 0.905  # Liberation Sans/Arial: the baseline sits this many font sizes below the top
NAME_PREFIX = "acrimonious-"  # marks annotations this program made (their /NM in the PDF)
UNDO_LIMIT = 200

_FONT_OPTIONS = cairo.FontOptions()
_FONT_OPTIONS.set_hint_metrics(cairo.HINT_METRICS_OFF)  # measure the same on screen and in PDF
_FONT_OPTIONS.set_hint_style(cairo.HINT_STYLE_NONE)


def _new_name():
    return NAME_PREFIX + uuid.uuid4().hex


def _font(size):
    desc = Pango.FontDescription.from_string(FONT_FAMILY)
    desc.set_absolute_size(size * Pango.SCALE)  # in user units, i.e. points
    return desc


def _setup_layout(layout, text, size):
    PangoCairo.context_set_font_options(layout.get_context(), _FONT_OPTIONS)
    layout.context_changed()
    layout.set_font_description(_font(size))
    layout.set_text(text, -1)
    return layout


@functools.lru_cache(maxsize=512)
def _text_extents(text, size):
    """(x1, y1, x2, y2) of text drawn at the origin, covering both the glyphs and the line boxes."""
    context = PangoCairo.FontMap.get_default().create_context()
    layout = _setup_layout(Pango.Layout.new(context), text or " ", size)
    ink, logical = layout.get_extents()
    rects = [(r.x, r.y, r.x + r.width, r.y + r.height) for r in (ink, logical) if r.width or r.height]
    pad = 1.0
    return (min(r[0] for r in rects) / Pango.SCALE - pad, min(r[1] for r in rects) / Pango.SCALE - pad,
            max(r[2] for r in rects) / Pango.SCALE + pad, max(r[3] for r in rects) / Pango.SCALE + pad)


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _stroke_distance(stroke, x, y):
    if len(stroke) == 1:
        return math.hypot(x - stroke[0][0], y - stroke[0][1])
    return min(_segment_distance(x, y, *a, *b) for a, b in zip(stroke, stroke[1:]))


def stroke_path(cr, points):
    """A smooth curve through pointer samples: quadratic segments between their midpoints."""
    cr.move_to(*points[0])
    if len(points) == 1:
        cr.line_to(*points[0])  # with round caps, a dot
        return
    for (cx, cy), (nx, ny) in zip(points[1:-1], points[2:]):
        x0, y0 = cr.get_current_point()
        mx, my = (cx + nx) / 2, (cy + ny) / 2
        cr.curve_to(x0 + 2 / 3 * (cx - x0), y0 + 2 / 3 * (cy - y0),
                    mx + 2 / 3 * (cx - mx), my + 2 / 3 * (cy - my), mx, my)
    cr.line_to(*points[-1])


@dataclass(frozen=True)
class TextBox:
    page: int
    x: float  # top left of the first line
    y: float
    text: str
    size: float = 11.0
    color: tuple = (0.0, 0.0, 0.0)
    name: str = field(default_factory=_new_name)

    kind = "text"

    def bounds(self):
        x1, y1, x2, y2 = _text_extents(self.text, self.size)
        return self.x + x1, self.y + y1, self.x + x2, self.y + y2

    def draw(self, cr):
        cr.save()
        cr.translate(self.x, self.y)
        cr.set_source_rgb(*self.color)
        PangoCairo.show_layout(cr, _setup_layout(PangoCairo.create_layout(cr), self.text, self.size))
        cr.restore()

    def hit(self, x, y, tolerance):
        x1, y1, x2, y2 = self.bounds()
        return x1 - tolerance <= x <= x2 + tolerance and y1 - tolerance <= y <= y2 + tolerance

    def moved(self, dx, dy):
        return replace(self, x=self.x + dx, y=self.y + dy)

    def restyled(self, color=None, size=None):
        return replace(self, color=color or self.color, size=size or self.size)

    def scaled(self, factor, ax, ay):
        """Grown or shrunk around the point (ax, ay): the text size scales with it."""
        return replace(self, x=ax + (self.x - ax) * factor, y=ay + (self.y - ay) * factor,
                       size=max(self.size * factor, 2.0))


@dataclass(frozen=True)
class Ink:
    page: int
    strokes: tuple  # of tuples of (x, y) points
    color: tuple = (0.0, 0.0, 0.0)
    width: float = 1.5
    name: str = field(default_factory=_new_name)

    kind = "ink"

    def bounds(self):
        xs = [x for stroke in self.strokes for x, _ in stroke]
        ys = [y for stroke in self.strokes for _, y in stroke]
        pad = self.width / 2 + 1
        return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad

    def draw(self, cr):
        cr.save()
        cr.set_source_rgb(*self.color)
        cr.set_line_width(self.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for stroke in self.strokes:
            stroke_path(cr, stroke)
            cr.stroke()
        cr.restore()

    def stroke_at(self, x, y, tolerance):
        """Index of a stroke passing within tolerance of (x, y), or None."""
        for i, stroke in enumerate(self.strokes):
            if _stroke_distance(stroke, x, y) <= self.width / 2 + tolerance:
                return i
        return None

    def hit(self, x, y, tolerance):
        return self.stroke_at(x, y, tolerance) is not None

    def moved(self, dx, dy):
        return replace(self, strokes=tuple(tuple((x + dx, y + dy) for x, y in s) for s in self.strokes))

    def restyled(self, color=None, width=None):
        return replace(self, color=color or self.color, width=width or self.width)

    def scaled(self, factor, ax, ay):
        """Grown or shrunk around the point (ax, ay), line width included."""
        strokes = tuple(tuple((ax + (x - ax) * factor, ay + (y - ay) * factor) for x, y in stroke)
                        for stroke in self.strokes)
        return replace(self, strokes=strokes, width=max(self.width * factor, 0.2))


def item_to_json(item):
    data = asdict(item)
    data["kind"] = item.kind
    return data


def item_from_json(data, page):
    common = {"page": page, "color": tuple(data["color"]), "name": data["name"]}
    if data["kind"] == "text":
        return TextBox(x=data["x"], y=data["y"], text=data["text"], size=data["size"], **common)
    strokes = tuple(tuple((x, y) for x, y in stroke) for stroke in data["strokes"])
    return Ink(strokes=strokes, width=data["width"], **common)


class Annotations(GObject.Object):
    """The user's annotations of one document, with undo and a record of what was last saved."""

    __gtype_name__ = "AcrimoniousAnnotations"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, items=()):
        super().__init__()
        self._items = tuple(items)
        self._saved = self._items
        self._undo, self._redo = [], []

    @property
    def items(self):
        return self._items

    @property
    def modified(self):
        return self._items != self._saved

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def on_page(self, page):
        return [item for item in self._items if item.page == page]

    def find(self, name):
        return next((item for item in self._items if item.name == name), None)

    def item_at(self, page, x, y, tolerance):
        """The topmost item on a page at (x, y)."""
        for item in reversed(self._items):
            if item.page == page and item.hit(x, y, tolerance):
                return item
        return None

    def add(self, item):
        self._change(self._items + (item,))

    def replace(self, item):
        self._change(tuple(item if old.name == item.name else old for old in self._items))

    def remove(self, name):
        self._change(tuple(item for item in self._items if item.name != name))

    def checkpoint(self):
        """Start an undo step made of several apply() calls (e.g. one sweep of the eraser)."""
        self._undo.append(self._items)
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def apply(self, items):
        self._items = tuple(items)
        self.emit("changed")

    def undo(self):
        if self._undo:
            self._redo.append(self._items)
            self.apply(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._items)
            self.apply(self._redo.pop())

    def mark_saved(self, items):
        self._saved = tuple(items)
        self.emit("changed")

    def _change(self, items):
        self.checkpoint()
        self.apply(items)
