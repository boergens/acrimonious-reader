import pytest

from acrimonious_reader.document import paper_description, rotate_point, rotated_size, unrotate_point
from acrimonious_reader.view import MAX_TEXTURE_SIDE, TILE_SIZE, tile_size

W, H = 595.0, 842.0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_round_trip(rotation):
    for x, y in [(0, 0), (W, H), (100, 700), (W, 0)]:
        u, v = rotate_point(x, y, rotation, W, H)
        rw, rh = rotated_size(W, H, rotation)
        assert 0 <= u <= rw and 0 <= v <= rh
        assert unrotate_point(u, v, rotation, W, H) == pytest.approx((x, y))


def test_rotate_clockwise():
    # The top left corner of a page turned clockwise ends up at the top right.
    assert rotate_point(0, 0, 90, W, H) == (H, 0)
    assert rotate_point(0, 0, 270, W, H) == (0, W)


def test_small_pages_render_whole_and_big_ones_in_tiles():
    assert tile_size(1600, 2200) == (1600, 2200)
    assert tile_size(9000, 3000) == (TILE_SIZE, TILE_SIZE)  # too wide for one texture
    assert tile_size(4000, 5000) == (TILE_SIZE, TILE_SIZE)  # too many pixels
    assert MAX_TEXTURE_SIDE >= TILE_SIZE


def test_paper_names():
    assert paper_description(595.28, 841.89) == "A4 (210 × 297 mm)"
    assert paper_description(792, 612) == "Letter, landscape (279 × 216 mm)"
    assert paper_description(500, 500) == "176 × 176 mm"
