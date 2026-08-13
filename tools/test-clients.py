#!/usr/bin/env python3
"""Automated client-protocol test suite for the Lyrion Python server.

Simulates the three remote controller apps WITHOUT a human in the loop,
using the exact protocols from their source code:

- SqueezeClient (maniac103/squeezeclient): Bayeux Cometd with
  connectionType 'streaming' — handshake, streaming connect (must reply
  within 5s), subscribe to /<clientId>/**, one-shot player status
  requests answered on /<clientId>/slim/request/<n>.
- SqueezeCtrl / jivelite (Jive): Cometd long-polling, /slim/subscribe
  with data.subscription, serverstatus -> players_loop.
- Squeezer (JSON-RPC, ioBroker.squeezeboxrpc format): players with
  playerindex, serverstatus, status, single-value queries
  (<cmd> ? -> {'_<cmd>': ...}), transport commands.

Usage: python3 tools/test-clients.py [--port 9000] [--host 127.0.0.1]
Exit 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# ----------------------------------------------------------------------
# Cometd helpers (raw socket for streaming / chunked responses)
# ----------------------------------------------------------------------

def cometd_post(host: str, port: int, messages: list, read_bytes: int = 20000,
                timeout: float = 8.0) -> tuple[bytes, bytes]:
    """POST a Bayeux batch to /cometd. Returns (http_head, decoded body).
    Reads up to read_bytes of the (possibly held-open) stream."""
    body = json.dumps(messages).encode()
    req = (f"POST /cometd HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
           f"\r\n").encode() + body
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    s.sendall(req)
    data = b""
    try:
        while len(data) < read_bytes:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    head, _, raw = data.partition(b"\r\n\r\n")
    # Only dechunk when the response actually IS chunked — the native
    # Cometd server answers finite responses (handshake, acks) with
    # Content-Length, which dechunk() would destroy.
    if b"chunked" in head.lower():
        return head, dechunk(raw)
    return head, raw


def dechunk(data: bytes) -> bytes:
    out = b""
    rest = data
    while rest:
        line, _, rest = rest.partition(b"\r\n")
        try:
            size = int(line, 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        rest = rest[size:]
        if rest.startswith(b"\r\n"):
            rest = rest[2:]
    return out


# ----------------------------------------------------------------------
# 1. SqueezeClient (streaming Cometd, maniac103/squeezeclient)
# ----------------------------------------------------------------------

def test_squeezeclient(host: str, port: int) -> None:
    print("\n== SqueezeClient (streaming Cometd) ==")
    head, body = cometd_post(host, port, [{
        "channel": "/meta/handshake", "id": 1,
        "supportedConnectionTypes": "streaming", "version": "1.0",
    }], read_bytes=2000)
    try:
        cid = json.loads(body)[0]["clientId"]
    except Exception:
        check("Handshake -> clientId", False, body[:120])
        return
    check("Handshake -> clientId", bool(cid), str(cid))

    # startListening: connect (streaming) + subscribe /<cid>/**
    head, body = cometd_post(host, port, [
        {"channel": "/meta/connect", "id": 2,
         "connectionType": "streaming", "clientId": cid},
        {"channel": "/meta/subscribe", "subscription": f"/{cid}/**",
         "clientId": cid, "id": 3},
    ], read_bytes=4000, timeout=5)
    check("Streaming-Connect: 200 + chunked",
          b"200 OK" in head and b"chunked" in head.lower())
    check("connect-ack sofort (5s-Fenster)",
          b'"channel": "/meta/connect"' in body and b'"successful": true' in body)
    check("subscribe-ack sofort",
          b'"channel": "/meta/subscribe"' in body)

    # serverstatus subscription on /<cid>/slim/serverstatus
    head, body = cometd_post(host, port, [{
        "channel": "/slim/subscribe", "clientId": cid, "id": 4,
        "data": {"request": ["", ["serverstatus", "0", "100", "subscribe:60"]],
                 "response": f"/{cid}/slim/serverstatus"},
    }], read_bytes=8000)
    check("serverstatus-Event mit players_loop",
          f"/{cid}/slim/serverstatus".encode() in body and b"players_loop" in body)

    # one-shot player status request (PlayerStatusRequest with menu)
    pid = _first_player(host, port)
    if pid:
        head, body = cometd_post(host, port, [{
            "channel": "/slim/request", "clientId": cid, "id": 5,
            "data": {"request": [pid, ["status", "-", "1",
                                       "menu:menu", "useContextMenu:1"]],
                     "response": f"/{cid}/slim/request/278"},
        }], read_bytes=8000)
        check("PlayerStatus-Request -> Antwort mit playlist_loop",
              f"/{cid}/slim/request/278".encode() in body
              and b"playlist_loop" in body)


# ----------------------------------------------------------------------
# 2. SqueezeCtrl / jivelite (Jive Cometd, long-polling)
# ----------------------------------------------------------------------

def test_jive(host: str, port: int) -> None:
    print("\n== SqueezeCtrl (Jive Cometd) ==")
    head, body = cometd_post(host, port, [{
        "channel": "/meta/handshake", "version": "1.0",
        "supportedConnectionTypes": ["streaming"], "id": "1",
    }], read_bytes=2000)
    try:
        cid = json.loads(body)[0]["clientId"]
    except Exception:
        check("Handshake -> clientId", False, body[:120])
        return
    check("Handshake -> clientId", bool(cid), str(cid))

    # Jive subscribe (data.subscription) + long-polling connect in one batch
    head, body = cometd_post(host, port, [
        {"channel": "/meta/connect", "clientId": cid, "id": "2",
         "connectionType": "long-polling", "advice": {"timeout": 0}},
        {"channel": "/slim/subscribe", "clientId": cid, "id": "3",
         "data": {"request": ["", ["serverstatus", "0", "50", "subscribe:60"]],
                  "subscription": "/slim/serverstatus", "subid": "s1"}},
    ], read_bytes=8000)
    check("long-poll connect-ack",
          b'"channel": "/meta/connect"' in body)
    check("subscribe-ack",
          b'"channel": "/slim/subscribe"' in body and b'"successful": true' in body)
    check("serverstatus-Event mit players_loop",
          b"/slim/serverstatus" in body and b"players_loop" in body)

    # SqueezeCtrl-style: /meta/subscribe with TOP-LEVEL subscription,
    # no request — serverstatus must still deliver players_loop
    head, body = cometd_post(host, port, [
        {"channel": "/meta/connect", "clientId": cid, "id": "5",
         "connectionType": "long-polling", "advice": {"timeout": 0}},
        {"channel": "/meta/subscribe", "clientId": cid, "id": "6",
         "subscription": f"/{cid}/slim/serverstatus"},
    ], read_bytes=10000)
    check("SqueezeCtrl top-level subscribe -> serverstatus-Event",
          f"/{cid}/slim/serverstatus".encode() in body and b"players_loop" in body)

    # Jive request round-trip
    head, body = cometd_post(host, port, [{
        "channel": "/slim/request", "clientId": cid, "id": "4",
        "data": {"request": ["", ["players"]],
                 "response": f"/{cid}/slim/request"},
    }], read_bytes=8000)
    check("Request players -> Antwort auf Response-Kanal",
          f"/{cid}/slim/request".encode() in body and b"players_loop" in body)


# ----------------------------------------------------------------------
# 3. Squeezer (JSON-RPC, ioBroker.squeezeboxrpc format)
# ----------------------------------------------------------------------

def _rpc(host: str, port: int, player: str, cmd: list) -> dict:
    body = json.dumps({"id": 1, "method": "slim.request",
                       "params": [player, cmd]}).encode()
    req = urllib.request.Request(f"http://{host}:{port}/jsonrpc.js", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("result") or {}


def _first_player(host: str, port: int) -> str:
    r = _rpc(host, port, "", ["players", "0", "100"])
    loop = r.get("players_loop") or []
    for p in loop:
        if p.get("connected") == 1:
            return p["playerid"]
    return ""


def test_squeezer(host: str, port: int) -> None:
    print("\n== Squeezer (JSON-RPC) ==")
    r = _rpc(host, port, "", ["players", "0", "100"])
    loop = r.get("players_loop") or []
    check("players -> players_loop", len(loop) > 0, f"{len(loop)} Player")
    idx = [p.get("playerindex") for p in loop]
    check("playerindex 0..n", idx == list(range(len(loop))), str(idx))
    check("playerid + connected", all(p.get("playerid") and p.get("connected") in (0, 1)
                                      for p in loop))

    r = _rpc(host, port, "", ["serverstatus"])
    check("serverstatus ohne args -> players_loop",
          isinstance(r.get("players_loop"), list) and len(r.get("players_loop", [])) > 0,
          f"count={r.get('count')}")

    # Favorites: loop_loop with the LMS item fields (Squeezer/ioBroker)
    r = _rpc(host, port, "", ["favorites", "items", "0", "888", "want_url:1", "item_id:"])
    loop = r.get("loop_loop") or []
    check("favorites -> loop_loop", len(loop) > 0, f"{len(loop)} Favoriten")
    if loop:
        it = loop[0]
        check("favorites Felder (isaudio/hasitems/image)",
              "isaudio" in it and "hasitems" in it and "image" in it
              and "name" in it, str(sorted(it.keys())))

    pid = _first_player(host, port)
    if not pid:
        check("verbundener Player", False)
        return
    check("verbundener Player", True, pid)

    r = _rpc(host, port, pid, ["status", "-", "1", "100"])
    check("status -> mode/title/artist/album/playlist_loop",
          "mode" in r and "title" in r and "artist" in r
          and "album" in r and "playlist_loop" in r, str(list(r.keys()))[:80])
    check("status -> time int + rate 0/1",
          isinstance(r.get("time"), int) and r.get("rate") in (0, 1),
          f"time={r.get('time')} rate={r.get('rate')}")
    if r.get("mode") == "play":
        check("spielender Player: time > 0 (STAT elapsed)",
              (r.get("time") or 0) > 0, f"time={r.get('time')}")
    if r.get("title", "").startswith("http"):
        check("Radio-Stream: remoteMeta vorhanden", "remoteMeta" in r)

    r = _rpc(host, port, pid, ["mode", "?"])
    check("mode ? -> _mode", r == {"_mode": r.get("_mode")} and "_mode" in r, str(r))
    r = _rpc(host, port, "", ["version", "?"])
    check("version ? (ohne Player) -> _version",
          "_version" in r and bool(r["_version"]), str(r))
    r = _rpc(host, port, pid, ["current_title", "?"])
    check("current_title ? -> _current_title", "_current_title" in r, str(r))
    r = _rpc(host, port, pid, ["artist", "?"])
    check("artist ? -> _artist", "_artist" in r, str(r))
    r = _rpc(host, port, pid, ["album", "?"])
    check("album ? -> _album", "_album" in r, str(r))
    r = _rpc(host, port, pid, ["mixer", "volume", "?"])
    check("mixer volume ? -> _volume", "_volume" in r and isinstance(r["_volume"], int),
          str(r))

    # Transport: stop -> mode stop, play -> mode play (state check).
    # Load a real track first — after a server restart the player
    # playlist is empty and play cannot start (mode stays stop).
    _rpc(host, port, pid, ["playlist", "play", "1"])
    time.sleep(1.5)
    _rpc(host, port, pid, ["stop"])
    time.sleep(0.5)
    r = _rpc(host, port, pid, ["mode", "?"])
    check("stop -> _mode stop", r.get("_mode") == "stop", str(r))
    _rpc(host, port, pid, ["play"])
    time.sleep(0.5)
    r = _rpc(host, port, pid, ["mode", "?"])
    check("play -> _mode play", r.get("_mode") == "play", str(r))
    # Leave the player stopped? No — restore to stop (it was likely idle).
    _rpc(host, port, pid, ["stop"])


# ----------------------------------------------------------------------
# 4. SlimProto discovery (classic 'd' + Jive TLV 'e')
# ----------------------------------------------------------------------

def test_discovery(host: str, port: int) -> None:
    print("\n== Discovery (SlimProto d + Jive TLV e) ==")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)

    # classic: deviceid(1) + revision(1) + mac(6) → 'D' + hostname
    s.sendto(struct.pack(">BB", 2, 16) + bytes.fromhex("000420123456"),
             (host, 3483))
    try:
        d, _ = s.recvfrom(4096)
        check("klassisch 'd' -> 'D'-Antwort",
              d[0:1] == b"D" and len(d) == 18, repr(d[:10]))
    except socket.timeout:
        check("klassisch 'd' -> 'D'-Antwort", False, "timeout")

    # Jive TLV: 'e' + NAME/IPAD/JSON/VERS/UUID → 'E' + Werte
    def tlv(t, v):
        return t + bytes([len(v)]) + v

    e_pkt = (b"e" + tlv(b"NAME", b"jive") + tlv(b"IPAD", b"")
             + tlv(b"JSON", b"") + tlv(b"VERS", b"") + tlv(b"UUID", b""))
    s.sendto(e_pkt, (host, 3483))
    try:
        d, _ = s.recvfrom(4096)
        check("Jive 'e'-TLV -> 'E' mit IPAD/JSON/VERS",
              d.startswith(b"E") and b"IPAD" in d and b"JSON" in d
              and b"VERS" in d, repr(d[:40]))
    except socket.timeout:
        check("Jive 'e'-TLV -> 'E' mit IPAD/JSON/VERS", False, "timeout")
    s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()

    test_squeezeclient(args.host, args.port)
    test_jive(args.host, args.port)
    test_squeezer(args.host, args.port)
    test_discovery(args.host, args.port)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
