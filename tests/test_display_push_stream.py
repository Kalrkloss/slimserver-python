"""String von einem Favoriten-Stream: der displaystatus-Push (Perl-Nachbau).

Hintergrund (Symptom „squeezeclient startet stream aus favoriten erfolgreich,
stürzt aber dann ab"): Perl beantwortet **jeden** Playmode-Wechsel mit einem
``displaynotify`` (``Display.pm:910-913``), das die jive-Anzeige der
``displaystatus``-Abonnenten (SqueezePlay/Squeezer/Squeeze Client) trägt.  Der
Inhalt ist ``$parts->{'jive'}`` aus ``Player.pm:651-673`` bzw. der Popup-Hash
der Aufrufer, und ``displaystatusQuery`` gibt ihn unter ``type`` + ``display``
aus (``Queries.pm:1666-1697``).

Alles hier ist gegen die Perl-Quelle (``/tmp/lms-ref``, ``/tmp/lms-web``) und
gegen Live-Antworten des Perl-LMS 9.1.1 (192.168.1.90:9000, nur lesend)
geschrieben; die Quellen stehen im jeweiligen Testkopf.
"""

from __future__ import annotations

import asyncio
import json
import time

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import (
    JSONRPCAPI,
    _perl_added_time,
    _perl_pretty_bitrate,
    _perl_proxied_image,
    jive_now_playing_display,
)
from lyrion.web.cometd import CometdManager

MAC = "00:04:20:2B:88:C8"
STREAM = "http://strm112.1.fm/ambientpsy_mobile_mp3"
CID = "1abc1234"


def _run(coro):
    return asyncio.run(coro)


def _player(**kw) -> PlayerState:
    base = dict(
        mac=MAC, name="Squeezebox Radio", ip="192.168.1.127", port=47413,
        model="baby", model_name="Squeezebox Radio", connected=True,
        power=True,
    )
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


class _StubRPC:
    """The JSON-RPC API the cometd manager dispatches through.

    Production wires ``CometdManager(JSONRPCAPI())`` — the same object holds
    the pending display notification (``set_pending_display``) and answers the
    re-executed ``displaystatus`` request, so the tests use the real handler.
    """

    def __init__(self) -> None:
        self.api = JSONRPCAPI()

    async def handle_request(self, body: bytes) -> bytes:
        return await self.api.handle_request(body)

    def set_pending_display(self, jive, kind="showbriefly", duration=None,
                            ttl=15.0):
        self.api.set_pending_display(jive, kind, duration, ttl)


# ---------------------------------------------------------------------------
# Der jive-Block (Player.pm:488-706 currentSongLines)
# ---------------------------------------------------------------------------

def test_jive_block_for_a_remote_stream_matches_perl_player_pm():
    """``Player.pm:651-673`` — Typ/Text/Style/Play-Mode/is-remote + Icon.

    Live Perl 9.1.1 für den laufenden 1.FM-Stream (read-only ``status``):
    ``remoteMeta.artwork_url`` = ``html/images/favorites.png`` →
    ``$remoteMeta->{cover}`` ist gesetzt, also ``imgKey = 'icon'``
    (``Player.pm:594-596``) und ``proxiedImage`` lässt den skin-relativen Pfad
    unverändert (``ImageProxy.pm:411``: nur ``^https?:`` wird umgeschrieben).
    """
    player = _player(mode="play", playlist=[STREAM], playlist_position=0,
                     current_url=STREAM, current_title="Dj Fada 2 - Life Breath (oct12)",
                     remote=1, stream_bitrate=256000)
    block = _run(jive_now_playing_display(player))
    assert block == {
        "type": "icon",
        "text": ["Now Playing", "Life Breath (oct12)"],
        "style": "play",
        "play-mode": "play",
        "is-remote": 1,
        "icon-id": "/html/images/radio.png",     # ohne Logo: Player.pm:623-626
    }, block


def test_jive_block_uses_icon_when_the_station_has_artwork():
    """Handler-``cover`` gewinnt ``icon``; ein externes Bild wird geproxied."""
    player = _player(mode="play", playlist=[STREAM], playlist_position=0,
                     current_url=STREAM, current_title="Life Breath (oct12)",
                     remote=1,
                     stream_images={STREAM: "https://cdn.example/logo.jpg"})
    block = _run(jive_now_playing_display(player))
    assert block["icon"] == "/imageproxy/https%3A%2F%2Fcdn.example%2Flogo.jpg/image.jpg"
    assert "icon-id" not in block


def test_jive_block_status_is_the_bare_token_for_pause_and_stop():
    """``$status`` trägt KEIN ``" (i OF n) "`` — das kommt nur in ``$lines[0]``.

    ``Player.pm:530-535`` hängt den Zusatz an ``$lines[0]``; ``$jive->{text}``
    (:653) bleibt der reine Status-String.  ``style`` ist ``$jiveIconStyle``,
    das ohne ``rew``/``fwd`` (Commands.pm:1026-1034) die Playmode ist (:507).
    """
    for mode, expected in (("pause", "Paused"), ("stop", "Stopped")):
        player = _player(mode=mode, playlist=[1, STREAM], playlist_position=1,
                         current_url=STREAM, current_title="Life Breath (oct12)")
        block = _run(jive_now_playing_display(player))
        assert block["text"][0] == expected
        assert block["style"] == mode and block["play-mode"] == mode


def test_jive_block_is_absent_with_an_empty_playlist():
    """``Player.pm:509-518`` setzt ``$jive`` nicht → kein jive-Teil.

    Ohne jive-Teil fällt ``displaystatusQuery`` auf den Zeilentext zurück
    (``Queries.pm:1691-1695``); der Port liefert dann ``None``.
    """
    player = _player(mode="stop", playlist=[])
    assert _run(jive_now_playing_display(player)) is None


def test_proxied_image_only_rewrites_external_urls():
    """``ImageProxy.pm:407-425`` wörtlich."""
    assert _perl_proxied_image("html/images/radio.png") == "html/images/radio.png"
    assert _perl_proxied_image("/html/images/radio.png") == "/html/images/radio.png"
    assert _perl_proxied_image(0) == 0
    assert (_perl_proxied_image("https://x/y.png")
            == "/imageproxy/https%3A%2F%2Fx%2Fy.png/image.png")


# ---------------------------------------------------------------------------
# Perl's Zeit-/Bitraten-Strings (Track.pm, DateTime.pm)
# ---------------------------------------------------------------------------

def test_perl_added_time_uses_the_language_default_formats(monkeypatch):
    """``Track.pm:337-341`` = ``longDateF`` + ``timeF`` (``DateTime.pm:52-105``).

    Perl rendert die Pref-Formate und entfernt danach das ``|``-Flag
    (``s/\\|0*//`` bzw. ``s/\\|0?(\\d+)/$1/``).  Für ``de_DE`` sind das
    ``%A, |%d. %B %Y`` / ``%H:%M`` (``strings.txt``
    ``SETUP_LONGDATEFORMAT_DEFAULT``) — live Perl 9.1.1:
    ``remoteMeta.addedTime`` = ``Montag, 14. September 2026, 14:19``.
    """
    monkeypatch.setattr(api_mod, "resolve_language", lambda *a, **k: "DE",
                        raising=False)
    monkeypatch.setattr("lyrion.i18n.resolve_language", lambda *a, **k: "DE")
    ts = time.mktime((2026, 9, 5, 9, 7, 0, 0, 0, -1))
    assert _perl_added_time(ts) == "Samstag, 5. September 2026, 09:07"


def test_perl_pretty_bitrate_matches_the_icy_value():
    """``Track.pm:353-363`` — live Perl: ``"256kb/s CBR"`` / ohne Wert ``0``."""
    assert _perl_pretty_bitrate(256000) == "256kb/s CBR"
    assert _perl_pretty_bitrate(0) == 0
    assert _perl_pretty_bitrate(192000, vbr_scale=1).endswith("VBR")


# ---------------------------------------------------------------------------
# remoteMeta ist tag-gesteuert (Queries.pm:4385-4391 + _songData)
# ---------------------------------------------------------------------------

def _status(args: list[str]) -> dict:
    _pm(_player(mode="play", playlist=[STREAM], playlist_position=0,
                current_url=STREAM,
                current_title="Dj Fada 2 - Life Breath (oct12)",
                remote=1, stream_bitrate=256000,
                stream_titles={STREAM: "1.FM - Ambient Psychill"}))
    return _run(JSONRPCAPI()._slim_request(MAC, ["status", "-", "1"] + args))


def test_remote_meta_carries_bitrate_and_added_time_for_the_full_tag_set():
    """Live Perl ``status - 1 tags:ABCDEKJZlcuxyrtSgad`` (Stream, read-only)::

        remoteMeta = {"id":"-94115167939280","title":"Life Breath (oct12)",
                      "artist":"Dj Fada 2","addedTime":"…","artwork_url":
                      "html/images/favorites.png","coverid":"-94115167939280",
                      "url":…,"remote":1,"year":"0","bitrate":"256kb/s CBR",
                      "duration":"0"}

    Reihenfolge = Tag-Reihenfolge (``_songData`` nutzt Tie::IxHash,
    ``Queries.pm:5898``); ``bitrate`` kommt aus ``$remoteMeta->{r}`` (:5937)
    und ``addedTime`` aus ``$track->addedTime`` (:5681, :6100-6102).
    """
    res = _status(["tags:ABCDEKJZlcuxyrtSgad"])
    meta = res["remoteMeta"]
    assert list(meta) == ["id", "title", "artist", "addedTime", "artwork_url",
                          "coverid", "url", "remote", "year", "bitrate",
                          "duration"], meta
    assert meta["bitrate"] == "256kb/s CBR"
    assert meta["duration"] == "0"          # live: STRING
    assert meta["year"] == "0"
    assert meta["remote"] == 1
    assert meta["url"] == STREAM
    assert meta["artwork_url"] == "html/images/radio.png"
    assert isinstance(meta["addedTime"], str) and ", " in meta["addedTime"]


def test_remote_meta_is_empty_without_tags_like_perl():
    """Live Perl ``status - 1`` (kein ``tags:``) → ``{"id":…, "title":…}``.

    ``Queries.pm:4012`` macht ``$tags = $request->getParam('tags') || ''``;
    die Zeile ``$tags = 'gald' if !defined $tags`` (:4363) ist damit toter
    Code.  Ohne ``tags:`` trägt remoteMeta also nur id + title.
    """
    assert list(_status([])["remoteMeta"]) == ["id", "title"]


def test_remote_meta_menu_mode_forces_the_jive_tag_set():
    """``Queries.pm:4356-4357``: menuMode erzwingt ``aAlKNcxJ``.

    Live Perl ``status - 10 menu:menu useContextMenu:1`` (Stream):
    ``{id,title,artist,artwork_url,remote_title,coverid,remote}`` — kein
    ``bitrate``/``addedTime``, weil deren Tags im Jive-Satz fehlen.
    """
    meta = _status(["menu:menu", "useContextMenu:1"])["remoteMeta"]
    assert "bitrate" not in meta and "addedTime" not in meta
    assert meta["remote_title"] == "1.FM - Ambient Psychill"
    assert list(meta) == ["id", "title", "artist", "artwork_url",
                          "remote_title", "coverid", "remote"], meta


# ---------------------------------------------------------------------------
# Der Push selbst (Display.pm:910-913 → Queries.pm:1616-1697)
# ---------------------------------------------------------------------------

def _manager_with_display_sub():
    rpc = _StubRPC()
    mgr = CometdManager(rpc)
    client = mgr.handshake()
    client.subscriptions[f"/{client.client_id}/slim/displaystatus/{MAC}"] = {
        "request": [MAC, ["displaystatus", "subscribe:showbriefly"]],
        "response": f"/{client.client_id}/slim/displaystatus/{MAC}",
    }
    return rpc, mgr, client


def test_notify_display_pushes_the_icon_block_on_the_subscribed_channel():
    """Perl: die displaystatus-Anfrage wird erneut ausgeführt (autoexecute).

    ``Queries.pm:1616-1620`` speichert Typ/parts/duration in ``privateData``
    und ``displaystatusQuery`` antwortet daraus (:1655-1697).  Erwartet wird
    deshalb genau ein Event auf dem Kanal des Clients mit ``type`` und dem
    **unveränderten** jive-Hash als ``display``.
    """
    rpc, mgr, client = _manager_with_display_sub()
    player = _player(mode="play", playlist=[STREAM], playlist_position=0,
                     current_url=STREAM, current_title="Life Breath (oct12)",
                     remote=1)
    block = _run(jive_now_playing_display(player))
    _run(mgr.notify_display(MAC, "showbriefly", block))

    assert len(client.events) == 1, client.events
    event = client.events[0]
    assert event["channel"] == f"/{client.client_id}/slim/displaystatus/{MAC}"
    assert event["data"]["type"] == "showbriefly"
    assert event["data"]["display"] == block
    assert event["data"]["display"]["icon-id"] == "/html/images/radio.png"

    # A poll of the same subscription answers the same record (Perl's
    # privateData survives until the next notification).
    polled = _run(rpc.api._slim_request(MAC, ["displaystatus", "0", "2"]))
    assert polled == event["data"]


def test_notify_display_of_a_non_subscribed_player_pushes_nothing():
    """Only the changed player's displaystatus subscribers get the record."""
    _rpc, mgr, client = _manager_with_display_sub()
    _run(mgr.notify_display("11:22:33:44:55:66", "showbriefly",
                            {"type": "icon", "text": ["X", "Y"]}))
    assert client.events == []


def test_notify_display_respects_the_subscribed_type():
    """``Queries.pm:1622-1623`` filtert nach dem ``subscribe:``-Typ.

    Ein ``subscribe:update``-Abonnent bekommt kein ``showbriefly``-Event
    (``$subs eq $type || ($subs eq 'bits' && $type ne 'showbriefly') || $subs eq 'all'``).
    """
    _rpc, mgr, client = _manager_with_display_sub()
    channel = f"/{client.client_id}/slim/displaystatus/{MAC}"
    client.subscriptions[channel] = {
        "request": [MAC, ["displaystatus", "subscribe:update"]],
        "response": channel,
    }
    _run(mgr.notify_display(MAC, "showbriefly", {"type": "icon"}))
    assert client.events == []


def test_stream_start_pushes_the_jive_block(monkeypatch):
    """Stream-Start = Playmode-Wechsel → displaynotify (Commands.pm:758-765).

    ``PlayerManager.play_url`` schließt mit Perl's
    ``$client->showBriefly($client->currentSongLines(), {duration => 2})`` ab;
    das löst ``Display.pm:287`` → ``:910-913`` aus.  Hier wird nur der
    Push-Pfad selbst geprüft (der strm-Frame braucht einen Protokoll-Handler).
    """
    player = _player(mode="play", playlist=[STREAM], playlist_position=0,
                     current_url=STREAM, current_title="Life Breath (oct12)",
                     remote=1, stream_bitrate=256000)
    pm = PlayerManager()
    pm.players = {player.mac: player}

    sent: list[tuple] = []

    class _Mgr:
        async def notify_display(self, player_id, kind, jive, duration=None):
            sent.append((player_id, kind, jive, duration))

    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: _Mgr())
    _run(pm.notify_now_playing_display(player, "showbriefly", 2))

    assert len(sent) == 1, sent
    player_id, kind, jive, duration = sent[0]
    assert player_id == MAC and kind == "showbriefly" and duration == 2
    assert jive == {
        "type": "icon",
        "text": ["Now Playing", "Life Breath (oct12)"],
        "style": "play",
        "play-mode": "play",
        "is-remote": 1,
        "icon-id": "/html/images/radio.png",
    }


def test_icy_bitrate_parser_scales_low_values_like_perl():
    """``HTTP.pm:731-734``: ``icy-br`` < 8000 wird mit 1000 multipliziert."""
    from lyrion.player.manager import _icy_bitrate_from_headers

    assert _icy_bitrate_from_headers({"icy-br": "256"}) == 256000
    assert _icy_bitrate_from_headers({"x-audiocast-bitrate": "128"}) == 128000
    assert _icy_bitrate_from_headers({"icy-br": "256000"}) == 256000
    assert _icy_bitrate_from_headers({}) == 0


def test_connect_advice_has_no_superset_keys():
    """D1: Perl ``Cometd.pm:277-279`` sendet nur ``interval``."""
    from lyrion.web.cometd import LONG_POLLING_INTERVAL, RETRY_DELAY_MS, connect_advice

    assert connect_advice("streaming") == {"interval": RETRY_DELAY_MS == 5000} \
        or connect_advice("streaming") == {"interval": RETRY_DELAY_MS}
    assert connect_advice("long-polling") == {
        "interval": LONG_POLLING_INTERVAL}
    assert set(connect_advice("streaming")) == {"interval"}


def test_displaystatus_request_json_round_trip():
    """Der gepushte Record ist JSON-serialisierbar (jive liest ``display``)."""
    _rpc, mgr, client = _manager_with_display_sub()
    block = {"type": "icon", "text": ["Now Playing", None],
             "style": "play", "play-mode": "play", "is-remote": 1}
    _run(mgr.notify_display(MAC, "showbriefly", block))
    raw = json.dumps(client.events[0])
    assert json.loads(raw)["data"]["display"]["text"] == ["Now Playing", None]
