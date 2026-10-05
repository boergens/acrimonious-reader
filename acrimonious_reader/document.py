"""Opening PDFs with Poppler and turning their pages into textures.

Poppler is not thread-safe, so every call into it holds Document.lock. Nearly all of those calls run
on the session's worker thread (see session.py), which keeps the interface responsive.
"""

import math
import sys
import threading
from collections import namedtuple
from dataclasses import dataclass, field

import cairo
from gi.repository import Gdk, Gio, GLib, Poppler

from .selection import PageText

PT_TO_PX = 96 / 72  # at 100 % a page appears at its printed size on a 96 dpi screen

FIND_FLAGS = Poppler.FindFlags.MULTILINE | Poppler.FindFlags.IGNORE_DIACRITICS

# A rectangle of a page rendered at `scale` device pixels per point, in those device pixels.
Tile = namedtuple("Tile", "page rotation scale x y width height")

_OPAQUE_FORMAT = (
    Gdk.MemoryFormat.B8G8R8X8 if sys.byteorder == "little" else Gdk.MemoryFormat.X8R8G8B8
)

PAPER_SIZES = {  # millimetres
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "B5": (176, 250),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
    "Tabloid": (279.4, 431.8),
}


@dataclass(frozen=True)
class Target:
    """Where a link or outline entry leads."""

    kind: str  # "page", "uri", "named" (a viewer action such as NextPage) or "remote" (another file)
    page: int = -1
    top: float | None = None  # points below the top of the unrotated page
    uri: str = ""  # the address, the action name or the other file


@dataclass
class Link:
    rect: tuple  # x1, y1, x2, y2 in points, origin at the top left of the unrotated page
    target: Target


@dataclass
class OutlineEntry:
    title: str
    target: Target | None
    expanded: bool
    children: list = field(default_factory=list)


def rotated_size(width, height, rotation):
    return (height, width) if rotation in (90, 270) else (width, height)


def rotate_point(x, y, rotation, width, height):
    """Map a point on the unrotated page (width × height) onto the page turned clockwise."""
    if rotation == 90:
        return height - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, width - x
    return x, y


def unrotate_point(u, v, rotation, width, height):
    """The inverse of rotate_point()."""
    if rotation == 90:
        return v, height - u
    if rotation == 180:
        return width - u, height - v
    if rotation == 270:
        return width - v, u
    return u, v


def apply_rotation(cr, rotation, width, height):
    if rotation == 90:
        cr.translate(height, 0)
        cr.rotate(math.pi / 2)
    elif rotation == 180:
        cr.translate(width, height)
        cr.rotate(math.pi)
    elif rotation == 270:
        cr.translate(0, width)
        cr.rotate(3 * math.pi / 2)


def paper_description(width, height):
    """'A4 (210 × 297 mm)' for a page of width × height points."""
    mm_w, mm_h = width * 25.4 / 72, height * 25.4 / 72
    size = f"{mm_w:.0f} × {mm_h:.0f} mm"
    short, long_ = sorted((mm_w, mm_h))
    for name, (a, b) in PAPER_SIZES.items():
        if abs(short - a) < 2 and abs(long_ - b) < 2:
            return f"{name}{', landscape' if mm_w > mm_h else ''} ({size})"
    return size


def needs_password(error):
    return (
        isinstance(error, GLib.Error)
        and error.domain == GLib.quark_to_string(Poppler.error_quark())
        and error.code == Poppler.Error.ENCRYPTED
    )


def load(gfile, password=None):
    """Open a PDF. This blocks, so call it off the main thread. Raises GLib.Error."""
    path = gfile.get_path()
    if path is not None:
        pdoc = Poppler.Document.new_from_file(gfile.get_uri(), password)
    else:
        pdoc = Poppler.Document.new_from_gfile(gfile, password, None)
    document = Document(gfile, pdoc, password)
    if path is not None:  # annotations are read and saved with pikepdf, which needs a local file
        from . import pdfwrite

        try:
            items = pdfwrite.read(path, password)
        except Exception as error:  # a PDF Poppler can show but pikepdf cannot parse
            print(f"acrimonious-reader: cannot edit annotations of {path}: {error}")
        else:
            document.take_annotations(items)
    return document


def _match_continues(rect):
    # Poppler marks every rectangle but the last of a match that spans several lines.
    try:
        return rect.find_get_match_continued()
    except AttributeError:
        return False


class Document:
    """A loaded PDF and everything read from it up front (sizes, labels, outline, metadata)."""

    def __init__(self, gfile, pdoc, password=None):
        self.file = gfile
        self.password = password
        self.lock = threading.Lock()
        self._pdoc = pdoc
        self.editable = False  # whether annotations can be added and saved
        self.annotations = []  # the program's own annotations, as annotations.TextBox and .Ink items
        self.n_pages = pdoc.get_n_pages()
        self.page_sizes = []
        self.page_labels = []
        for i in range(self.n_pages):
            page = pdoc.get_page(i)
            width, height = page.get_size()
            self.page_sizes.append((max(width, 1.0), max(height, 1.0)))
            self.page_labels.append(page.get_label() or str(i + 1))
        self.has_page_labels = any(label != str(i + 1) for i, label in enumerate(self.page_labels))
        self.title = " ".join((pdoc.get_title() or "").split())
        self.outline = self._read_outline()
        self.properties = self._read_properties()

    def take_annotations(self, items):
        """Take over the program's own annotations from the file: the view draws them (so that they can be
        edited), so they are removed from Poppler's copy of the document."""
        names = {item.name for item in items}
        for page in sorted({item.page for item in items}):
            ppage = self._pdoc.get_page(page)
            for mapping in ppage.get_annot_mapping():
                if mapping.annot.get_name() in names:
                    ppage.remove_annot(mapping.annot)
        self.annotations = items
        self.editable = True

    @property
    def display_name(self):
        return self.title or self.file.get_basename() or self.file.get_uri()

    # Worker-side calls. Each one is safe to run on any thread.

    def render(self, tile):
        """Render a tile into a Gdk.Texture."""
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, tile.width, tile.height)
        cr = cairo.Context(surface)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.translate(-tile.x, -tile.y)
        cr.scale(tile.scale, tile.scale)
        apply_rotation(cr, tile.rotation, *self.page_sizes[tile.page])
        with self.lock:
            self._pdoc.get_page(tile.page).render(cr)
        surface.flush()
        data = GLib.Bytes.new(bytes(surface.get_data()))
        return Gdk.MemoryTexture.new(
            tile.width, tile.height, _OPAQUE_FORMAT, data, surface.get_stride()
        )

    def render_thumbnail(self, page, width):
        page_w, page_h = self.page_sizes[page]
        scale = width / page_w
        return self.render(Tile(page, 0, scale, 0, 0, width, max(1, round(page_h * scale))))

    def render_for_printing(self, page, cr):
        with self.lock:
            self._pdoc.get_page(page).render_for_printing(cr)

    def links(self, page):
        height = self.page_sizes[page][1]
        links = []
        with self.lock:
            for mapping in self._pdoc.get_page(page).get_link_mapping():
                target = self._target(mapping.action)
                if target is not None:
                    a = mapping.area
                    links.append(Link((a.x1, height - a.y2, a.x2, height - a.y1), target))
        return links

    def text(self, page):
        with self.lock:
            ppage = self._pdoc.get_page(page)
            text = ppage.get_text() or ""
            ok, rects = ppage.get_text_layout()
        return PageText(text, [(r.x1, r.y1, r.x2, r.y2) for r in rects] if ok else [])

    def plain_text(self, page):
        with self.lock:
            return self._pdoc.get_page(page).get_text() or ""

    def find(self, page, query, flags):
        """Matches of query on a page; each match is a list of rectangles (one per line)."""
        height = self.page_sizes[page][1]
        with self.lock:
            rects = self._pdoc.get_page(page).find_text_with_options(query, flags)
        matches, current = [], []
        for r in rects:
            current.append((r.x1, height - r.y2, r.x2, height - r.y1))
            if not _match_continues(r):
                matches.append(current)
                current = []
        if current:
            matches.append(current)
        return matches

    # Reading the document when it is opened.

    def _target(self, action):
        kind = action.type
        if kind == Poppler.ActionType.GOTO_DEST:
            return self._dest_target(action.goto_dest.dest)
        if kind == Poppler.ActionType.URI and action.uri.uri:
            return Target("uri", uri=action.uri.uri)
        if kind == Poppler.ActionType.NAMED and action.named.named_dest:
            return Target("named", uri=action.named.named_dest)
        if kind == Poppler.ActionType.GOTO_REMOTE and action.goto_remote.file_name:
            return Target("remote", uri=action.goto_remote.file_name)
        return None

    def _dest_target(self, dest):
        if dest is not None and dest.type == Poppler.DestType.NAMED:
            dest = self._pdoc.find_dest(dest.named_dest)
        if dest is None or not 1 <= dest.page_num <= self.n_pages:
            return None
        page = dest.page_num - 1
        top = None
        # change_top is a C bitfield that introspection cannot read, but Poppler stores an unset top
        # as 0 (the very bottom of the page), so a positive top is a real one.
        kinds = (Poppler.DestType.XYZ, Poppler.DestType.FITH, Poppler.DestType.FITBH, Poppler.DestType.FITR)
        if dest.type in kinds and dest.top > 0:
            height = self.page_sizes[page][1]
            top = height - min(max(dest.top, 0.0), height)
        return Target("page", page=page, top=top)

    def _read_outline(self):
        try:
            iter_ = Poppler.IndexIter.new(self._pdoc)
        except TypeError:  # PyGObject raises when the constructor returns NULL: no outline
            return []
        return self._walk_outline(iter_) if iter_ is not None else []

    def _walk_outline(self, iter_):
        entries = []
        while True:
            action = iter_.get_action()
            title = " ".join((action.any.title or "").split())
            child = iter_.get_child()
            children = self._walk_outline(child) if child is not None else []
            entries.append(OutlineEntry(title, self._target(action), iter_.is_open(), children))
            if not iter_.next():
                return entries

    def _read_properties(self):
        d = self._pdoc

        def date(value):
            return value.to_local().format("%-d %B %Y, %H:%M") if value is not None else ""

        try:
            info = self.file.query_info("standard::size", Gio.FileQueryInfoFlags.NONE, None)
            file_size = GLib.format_size(info.get_size())
        except GLib.Error:
            file_size = ""
        return [
            ("Title", self.title),
            ("Subject", d.get_subject() or ""),
            ("Author", d.get_author() or ""),
            ("Keywords", d.get_keywords() or ""),
            ("Creator", d.get_creator() or ""),
            ("Producer", d.get_producer() or ""),
            ("Created", date(d.get_creation_date_time())),
            ("Modified", date(d.get_modification_date_time())),
            ("Pages", str(self.n_pages)),
            ("Page Size", paper_description(*self.page_sizes[0]) if self.n_pages else ""),
            ("Format", (d.get_pdf_version_string() or "").replace("-", " ")),
            ("Optimized for Web", "Yes" if d.is_linearized() else "No"),
            ("File Size", file_size),
            ("Location", self.file.get_parse_name()),
        ]
