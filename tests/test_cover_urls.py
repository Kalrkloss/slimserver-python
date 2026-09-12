"""LMS artwork URLs — the size-encoded cover form Jive/SqueezePlay use.

Live regression this locks down (log 2026-09-12, earlier on the mis-advertised
port): SqueezePlay asks for ``GET /music/3079/cover_40x40_m.jpg`` (from its
``artworkspec add 40x40_m squeezeplayskin``). Our router only knew
``/music/<id>/cover.jpg`` and answered 404, so every cover in the album list
stayed blank and the list kept spinning.

Perl serves both forms through its ImageProxy; the trailing ``_m``/``_f`` is
the crop/fill flag and the numbers are the requested box size.
"""

import io

from PIL import Image

from lyrion.web.app import _parse_cover_path, _resize_cover


def test_plain_cover_path_has_no_size():
    assert _parse_cover_path("/music/42/cover.jpg") == (42, None)
    assert _parse_cover_path("/music/42/cover.png") == (42, None)


def test_sized_cover_path_parses_box_and_flags():
    assert _parse_cover_path("/music/3079/cover_40x40_m.jpg") == (3079, (40, 40))
    assert _parse_cover_path("/music/3079/cover_40x40_f.jpg") == (3079, (40, 40))
    assert _parse_cover_path("/music/3079/cover_300x300_m.png") == (3079, (300, 300))
    assert _parse_cover_path("/music/7/cover_50x50.jpg") == (7, (50, 50))


def test_unrelated_paths_are_not_covers():
    assert _parse_cover_path("/music/42/cover_40x40_m.gif") is None
    assert _parse_cover_path("/music/42/cover") is None
    assert _parse_cover_path("/music/cover.jpg") is None
    assert _parse_cover_path("/html/images/cover.jpg") is None


def _jpeg(width: int, height: int) -> bytes:
    im = Image.new("RGB", (width, height), (10, 20, 30))
    out = io.BytesIO()
    im.save(out, format="JPEG")
    return out.getvalue()


def test_resize_downscales_to_the_requested_box():
    data = _resize_cover(_jpeg(600, 600), (40, 40))
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= 40, im.size


def test_resize_leaves_small_images_untouched():
    original = _jpeg(40, 40)
    assert _resize_cover(original, (40, 40)) == original


def test_resize_survives_garbage_input():
    assert _resize_cover(b"not an image", (40, 40)) == b"not an image"


def test_sized_cover_without_extension_is_accepted():
    """Jive fetches browser thumbnails WITHOUT a file extension
    (`fetchArtwork(iconId, icon, size)` with no imgFormat): SqueezePlay
    requested '/music/2441/cover_40x40_m' and a strict '\.jpg' rule
    answered 404, so the cover stayed a placeholder."""
    assert _parse_cover_path("/music/2441/cover_40x40_m") == (2441, (40, 40))
    assert _parse_cover_path("/music/2441/cover_300x300_f") == (2441, (300, 300))
    assert _parse_cover_path("/music/2441/cover_40x40_m.png") == (2441, (40, 40))
