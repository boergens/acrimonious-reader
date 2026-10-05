import cairo
import pikepdf
import pytest
from gi.repository import Gio, Poppler

from acrimonious_reader import document, pdfwrite
from acrimonious_reader.annotations import Annotations, Ink, TextBox


def test_text_box_geometry():
    short, long_ = TextBox(0, 100, 200, "Hi"), TextBox(0, 100, 200, "Hello there, world")
    x1, y1, x2, y2 = short.bounds()
    assert x1 < 100 < x2 and y1 < 200 < y2
    assert long_.bounds()[2] > x2
    assert 11 < y2 - y1 < 20  # one line of 11 pt text
    assert short.hit(101, 205, 0) and not short.hit(300, 205, 0)
    assert TextBox(0, 0, 0, "a\nb").bounds()[3] > TextBox(0, 0, 0, "a").bounds()[3]
    assert short.moved(5, 5).bounds()[0] == pytest.approx(x1 + 5)


def test_ink_geometry():
    ink = Ink(0, (((10, 10), (50, 10)), ((10, 40),)), width=2)
    assert ink.bounds() == (8, 8, 52, 42)
    assert ink.stroke_at(30, 11, 0) == 0
    assert ink.stroke_at(10.5, 40.5, 0) == 1  # a single dot
    assert ink.stroke_at(30, 25, 2) is None
    assert ink.moved(1, 2).strokes[0][0] == (11, 12)


def test_undo_redo_and_modified():
    box = TextBox(0, 10, 10, "one")
    model = Annotations([box])
    assert not model.modified and not model.can_undo
    model.replace(TextBox(0, 10, 10, "two", name=box.name))
    model.add(Ink(1, (((0, 0), (5, 5)),)))
    assert model.modified and len(model.items) == 2 and model.find(box.name).text == "two"
    model.undo()
    model.undo()
    assert not model.modified and model.items == (box,)
    model.redo()
    assert model.find(box.name).text == "two"
    model.mark_saved(model.items)
    assert not model.modified
    assert model.item_at(0, 12, 15, 1).name == box.name and model.item_at(1, 12, 15, 1) is None


def _render(path, page, scale=2.0):
    pdoc = Poppler.Document.new_from_file(Gio.File.new_for_path(str(path)).get_uri(), None)
    ppage = pdoc.get_page(page)
    width, height = ppage.get_size()
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, int(width * scale), int(height * scale))
    cr = cairo.Context(surface)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.scale(scale, scale)
    ppage.render(cr)
    surface.flush()
    return surface


def _changed_box(before, after, scale=2.0):
    """Bounding box, in points, of the pixels that differ between two renders."""
    a, b = bytes(before.get_data()), bytes(after.get_data())
    stride, width = before.get_stride(), before.get_width()
    xs, ys = [], []
    for y in range(before.get_height()):
        row = slice(y * stride, y * stride + width * 4)
        if a[row] != b[row]:
            ys.append(y)
            xs += [x for x in range(width) if a[y * stride + x * 4: y * stride + x * 4 + 3] != b[y * stride + x * 4: y * stride + x * 4 + 3]]
    return min(xs) / scale, min(ys) / scale, (max(xs) + 1) / scale, (max(ys) + 1) / scale


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_saved_annotations_appear_where_drawn(sample_pdf, tmp_path, rotation):
    # A page with a rotation and a crop box offset, to exercise the coordinate mapping.
    source = tmp_path / "source.pdf"
    with pikepdf.open(sample_pdf) as pdf:
        page = pdf.pages[1].obj
        page.Rotate = rotation
        page.CropBox = [20, 30, 560, 800]
        pdf.save(source)
    text = TextBox(1, 100, 60, "Upright text that is wide", size=14)
    ink = Ink(1, (((80, 300), (200, 340), (260, 300)),), color=(0, 0, 0.6), width=3)
    saved = tmp_path / "saved.pdf"
    pdfwrite.save(str(source), [text, ink], str(saved))

    before = _render(source, 1)
    for item in (text, ink):
        pdfwrite.save(str(source), [item], str(saved))
        x1, y1, x2, y2 = _changed_box(before, _render(saved, 1))
        bx1, by1, bx2, by2 = item.bounds()
        assert bx1 - 1 <= x1 and y1 >= by1 - 1 and x2 <= bx2 + 1 and y2 <= by2 + 1, (item.kind, rotation)
        assert (x2 - x1) > 0.6 * (bx2 - bx1) and (y2 - y1) > 0.5 * (by2 - by1)  # upright, not turned


def test_round_trip_and_resave(sample_pdf, tmp_path):
    text = TextBox(0, 90, 470, "University of Illinois Chicago", color=(0, 0, 0.5))
    ink = Ink(4, (((100, 600), (150, 640), (200, 610)), ((210, 620),)), width=2)
    out = tmp_path / "out.pdf"
    pdfwrite.save(str(sample_pdf), [text, ink], str(out))
    assert pdfwrite.read(str(out)) == [text, ink]

    pdoc = Poppler.Document.new_from_file(Gio.File.new_for_path(str(out)).get_uri(), None)
    kinds = [m.annot.get_annot_type().value_nick for m in pdoc.get_page(0).get_annot_mapping()]
    assert "free-text" in kinds and "link" in kinds  # existing links survive
    assert ink.name in [m.annot.get_name() for m in pdoc.get_page(4).get_annot_mapping()]

    # Saving the file over itself replaces Acrimonious Reader's annotations rather than adding copies.
    moved = text.moved(10, 0)
    pdfwrite.save(str(out), [moved], str(out))
    assert pdfwrite.read(str(out)) == [moved]
    with pikepdf.open(out) as pdf:
        assert not any("/AcrimoniousItem" in a for a in pdf.pages[4].obj.get("/Annots", []))

    # Loading takes the annotations over for editing and hides them from Poppler's rendering.
    doc = document.load(Gio.File.new_for_path(str(out)))
    assert doc.editable and doc.annotations == [moved]
    assert [m.annot.get_annot_type().value_nick for m in doc._pdoc.get_page(0).get_annot_mapping()].count("free-text") == 0


def test_moves_made_by_other_programs_are_kept(sample_pdf, tmp_path):
    text = TextBox(0, 90, 470, "moved")
    out = tmp_path / "out.pdf"
    pdfwrite.save(str(sample_pdf), [text], str(out))
    with pikepdf.open(out, allow_overwriting_input=True) as pdf:
        annot = [a for a in pdf.pages[0].obj.Annots if "/AcrimoniousItem" in a][0]
        annot.Rect = [float(v) + d for v, d in zip(annot.Rect, (15, -20, 15, -20))]  # right and down
        pdf.save()
    (item,) = pdfwrite.read(str(out))
    assert (item.x, item.y) == pytest.approx((105, 490))


def test_encrypted_files_stay_encrypted(locked_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    pdfwrite.save(str(locked_pdf), [TextBox(0, 50, 50, "secret note")], str(out), password="secret")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    assert pdfwrite.read(str(out), "secret")[0].text == "secret note"


def test_saving_into_a_read_only_folder(sample_pdf, tmp_path):
    # The file is writable but its folder is not, so it cannot be replaced: it is overwritten in place.
    folder = tmp_path / "read-only"
    folder.mkdir()
    out = folder / "form.pdf"
    out.write_bytes(sample_pdf.read_bytes())
    folder.chmod(0o555)
    try:
        pdfwrite.save(str(out), [TextBox(0, 50, 50, "in place")], str(out))
    finally:
        folder.chmod(0o755)
    assert pdfwrite.read(str(out))[0].text == "in place"


def test_file_mode_is_kept(sample_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    out.write_bytes(sample_pdf.read_bytes())
    out.chmod(0o640)
    pdfwrite.save(str(out), [TextBox(0, 50, 50, "x")], str(out))
    assert out.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".acrimonious-*"))  # no temporary files left behind


def test_files_saved_under_the_old_name_stay_editable(sample_pdf, tmp_path):
    # Before the rename the program marked its annotations "quire-…" with a /QuireItem entry.
    out = tmp_path / "out.pdf"
    pdfwrite.save(str(sample_pdf), [TextBox(0, 90, 470, "old", name="quire-0123")], str(out))
    with pikepdf.open(out, allow_overwriting_input=True) as pdf:
        for annot in pdf.pages[0].obj.Annots:
            if "/AcrimoniousItem" in annot:
                annot["/QuireItem"] = annot["/AcrimoniousItem"]
                del annot["/AcrimoniousItem"]
        pdf.save()
    (item,) = pdfwrite.read(str(out))
    assert item.text == "old" and item.name == "quire-0123"
    pdfwrite.save(str(out), [item.moved(5, 0)], str(out))  # re-saving replaces it, no duplicate
    assert [i.x for i in pdfwrite.read(str(out))] == [95]
