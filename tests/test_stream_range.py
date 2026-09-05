"""Tests for HTTP Range parsing on /stream (Perl HTTP.pm parity).

Regression: suffix ranges (bytes=-10) and inverted ranges (bytes=20-10)
were answered with 206 and broken Content-Range/Content-Length values.
"""

import asyncio

from lyrion.web import stream as stream_mod


def _run_range(tmp_path, range_value):
    f = tmp_path / "audio.mp3"
    f.write_bytes(b"0123456789abcdef")  # 16 bytes
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        sent.append(event)

    async def run():
        scope = {
            "type": "http", "method": "GET",
            "query_string": b"id=1",
            "headers": [(b"range", range_value)],
        }
        original = stream_mod._load_track

        async def _fake_load(track_id):
            return f, "audio/mpeg"

        stream_mod._load_track = _fake_load
        try:
            await stream_mod.stream_track(scope, receive, send)
        finally:
            stream_mod._load_track = original
        return sent

    return asyncio.run(run())


def _status_and_headers(sent):
    start = sent[0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers


def test_suffix_range_is_ignored_or_rejected(tmp_path):
    sent = _run_range(tmp_path, b"bytes=-10")
    status, headers = _status_and_headers(sent)
    # Perl ignores unsupported (suffix) ranges → full 200; never a broken 206.
    assert status != 206 or "content-range" not in headers


def test_inverted_range_returns_400(tmp_path):
    # first (10) is within the file (16) but after last (5) → 400
    sent = _run_range(tmp_path, b"bytes=10-5")
    status, _ = _status_and_headers(sent)
    assert status == 400, f"inverted range must be 400, got {status}"


def test_first_past_end_returns_416(tmp_path):
    sent = _run_range(tmp_path, b"bytes=99-")
    status, _ = _status_and_headers(sent)
    assert status == 416, f"range past end must be 416, got {status}"


def test_valid_range_returns_206(tmp_path):
    sent = _run_range(tmp_path, b"bytes=2-5")
    status, headers = _status_and_headers(sent)
    assert status == 206
    assert headers["content-range"] == "bytes 2-5/16"
    assert headers["content-length"] == "4"
