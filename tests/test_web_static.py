"""Security unit tests for the web static-file handler.

Run in-process (no live server needed) so they gate every verify.
"""

from lyrion.web.api import WebAPIHandler


def test_static_handler_blocks_parent_traversal(tmp_path):
    static = tmp_path / "html"
    static.mkdir()
    (static / "index.html").write_text("<html>ok</html>")
    (tmp_path / "secret.txt").write_text("TOP SECRET")

    handler = WebAPIHandler()
    handler.set_static_dir(str(static))

    status, _, body = handler._serve_static("/../secret.txt")
    assert status in (403, 404), f"traversal must not leak, got {status}: {body!r}"


def test_static_handler_blocks_sibling_traversal_after_prefix_strip(tmp_path):
    static = tmp_path / "html"
    static.mkdir()
    (static / "index.html").write_text("<html>ok</html>")
    (tmp_path / "secret.txt").write_text("TOP SECRET")

    handler = WebAPIHandler()
    handler.set_static_dir(str(static))

    # "html/../secret.txt" — the leading "html/" is stripped first, then the
    # ".." escapes the static root.
    status, _, body = handler._serve_static("/html/../secret.txt")
    assert status in (403, 404), f"traversal must not leak, got {status}: {body!r}"


def test_static_handler_serves_legit_file(tmp_path):
    static = tmp_path / "html"
    static.mkdir()
    (static / "index.html").write_text("<html>ok</html>")

    handler = WebAPIHandler()
    handler.set_static_dir(str(static))

    status, _, body = handler._serve_static("/html/index.html")
    assert status == 200
    assert b"<html>ok</html>" in body
