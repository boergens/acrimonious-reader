"""Saving annotations into the PDF as standard FreeText and Ink annotations, and reading them back.

Poppler can display PDFs but cannot write ink annotations, so this uses pikepdf. Every annotation gets
an appearance drawn by the same cairo code that draws it on screen, so it looks the same in any PDF
viewer. The program's own description of the item travels along in a private /AcrimoniousItem entry,
which lets a saved file be edited again.
"""

import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

import cairo
import pikepdf

from .annotations import NAME_PREFIX, TextBox, item_from_json, item_to_json
from .document import rotate_point, unrotate_point

ITEM_KEY = pikepdf.Name("/AcrimoniousItem")
# Files saved while the program was still called Quire use these markers.
LEGACY_PREFIX = "quire-"
LEGACY_ITEM_KEY = pikepdf.Name("/QuireItem")


class _Geometry:
    """Maps between Poppler's page space (points, origin top left, rotation applied) and the PDF's
    user space (origin bottom left of the media, unrotated)."""

    def __init__(self, page):
        x0, y0, x1, y1 = (float(v) for v in _inherited(page, "/CropBox") or _inherited(page, "/MediaBox"))
        self.x0, self.y0 = min(x0, x1), min(y0, y1)
        self.x1, self.y1 = max(x0, x1), max(y0, y1)
        self.rotation = int(_inherited(page, "/Rotate") or 0) % 360

    def to_user(self, u, v):
        a, b = unrotate_point(u, v, self.rotation, self.x1 - self.x0, self.y1 - self.y0)
        return self.x0 + a, self.y1 - b

    def from_user(self, x, y):
        return rotate_point(x - self.x0, self.y1 - y, self.rotation, self.x1 - self.x0, self.y1 - self.y0)


def _inherited(page, key):
    node = page
    while node is not None:
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _is_ours(annot):
    name = annot.get("/NM")
    return (isinstance(annot, pikepdf.Dictionary) and name is not None
            and str(name).startswith((NAME_PREFIX, LEGACY_PREFIX)))


def read(path, password=None):
    """The program's own annotations in a PDF file, adjusted for moves other programs may have made."""
    items = []
    with pikepdf.open(path, password=password or "") as pdf:
        for index, page in enumerate(pdf.pages):
            for annot in page.obj.get("/Annots") or ():
                key = ITEM_KEY if ITEM_KEY in annot else LEGACY_ITEM_KEY
                if not _is_ours(annot) or key not in annot:
                    continue
                try:
                    data = json.loads(str(annot[key]))
                    item = item_from_json(data, index)
                except (ValueError, KeyError, TypeError):
                    continue
                geometry = _Geometry(page.obj)
                stored, rect = data.get("rect"), [float(v) for v in annot.Rect]
                if stored and max(abs(a - b) for a, b in zip(stored, rect)) > 0.01:
                    (u0, v0), (u1, v1) = geometry.from_user(*stored[:2]), geometry.from_user(*rect[:2])
                    item = item.moved(u1 - u0, v1 - v0)
                if isinstance(item, TextBox) and "/Contents" in annot and str(annot.Contents) != item.text:
                    item = TextBox(item.page, item.x, item.y, str(annot.Contents), item.size, item.color, item.name)
                items.append(item)
    return items


def save(source, items, destination, password=None):
    """Write `source` with `items` as its annotations to `destination` (may be the same file).

    Returns True if the destination was overwritten in place rather than replaced. Anything still
    reading the old file then sees the new bytes, so a document open on it must be reloaded."""
    with pikepdf.open(source, password=password or "") as pdf:
        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            if annots is not None and any(_is_ours(a) for a in annots):
                kept = [a for a in annots if not _is_ours(a)]
                if kept:
                    page.obj.Annots = pdf.make_indirect(pikepdf.Array(kept))
                else:
                    del page.obj["/Annots"]
        appearance_sources = []  # the appearances' own PDFs must outlive the save
        for item in items:
            page = pdf.pages[item.page].obj
            annot, appearance = _annotation(pdf, item, _Geometry(page))
            appearance_sources.append(appearance)
            if "/Annots" not in page:
                page.Annots = pdf.make_indirect(pikepdf.Array())
            page.Annots.append(annot)
        # Write a temporary file next to the destination and swap it in, so that a failed save
        # never leaves half a file behind.
        folder = os.path.dirname(os.path.abspath(destination))
        try:
            fd, temporary = tempfile.mkstemp(dir=folder, prefix=".acrimonious-", suffix=".pdf")
        except PermissionError:  # a read-only folder: the file itself may still be writable
            fd, temporary = tempfile.mkstemp(prefix=".acrimonious-", suffix=".pdf")
        os.close(fd)
        try:
            pdf.save(temporary, encryption=pdf.is_encrypted or None)
            mode = os.stat(destination).st_mode if os.path.exists(destination) else 0o666 & ~_umask()
            os.chmod(temporary, mode & 0o7777)
            try:
                os.replace(temporary, destination)
                return False
            except OSError:
                # Allowed to write the file but not to replace it (someone else's file in a shared
                # folder, or a read-only folder): overwrite it in place instead.
                shutil.copyfile(temporary, destination)
                os.unlink(temporary)
                return True
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _appearance(pdf, item, bounds):
    """A form XObject drawing the item upright, made with cairo and copied into `pdf`."""
    x1, y1, x2, y2 = bounds
    buffer = io.BytesIO()
    surface = cairo.PDFSurface(buffer, x2 - x1, y2 - y1)
    cr = cairo.Context(surface)
    cr.translate(-x1, -y1)
    item.draw(cr)
    surface.finish()
    source = pikepdf.open(io.BytesIO(buffer.getvalue()))
    return pdf.copy_foreign(source.pages[0].as_form_xobject()), source


def _annotation(pdf, item, geometry):
    bounds = item.bounds()
    appearance, source = _appearance(pdf, item, bounds)
    if geometry.rotation:
        # The page is turned clockwise when shown, so turn the appearance back to keep it upright.
        r = geometry.rotation
        cos, sin = _cos(r), _sin(r)
        appearance.Matrix = pikepdf.Array([cos, sin, -sin, cos, 0, 0])
    x1, y1, x2, y2 = bounds
    corners = [geometry.to_user(u, v) for u, v in ((x1, y1), (x2, y1), (x1, y2), (x2, y2))]
    rect = [min(x for x, _ in corners), min(y for _, y in corners),
            max(x for x, _ in corners), max(y for _, y in corners)]
    data = item_to_json(item)
    data["rect"] = rect
    del data["page"]
    entries = {
        "/Type": pikepdf.Name.Annot,
        "/Rect": rect,
        "/NM": pikepdf.String(item.name),
        "/F": 4,  # print it
        "/M": pikepdf.String(datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")),
        "/AP": pikepdf.Dictionary({"/N": appearance}),
        str(ITEM_KEY): pikepdf.String(json.dumps(data, separators=(",", ":"))),
    }
    r, g, b = item.color
    if isinstance(item, TextBox):
        entries.update({
            "/Subtype": pikepdf.Name.FreeText,
            "/Contents": pikepdf.String(item.text),
            "/DA": pikepdf.String(f"/Helv {item.size:g} Tf {r:g} {g:g} {b:g} rg"),
            "/BS": pikepdf.Dictionary({"/W": 0}),
            "/Border": [0, 0, 0],
        })
    else:
        entries.update({
            "/Subtype": pikepdf.Name.Ink,
            "/InkList": [[c for u, v in stroke for c in geometry.to_user(u, v)] for stroke in item.strokes],
            "/C": [r, g, b],
            "/BS": pikepdf.Dictionary({"/W": item.width, "/S": pikepdf.Name.S}),
        })
    return pdf.make_indirect(pikepdf.Dictionary(entries)), source


def _cos(degrees):
    return {0: 1, 90: 0, 180: -1, 270: 0}[degrees]


def _sin(degrees):
    return {0: 0, 90: 1, 180: 0, 270: -1}[degrees]
