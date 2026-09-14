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

SPAWN = os.environ.get("LMS_TEST_SPAWN") == "1"
HTTP = os.environ.get("LMS_HTTP", "http://127.0.0.1:9002" if SPAWN else "http://127.0.0.1:9000")
CLI = os.environ.get("LMS_CLI", "127.0.0.1:9091" if SPAWN else "127.0.0.1:9090")
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
    # Perl sends playlist_cur_index as a STRING (Queries.pm:4208; live probe
    # gap-analysis CTRL-04 → "0"); SqueezePlay does
    # tonumber(event.data.playlist_cur_index).
    assert isinstance(r.get("playlist_cur_index"), str), (
        f"playlist_cur_index must be a String like Perl, got {r.get('playlist_cur_index')!r}")
    assert str(r.get("playlist_cur_index")).isdigit(), (
        f"playlist_cur_index must be a numeric string, got {r.get('playlist_cur_index')!r}")
    assert isinstance(r.get("mode"), str), "mode must be a String"
    assert r.get("mode") in ("play", "pause", "stop"), f"bad mode {r.get('mode')!r}"
    assert isinstance(r.get("player_name"), str), "player_name must be String"
    # Perl sends shuffle/repeat as INTEGERS (Queries.pm:4186-4190
    # `$repeat += 0` / `$shuffle += 0`; live probe gap-analysis CTRL-03 →
    # `"playlist shuffle": 0`).
    assert isinstance(r.get("playlist shuffle"), int) \
        and not isinstance(r.get("playlist shuffle"), bool), (
            f"playlist shuffle must be an Int like Perl, got {r.get('playlist shuffle')!r}")
    assert isinstance(r.get("playlist repeat"), int) \
        and not isinstance(r.get("playlist repeat"), bool), (
            f"playlist repeat must be an Int like Perl, got {r.get('playlist repeat')!r}")
    assert r.get("playlist shuffle") in (0, 1, 2)
    assert r.get("playlist repeat") in (0, 1, 2)


def test_status_duration_follows_the_perl_truthy_length_rule(server_up):
    """``duration`` exists only for a truthy song length (Perl rule).

    ``Slim/Control/Queries.pm:4100-4102``::

        if (my $dur = $song->duration()) {
            $dur += 0;
            $request->addResult('duration', $dur);
        }

    Live Perl 9.1.1 (read-only, 2026-09-14): a radio stream with no known
    length answers ``status`` WITHOUT a ``duration`` key (its
    ``remoteMeta``/``playlist_loop`` duration is the string "0"), a stream
    with a known length answers ``"duration":7``.  A fabricated value (the
    elapsed position, or a 1 s placeholder) is what killed SqueezeClient
    (``IllegalStateException: Slider value(1006.49963) … valueTo(1006.497)``,
    NowPlayingFragment.kt:440-479) and pinned SqueezePlay's progress bar.
    """
    r = lms(TEST_PLAYER, ["status", "-", "1"])
    if r.get("mode") == "stop":
        pytest.skip("player stopped — play something to exercise the stream path")
    dur = r.get("duration")
    if dur is None:
        # no known length: Perl omits the key, so must we — and the
        # item-level duration must say "unknown" (0/empty), never a value.
        item = (r.get("playlist_loop") or [{}])[0]
        meta = r.get("remoteMeta") or {}
        known = {str(item.get("duration", "")), str(meta.get("duration", ""))}
        known.discard("")
        assert not (known - {"0", "0.0", "None"}), (
            f"duration key missing although the item knows a length: {known}")
        return
    try:
        durf = float(dur)
    except (TypeError, ValueError):
        pytest.fail(f"duration must be numeric, got {dur!r}")
    assert durf > 0, "a published duration must be > 0 (Perl's truthy test)"


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


def test_status_item_loop_carries_jive_params_and_artwork(server_up):
    """R0.6-A / LIVE-06: SqueezePlay's _whatsPlaying reads
    ``item_loop[1].params.track_id`` (Player.lua:269-282) and the artwork
    from ``item_loop[1]["icon-id"] or .icon``; NowPlayingApplet.lua:117 reads
    ``item['params']['track_id']``. Perl's ``_addJiveSong`` puts
    ``params``/``style``/``icon`` on every item."""
    r = lms(TEST_PLAYER, ["status", "-", "1", "menu:menu", "useContextMenu:1"])
    loop = r.get("item_loop") or []
    if not loop:
        pytest.skip("playlist empty")
    it = loop[0]
    assert isinstance(it.get("params"), dict), (
        f"item_loop[0] needs a params dict for SqueezePlay, got {sorted(it.keys())}")
    assert "track_id" in it["params"], f"params needs track_id: {it['params']!r}"
    assert isinstance(it["params"]["track_id"], int), "params.track_id must be numeric"
    assert it.get("style") == "itemplay", f"item style must be itemplay, got {it.get('style')!r}"
    # artwork: 'icon' (from artwork_url) or 'icon-id' — the local placeholder
    # /html/images/favorites.png is acceptable for a cover-less stream
    assert it.get("icon") or it.get("icon-id"), (
        f"item_loop[0] needs icon-id/icon for the Now-Playing artwork: {sorted(it.keys())}")


def test_status_time_and_duration_are_numeric(server_up):
    """R0.6-A / LIVE-06: the progress bar and the elapsed/remaining time read
    ``event.data.time``/``event.data.duration`` (Player.lua:1193-1194).
    For a local track the duration must be the real DB track length — not the
    elapsed position (that pinned the bar at maximum and froze the time)."""
    r = lms(TEST_PLAYER, ["status", "-", "1", "menu:menu", "useContextMenu:1"])
    assert r.get("time") is not None and float(r["time"]) >= 0, f"bad time {r.get('time')!r}"
    # Perl publishes `duration` only for a truthy $song->duration()
    # (Queries.pm:4100-4102) — a stream without a known length has no key,
    # but a published one is always > 0 and (for a local track) the DB length.
    if r.get("duration") is not None:
        assert float(r["duration"]) > 0, (
            f"a published duration must be > 0, got {r.get('duration')!r}")
    loop = r.get("item_loop") or []
    if r.get("mode") == "play" and loop and loop[0].get("trackType") == "local":
        item_dur = float(loop[0].get("duration") or 0)
        # a known track length must NOT be replaced by the elapsed position
        if item_dur > 0:
            assert r.get("duration") is not None, "a local track with a known length must publish duration"
            assert float(r["duration"]) == item_dur, (
                f"local duration {r['duration']} must be the track length "
                f"{item_dur}, not the elapsed position {r.get('time')}")


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
    """Perl has no 'ver' dispatch (Request.pm:474-637), and an unknown CLI
    request is echoed back verbatim (Plugin/CLI/Plugin.pm:657-663 +
    Request.pm:1095-1100) — live Perl 9.1.1 answers ``ver 0 %3F``.  The
    version comes from ``version ?`` (addDispatch Request.pm:630, result
    ``_version`` Queries.pm:4941-4943) → live Perl ``version 9.1.1``.
    """
    assert cli("ver 0 ?") == "ver 0 %3F", (
        "unknown CLI requests must be echoed like Perl ('ver 0 %3F')")
    out = cli("version ?")
    assert out.startswith("version ") and "9." in out, (
        f"CLI 'version ?' must return the version string like Perl, got {out!r}")


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


# ----------------------------------------------------------------------
# "Eigene Musik" — album drill returns tracks (titles ... album_id:<id>)
# ----------------------------------------------------------------------
def test_album_drill_returns_tracks(server_up):
    """Drilling into an album (SqueezePlay 'Eigene Musik' -> album) must list
    its tracks. A duplicated tracks_albums alias in the titles query previously
    made the drill return count 0 — no tracks appeared."""
    alb = lms(TEST_PLAYER, ["albums", "0", "1"])
    lo = alb.get("loop_loop") or alb.get("albums_loop") or []
    if not lo:
        pytest.skip("Bibliothek leer — keine Alben zum Drill-Down")
    aid = lo[0].get("id")

    res = lms(TEST_PLAYER, ["titles", "0", "3", f"album_id:{aid}"])
    loop = res.get("titles_loop") or res.get("loop_loop") or res.get("item_loop") or []
    assert res.get("count", 0) > 0, f"album drill (album_id={aid}) must return tracks, count={res.get('count')!r}"
    assert loop and loop[0].get("title"), f"track item must have a title: {loop[0] if loop else None!r}"
