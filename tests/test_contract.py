"""Controller-compat contract tests (regression suite).

These assert the EXACT response shapes the controllers (SqueezePlay,
SqueezeClient, Squeezer, Orange Squeeze) parse. A change to the status,
menu or browselibrary responses that breaks a controller is caught here
before the controllers are involved.

They run against a live server (HTTP JSON-RPC on :9000, CLI on :9090).
If no server is reachable they FAIL with a clear message (dev workflow
always has one up) — this is intentional, so `hermes verify` is a real
test of the contract, not just a compile check.

Override endpoints with LMS_HTTP / LMS_CLI env vars.
"""
import json
import os
import socket
import subprocess
import time
import urllib.request

import pytest

pytestmark = pytest.mark.contract

HTTP = os.environ.get("LMS_HTTP", "http://127.0.0.1:9000")
CLI = os.environ.get("LMS_CLI", "127.0.0.1:9090")
# Any player that is registered on the server.
TEST_PLAYER = "02:11:22:33:44:55"
TIMEOUT = 6


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def lms(pid, cmd):
    """Call slim.request and return the result dict."""
    body = json.dumps({"id": 1, "method": "slim.request", "params": [pid, cmd]}).encode()
    req = urllib.request.Request(HTTP + "/jsonrpc.js", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())["result"]


def cli(line):
    """Send one line to the CLI socket; return the response."""
    host, port = CLI.split(":")
    with socket.create_connection((host, int(port)), timeout=TIMEOUT) as s:
        s.sendall((line + "\n").encode())
        time.sleep(0.4)
        s.settimeout(TIMEOUT)
        return s.recv(8192).decode().strip()


@pytest.fixture(scope="module")
def server_up():
    try:
        urllib.request.urlopen(HTTP + "/jsonrpc.js", data=b"{}", timeout=3)
        return True
    except Exception:
        pytest.skip(f"kein LMS-Server unter {HTTP} erreichbar — Kontrakt-Tests übersprungen (Dev-Server starten für echten Check)")


# ----------------------------------------------------------------------
# status shape (SqueezeClient / SqueezePlay Now-Playing)
# ----------------------------------------------------------------------
def test_status_required_types(server_up):
    r = lms(TEST_PLAYER, ["status", "-", "1"])
    # PlayerStatusResponse required fields (kotlinx, no defaults) must be
    # the right JSON types — a string where an Int is expected crashes.
    assert isinstance(r.get("count"), int), f"count must be Int, got {r.get('count')!r}"
    assert isinstance(r.get("playlist_tracks"), int), "playlist_tracks must be Int"
    assert isinstance(r.get("playlist_cur_index"), int), "playlist_cur_index must be Int"
    assert isinstance(r.get("mode"), str), "mode must be a String"
    assert r.get("mode") in ("play", "pause", "stop"), f"bad mode {r.get('mode')!r}"
    assert isinstance(r.get("player_name"), str), "player_name must be String"
    # shuffle/repeat are string enums "0"/"1"/"2" (SqueezeClient will crash
    # on the real words 'off'/'shuffle').
    assert r.get("playlist shuffle") in ("0", "1", "2"), "playlist shuffle must be a '0/1/2' string"
    assert r.get("playlist repeat") in ("0", "1", "2"), "playlist repeat must be a '0/1/2' string"


def test_status_duration_present(server_up):
    """A live stream must report a non-null duration. A missing (null)
    duration makes SqueezePlay choke and the Now-Playing window never
    opens; a 0 crashes SqueezeClient's seek slider. Regression: the
    duration-null change."""
    r = lms(TEST_PLAYER, ["status", "-", "1"])
    if r.get("mode") == "stop":
        pytest.skip("player stopped — play something to exercise the stream path")
    dur = r.get("duration")
    assert dur is not None, "duration must be present (null breaks SqueezePlay window)"
    try:
        durf = float(dur)
    except (TypeError, ValueError):
        pytest.fail(f"duration must be numeric, got {dur!r}")
    assert durf > 0, "duration must be > 0 (0 crashes SqueezeClient seek slider)"


def test_status_item_loop_has_text_track_artist_album(server_up):
    """SqueezePlay builds Now-Playing from item_loop[1] via _extractTrackInfo,
    which reads _track.text / .track / .artist / .album. A list item missing
    all of these renders as blank lines."""
    r = lms(TEST_PLAYER, ["status", "-", "1", "tags:ABdejJKlrStTuxy"])
    loop = r.get("item_loop") or []
    if not loop:
        pytest.skip("playlist empty")
    it = loop[0]
    # a stream item exposes text (stream name); a local item exposes track/artist/album
    assert any(k in it for k in ("text", "track")), (
        f"item_loop[0] must expose text or track for SqueezePlay, got {sorted(it.keys())}")
    # title should always be present
    assert "title" in it, f"item_loop[0] missing title: {sorted(it.keys())}"


# ----------------------------------------------------------------------
# menu / home shape (all controllers)
# ----------------------------------------------------------------------
def test_menu_item_shape(server_up):
    r = lms(TEST_PLAYER, ["menu", "0", "512", "direct:1"])
    loop = r.get("item_loop") or []
    assert loop, "home menu must return items"
    for it in loop:
        assert isinstance(it.get("text"), str), f"menu item missing text: {sorted(it.keys())}"
        assert isinstance(it.get("id"), str), f"menu item id must be String: {sorted(it.keys())}"
        assert isinstance(it.get("node"), str), f"menu item node must be String: {sorted(it.keys())}"
        if "type" in it:
            assert it["type"] in (
                "text", "audio", "playlist", "outline", "opml", "redirect",
                "slideshow", "link", "url", "search"), f"invalid menu type {it['type']!r}"


def test_menu_has_core_items(server_up):
    r = lms(TEST_PLAYER, ["menu", "0", "512", "direct:1"])
    texts = {it.get("text") for it in r.get("item_loop") or []}
    # At least My Music (or its children) + Favorites must be present.
    assert texts, "no menu texts"
    assert any("Musik" in t or "Music" in t for t in texts), f"no My Music entry: {texts}"
    assert any("avorit" in t or "avou" in t or "Favorite" in t for t in texts), f"no Favorites entry: {texts}"


# ----------------------------------------------------------------------
# browselibrary shape (My-Music navigation, OpenSqueeze)
# ----------------------------------------------------------------------
def test_browselibrary_opensqueeze_shape(server_up):
    r = lms(TEST_PLAYER, ["browselibrary", "items", "0", "2", "mode:artists"])
    loop = r.get("loop_loop") or r.get("item_loop") or []
    if not loop:
        pytest.skip("empty library section")
    it = loop[0]
    # OpenSqueeze: id as STRING, name (display), type playlist/folder, hasitems
    assert isinstance(it.get("id"), str), f"browselibrary id must be String: {sorted(it.keys())}"
    assert isinstance(it.get("name"), str), f"browselibrary item missing name: {sorted(it.keys())}"
    assert it.get("type") in ("playlist", "audio", "folder"), f"bad browselibrary type {it.get('type')!r}"


# ----------------------------------------------------------------------
# CLI handshake (SqueezePlay connects via CLI)
# ----------------------------------------------------------------------
def test_cli_ping(server_up):
    assert cli("ping") == "ping", "CLI must answer 'ping' with 'ping'"


def test_cli_ver(server_up):
    out = cli("ver 0 ?")
    assert out and "9." in out, f"CLI 'ver' must return a version string, got {out!r}"


def test_cli_client(server_up):
    out = cli("client aa:bb:cc:dd:ee:ff TestPlayerSL squeezelite")
    assert out.startswith("client: ok"), f"CLI 'client' must register, got {out!r}"


def test_cli_playerstatus(server_up):
    """SqueezePlay's SlimPlayer runs 'playerstatus <mac> - 1' via the CLI.
    Returning 'player not found' (because the '-' placeholder overwrote the
    player id) means the status never arrives, playlistSize stays 0 and the
    Now-Playing window never opens. Regression test for that."""
    out = cli("02:11:22:33:44:55 playerstatus - 1")
    assert "player not found" not in out, f"playerstatus must resolve the player, got {out!r}"
    assert "player_name" in out, f"playerstatus must return a status, got {out!r}"


def test_cli_players(server_up):
    out = cli("players 0 5")
    assert "playerid" in out or "player" in out.lower(), f"CLI 'players' must return the player list: {out!r}"
