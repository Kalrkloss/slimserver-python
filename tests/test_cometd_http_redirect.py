"""Native Cometd server: non-Cometd requests get a 302 to the web port.

Live regression this locks down (log 2026-09-12): the discovery beacon
advertises the native Cometd port, and Jive resolves EVERY server URL
against it — artwork (``/music/3079/cover_40x40_m.jpg``), ``/html/...``,
``/stream.mp3``. The Cometd server only understood ``POST /cometd`` and
logged ``unerwartete Zeile`` for the rest, so every cover came back empty
and the album list spun forever.
"""

import asyncio

from lyrion.networking.cometd_stream import _read_http_request


def _run(data: bytes):
    """Feed ``data`` to a fresh StreamReader and parse one request.

    The reader must be created inside the running loop (uvloop refuses a
    StreamReader built outside one).
    """

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await _read_http_request(reader)

    return asyncio.run(go())


def test_get_request_is_reported_for_redirect():
    raw = (
        b"GET /music/3079/cover_40x40_m.jpg HTTP/1.1\r\n"
        b"Host: 192.168.1.130:9080\r\n"
        b"User-Agent: squeezeplay\r\n\r\n"
    )
    req = _run(raw)
    assert req is not None
    assert req["method"] == "GET"
    assert req["target"] == b"/music/3079/cover_40x40_m.jpg"
    assert req["headers"][b"host"] == b"192.168.1.130:9080"


def test_head_request_is_reported_too():
    raw = b"HEAD /html/images/icon.png HTTP/1.1\r\nHost: h:9080\r\n\r\n"
    req = _run(raw)
    assert req is not None
    assert req["method"] == "HEAD"
    assert req["target"] == b"/html/images/icon.png"


def test_post_cometd_still_reads_the_body():
    body = b'[{"channel":"/meta/handshake","id":"1"}]'
    raw = (
        b"POST /cometd HTTP/1.1\r\n"
        b"Host: 192.168.1.130:9080\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    req = _run(raw)
    assert req is not None
    assert req["body"] == body
    assert "method" not in req


def test_other_verbs_are_still_ignored():
    raw = b"DELETE /whatever HTTP/1.1\r\nHost: h:9080\r\n\r\n"
    assert _run(raw) is None
