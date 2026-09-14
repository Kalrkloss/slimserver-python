#!/usr/bin/env python3
"""Raw probe for the single-port consolidation.

Measures, against a RUNNING server, the four things the consolidation must
hold (task steps 4a-4d). Every line carries a wall-clock timestamp so the
output can be pasted as evidence as-is. Read-only: it issues normal client
requests (JSON-RPC, artwork, /stream.mp3, Bayeux, TLV discovery) and never
writes to the DB.

    .venv/bin/python3 tools/_probe_single_port.py [--public 9000] [--internal 9001]

Sections:
  A  SqueezePlay pipelining: [subscribe,connect] + [subscribe,subscribe,
     request] on ONE socket, plus a pipelined GET on that OPEN socket
  B  /stream.mp3: time-to-first-byte + bytes streamed in N seconds
  C  JSON-RPC POST, artwork GET, Cometd streaming client (advice.interval)
  D  announcements: TLV discovery (UDP 3483)
"""
from __future__ import annotations

import argparse
import datetime
import json
import socket
import time

STREAM_TRACK = 78623      # ~898 MB FLAC — long enough to never finish
ALBUM_ID = 8              # album with real cover art
PLAYER = "1c:87:2c:47:fc:36"


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(*parts) -> None:
    print(ts(), *parts, flush=True)


class Conn:
    """Tiny buffered client socket — enough HTTP/1.x to be a real client."""

    def __init__(self, port: int, timeout: float = 8.0) -> None:
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _fill(self) -> bytes:
        chunk = self.sock.recv(65536)
        self.buf += chunk
        return chunk

    def read_head(self) -> bytes:
        while b"\r\n\r\n" not in self.buf:
            if not self._fill():
                break
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        return head

    def read_line(self) -> bytes:
        while b"\r\n" not in self.buf:
            if not self._fill():
                return self.buf
        line, _, self.buf = self.buf.partition(b"\r\n")
        return line

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                break
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_all(self, seconds: float) -> bytes:
        deadline = time.time() + seconds
        out = self.buf
        self.buf = b""
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            out += chunk
        return out

    # ── framing ──────────────────────────────────────────────────────────
    def read_chunk(self) -> bytes:
        """One chunk of a chunked body. b'' on the terminating chunk."""
        line = self.read_line()
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            raise ValueError(f"not a chunk size line: {line[:40]!r}") from None
        if size == 0:
            # consume trailers up to the blank line (keeps the buffer aligned
            # for the NEXT response — pipelining depends on it)
            while True:
                trailer = self.read_line()
                if not trailer:
                    break
            return b""
        return self.read_exact(size + 2)[:size]

    def read_body(self, head: bytes) -> bytes:
        """Body of the response whose head was just read (CL or chunked)."""
        if b"chunked" in head.lower():
            out = b""
            while True:
                try:
                    part = self.read_chunk()
                except ValueError:
                    break
                if not part:
                    break
                out += part
            return out
        length = int(hdr(head, b"content-length") or 0)
        return self.read_exact(length)

    def read_http_response(self) -> bytes:
        """Read ONE complete response that was spliced into the stream."""
        head = self.read_head()
        return head + b"\r\n\r\n" + self.read_body(head)

    def peek(self, n: int = 5) -> bytes:
        """Look at the next bytes without consuming them (may short-read)."""
        while len(self.buf) < n:
            if not self._fill():
                break
        return self.buf


def hdr(head: bytes, name: bytes) -> bytes:
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == name.lower():
            return v.strip()
    return b""


def status_of(head: bytes) -> str:
    return head.split(b"\r\n", 1)[0].decode("latin1", "replace") if head else "<none>"


def post_cometd(port: int, messages: list) -> bytes:
    body = json.dumps(messages).encode()
    return (b"POST /cometd HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def get(port: int, path: str, accept: str = "*/*") -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Accept: {accept}\r\n\r\n").encode()


# ── A: SqueezePlay pipelining ───────────────────────────────────────────────


def probe_pipelining(port: int) -> None:
    log(f"A public({port}) pipelining: opening socket")
    with Conn(port) as c:
        c.send(post_cometd(port, [{"channel": "/meta/handshake", "id": 1}]))
        head = c.read_head()
        cid = json.loads(c.read_body(head))[0]["clientId"]
        log(f"A handshake: {status_of(head)} cid={cid}")

        # [subscribe, connect] pipelined, no read in between (SqueezePlay shape)
        c.send(post_cometd(port, [
            {"channel": "/meta/connect", "clientId": cid, "id": 2,
             "connectionType": "streaming"},
            {"channel": "/meta/subscribe", "clientId": cid, "id": 3,
             "subscription": f"/{cid}/**"},
        ]))
        head = c.read_head()
        log(f"A [subscribe,connect] reply: {status_of(head)} "
            f"TE={hdr(head, b'transfer-encoding')!r} CL={hdr(head, b'content-length')!r}")
        first = json.loads(c.read_chunk() or b"[]")
        log(f"A connect frame channels={[m.get('channel') for m in first]}")

        # [subscribe,subscribe,request] pipelined onto the OPEN streaming socket
        c.send(post_cometd(port, [
            {"channel": "/meta/subscribe", "clientId": cid, "id": 4,
             "subscription": f"/{cid}/slim/playerstatus/{PLAYER}"},
            {"channel": "/slim/subscribe", "clientId": cid, "id": 5,
             "data": {"request": [PLAYER, ["menustatus"]],
                      "response": f"/{cid}/slim/menustatus/{PLAYER}"}},
            {"channel": "/slim/request", "clientId": cid, "id": 6,
             "data": {"request": [PLAYER, ["status", 0, 200]],
                      "response": f"/{cid}/slim/request/1"}},
        ]))
        # ... plus a pipelined GET on the same socket (artwork, like Jive)
        c.send(get(port, f"/music/{ALBUM_ID}/cover_40x40_m.jpg", "image/*"))

        channels: list[str] = []
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                if c.peek()[:5] == b"HTTP/":
                    resp = c.read_http_response()
                    log(f"A pipelined GET on the stream socket: spliced answer "
                        f"{resp[:24]!r} ({len(resp)}B)")
                    continue
                part = c.read_chunk()
            except ValueError as exc:
                log(f"A framing surprise: {exc}")
                break
            except (socket.timeout, TimeoutError):
                break
            if not part:
                break
            try:
                channels += [m.get("channel") for m in json.loads(part)]
            except Exception:  # noqa: BLE001
                log(f"A non-JSON frame: {part[:80]!r}")
        log(f"A pipelined batch channels={channels}")

        # socket still usable afterwards? /meta/ping must be answered
        try:
            c.send(post_cometd(port, [{"channel": "/meta/ping", "clientId": cid,
                                       "id": 7}]))
            ping = c.read_chunk()
            log(f"A /meta/ping after the pipeline: {ping[:110]!r} "
                f"({'socket STILL OPEN' if ping else 'SOCKET DEAD'})")
        except Exception as exc:  # noqa: BLE001
            log(f"A /meta/ping failed: {exc!r} -> SOCKET DEAD")


def probe_pipelined_gets(port: int) -> None:
    """Two independent GETs written back-to-back on one socket."""
    log(f"A public({port}) pipelined GETs (2 requests, one socket)")
    with Conn(port) as c:
        c.send(get(port, f"/music/{ALBUM_ID}/cover_40x40_m.jpg") * 2)
        buf = c.read_all(5.0)
        statuses = [ln for ln in buf.split(b"\r\n") if ln.startswith(b"HTTP/")]
        log(f"A pipelined GETs: {len(statuses)} response(s): {statuses}")


# ── B: /stream.mp3 ─────────────────────────────────────────────────────────


def probe_stream(port: int, seconds: float = 3.0) -> None:
    log(f"B public({port}) /stream.mp3?id={STREAM_TRACK}")
    with Conn(port, timeout=10) as c:
        t0 = time.time()
        c.send(get(port, f"/stream.mp3?id={STREAM_TRACK}"))
        head = c.read_head()
        ttfb = (time.time() - t0) * 1000
        log(f"B stream head: {status_of(head)} ctype={hdr(head, b'content-type')!r}"
            f" CL={hdr(head, b'content-length')!r}"
            f" TE={hdr(head, b'transfer-encoding')!r} ttfb={ttfb:.0f}ms")
        total = len(c.buf)
        first = c.buf[:4]
        closed = False
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = c.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                closed = True
                break
            total += len(chunk)
        log(f"B stream bytes in {seconds:.0f}s: {total} ({total/1024:.0f} KiB)"
            f" first-body-bytes={first!r} closed_by_peer={closed}")


# ── C: JSON-RPC, artwork, cometd streaming client ──────────────────────────


def probe_jsonrpc(port: int) -> None:
    body = json.dumps({"id": 1, "method": "slim.request",
                       "params": ["", ["serverstatus", 0, 10]]}).encode()
    with Conn(port) as c:
        c.send(b"POST /jsonrpc.js HTTP/1.1\r\n"
               + f"Host: 127.0.0.1:{port}\r\n".encode()
               + b"Content-Type: application/json\r\n"
               + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        head = c.read_head()
        payload = c.read_body(head)
        try:
            res = json.loads(payload)["result"]
            log(f"C JSON-RPC POST: {status_of(head)} version={res.get('version')!r} "
                f"httpport={res.get('httpport')!r} CL={hdr(head, b'content-length')!r} "
                f"TE={hdr(head, b'transfer-encoding')!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"C JSON-RPC POST: {status_of(head)} unparsable ({exc!r}) "
                f"{payload[:80]!r}")


def probe_artwork(port: int) -> None:
    with Conn(port) as c:
        c.send(get(port, f"/music/{ALBUM_ID}/cover_40x40_m.jpg", "image/*"))
        head = c.read_head()
        data = c.read_body(head)
        magic = data[:4]
        kind = ("JPEG" if magic[:2] == b"\xff\xd8"
                else "PNG" if magic[:4] == b"\x89PNG" else "?")
        log(f"C artwork GET: {status_of(head)} ctype={hdr(head, b'content-type')!r}"
            f" bytes={len(data)} magic={magic!r} ({kind})")


def probe_cometd_stream_client(port: int, seconds: float = 3.0) -> None:
    """Squeeze-Client form: handshake, streaming connect, then read frames."""
    log(f"C public({port}) Cometd streaming client (advice.interval)")
    with Conn(port) as c:
        c.send(post_cometd(port, [{"channel": "/meta/handshake", "id": 1}]))
        head = c.read_head()
        cid = json.loads(c.read_body(head))[0]["clientId"]
        c.send(post_cometd(port, [{"channel": "/meta/connect", "clientId": cid,
                                   "id": 2, "connectionType": "streaming"}]))
        head = c.read_head()
        first = json.loads(c.read_chunk() or b"[]")
        ack = [m for m in first if m.get("channel") == "/meta/connect"]
        advice = ack[0].get("advice") if ack else None
        log(f"C cometd stream: {status_of(head)} cid={cid} "
            f"frames={[m.get('channel') for m in first]} advice={advice}")
        frames = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data = c.read_chunk()
            except (ValueError, socket.timeout, TimeoutError):
                break
            if not data:
                break
            frames += 1
        log(f"C cometd stream: {frames} further frame(s) in {seconds:.0f}s, "
            f"socket stayed open")


# ── D: announcements ───────────────────────────────────────────────────────


def probe_tlv(port: int = 3483) -> None:
    """Send a real Jive TLV discovery request and decode the JSON field."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(b"e" + b"JSON" + bytes([0]), ("127.0.0.1", port))
        data, _ = s.recvfrom(4096)
        fields: dict[str, str] = {}
        body = data[1:]
        pos = 0
        while pos + 5 <= len(body):
            t = body[pos:pos + 4]
            ln = body[pos + 4]
            v = body[pos + 5:pos + 5 + ln]
            fields[t.decode()] = v.decode("latin1", "replace")
            pos += 5 + ln
        log(f"D TLV discovery reply: {fields}")
    except Exception as exc:  # noqa: BLE001
        log(f"D TLV discovery failed: {exc!r}")
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", type=int, default=9000)
    ap.add_argument("--internal", type=int, default=0)
    ap.add_argument("--no-stream", action="store_true")
    args = ap.parse_args()

    log(f"=== probe public={args.public} internal={args.internal or '-'} ===")
    for fn in (probe_pipelining, probe_pipelined_gets, probe_jsonrpc,
               probe_artwork, probe_cometd_stream_client):
        try:
            fn(args.public)
        except Exception as exc:  # noqa: BLE001
            log(f"{fn.__name__} FAILED: {exc!r}")
    if args.internal:
        try:
            probe_stream(args.internal, seconds=2.0)
        except Exception as exc:  # noqa: BLE001
            log(f"probe_stream(internal) FAILED: {exc!r}")
    if not args.no_stream:
        try:
            probe_stream(args.public)
        except Exception as exc:  # noqa: BLE001
            log(f"probe_stream FAILED: {exc!r}")
    try:
        probe_tlv()
    except Exception as exc:  # noqa: BLE001
        log(f"probe_tlv FAILED: {exc!r}")
    log("=== done ===")


if __name__ == "__main__":
    main()
