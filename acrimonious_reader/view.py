"""The document view: pages laid out in a scrolling column (or two), rendered tile by tile.

Coordinates come in three kinds:
- widget: logical pixels relative to the visible area;
- content: logical pixels relative to the whole laid-out document (widget + scroll offset);
- page points: PDF points, origin at the top left of the unrotated page.
"""

import bisect
import math

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk

from .document import PT_TO_PX, Tile, rotate_point, rotated_size, unrotate_point
from .editing import NIGHT_HUE, NIGHT_STRENGTH, AnnotationTools
from .editing import rect as _rect
from .selection import END, Selection
from .session import PRIORITY_PREFETCH, PRIORITY_VISIBLE

PAGE_GAP = 12  # logical pixels between pages
MARGIN = 16  # around the document
MIN_ZOOM = 0.1
MAX_ZOOM = 16.0
ZOOM_STEPS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0,
              4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0)
ZOOM_SETTLE_MS = 150  # after a zoom change, wait this long before rendering at the new scale
SCROLL_STEP = 48
HISTORY_LIMIT = 100

# Pages up to this many device pixels are rendered in one piece; bigger ones in square tiles.
WHOLE_PAGE_PIXELS = 12_000_000
MAX_TEXTURE_SIDE = 8192
TILE_SIZE = 1024


def _rgba(spec):
    color = Gdk.RGBA()
    color.parse(spec)
    return color


PAPER = _rgba("white")
SHADOW = _rgba("rgba(0, 0, 0, 0.3)")
MATCH = _rgba("rgba(246, 211, 45, 0.45)")
CURRENT_MATCH = _rgba("rgba(255, 120, 0, 0.55)")


def _night_filter():
    """Invert brightness but keep hues: invert, then rotate hue by 180° (as in CSS hue-rotate).

    White paper becomes dark grey rather than black, which is easier on the eyes.
    """
    a = [[-NIGHT_STRENGTH * v for v in row] for row in NIGHT_HUE]
    # GSK computes transpose(matrix) * colour, so the rows given here are the columns of `a`.
    values = [a[0][0], a[1][0], a[2][0], 0,
              a[0][1], a[1][1], a[2][1], 0,
              a[0][2], a[1][2], a[2][2], 0,
              0, 0, 0, 1]
    matrix, offset = Graphene.Matrix(), Graphene.Vec4()
    matrix.init_from_float(values)
    offset.init(1, 1, 1, 0)
    return matrix, offset


NIGHT_FILTER = _night_filter()


def tile_size(page_w, page_h):
    """Tile dimensions for a page image of page_w × page_h device pixels."""
    if page_w * page_h <= WHOLE_PAGE_PIXELS and max(page_w, page_h) <= MAX_TEXTURE_SIDE:
        return page_w, page_h
    return TILE_SIZE, TILE_SIZE


class DocumentView(AnnotationTools, Gtk.Widget, Gtk.Scrollable):
    __gtype_name__ = "AcrimoniousDocumentView"

    __gsignals__ = {
        # A link asked for a viewer action this widget cannot do itself, such as "Print".
        "named-action": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # A link points into another PDF file (a path, possibly relative to this document).
        "open-remote": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "toast": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # The annotation chosen (to move, delete or restyle) changed; the item, or None.
        "annotation-chosen": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)

    def __init__(self):
        super().__init__(focusable=True, overflow=Gtk.Overflow.HIDDEN)
        self.add_css_class("document-view")
        self.session = None
        self.search = None  # a Search, set by the window
        self._hadj = self._vadj = None
        self._adj_handlers = {}
        self._session_handlers = []
        self._zoom = 1.0
        self._zoom_mode = "auto"  # "auto", "width", "page" or "free"
        self._rotation = 0
        self._dual = False
        self._night = False
        self._alloc = (0, 0)
        self._layout = []  # per page: (x, y, width, height) in content coordinates
        self._rows = []  # (pages, top, height)
        self._row_tops = []
        self._content = (0, 0)
        self._layout_valid = False
        self._pending_position = None
        self._page = 0
        self._zoom_changed_at = 0
        self._settle_id = 0
        self._selection = None
        self._selection_rects = {}
        self._press_link = None
        self._drag_point = None
        self._autoscroll_id = 0
        self._pointer = None
        self._cursor = None
        self._context_uri = None
        self._popover = None
        self._back, self._forward = [], []
        self._scroll_animation = None
        self._scroll_goal = None
        self._pinch_zoom = 1.0
        self._surface_handler = None
        self._init_annotation_tools()
        self.set_hadjustment(None)
        self.set_vadjustment(None)
        self._setup_input()

        actions = Gio.SimpleActionGroup()
        copy_link = Gio.SimpleAction.new("copy-link", None)
        copy_link.connect("activate", lambda *_: self._copy(self._context_uri or ""))
        actions.add_action(copy_link)
        delete = Gio.SimpleAction.new("delete-annotation", None)
        delete.connect("activate", lambda *_: self.delete_chosen())
        actions.add_action(delete)
        self.insert_action_group("view", actions)

    # Properties

    @GObject.Property(type=Gtk.Adjustment)
    def hadjustment(self):
        return self._hadj

    @hadjustment.setter
    def hadjustment(self, adjustment):
        self._set_adjustment("_hadj", adjustment)

    @GObject.Property(type=Gtk.Adjustment)
    def vadjustment(self):
        return self._vadj

    @vadjustment.setter
    def vadjustment(self, adjustment):
        self._set_adjustment("_vadj", adjustment)

    @GObject.Property(type=int, flags=GObject.ParamFlags.READABLE)
    def page(self):
        """The page that fills most of the view."""
        return self._page

    @GObject.Property(type=float, minimum=MIN_ZOOM, maximum=MAX_ZOOM, default=1.0)
    def zoom(self):
        return self._zoom

    @zoom.setter
    def zoom(self, value):
        self.set_zoom(value)

    @GObject.Property(type=str, default="auto")
    def zoom_mode(self):
        return self._zoom_mode

    @zoom_mode.setter
    def zoom_mode(self, mode):
        if mode not in ("auto", "width", "page", "free") or mode == self._zoom_mode:
            return
        self._change_layout(lambda: setattr(self, "_zoom_mode", mode))

    @GObject.Property(type=int, default=0)
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, value):
        value %= 360
        if value == self._rotation or value % 90:
            return
        page = self._page
        self._change_layout(lambda: setattr(self, "_rotation", value))
        self.go_to(page, record=False)

    @GObject.Property(type=bool, default=False)
    def dual_page(self):
        return self._dual

    @dual_page.setter
    def dual_page(self, value):
        if value != self._dual:
            self._change_layout(lambda: setattr(self, "_dual", value))

    @GObject.Property(type=bool, default=False)
    def night_mode(self):
        return self._night

    @night_mode.setter
    def night_mode(self, value):
        self._night = value
        if self._editor is not None:
            self._apply_editor_style()
        self.queue_draw()

    @GObject.Property(type=str, default="browse")
    def tool(self):
        """One of editing.TOOLS: "browse", "text", "pen" or "eraser"."""
        return self._tool

    @tool.setter
    def tool(self, value):
        self._set_tool(value)

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READABLE)
    def has_selection(self):
        return self._selection is not None and not self._selection.is_empty

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READABLE)
    def can_go_back(self):
        return bool(self._back)

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READABLE)
    def can_go_forward(self):
        return bool(self._forward)

    # Document

    def set_session(self, session, position=None, view_state=None):
        """Show a document. position is (page, fraction of the page above the view's top)."""
        self._attach_annotations(self.session, session)
        for handler in self._session_handlers:
            self.session.disconnect(handler)
        self.session = session
        self._session_handlers = []
        if session is not None:
            self._session_handlers = [
                session.connect("tile-ready", lambda *_: self.queue_draw()),
                session.connect("page-data-ready", self._on_page_data),
            ]
        if view_state:  # from the state file, so check everything
            if view_state.get("zoom_mode") in ("auto", "width", "page", "free"):
                self._zoom_mode = view_state["zoom_mode"]
            if isinstance(view_state.get("zoom"), (int, float)):
                self._zoom = min(max(view_state["zoom"], MIN_ZOOM), MAX_ZOOM)
            if view_state.get("rotation") in (0, 90, 180, 270):
                self._rotation = view_state["rotation"]
            self._dual = bool(view_state.get("dual", self._dual))
            for name in ("zoom-mode", "zoom", "rotation", "dual-page"):
                self.notify(name)
        self._selection = None
        self._selection_rects = {}
        self._back, self._forward = [], []
        self._press_link = None
        self._layout_valid = False
        self._pending_position = position or (0, 0.0)
        self._page = min(self._pending_position[0], session.document.n_pages - 1) if session else 0
        for name in ("page", "has-selection", "can-go-back", "can-go-forward"):
            self.notify(name)
        self.queue_allocate()
        self.queue_draw()

    def get_view_state(self):
        return {"zoom_mode": self._zoom_mode, "zoom": self._zoom, "rotation": self._rotation,
                "dual": self._dual}

    def get_position(self):
        """(page, fraction of that page above the top of the view)."""
        if self._pending_position is not None or not self._layout_valid:
            return self._pending_position or (0, 0.0)
        top = self._vadj.get_value() + PAGE_GAP / 2
        page = self._nearest_page(self._hadj.get_value() + self._alloc[0] / 2, top)
        _, y, _, height = self._layout[page]
        return page, (top - y) / height

    def set_position(self, position):
        if not self._layout_valid:
            self._pending_position = position
            self.queue_allocate()
            return
        page, fraction = position
        page = min(max(page, 0), len(self._layout) - 1)
        x, y, width, height = self._layout[page]
        self._vadj.set_value(y + fraction * height - PAGE_GAP / 2)  # a little gap above the page

    # Zoom

    def set_zoom(self, zoom, anchor=None):
        """Zoom freely, keeping the point under `anchor` (widget coordinates) in place."""
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if self._zoom_mode == "free" and abs(zoom - self._zoom) < 1e-4:
            return

        def apply():
            self._zoom_mode = "free"
            self._zoom = zoom

        self._change_layout(apply, anchor)

    def zoom_in(self, anchor=None):
        self.set_zoom(next((z for z in ZOOM_STEPS if z > self._zoom * 1.01), MAX_ZOOM), anchor)

    def zoom_out(self, anchor=None):
        smaller = (z for z in reversed(ZOOM_STEPS) if z < self._zoom / 1.01)
        self.set_zoom(next(smaller, MIN_ZOOM), anchor)

    def _change_layout(self, apply, anchor=None):
        old = (self._zoom, self._zoom_mode, self._rotation, self._dual)
        if self.session is None or not self._layout_valid:
            apply()
            self._layout_valid = False
            self.queue_allocate()
        else:
            width, height = self._alloc
            point = self._anchor(*(anchor or (width / 2, height / 2)))
            apply()
            self._relayout()
            self._configure_adjustments()
            self._restore_anchor(point)
            self._zoom_changed_at = GLib.get_monotonic_time()
            self._selection_rects = {}
            self.queue_draw()
            if self._editor is not None:
                self.queue_allocate()
        for name, before, after in zip(("zoom", "zoom-mode", "rotation", "dual-page"), old,
                                       (self._zoom, self._zoom_mode, self._rotation, self._dual)):
            if before != after:
                self.notify(name)

    def _fit_zoom(self, width, height):
        doc = self.session.document
        per_row = 2 if self._dual else 1
        sizes = [rotated_size(w, h, self._rotation) for w, h in doc.page_sizes]
        widest = max(sum(w for w, _ in sizes[i:i + per_row]) for i in range(0, len(sizes), per_row))
        tallest = max(h for _, h in sizes)
        fit_width = max(width - 2 * MARGIN - PAGE_GAP * (per_row - 1), 50) / (widest * PT_TO_PX)
        fit_page = max(height - 2 * MARGIN, 50) / (tallest * PT_TO_PX)
        if self._zoom_mode == "width":
            zoom = fit_width
        elif self._zoom_mode == "page":
            zoom = min(fit_width, fit_page)
        else:  # automatic: fit the width, but no bigger than actual size
            zoom = min(fit_width, 1.0)
        return min(max(zoom, MIN_ZOOM), MAX_ZOOM)

    # Layout and scrolling

    def do_measure(self, orientation, for_size):
        return 0, 300, -1, -1

    def do_size_allocate(self, width, height, baseline):
        if self._popover is not None:
            self._popover.present()
        if self.session is None or width < 2 or height < 2:
            self._alloc = (width, height)
            self._configure_adjustments()
            return
        if (width, height) != self._alloc or not self._layout_valid:
            point = None
            if self._layout_valid and self._pending_position is None:
                point = self._anchor(self._alloc[0] / 2, 0)
            self._alloc = (width, height)
            old_zoom = self._zoom
            self._relayout()
            self._configure_adjustments()
            if self._pending_position is not None:
                position, self._pending_position = self._pending_position, None
                self.set_position(position)
            elif point is not None:
                self._restore_anchor(point[:3] + (width / 2, 0))
            if self._zoom != old_zoom:
                self.notify("zoom")
            self._selection_rects = {}
        else:
            self._configure_adjustments()
        if self._editor is not None:
            self._place_editor()
        self._update_page()

    def _relayout(self):
        width, height = self._alloc
        doc = self.session.document
        if self._zoom_mode != "free":
            self._zoom = self._fit_zoom(width, height)
        k = self._zoom * PT_TO_PX
        sizes = [rotated_size(w, h, self._rotation) for w, h in doc.page_sizes]
        per_row = 2 if self._dual else 1
        rows, y, widest = [], MARGIN, 0
        for first in range(0, doc.n_pages, per_row):
            pages = list(range(first, min(first + per_row, doc.n_pages)))
            row_width = sum(sizes[i][0] for i in pages) * k + PAGE_GAP * (len(pages) - 1)
            row_height = max(sizes[i][1] for i in pages) * k
            rows.append((pages, y, row_height, row_width))
            widest = max(widest, row_width)
            y += row_height + PAGE_GAP
        content_width = max(widest + 2 * MARGIN, width)
        layout = [None] * doc.n_pages
        for pages, top, row_height, row_width in rows:
            x = (content_width - row_width) / 2
            for i in pages:
                page_w, page_h = sizes[i][0] * k, sizes[i][1] * k
                layout[i] = (x, top + (row_height - page_h) / 2, page_w, page_h)
                x += page_w + PAGE_GAP
        self._layout = layout
        self._rows = [(pages, top, row_height) for pages, top, row_height, _ in rows]
        self._row_tops = [top for _, top, _ in self._rows]
        self._content = (content_width, y - PAGE_GAP + MARGIN)
        self._layout_valid = True

    def _set_adjustment(self, attr, adjustment):
        old = getattr(self, attr)
        if adjustment is None:
            adjustment = Gtk.Adjustment()
        if old is adjustment:
            return
        if old is not None:
            old.disconnect(self._adj_handlers.pop(attr))
        setattr(self, attr, adjustment)
        self._adj_handlers[attr] = adjustment.connect("value-changed", self._on_scrolled)
        self._configure_adjustments()

    def _configure_adjustments(self):
        width, height = self._alloc
        content = self._content if self._layout_valid and self.session else (width, height)
        for adjustment, size, total in ((self._hadj, width, content[0]), (self._vadj, height, content[1])):
            if adjustment is None:
                continue
            upper = max(total, size)
            value = min(max(adjustment.get_value(), 0), upper - size)
            adjustment.configure(value, 0, upper, SCROLL_STEP, size * 0.9, size)

    def _on_scrolled(self, adjustment):
        self._update_page()
        self.queue_draw()
        if self._editor is not None:
            self.queue_allocate()

    def _update_page(self):
        if not self._layout_valid or self.session is None:
            return
        top, height = self._vadj.get_value(), self._alloc[1]
        at_end = top >= self._vadj.get_upper() - height - 1
        best, best_area = self._page, -1
        for i in self._pages_in_range(top, top + height):
            _, y, _, page_h = self._layout[i]
            visible = min(y + page_h, top + height) - max(y, top)
            if visible > best_area + 0.5 or (at_end and visible > 0):
                best, best_area = i, visible
        if best != self._page:
            self._page = best
            self.notify("page")

    def _pages_in_range(self, y0, y1):
        r = max(bisect.bisect_right(self._row_tops, y0) - 1, 0)
        while r < len(self._rows) and self._rows[r][1] < y1:
            pages, top, height = self._rows[r]
            if top + height > y0:
                yield from pages
            r += 1

    def _nearest_page(self, cx, cy):
        r = max(bisect.bisect_right(self._row_tops, cy) - 1, 0)
        pages, top, height = self._rows[r]
        if cy > top + height and r + 1 < len(self._rows):
            next_pages, next_top, _ = self._rows[r + 1]
            if next_top - cy < cy - (top + height):
                pages = next_pages

        def distance(i):
            x, _, width, _ = self._layout[i]
            return 0 if x <= cx <= x + width else min(abs(cx - x), abs(cx - x - width))

        return min(pages, key=distance)

    def _anchor(self, vx, vy):
        """Remember which document point is at widget position (vx, vy)."""
        cx, cy = self._hadj.get_value() + vx, self._vadj.get_value() + vy
        page = self._nearest_page(cx, cy)
        x, y, width, height = self._layout[page]
        return page, (cx - x) / width, (cy - y) / height, vx, vy

    def _restore_anchor(self, point):
        page, fx, fy, vx, vy = point
        x, y, width, height = self._layout[page]
        self._hadj.set_value(x + fx * width - vx)
        self._vadj.set_value(y + fy * height - vy)

    def scroll_by(self, dx, dy):
        """Scroll smoothly; repeated calls (a held key) add up."""
        animating = (self._scroll_animation is not None
                     and self._scroll_animation.get_state() == Adw.AnimationState.PLAYING)
        x, y = self._scroll_goal if animating else (self._hadj.get_value(), self._vadj.get_value())
        self.scroll_to(x + dx, y + dy)

    def scroll_to(self, x, y, animate=True):
        x = min(max(x, 0), self._hadj.get_upper() - self._hadj.get_page_size())
        y = min(max(y, 0), self._vadj.get_upper() - self._vadj.get_page_size())
        if self._scroll_animation is not None:
            self._scroll_animation.pause()
            self._scroll_animation = None
        if not animate:
            self._hadj.set_value(x)
            self._vadj.set_value(y)
            return
        start = (self._hadj.get_value(), self._vadj.get_value())
        self._scroll_goal = (x, y)

        def step(t):
            self._hadj.set_value(start[0] + (x - start[0]) * t)
            self._vadj.set_value(start[1] + (y - start[1]) * t)

        self._scroll_animation = Adw.TimedAnimation.new(
            self, 0, 1, 180, Adw.CallbackAnimationTarget.new(step))
        self._scroll_animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._scroll_animation.play()

    # Navigation

    def go_to(self, page, top=None, record=True):
        """Show a page, optionally scrolled to `top` points below its top edge."""
        if self.session is None:
            return
        page = min(max(page, 0), self.session.document.n_pages - 1)
        if record:
            self._push_history()
        if not self._layout_valid:
            self._pending_position = (page, 0.0)
            self._page = page
            self.notify("page")
            return
        x, y, width, height = self._layout[page]
        target = y - PAGE_GAP / 2
        if top is not None and self._rotation in (0, 180):
            w0, h0 = self.session.document.page_sizes[page]
            _, v = rotate_point(0, top, self._rotation, w0, h0)
            target = y + v * self._zoom * PT_TO_PX - 2 * PAGE_GAP
        hv, view_w = self._hadj.get_value(), self._alloc[0]
        left = hv if hv <= x and x + width <= hv + view_w else x - (view_w - width) / 2
        self.scroll_to(left, target, animate=False)

    def go_to_target(self, target):
        """Follow a link or outline target."""
        if target.kind == "page":
            self.go_to(target.page, target.top)
        elif target.kind == "uri":
            launcher = Gtk.UriLauncher.new(target.uri)
            launcher.launch(self.get_root(), None, self._on_uri_launched)
        elif target.kind == "remote":
            self.emit("open-remote", target.uri)
        elif target.kind == "named":
            actions = {"NextPage": self.next_page, "PrevPage": self.previous_page,
                       "FirstPage": lambda: self.go_to(0),
                       "LastPage": lambda: self.go_to(self.session.document.n_pages - 1),
                       "GoBack": self.go_back, "GoForward": self.go_forward}
            if target.uri in actions:
                actions[target.uri]()
            else:
                self.emit("named-action", target.uri)

    def _on_uri_launched(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.emit("toast", f"Could not open link: {error.message}")

    def next_page(self):
        self._step_row(1)

    def previous_page(self):
        self._step_row(-1)

    def _step_row(self, delta):
        if self.session is None or not self._rows:
            return
        per_row = 2 if self._dual else 1
        row = min(max(self._page // per_row + delta, 0), len(self._rows) - 1)
        self.go_to(self._rows[row][0][0], record=False)

    def _push_history(self):
        self._back.append(self.get_position())
        del self._back[:-HISTORY_LIMIT]
        self._forward.clear()
        self.notify("can-go-back")
        self.notify("can-go-forward")

    def go_back(self):
        if self._back:
            self._forward.append(self.get_position())
            self.set_position(self._back.pop())
            self.notify("can-go-back")
            self.notify("can-go-forward")

    def go_forward(self):
        if self._forward:
            self._back.append(self.get_position())
            self.set_position(self._forward.pop())
            self.notify("can-go-back")
            self.notify("can-go-forward")

    def reveal(self, page, rect):
        """Scroll so that a rectangle (page points) is comfortably in view."""
        if not self._layout_valid:
            self.go_to(page, record=False)
            return
        x, y, w, h = self._rect_in_content(page, rect)
        hv, vv = self._hadj.get_value(), self._vadj.get_value()
        view_w, view_h = self._alloc
        if not (vv + 0.1 * view_h <= y and y + h <= vv + 0.9 * view_h):
            vv = y + h / 2 - view_h / 2
        if not (hv <= x and x + w <= hv + view_w):
            hv = x + w / 2 - view_w / 2
        self.scroll_to(hv, vv, animate=False)

    def reveal_match(self, page, index):
        rects = self.search.matches(page)[index]
        self.reveal(page, (min(r[0] for r in rects), min(r[1] for r in rects),
                           max(r[2] for r in rects), max(r[3] for r in rects)))

    # Geometry helpers

    def _device_scale(self):
        native = self.get_native()
        surface = native.get_surface() if native is not None else None
        return surface.get_scale() if surface is not None else self.get_scale_factor()

    def _rect_in_content(self, page, rect, pad=0.0):
        """(x, y, width, height) in content coordinates of a rectangle in page points."""
        x1, y1, x2, y2 = rect
        w0, h0 = self.session.document.page_sizes[page]
        ax, ay = rotate_point(x1 - pad, y1 - pad, self._rotation, w0, h0)
        bx, by = rotate_point(x2 + pad, y2 + pad, self._rotation, w0, h0)
        k = self._zoom * PT_TO_PX
        x, y, _, _ = self._layout[page]
        return x + min(ax, bx) * k, y + min(ay, by) * k, abs(bx - ax) * k, abs(by - ay) * k

    def _page_at(self, x, y, nearest=False):
        """(page, px, py): the page at widget point (x, y), and the point in page points.

        With nearest, a point beside or between pages counts for the closest page."""
        if self.session is None or not self._layout_valid:
            return None
        cx, cy = x + self._hadj.get_value(), y + self._vadj.get_value()
        page = self._nearest_page(cx, cy)
        left, top, width, height = self._layout[page]
        inside = left <= cx <= left + width and top <= cy <= top + height
        if not inside and not nearest:
            return None
        k = self._zoom * PT_TO_PX
        u = min(max(cx - left, 0), width) / k
        v = min(max(cy - top, 0), height) / k
        w0, h0 = self.session.document.page_sizes[page]
        return (page, *unrotate_point(u, v, self._rotation, w0, h0))

    def _link_at(self, x, y):
        hit = self._page_at(x, y)
        if hit is None:
            return None
        page, px, py = hit
        for link in self.session.links(page) or ():
            x1, y1, x2, y2 = link.rect
            if x1 <= px <= x2 and y1 <= py <= y2:
                return link
        return None

    def _over_text(self, x, y):
        hit = self._page_at(x, y)
        if hit is None:
            return False
        text = self.session.text(hit[0])
        return text is not None and text.is_over_text(hit[1], hit[2])

    # Drawing

    def do_snapshot(self, snapshot):
        if self.session is None or not self._layout_valid:
            return
        width, height = self._alloc
        s = self._device_scale()
        scale = round(self._zoom * PT_TO_PX * s, 4)
        hv, vv = self._hadj.get_value(), self._vadj.get_value()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        accent.alpha = 0.35
        wanted = []
        for page in self._pages_in_range(vv, vv + height):
            origin = self._page_origin(page, hv, vv, s)
            tiles = self._page_tiles(page, origin, scale, s, 0, height)
            self._draw_page(snapshot, page, origin, s, tiles, wanted, accent)
            self.session.links(page)  # fetch early, so links react as soon as the pointer arrives
        if self._editor is not None:
            self.snapshot_child(self._editor, snapshot)

        if GLib.get_monotonic_time() - self._zoom_changed_at < ZOOM_SETTLE_MS * 1000:
            # Mid-zoom: keep showing scaled tiles and render only once the zoom settles.
            self.session.request_tiles([])
            self._schedule_settle()
            return
        visible = {tile for tile, _ in wanted}
        for page in self._pages_in_range(vv - height / 2, vv + 2 * height):
            origin = self._page_origin(page, hv, vv, s)
            for tile in self._page_tiles(page, origin, scale, s, -height / 2, 2 * height):
                if tile not in visible:
                    wanted.append((tile, PRIORITY_PREFETCH))
        self.session.request_tiles(wanted)

    def _page_origin(self, page, hv, vv, s):
        # Snap pages to device pixels so that text stays crisp.
        x, y, _, _ = self._layout[page]
        return round((x - hv) * s) / s, round((y - vv) * s) / s

    def _page_tiles(self, page, origin, scale, s, y0, y1):
        """The tiles of a page that intersect the widget's rows y0..y1."""
        ox, oy = origin
        w0, h0 = self.session.document.page_sizes[page]
        rw, rh = rotated_size(w0, h0, self._rotation)
        page_w, page_h = max(1, math.ceil(rw * scale)), max(1, math.ceil(rh * scale))
        tw, th = tile_size(page_w, page_h)
        dx0, dy0 = max(0.0, -ox * s), max(0.0, (y0 - oy) * s)
        dx1, dy1 = min(page_w, (self._alloc[0] - ox) * s), min(page_h, (y1 - oy) * s)
        if dx1 <= dx0 or dy1 <= dy0:
            return []
        tiles = []
        for ty in range(int(dy0 // th), math.ceil(dy1 / th)):
            for tx in range(int(dx0 // tw), math.ceil(dx1 / tw)):
                x, y = tx * tw, ty * th
                tiles.append(Tile(page, self._rotation, scale, x, y, min(tw, page_w - x), min(th, page_h - y)))
        return tiles

    def _draw_page(self, snapshot, page, origin, s, tiles, wanted, accent):
        ox, oy = origin
        _, _, width, height = self._layout[page]
        bounds = _rect(ox, oy, width, height)
        outline = Gsk.RoundedRect()
        outline.init_from_rect(bounds, 0)
        snapshot.append_outset_shadow(outline, SHADOW, 0, 1, 0, 4)
        snapshot.append_color(PAPER, bounds)
        snapshot.push_clip(bounds)
        if self._night:
            snapshot.push_color_matrix(*NIGHT_FILTER)
        ready, missing = [], False
        for tile in tiles:
            texture = self.session.tiles.get(tile)
            if texture is None:
                missing = True
                wanted.append((tile, PRIORITY_VISIBLE))
                texture = self.session.stale_tile(tile)
            if texture is not None:
                ready.append((tile, texture))
        if missing:
            self._draw_stand_in(snapshot, page, origin, s, tiles[0].scale)
        for tile, texture in ready:
            snapshot.append_texture(
                texture, _rect(ox + tile.x / s, oy + tile.y / s, tile.width / s, tile.height / s))
        self._draw_annotations(snapshot, page, ox, oy, bounds)
        if self._night:
            snapshot.pop()
        snapshot.pop()
        self._draw_highlights(snapshot, page, ox, oy, accent)
        self._draw_chosen(snapshot, page, ox, oy)

    def _draw_stand_in(self, snapshot, page, origin, s, scale):
        """While tiles render, show the page cached at another zoom level, or its thumbnail."""
        ox, oy = origin
        view_w, view_h = self._alloc
        others = self.session.tiles.scales(page, self._rotation)
        best = min((other for other in others if other != scale),
                   key=lambda other: abs(math.log(other / scale)), default=None)
        if best is not None:
            f = scale / best / s
            for tile, texture in others[best].items():
                x, y, w, h = ox + tile.x * f, oy + tile.y * f, tile.width * f, tile.height * f
                if x < view_w and y < view_h and x + w > 0 and y + h > 0:
                    snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, _rect(x, y, w, h))
            return
        thumbnail = self.session.any_thumbnail(page) if self._rotation == 0 else None
        if thumbnail is not None:
            _, _, width, height = self._layout[page]
            snapshot.append_scaled_texture(thumbnail, Gsk.ScalingFilter.LINEAR, _rect(ox, oy, width, height))

    def _draw_highlights(self, snapshot, page, ox, oy, accent):
        x, y, _, _ = self._layout[page]
        dx, dy = ox - x, oy - y  # content -> widget
        if self.search is not None:
            for index, match in enumerate(self.search.matches(page)):
                color = CURRENT_MATCH if self.search.current == (page, index) else MATCH
                for rect in match:
                    rx, ry, rw, rh = self._rect_in_content(page, rect, pad=1)
                    snapshot.append_color(color, _rect(rx + dx, ry + dy, rw, rh))
        for rect in self._page_selection_rects(page):
            rx, ry, rw, rh = self._rect_in_content(page, rect)
            snapshot.append_color(accent, _rect(rx + dx, ry + dy, rw, rh))

    def _schedule_settle(self):
        if self._settle_id:
            return

        def settled():
            self._settle_id = 0
            self.queue_draw()
            return GLib.SOURCE_REMOVE

        self._settle_id = GLib.timeout_add(ZOOM_SETTLE_MS + 10, settled)

    def do_realize(self):
        Gtk.Widget.do_realize(self)
        surface = self.get_native().get_surface()
        self._surface_handler = (surface, surface.connect("notify::scale", lambda *_: self.queue_draw()))

    def do_unrealize(self):
        if self._surface_handler is not None:
            surface, handler = self._surface_handler
            surface.disconnect(handler)
            self._surface_handler = None
        Gtk.Widget.do_unrealize(self)

    def do_dispose(self):
        if self._popover is not None:
            self._popover.unparent()
            self._popover = None
        if self._editor is not None:
            self._editor.unparent()
            self._editor = None
        Gtk.Widget.do_dispose(self)

    # Selection

    def _set_selection(self, selection):
        had = self.props.has_selection
        self._selection = selection
        self._selection_rects = {}
        if self.props.has_selection != had:
            self.notify("has-selection")
        self.queue_draw()

    def clear_selection(self):
        if self._selection is not None:
            self._set_selection(None)

    def select_all(self):
        if self.session is not None:
            self._set_selection(Selection((0, 0), (self.session.document.n_pages - 1, END)))

    def _selection_bounds(self):
        """((first page, index), (last page, index)) of the selection, expanded to whole words or
        lines when it was made with a double or triple click."""
        sel = self._selection
        start, end = sorted((sel.anchor, sel.focus))
        if sel.mode != "char":
            first, last = self.session.text_now(start[0]), self.session.text_now(end[0])
            if sel.mode == "word":
                # end is the cursor after the last selected character; expand around that character
                last_char = end[1] - 1 if end != start and end[1] > 0 else end[1]
                start = (start[0], first.word_range(start[1])[0])
                end = (end[0], last.word_range(last_char)[1])
            else:
                start = (start[0], first.line_range(start[1])[0])
                end = (end[0], last.line_range(end[1])[1])
        return start, end

    def _page_selection_rects(self, page):
        if not self.props.has_selection:
            return ()
        rects = self._selection_rects.get(page)
        if rects is None:
            (first, start), (last, end) = self._selection_bounds()
            if not first <= page <= last:
                return ()
            text = self.session.text(page)
            if text is None:
                return ()  # drawn once the text arrives
            rects = text.highlight(start if page == first else 0, end if page == last else END)
            self._selection_rects[page] = rects
        return rects

    def selected_text(self):
        if not self.props.has_selection:
            return ""
        (first, start), (last, end) = self._selection_bounds()
        parts = []
        for page in range(first, last + 1):
            if page in (first, last):
                text = self.session.text_now(page).text
                parts.append(text[start if page == first else 0:end if page == last else END])
            else:
                parts.append(self.session.document.plain_text(page))
        return "\n".join(part.rstrip("\n") for part in parts)

    def copy_selection(self):
        text = self.selected_text()
        if text:
            self._copy(text)

    def _copy(self, text):
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))

    # Input

    def _setup_input(self):
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        select = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        select.connect("drag-begin", lambda _, x, y: self._annotation_drag_begin(x, y))
        select.connect("drag-update", self._on_select_drag)
        select.connect("drag-end", self._on_select_end)
        self.add_controller(select)

        pan = Gtk.GestureDrag(button=Gdk.BUTTON_MIDDLE)
        pan.connect("drag-begin", self._on_pan_begin)
        pan.connect("drag-update", self._on_pan_update)
        pan.connect("drag-end", lambda *_: self._set_cursor(None))
        self.add_controller(pan)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: setattr(self, "_pointer", None))
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        pinch = Gtk.GestureZoom()
        pinch.connect("begin", lambda *_: setattr(self, "_pinch_zoom", self._zoom))
        pinch.connect("scale-changed", self._on_pinch)
        self.add_controller(pinch)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        self.set_has_tooltip(True)

    def _set_cursor(self, name):
        if name != self._cursor:
            self._cursor = name
            self.set_cursor_from_name(name)

    def _on_motion(self, controller, x, y):
        self._pointer = (x, y)
        if self.session is None:
            return
        cursor = self._annotation_cursor(x, y)
        if cursor is not False:
            self._set_cursor(cursor)
        elif self._link_at(x, y) is not None:
            self._set_cursor("pointer")
        elif self._over_text(x, y):
            self._set_cursor("text")
        else:
            self._set_cursor(None)

    def _on_page_data(self, session, page):
        if self._pointer is not None:
            self._on_motion(None, *self._pointer)
        self._selection_rects.pop(page, None)
        self.queue_draw()

    def do_query_tooltip(self, x, y, keyboard_mode, tooltip):
        link = self._link_at(x, y) if not keyboard_mode else None
        if link is None:
            return False
        target = link.target
        if target.kind == "page":
            text = f"Go to page {self.session.document.page_labels[target.page]}"
        elif target.kind == "named":
            text = f"Action: {target.uri}"
        else:
            text = target.uri
        tooltip.set_text(text)
        return True

    def _on_pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        if self._editor is not None and self._editor_contains(x, y):
            return  # the text box being typed handles its own clicks
        self.grab_focus()
        if self.session is None:
            return
        if button == 8:
            self.go_back()
        elif button == 9:
            self.go_forward()
        elif button == Gdk.BUTTON_SECONDARY:
            self._show_context_menu(x, y)
        elif button == Gdk.BUTTON_PRIMARY:
            if self._annotation_press(n_press, x, y):
                return
            self._press_link = self._link_at(x, y) if n_press == 1 else None
            if self._press_link is not None:
                return
            hit = self._text_hit(x, y)
            shift = gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
            if shift and self._selection is not None and n_press == 1:
                self._set_selection(self._selection.with_focus(hit))
            else:
                mode = {1: "char", 2: "word"}.get(n_press, "line")
                self._set_selection(Selection(hit, hit, mode) if hit is not None else None)

    def _on_released(self, gesture, n_press, x, y):
        if gesture.get_current_button() == Gdk.BUTTON_PRIMARY and self._annotation_release(x, y):
            return
        link, self._press_link = self._press_link, None
        if link is not None and gesture.get_current_button() == Gdk.BUTTON_PRIMARY:
            if self._link_at(x, y) is link:
                self.go_to_target(link.target)

    def _text_hit(self, x, y):
        hit = self._page_at(x, y, nearest=True)
        if hit is None:
            return None
        page, px, py = hit
        return page, self.session.text_now(page).index_at(px, py)

    def _on_select_drag(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if self._annotation_drag_update(sx + dx, sy + dy):
            return
        if self._selection is None or self._press_link is not None:
            return
        self._drag_point = (sx + dx, sy + dy)
        self._extend_selection()
        if not self._autoscroll_id:
            self._autoscroll_id = self.add_tick_callback(self._autoscroll)

    def _extend_selection(self):
        hit = self._text_hit(*self._drag_point)
        if hit is not None and hit != self._selection.focus:
            self._set_selection(self._selection.with_focus(hit))

    def _autoscroll(self, widget, clock):
        """While selecting, scroll when the pointer is dragged past the top or bottom edge."""
        if self._drag_point is None:
            self._autoscroll_id = 0
            return GLib.SOURCE_REMOVE
        y, height = self._drag_point[1], self._alloc[1]
        overshoot = y if y < 0 else max(y - height, 0)
        if overshoot:
            self._vadj.set_value(self._vadj.get_value() + overshoot * 0.3)
            self._extend_selection()
        return GLib.SOURCE_CONTINUE

    def _on_select_end(self, gesture, dx, dy):
        if self._annotation_drag_end():
            return
        self._drag_point = None
        if self.props.has_selection:
            self.get_primary_clipboard().set_content(
                Gdk.ContentProvider.new_for_value(self.selected_text()))

    def _on_pan_begin(self, gesture, x, y):
        self._pan_start = (self._hadj.get_value(), self._vadj.get_value())
        self._set_cursor("grabbing")

    def _on_pan_update(self, gesture, dx, dy):
        self._hadj.set_value(self._pan_start[0] - dx)
        self._vadj.set_value(self._pan_start[1] - dy)

    def _on_scroll(self, controller, dx, dy):
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return False  # plain scrolling is left to the scrolled window
        if self.session is not None and dy:
            if controller.get_unit() == Gdk.ScrollUnit.WHEEL:
                factor = 1.2 ** -dy
            else:
                factor = math.exp(-dy * 0.01)
            self.set_zoom(self._zoom * factor, self._pointer)
        return True

    def _on_pinch(self, gesture, scale):
        ok, x, y = gesture.get_bounding_box_center()
        self.set_zoom(self._pinch_zoom * scale, (x, y) if ok else None)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if self.session is None or state & Gdk.ModifierType.ALT_MASK:
            return False
        if self._annotation_key(keyval, state):
            return True
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        page_step = self._alloc[1] * 0.9
        can_scroll_x = self._hadj.get_upper() > self._hadj.get_page_size() + 1
        keys = Gdk
        if keyval in (keys.KEY_Home, keys.KEY_KP_Home):
            self.scroll_to(self._hadj.get_value(), 0)
        elif keyval in (keys.KEY_End, keys.KEY_KP_End):
            self.scroll_to(self._hadj.get_value(), self._vadj.get_upper())
        elif ctrl:
            # Handled here rather than as accelerators so that text entries keep these keys.
            if keyval in (keys.KEY_c, keys.KEY_C, keys.KEY_Insert):
                self.copy_selection()
            elif keyval in (keys.KEY_a, keys.KEY_A):
                self.select_all()
            elif keyval in (keys.KEY_Left, keys.KEY_KP_Left):
                self.props.rotation = self._rotation - 90
            elif keyval in (keys.KEY_Right, keys.KEY_KP_Right):
                self.props.rotation = self._rotation + 90
            else:
                return False
        elif keyval in (keys.KEY_Up, keys.KEY_KP_Up, keys.KEY_k):
            self.scroll_by(0, -SCROLL_STEP)
        elif keyval in (keys.KEY_Down, keys.KEY_KP_Down, keys.KEY_j):
            self.scroll_by(0, SCROLL_STEP)
        elif keyval in (keys.KEY_Page_Up, keys.KEY_KP_Page_Up, keys.KEY_BackSpace) or (
                keyval == keys.KEY_space and shift):
            self.scroll_by(0, -page_step)
        elif keyval in (keys.KEY_Page_Down, keys.KEY_KP_Page_Down, keys.KEY_space):
            self.scroll_by(0, page_step)
        elif keyval in (keys.KEY_Left, keys.KEY_KP_Left, keys.KEY_h):
            self.scroll_by(-SCROLL_STEP, 0) if can_scroll_x else self.previous_page()
        elif keyval in (keys.KEY_Right, keys.KEY_KP_Right, keys.KEY_l):
            self.scroll_by(SCROLL_STEP, 0) if can_scroll_x else self.next_page()
        elif keyval == keys.KEY_n:
            self.next_page()
        elif keyval == keys.KEY_p:
            self.previous_page()
        elif keyval == keys.KEY_Escape and self.props.has_selection:
            self.clear_selection()
        else:
            return False
        return True

    def _show_context_menu(self, x, y):
        menu = Gio.Menu()
        edit = Gio.Menu()
        for label, action, accel in (("_Copy", "win.copy", "<Control>c"),
                                     ("Select _All", "win.select-all", "<Control>a")):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("accel", GLib.Variant.new_string(accel))  # keys handled above
            edit.append_item(item)
        menu.append_section(None, edit)
        item, _ = self._item_at(x, y)
        if item is not None:
            self._choose(item.name)
            annotation = Gio.Menu()
            annotation.append("_Delete", "view.delete-annotation")
            menu.prepend_section(None, annotation)
        link = self._link_at(x, y)
        if link is not None and link.target.kind == "uri":
            self._context_uri = link.target.uri
            link_section = Gio.Menu()
            link_section.append("Copy _Link Address", "view.copy-link")
            menu.append_section(None, link_section)
        history = Gio.Menu()
        history.append("_Back", "win.back")
        history.append("_Forward", "win.forward")
        menu.append_section(None, history)

        if self._popover is not None:
            self._popover.unparent()
        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)
        self._popover.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()
