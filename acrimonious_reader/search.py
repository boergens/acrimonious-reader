"""Finding text in the whole document, one page at a time on the worker thread."""

import bisect

from gi.repository import GObject, Poppler

from .document import FIND_FLAGS
from .session import PRIORITY_SEARCH


class Search(GObject.Object):
    """Search results for one query. Pages are searched starting at the current page and wrapping
    around, so the nearest matches turn up first."""

    __gtype_name__ = "AcrimoniousSearch"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The current match moved to (page, index in that page's matches); scroll it into view.
        "reveal": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
    }

    def __init__(self):
        super().__init__()
        self._session = None
        self._generation = 0
        self._results = {}  # page -> list of matches; a match is a list of rectangles
        self._pages = []  # pages with matches, sorted
        self._queue = []  # pages still to search, next one last
        self._flags = FIND_FLAGS
        self.query = ""
        self.current = None  # (page, index)
        self.running = False

    def start(self, session, query, from_page=0, case_sensitive=False, whole_words=False):
        self.clear(notify=False)
        self._session = session
        self.query = query
        if session is not None and query:
            self._flags = FIND_FLAGS
            if case_sensitive:
                self._flags |= Poppler.FindFlags.CASE_SENSITIVE
            if whole_words:
                self._flags |= Poppler.FindFlags.WHOLE_WORDS_ONLY
            n = session.document.n_pages
            self._queue = [(from_page + k) % n for k in reversed(range(n))]
            self.running = True
            self._search_next(self._generation)
        self.emit("changed")

    def clear(self, notify=True):
        self._generation += 1
        if self._session is not None:
            self._session.renderer.discard(lambda key: key[0] == "search")
        self._results, self._pages, self._queue = {}, [], []
        self.query = ""
        self.current = None
        self.running = False
        if notify:
            self.emit("changed")

    def matches(self, page):
        return self._results.get(page, ())

    @property
    def total(self):
        return sum(len(matches) for matches in self._results.values())

    @property
    def position(self):
        """1-based number of the current match among all matches, or 0."""
        if self.current is None:
            return 0
        page, index = self.current
        before = bisect.bisect_left(self._pages, page)
        return sum(len(self._results[p]) for p in self._pages[:before]) + index + 1

    def step(self, forward, from_page):
        """Make the next (or previous) match current, starting from from_page if there is none."""
        pages = self._pages
        if not pages:
            return
        if self.current is None:
            if forward:
                page = pages[bisect.bisect_left(pages, from_page) % len(pages)]
            else:
                page = pages[bisect.bisect_right(pages, from_page) - 1]
            index = 0 if forward else len(self._results[page]) - 1
        else:
            page, index = self.current
            index += 1 if forward else -1
            if not 0 <= index < len(self._results[page]):
                page = pages[(pages.index(page) + (1 if forward else -1)) % len(pages)]
                index = 0 if forward else len(self._results[page]) - 1
        self.current = (page, index)
        self.emit("reveal", page, index)
        self.emit("changed")

    def _search_next(self, generation):
        if generation != self._generation:
            return
        if not self._queue:
            self.running = False
            self.emit("changed")
            return
        page = self._queue.pop()
        document, query, flags = self._session.document, self.query, self._flags
        self._session.renderer.submit(
            ("search", generation, page), PRIORITY_SEARCH,
            lambda: document.find(page, query, flags),
            lambda matches: self._found(generation, page, matches),
            default=[],
        )

    def _found(self, generation, page, matches):
        if generation != self._generation:
            return
        if matches:
            self._results[page] = matches
            bisect.insort(self._pages, page)
            if self.current is None:
                self.current = (page, 0)
                self.emit("reveal", page, 0)
            self.emit("changed")
        self._search_next(generation)
