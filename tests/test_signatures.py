import pytest

from acrimonious_reader.annotations import Ink, TextBox
from acrimonious_reader.signatures import Signature, SignatureStore


def test_signature_is_stored_relative_to_its_top_left():
    signature = Signature.from_strokes([[(110, 220), (150, 205)], [(160, 230)]], 225, 2.0, (0, 0, 0.6))
    assert signature.strokes == (((0, 15), (40, 0)), ((50, 25),))
    assert signature.baseline == 20
    assert signature.size == (50, 25)


def test_placing_puts_the_baseline_at_the_click():
    signature = Signature.from_strokes([[(0, 0), (40, 30)]], 20, 1.5, (0, 0, 0))
    ink = signature.place(3, 100, 500)
    assert ink.page == 3 and ink.strokes == (((100, 480), (140, 510)),)
    assert ink.width == 1.5 and ink.name != signature.name


def test_signature_from_a_drawing_on_a_page():
    ink = Ink(2, (((300, 600), (360, 640)),), (0, 0, 1), 2.5)
    signature = Signature.from_ink(ink)
    assert signature.strokes == (((0, 0), (60, 40)),)
    assert signature.baseline == pytest.approx(30)
    assert (signature.width, signature.color) == (2.5, (0, 0, 1))
    assert signature.place(2, 300, 630).strokes == ink.strokes  # put back where it came from


def test_store_keeps_signatures_between_sessions_and_privately(tmp_path):
    path = tmp_path / "data" / "signatures.json"
    store = SignatureStore(path)
    changes = []
    store.connect("changed", lambda *_: changes.append(1))
    first = Signature.from_strokes([[(0, 0), (10, 5)]], 4, 1.5, (0, 0, 0))
    second = Signature.from_strokes([[(0, 0), (3, 3)]], 2, 1.0, (0, 0, 1))
    store.add(first)
    store.add(second)
    assert path.stat().st_mode & 0o777 == 0o600
    assert SignatureStore(path).signatures == (first, second)  # a new session reads them back
    store.remove(first.name)
    assert SignatureStore(path).signatures == (second,)
    assert len(changes) == 3
    assert not list(path.parent.glob(".signatures-*"))


def test_broken_store_file_is_ignored(tmp_path):
    path = tmp_path / "signatures.json"
    path.write_text("{not json")
    assert SignatureStore(path).signatures == ()


def test_scaling_annotations():
    ink = Ink(0, (((10, 10), (30, 20)),), width=2)
    bigger = ink.scaled(2, 10, 10)
    assert bigger.strokes == (((10, 10), (50, 30)),) and bigger.width == 4
    box = TextBox(0, 20, 20, "Hi", size=10)
    half = box.scaled(0.5, 10, 10)
    assert (half.x, half.y, half.size) == (15, 15, 5)
