"""Saved signatures: drawn once on a signing pad (or taken from a drawing on a page), kept between
sessions, and placed on documents with one click.

They are stored as strokes, not pictures, in $XDG_DATA_HOME/acrimonious-reader/signatures.json,
readable only by the user.
"""

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cairo
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from .annotations import Ink, stroke_path

PAD_WIDTH, PAD_HEIGHT = 480, 180  # pixels
PAD_SCALE = 0.35  # points per pad pixel: a signature filling the pad comes out about 2.3 × 0.9 in
PAD_BASELINE = 0.72  # where the guide line sits, as a fraction of the pad's height


def _new_name():
    return "signature-" + uuid.uuid4().hex


@dataclass(frozen=True)
class Signature:
    strokes: tuple  # tuples of (x, y) points, in points, with the top left of the strokes at (0, 0)
    baseline: float  # points below the top: where the signature sits on a line
    width: float = 1.5
    color: tuple = (0.0, 0.0, 0.0)
    name: str = field(default_factory=_new_name)

    @classmethod
    def from_strokes(cls, strokes, baseline, width, color):
        """A signature from strokes anywhere: they are moved so that their top left is at (0, 0)."""
        x0 = min(x for stroke in strokes for x, _ in stroke)
        y0 = min(y for stroke in strokes for _, y in stroke)
        moved = tuple(tuple((round(x - x0, 2), round(y - y0, 2)) for x, y in stroke) for stroke in strokes)
        return cls(moved, max(baseline - y0, 0.0), width, tuple(color))

    @classmethod
    def from_ink(cls, ink):
        """A signature from a drawing on a page; its baseline is a guess, three quarters down."""
        y0 = min(y for stroke in ink.strokes for _, y in stroke)
        y1 = max(y for stroke in ink.strokes for _, y in stroke)
        return cls.from_strokes(ink.strokes, y0 + 0.75 * (y1 - y0), ink.width, ink.color)

    @property
    def size(self):
        return (max(x for stroke in self.strokes for x, _ in stroke),
                max(y for stroke in self.strokes for _, y in stroke))

    def place(self, page, x, y):
        """An ink annotation with this signature's baseline starting at (x, y) on a page."""
        strokes = tuple(tuple((px + x, py + y - self.baseline) for px, py in stroke) for stroke in self.strokes)
        return Ink(page, strokes, self.color, self.width)

    def to_json(self):
        return {"name": self.name, "strokes": self.strokes, "baseline": self.baseline,
                "width": self.width, "color": self.color}

    @classmethod
    def from_json(cls, data):
        strokes = tuple(tuple((x, y) for x, y in stroke) for stroke in data["strokes"])
        return cls(strokes, data["baseline"], data["width"], tuple(data["color"]), data["name"])


class SignatureStore(GObject.Object):
    """The user's saved signatures, shared by all windows."""

    __gtype_name__ = "AcrimoniousSignatureStore"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path or Path(GLib.get_user_data_dir()) / "acrimonious-reader" / "signatures.json")
        try:
            self._signatures = [Signature.from_json(data) for data in json.loads(self.path.read_text())]
        except (OSError, ValueError, KeyError, TypeError):
            self._signatures = []

    @property
    def signatures(self):
        return tuple(self._signatures)

    def add(self, signature):
        self._signatures.append(signature)
        self._save()

    def remove(self, name):
        self._signatures = [s for s in self._signatures if s.name != name]
        self._save()

    def _save(self):
        """Write the file atomically and readable only by the user: a signature is sensitive."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".signatures-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:  # mkstemp creates the file with mode 0600
                json.dump([s.to_json() for s in self._signatures], f)
            os.replace(temporary, self.path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        self.emit("changed")


def draw_signature(cr, signature, width, height, padding=4):
    """Draw a signature scaled to fit width × height (for previews)."""
    sw, sh = signature.size
    scale = min((width - 2 * padding) / max(sw, 1), (height - 2 * padding) / max(sh, 1))
    cr.translate((width - sw * scale) / 2, (height - sh * scale) / 2)
    cr.scale(scale, scale)
    signature.place(0, 0, signature.baseline).draw(cr)


class SignaturePreview(Gtk.DrawingArea):
    """A small picture of a saved signature."""

    __gtype_name__ = "AcrimoniousSignaturePreview"

    def __init__(self, signature, width=160, height=56):
        super().__init__(content_width=width, content_height=height)
        self.signature = signature
        self.add_css_class("signature-preview")
        self.set_draw_func(lambda area, cr, w, h: draw_signature(cr, self.signature, w, h))


class SignaturePad(Adw.Dialog):
    """Draw a signature with the mouse, a pen or a finger."""

    __gtype_name__ = "AcrimoniousSignaturePad"
    __gsignals__ = {"saved": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, color=(0.0, 0.0, 0.0), width=1.5):
        super().__init__(title="New Signature", content_width=PAD_WIDTH + 48)
        self._color, self._width = color, width
        self._strokes = []

        self._area = Gtk.DrawingArea(content_width=PAD_WIDTH, content_height=PAD_HEIGHT,
                                     halign=Gtk.Align.CENTER,
                                     cursor=Gdk.Cursor.new_from_name("crosshair", None))
        self._area.add_css_class("signature-pad")
        self._area.set_draw_func(self._draw)
        self._area.update_property([Gtk.AccessibleProperty.LABEL], ["Signing pad"])
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._area.add_controller(drag)

        hint = Gtk.Label(label="Sign above the line. It is saved on this computer for use in any document.",
                         wrap=True, justify=Gtk.Justification.CENTER)
        hint.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=24,
                      margin_start=24, margin_end=24)
        box.append(self._area)
        box.append(hint)

        self._clear = Gtk.Button(label="_Clear", use_underline=True, sensitive=False)
        self._clear.connect("clicked", lambda *_: self._set_strokes([]))
        self._save = Gtk.Button(label="_Save", use_underline=True, sensitive=False)
        self._save.add_css_class("suggested-action")
        self._save.connect("clicked", self._on_save)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        header.pack_end(self._save)
        header.pack_end(self._clear)
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.set_default_widget(self._save)

    def _set_strokes(self, strokes):
        self._strokes = strokes
        self._clear.set_sensitive(bool(strokes))
        self._save.set_sensitive(bool(strokes))
        self._area.queue_draw()

    def _on_drag_begin(self, gesture, x, y):
        self._set_strokes(self._strokes + [[(x, y)]])

    def _on_drag_update(self, gesture, dx, dy):
        ok, x, y = gesture.get_start_point()
        stroke = self._strokes[-1]
        lx, ly = stroke[-1]
        if abs(x + dx - lx) + abs(y + dy - ly) >= 1:
            stroke.append((x + dx, y + dy))
            self._area.queue_draw()

    def _draw(self, area, cr, width, height):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)  # the line to sign on
        cr.set_line_width(1)
        cr.move_to(24, round(height * PAD_BASELINE) + 0.5)
        cr.line_to(width - 24, round(height * PAD_BASELINE) + 0.5)
        cr.stroke()
        cr.set_source_rgb(*self._color)
        cr.set_line_width(self._width / PAD_SCALE)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for stroke in self._strokes:
            stroke_path(cr, stroke)
            cr.stroke()

    def _on_save(self, button):
        strokes = [[(x * PAD_SCALE, y * PAD_SCALE) for x, y in stroke] for stroke in self._strokes]
        baseline = self._area.get_height() * PAD_BASELINE * PAD_SCALE
        self.emit("saved", Signature.from_strokes(strokes, baseline, self._width, self._color))
        self.close()
