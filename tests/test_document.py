import time

import pytest
from gi.repository import Gio, GLib

from acrimonious_reader import document
from acrimonious_reader.document import FIND_FLAGS, Tile
from acrimonious_reader.search import Search
from acrimonious_reader.session import Session
from acrimonious_reader.state import StateStore


@pytest.fixture(scope="module")
def doc(sample_pdf):
    return document.load(Gio.File.new_for_path(str(sample_pdf)))


def test_basic_facts(doc):
    assert doc.n_pages == 7
    assert doc.title == "Acrimonious Reader Sample Document"
    assert doc.page_labels[:3] == ["1", "2", "3"]
    assert not doc.has_page_labels
    assert doc.page_sizes[3][0] > doc.page_sizes[3][1]  # chapter 3 is landscape
    properties = dict(doc.properties)
    assert properties["Author"] == "Acrimonious Reader test suite"
    assert properties["Page Size"].startswith("A4")


def test_outline(doc):
    assert [entry.title for entry in doc.outline] == [f"Chapter {n}" for n in range(1, 7)]
    chapter = doc.outline[1]
    assert chapter.target.kind == "page" and chapter.target.page == 2
    assert chapter.target.top == pytest.approx(83, abs=1)  # the heading, not the page top
    section = chapter.children[0]
    assert section.title == "Section 2.1" and section.target.top == pytest.approx(300, abs=1)


def test_links(doc):
    links = doc.links(0)
    pages = sorted(link.target.page for link in links if link.target.kind == "page")
    assert pages == [1, 2, 3, 4, 5, 6]
    (web,) = [link for link in links if link.target.kind == "uri"]
    assert web.target.uri == "https://www.gnome.org"
    x1, y1, x2, y2 = web.rect
    assert x1 < x2 and y1 < y2 and 400 < y1 < 430  # top-left origin, as drawn


def test_find_and_text(doc):
    matches = doc.find(1, "quick brown", FIND_FLAGS)
    assert len(matches) == 3
    assert all(len(match) == 1 for match in matches)
    assert doc.find(1, "QUICK BROWN", FIND_FLAGS)  # case-insensitive by default
    text = doc.text(1)
    assert text.text.startswith("Chapter 1")
    assert len(text.text) == len(text.rects)


def test_render(doc):
    texture = doc.render(Tile(3, 90, 1.5, 0, 0, 300, 200))
    assert (texture.get_width(), texture.get_height()) == (300, 200)
    thumbnail = doc.render_thumbnail(0, 120)
    assert thumbnail.get_width() == 120 and thumbnail.get_height() == 170


def test_password(locked_pdf):
    gfile = Gio.File.new_for_path(str(locked_pdf))
    with pytest.raises(GLib.Error) as info:
        document.load(gfile)
    assert document.needs_password(info.value)
    with pytest.raises(GLib.Error) as info:
        document.load(gfile, "wrong")
    assert document.needs_password(info.value)
    assert document.load(gfile, "secret").n_pages == 7


def test_not_a_pdf(tmp_path):
    path = tmp_path / "notes.pdf"
    path.write_text("not a PDF at all")
    with pytest.raises(GLib.Error) as info:
        document.load(Gio.File.new_for_path(str(path)))
    assert not document.needs_password(info.value)


def _wait(condition, timeout=10):
    context, deadline = GLib.MainContext.default(), time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        context.iteration(False)
        time.sleep(0.005)


def test_search_through_the_worker(doc):
    session = Session(doc)
    try:
        search = Search()
        revealed = []
        search.connect("reveal", lambda _, page, index: revealed.append((page, index)))
        search.start(session, "quick brown", from_page=3)
        _wait(lambda: not search.running)
        assert search.total == 18
        assert revealed[0] == (3, 0)  # the first match at or after the current page
        assert search.position == 7
        search.step(False, 3)
        assert search.current == (2, 2)
        search.step(True, 3)
        search.step(True, 3)
        assert search.current == (3, 1)
        search.start(session, "quick brown", case_sensitive=True, whole_words=True)
        _wait(lambda: not search.running)
        assert search.total == 18
        search.start(session, "Quick Brown", case_sensitive=True)
        _wait(lambda: not search.running)
        assert search.total == 0
    finally:
        session.close()


def test_session_caches(doc):
    session = Session(doc)
    try:
        ready = []
        session.connect("page-data-ready", lambda _, page: ready.append(page))
        assert session.links(0) is None  # requested in the background
        assert session.text(1) is None
        _wait(lambda: len(ready) == 2)
        assert len(session.links(0)) == 7
        assert session.text(1).text.startswith("Chapter 1")
        tile = Tile(0, 0, 1.0, 0, 0, 100, 100)
        session.request_tiles([(tile, 1)])
        _wait(lambda: tile in session.tiles)
        assert session.tiles.get(tile).get_width() == 100
    finally:
        session.close()


def test_state_store(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.set_document("file:///a.pdf", {"page": 4})
    store.set_preference("night-mode", True)
    store.save()
    again = StateStore(path)
    assert again.document("file:///a.pdf") == {"page": 4}
    assert again.preference("night-mode") is True
    assert again.document("file:///missing.pdf") is None
