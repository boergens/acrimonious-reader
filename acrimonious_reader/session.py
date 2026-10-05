"""Per-document runtime state: the worker thread that talks to Poppler, and the caches it fills."""

import itertools
import threading
import traceback
from collections import OrderedDict
from functools import partial

from gi.repository import GLib, GObject

from .annotations import Annotations
from .selection import PageText

# Worker priorities, most urgent first.
PRIORITY_DATA = 0  # links and text of pages on screen: cheap and needed for interaction
PRIORITY_VISIBLE = 1
PRIORITY_PREFETCH = 2
PRIORITY_SEARCH = 3
PRIORITY_THUMBNAIL = 4

TILE_BUDGET = 400 * 1024 * 1024  # bytes of rendered tiles to keep
THUMBNAIL_LIMIT = 400
TEXT_LIMIT = 64  # pages of text layout to keep (they are large in Python)
STALE_SECONDS = 5  # after a reload, how long the old version's tiles may stand in

_NO_RESULT = object()


class _Job:
    __slots__ = ("key", "priority", "order", "func", "callback", "default")


class Renderer:
    """Runs jobs one at a time on a worker thread, most urgent first.

    Results come back on the main thread. A job that is resubmitted while still queued is updated
    rather than duplicated, and one that is already running is not queued again.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._jobs = {}
        self._running = set()
        self._counter = itertools.count()
        self._stopped = False
        threading.Thread(target=self._work, name="acrimonious-worker", daemon=True).start()

    def submit(self, key, priority, func, callback, order=None, default=_NO_RESULT):
        """Queue func() on the worker; callback(result) then runs on the main thread.

        Jobs of equal priority run in ascending `order`, by default newest first. If func raises
        and a default is given, callback receives the default instead.
        """
        with self._cond:
            if self._stopped or key in self._running:
                return
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = _Job()
                job.key = key
            job.priority = priority
            job.order = -next(self._counter) if order is None else order
            job.func, job.callback, job.default = func, callback, default
            self._cond.notify()

    def discard(self, predicate):
        """Drop queued jobs whose key matches."""
        with self._cond:
            for key in [key for key in self._jobs if predicate(key)]:
                del self._jobs[key]

    def stop(self):
        with self._cond:
            self._stopped = True
            self._jobs.clear()
            self._cond.notify()

    def _work(self):
        while True:
            with self._cond:
                while not self._jobs and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                job = min(self._jobs.values(), key=lambda j: (j.priority, j.order))
                del self._jobs[job.key]
                self._running.add(job.key)
            try:
                result = job.func()
            except Exception:
                traceback.print_exc()
                result = job.default
            GLib.idle_add(self._deliver, job, result)

    def _deliver(self, job, result):
        with self._cond:
            self._running.discard(job.key)
            stopped = self._stopped
        if not stopped and result is not _NO_RESULT:
            job.callback(result)
        return GLib.SOURCE_REMOVE


class TileCache:
    """Rendered tiles, evicted least recently used once they exceed a memory budget."""

    def __init__(self, budget):
        self._budget = budget
        self._used = 0
        self._lru = OrderedDict()  # Tile -> Gdk.Texture
        self._by_page = {}  # (page, rotation) -> {scale: {Tile: Gdk.Texture}}

    def __contains__(self, tile):
        return tile in self._lru

    def get(self, tile):
        texture = self._lru.get(tile)
        if texture is not None:
            self._lru.move_to_end(tile)
        return texture

    def put(self, tile, texture):
        if tile in self._lru:
            return
        self._lru[tile] = texture
        self._used += tile.width * tile.height * 4
        scales = self._by_page.setdefault((tile.page, tile.rotation), {})
        scales.setdefault(tile.scale, {})[tile] = texture
        while self._used > self._budget and len(self._lru) > 1:
            self._evict()

    def scales(self, page, rotation):
        """All cached tiles of a page: {scale: {Tile: texture}}."""
        return self._by_page.get((page, rotation), {})

    def _evict(self):
        tile, _ = self._lru.popitem(last=False)
        self._used -= tile.width * tile.height * 4
        scales = self._by_page[(tile.page, tile.rotation)]
        tiles = scales[tile.scale]
        del tiles[tile]
        if not tiles:
            del scales[tile.scale]
        if not scales:
            del self._by_page[(tile.page, tile.rotation)]


class Session(GObject.Object):
    """One open document with its worker thread and caches."""

    __gtype_name__ = "AcrimoniousSession"
    __gsignals__ = {
        "tile-ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "thumbnail-ready": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "page-data-ready": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, document, previous=None):
        """previous: the session of an earlier version of the same file. Its pictures stand in
        while this version renders, so that reloading does not flash blank pages."""
        super().__init__()
        self.document = document
        self.renderer = Renderer()
        self.tiles = TileCache(TILE_BUDGET)
        self._stale_tiles = previous.tiles if previous is not None else None
        self._thumbnails = OrderedDict()  # (page, width) -> texture
        self._latest_thumbnail = dict(previous._latest_thumbnail) if previous is not None else {}
        self._links = {}
        self._texts = OrderedDict()
        self.annotations = Annotations(document.annotations)
        if previous is not None:
            GLib.timeout_add_seconds(STALE_SECONDS, self._drop_stale)

    def close(self):
        self.renderer.stop()
        self._stale_tiles = None

    def _drop_stale(self):
        self._stale_tiles = None
        return GLib.SOURCE_REMOVE

    # Tiles

    def request_tiles(self, wanted):
        """Render the (tile, priority) pairs in order and drop queued tiles not among them."""
        keep = set()
        for order, (tile, priority) in enumerate(wanted):
            key = ("tile", tile)
            keep.add(key)
            if tile not in self.tiles:
                self.renderer.submit(
                    key, priority, partial(self.document.render, tile),
                    partial(self._tile_done, tile), order=order,
                )
        self.renderer.discard(lambda key: key[0] == "tile" and key not in keep)

    def stale_tile(self, tile):
        """The same tile from the previous version of the document, if still around."""
        return self._stale_tiles.get(tile) if self._stale_tiles is not None else None

    def _tile_done(self, tile, texture):
        self.tiles.put(tile, texture)
        self.emit("tile-ready")

    # Thumbnails

    def thumbnail(self, page, width):
        """The thumbnail of a page, or None while it is being rendered."""
        key = (page, width)
        texture = self._thumbnails.get(key)
        if texture is not None:
            self._thumbnails.move_to_end(key)
            return texture
        self.renderer.submit(
            ("thumbnail", page, width), PRIORITY_THUMBNAIL,
            partial(self.document.render_thumbnail, page, width),
            partial(self._thumbnail_done, key),
        )
        return None

    def any_thumbnail(self, page):
        return self._latest_thumbnail.get(page)

    def _thumbnail_done(self, key, texture):
        self._thumbnails[key] = texture
        self._latest_thumbnail[key[0]] = texture
        while len(self._thumbnails) > THUMBNAIL_LIMIT:
            (page, _), old = self._thumbnails.popitem(last=False)
            if self._latest_thumbnail.get(page) is old:
                del self._latest_thumbnail[page]
        self.emit("thumbnail-ready", key[0])

    # Links and text

    def links(self, page):
        """The links on a page, or None while they are being read."""
        links = self._links.get(page)
        if links is None:
            self.renderer.submit(
                ("links", page), PRIORITY_DATA, partial(self.document.links, page),
                partial(self._links_done, page), default=[],
            )
        return links

    def _links_done(self, page, links):
        self._links[page] = links
        self.emit("page-data-ready", page)

    def text(self, page):
        """The text layout of a page, or None while it is being read."""
        text = self._texts.get(page)
        if text is None:
            self.renderer.submit(
                ("text", page), PRIORITY_DATA, partial(self.document.text, page),
                partial(self._text_done, page), default=PageText("", []),
            )
        else:
            self._texts.move_to_end(page)
        return text

    def text_now(self, page):
        """The text layout of a page, read on the spot if necessary (this may block briefly)."""
        text = self._texts.get(page)
        if text is None:
            self.renderer.discard(lambda key: key == ("text", page))
            text = self.document.text(page)
            self._store_text(page, text)
        return text

    def _text_done(self, page, text):
        self._store_text(page, text)
        self.emit("page-data-ready", page)

    def _store_text(self, page, text):
        self._texts[page] = text
        self._texts.move_to_end(page)
        while len(self._texts) > TEXT_LIMIT:
            self._texts.popitem(last=False)
