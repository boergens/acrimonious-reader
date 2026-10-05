"""Writing and drawing on pages: the annotation tools of the document view.

A mixin for DocumentView. The tools are:
- "browse": click an annotation to choose it, drag to move it, double-click a text box to edit it;
- "text": click to type a new text box (the click marks the baseline), click a text box to edit it;
- "pen": draw freehand. Strokes made in quick succession join one ink annotation, so that a
  signature moves and deletes as one piece;
- "eraser": wipe over strokes or text boxes to remove them.
"""

import math
from dataclasses import replace

from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango

from .annotations import ASCENT, FONT_FAMILY, Ink, TextBox
from .document import PT_TO_PX, apply_rotation, rotate_point, rotated_size, unrotate_point

TOOLS = ("browse", "text", "pen", "eraser")
INK_GROUP_SECONDS = 4  # a stroke this soon after the previous one joins its annotation...
INK_GROUP_DISTANCE = 60  # ...if it is also this close to it, in points
HIT_PIXELS = 4  # how near the pointer must be to an annotation to grab it
NIGHT_HUE = ((-0.574, 1.430, 0.144), (0.426, 0.430, 0.144), (0.426, 1.430, -0.856))  # CSS hue-rotate(180°)
NIGHT_STRENGTH = 0.88


def rect(x, y, w, h):
    # Keep the struct itself: init() returns a wrapper that does not own it, so returning that
    # wrapper would let Python free the rectangle while GTK still reads it.
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


def night_color(rgb):
    """A colour as night mode shows it: brightness inverted, hue kept."""
    return tuple(min(max(1 - NIGHT_STRENGTH * sum(h * c for h, c in zip(row, rgb)), 0), 1) for row in NIGHT_HUE)


class AnnotationTools:
    def _init_annotation_tools(self):
        self._tool = "browse"
        self.style = {"color": (0.0, 0.0, 0.0), "size": 11.0, "width": 1.5}
        self._chosen = None  # name of the chosen annotation
        self._moving = None  # [item, page, x, y]: an annotation pressed on, which a drag moves
        self._move_preview = None
        self._stroke = None  # (page, [points]) while drawing
        self._last_ink = None  # (name, time) of the ink annotation the next stroke may join
        self._erase_started = False
        self._editor = None  # a Gtk.TextView while a text box is being typed
        self._editing = None  # (TextBox, is new)
        self._editor_tag = None
        self._editor_zoom = None
        self._annotations_handler = None

    @property
    def editable(self):
        return self.session is not None and self.session.document.editable

    def _attach_annotations(self, old, new):
        if self._editor is not None:  # the document is being replaced: drop the editor unsaved
            self._editor.unparent()
            self._editor = self._editing = None
        if old is not None and self._annotations_handler:
            old.annotations.disconnect(self._annotations_handler)
        self._annotations_handler = None
        if new is not None:
            self._annotations_handler = new.annotations.connect("changed", self._on_annotations_changed)
        self._chosen = self._moving = self._move_preview = self._stroke = self._last_ink = None

    def _on_annotations_changed(self, annotations):
        if self._chosen is not None and annotations.find(self._chosen) is None:
            self._choose(None)
        self.queue_draw()

    def _set_tool(self, tool):
        if tool not in TOOLS or tool == self._tool:
            return
        self.commit_editing()
        self._tool = tool
        self._last_ink = None
        if tool in ("pen", "eraser"):
            self._choose(None)
        self.notify("tool")

    # The chosen annotation

    def chosen_item(self):
        if self._chosen is None or self.session is None:
            return None
        return self.session.annotations.find(self._chosen)

    def _choose(self, name):
        if name != self._chosen:
            self._chosen = name
            self.emit("annotation-chosen", self.chosen_item())
            self.queue_draw()

    def delete_chosen(self):
        item = self.chosen_item()
        if item is not None:
            self.session.annotations.remove(item.name)

    def restyle(self, color=None, size=None, width=None):
        """Set the style for new annotations, and apply it to the one chosen or being typed."""
        for key, value in (("color", color), ("size", size), ("width", width)):
            if value is not None:
                self.style[key] = value
        if self._editing is not None:
            item, new = self._editing
            self._editing = (item.restyled(color=color, size=size), new)
            self._apply_editor_style()
            self.queue_allocate()
            return
        item = self.chosen_item()
        if item is not None:
            changed = item.restyled(color=color, size=size) if item.kind == "text" else item.restyled(color=color, width=width)
            if changed != item:
                self.session.annotations.replace(changed)

    # Typing text boxes

    def start_editing(self, item, new=False):
        self.commit_editing(refocus=False)
        self._choose(None)
        editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, accepts_tab=False, left_margin=0,
                              right_margin=0, top_margin=0, bottom_margin=0)
        editor.add_css_class("annotation-editor")
        editor.update_property([Gtk.AccessibleProperty.LABEL], ["Text box"])
        buffer = editor.get_buffer()
        buffer.set_text(item.text, -1)
        self._editor_tag = buffer.create_tag(None)
        buffer.connect("changed", self._on_editor_changed)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_editor_key)
        editor.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.idle_add(self._commit_if_focus_left))
        editor.add_controller(focus)
        self._editor, self._editing = editor, (item, new)
        self._apply_editor_style()
        buffer.place_cursor(buffer.get_end_iter())
        editor.set_parent(self)
        self.queue_allocate()
        self.queue_draw()
        editor.grab_focus()

    def commit_editing(self, refocus=True):
        """Finish typing: keep the text (an emptied box is removed)."""
        if self._editor is None:
            return
        editor, (item, new) = self._editor, self._editing
        buffer = editor.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).rstrip()
        self._editor = self._editing = self._editor_tag = None
        editor.unparent()
        model = self.session.annotations
        edited = replace(item, text=text)
        if not text:
            if not new:
                model.remove(item.name)
        elif new:
            model.add(edited)
        elif edited != model.find(item.name):
            model.replace(edited)
        if text:
            self._choose(item.name)
        if refocus:
            self.grab_focus()
        self.queue_draw()

    def _commit_if_focus_left(self):
        # Focus moving elsewhere in the window ends typing. Focus leaving the window (for another
        # application, or the colour dialog) does not.
        root = self.get_root()
        if self._editor is not None and root is not None and root.is_active():
            focus = root.get_focus()
            if focus is None or not (focus is self._editor or focus.is_ancestor(self._editor)):
                self.commit_editing(refocus=False)
        return GLib.SOURCE_REMOVE

    def _on_editor_key(self, controller, keyval, keycode, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_Escape or (ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)):
            self.commit_editing()
            return True
        return False

    def _on_editor_changed(self, buffer):
        buffer.apply_tag(self._editor_tag, buffer.get_start_iter(), buffer.get_end_iter())
        self.queue_allocate()

    def _apply_editor_style(self):
        """Make the editor's text match the text box at the current zoom."""
        item = self._editing[0]
        desc = Pango.FontDescription.from_string(FONT_FAMILY)
        desc.set_absolute_size(item.size * self._zoom * PT_TO_PX * Pango.SCALE)
        color = Gdk.RGBA()
        color.red, color.green, color.blue = night_color(item.color) if self._night else item.color
        color.alpha = 1
        self._editor_tag.props.font_desc = desc
        self._editor_tag.props.foreground_rgba = color
        buffer = self._editor.get_buffer()
        buffer.apply_tag(self._editor_tag, buffer.get_start_iter(), buffer.get_end_iter())
        self._editor_zoom = self._zoom

    def _place_editor(self):
        """Lay the editor exactly over the text box (called while allocating the view)."""
        item = self._editing[0]
        left, top, _, _ = self._layout[item.page]
        u, v = rotate_point(item.x, item.y, self._rotation, *self.session.document.page_sizes[item.page])
        k = self._zoom * PT_TO_PX
        if self._editor_zoom != self._zoom:
            self._apply_editor_style()
        width = max(self._editor.measure(Gtk.Orientation.HORIZONTAL, -1)[1] + 2, round(item.size * k * 3))
        height = self._editor.measure(Gtk.Orientation.VERTICAL, width)[1]
        point = Graphene.Point()
        point.init(left + u * k - self._hadj.get_value(), top + v * k - self._vadj.get_value())
        self._editor.allocate(width, height, -1, Gsk.Transform.new().translate(point))

    def _editor_contains(self, x, y):
        ok, bounds = self._editor.compute_bounds(self)
        return ok and bounds.get_x() <= x <= bounds.get_x() + bounds.get_width() and \
            bounds.get_y() <= y <= bounds.get_y() + bounds.get_height()

    # Drawing

    def _page_items(self, page):
        """The annotations to draw on a page, with any in-progress change applied."""
        editing = self._editing[0].name if self._editing is not None else None
        preview = self._move_preview
        items = []
        for item in self.session.annotations.on_page(page):
            if item.name != editing:
                items.append(preview if preview is not None and preview.name == item.name else item)
        if self._stroke is not None and self._stroke[0] == page:
            items.append(Ink(page, (tuple(self._stroke[1]),), self.style["color"], self.style["width"], name=""))
        return items

    def _draw_annotations(self, snapshot, page, ox, oy, bounds):
        items = self._page_items(page)
        if not items:
            return
        view_w, view_h = self._alloc
        x1, y1 = max(bounds.get_x(), 0), max(bounds.get_y(), 0)
        x2 = min(bounds.get_x() + bounds.get_width(), view_w)
        y2 = min(bounds.get_y() + bounds.get_height(), view_h)
        if x2 <= x1 or y2 <= y1:
            return
        cr = snapshot.append_cairo(rect(x1, y1, x2 - x1, y2 - y1))
        cr.translate(ox, oy)
        cr.scale(self._zoom * PT_TO_PX, self._zoom * PT_TO_PX)
        apply_rotation(cr, self._rotation, *self.session.document.page_sizes[page])
        for item in items:
            item.draw(cr)

    def _draw_chosen(self, snapshot, page, ox, oy):
        item = self._move_preview or self.chosen_item()
        if item is None or item.page != page:
            return
        x, y, _, _ = self._layout[page]
        rx, ry, rw, rh = self._rect_in_content(page, item.bounds(), pad=2)
        outline = Gsk.RoundedRect()
        outline.init_from_rect(rect(rx + ox - x, ry + oy - y, rw, rh), 3)
        snapshot.append_border(outline, [1.5] * 4, [_accent()] * 4)

    # Pointer and keys. Each handler returns True when the annotation tools took the event.

    def _tolerance(self):
        return HIT_PIXELS / (self._zoom * PT_TO_PX)

    def _to_page(self, page, x, y):
        """Widget point (x, y) in a given page's points, clamped to the page."""
        left, top, _, _ = self._layout[page]
        k = self._zoom * PT_TO_PX
        w0, h0 = self.session.document.page_sizes[page]
        rw, rh = rotated_size(w0, h0, self._rotation)
        u = min(max((x + self._hadj.get_value() - left) / k, 0), rw)
        v = min(max((y + self._vadj.get_value() - top) / k, 0), rh)
        return unrotate_point(u, v, self._rotation, w0, h0)

    def _item_at(self, x, y):
        hit = self._page_at(x, y)
        if hit is None or not self.editable:
            return None, hit
        page, px, py = hit
        return self.session.annotations.item_at(page, px, py, self._tolerance()), hit

    def _annotation_cursor(self, x, y):
        """The cursor the tools want at (x, y), or False to leave it to browsing."""
        if not self.editable:
            return False
        if self._tool in ("pen", "eraser"):
            return "crosshair" if self._tool == "pen" else "cell"
        item, hit = self._item_at(x, y)
        if item is not None:
            return "move"
        if self._tool == "text":
            return "text" if hit is not None else None
        return False

    def _annotation_press(self, n_press, x, y):
        self._moving = None
        if self._editor is not None:
            if self._editor_contains(x, y):
                return True
            self.commit_editing()
        if not self.editable:
            return False
        if self._tool in ("pen", "eraser"):
            return True  # the drag handlers draw and erase
        item, hit = self._item_at(x, y)
        if item is not None:
            self._choose(item.name)
            if n_press >= 2 and item.kind == "text":
                self.start_editing(item)
            else:
                self._moving = [item, item.page, hit[1], hit[2]]
            return True
        self._choose(None)
        if self._tool == "text" and hit is not None:
            page, px, py = hit
            size = self.style["size"]
            self.start_editing(TextBox(page, px, py - ASCENT * size, "", size, self.style["color"]), new=True)
            return True
        return False

    def _annotation_release(self, x, y):
        if self._moving is not None and self._move_preview is None and self._tool == "text":
            item = self._moving[0]
            if item.kind == "text":  # a click (not a drag) on a text box with the text tool
                self._moving = None
                self.start_editing(item)
                return True
        return self._moving is not None

    def _annotation_drag_begin(self, x, y):
        if not self.editable:
            return False
        if self._tool == "pen":
            hit = self._page_at(x, y)
            if hit is not None:
                self._stroke = (hit[0], [(round(hit[1], 2), round(hit[2], 2))])
                self.queue_draw()
            return True
        if self._tool == "eraser":
            self._erase_started = False
            self._erase_at(x, y)
            return True
        return False

    def _annotation_drag_update(self, x, y):
        if self._tool == "pen":
            # GTK delivers about one position per frame; the curves in _stroke_path smooth between them.
            if self._stroke is not None:
                page, points = self._stroke
                px, py = self._to_page(page, x, y)
                lx, ly = points[-1]
                if math.hypot(px - lx, py - ly) * self._zoom * PT_TO_PX >= 0.75:
                    points.append((round(px, 2), round(py, 2)))
                    self.queue_draw()
            return True
        if self._tool == "eraser":
            self._erase_at(x, y)
            return True
        if self._moving is not None:
            item, page, sx, sy = self._moving
            px, py = self._to_page(page, x, y)
            self._move_preview = item.moved(px - sx, py - sy) if (px, py) != (sx, sy) else None
            self.queue_draw()
            return True
        return False

    def _annotation_drag_end(self):
        if self._stroke is not None:
            self._finish_stroke()
            return True
        if self._tool == "eraser":
            return True
        if self._move_preview is not None:
            preview, self._move_preview, self._moving = self._move_preview, None, None
            self.session.annotations.replace(preview)
            return True
        return self._moving is not None

    def _finish_stroke(self):
        page, points = self._stroke
        self._stroke = None
        stroke = tuple(points)
        model = self.session.annotations
        now = GLib.get_monotonic_time()
        last = model.find(self._last_ink[0]) if self._last_ink is not None else None
        style = (self.style["color"], self.style["width"])
        if (last is not None and last.page == page and (last.color, last.width) == style
                and now - self._last_ink[1] < INK_GROUP_SECONDS * 1_000_000
                and _near(last.bounds(), Ink(page, (stroke,)).bounds(), INK_GROUP_DISTANCE)):
            ink = replace(last, strokes=last.strokes + (stroke,))
            model.replace(ink)
        else:
            ink = Ink(page, (stroke,), *style)
            model.add(ink)
        self._last_ink = (ink.name, now)

    def _erase_at(self, x, y):
        hit = self._page_at(x, y)
        if hit is None:
            return
        page, px, py = hit
        tolerance = self._tolerance() * 1.5
        model = self.session.annotations
        items, changed = [], False
        for item in model.items:
            if item.page == page and item.hit(px, py, tolerance):
                changed = True
                if item.kind == "ink" and len(item.strokes) > 1:
                    i = item.stroke_at(px, py, tolerance)
                    items.append(replace(item, strokes=item.strokes[:i] + item.strokes[i + 1:]))
                continue
            items.append(item)
        if changed:
            if not self._erase_started:  # one undo step per sweep of the eraser
                model.checkpoint()
                self._erase_started = True
            model.apply(items)

    def _annotation_key(self, keyval, state):
        if self._editor is not None or not self.editable:
            return False
        model = self.session.annotations
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            model.redo() if shift else model.undo()
            return True
        if ctrl and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            model.redo()
            return True
        item = self.chosen_item()
        if item is None or ctrl:
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace):
            model.remove(item.name)
        elif keyval == Gdk.KEY_Escape:
            self._choose(None)
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and item.kind == "text":
            self.start_editing(item)
        elif keyval in ARROWS:
            # Nudge by a point (ten with Shift), in screen directions whatever the rotation.
            du, dv = ARROWS[keyval]
            step = 10 if shift else 1
            w0, h0 = self.session.document.page_sizes[item.page]
            ax, ay = unrotate_point(0, 0, self._rotation, w0, h0)
            bx, by = unrotate_point(du * step, dv * step, self._rotation, w0, h0)
            model.replace(item.moved(bx - ax, by - ay))
        else:
            return False
        return True


ARROWS = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1),
          Gdk.KEY_KP_Left: (-1, 0), Gdk.KEY_KP_Right: (1, 0), Gdk.KEY_KP_Up: (0, -1), Gdk.KEY_KP_Down: (0, 1)}


def _near(a, b, distance):
    return not (b[0] > a[2] + distance or b[2] < a[0] - distance or b[1] > a[3] + distance or b[3] < a[1] - distance)


def _accent():
    color = Adw.StyleManager.get_default().get_accent_color_rgba()
    color.alpha = 1
    return color
