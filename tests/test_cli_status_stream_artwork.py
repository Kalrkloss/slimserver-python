"""CLI status ``playlist_loop``: a stream entry carries Perl's artwork fields.

Symptom: our CLI ``status`` answered a radio entry with ``url`` only — no
``artwork_url`` — while the JSON-RPC status of the same state already carried
the station logo and Perl carries it in the CLI too.

Raw Perl evidence (live LMS 9.1.1 at 192.168.1.90:9090, read-only, 2026-09-20;
the CLI takes the player id FIRST: ``<mac> status <start> <count> [tags:]``)::

    <mac> status 0 3            → playlist index:0 id:-94115167819792
                                  title:A-Frame
    <mac> status 0 3 tags:K     → … artwork_url:/imageproxy/http%3A%2F%2F
                                  cdn-radiotime-logos.tunein.com%2Fs111987q.png
                                  /image.png
    <mac> status 0 3 tags:c     → … coverid:-94115167819792
    <mac> status 0 3 tags:KJcj  → … artwork_url:… coverid:… coverart:0

Perl's rule: ``_songData`` walks the same tag map for a ``RemoteTrack``
(``Slim/Control/Queries.pm:5964-6108``) — ``'K' => ['artwork_url', '',
'coverurl']`` (:5714) with ``proxiedImage`` applied (:6101), ``'c'`` →
``RemoteTrack::coverid`` = the track id
(``Slim/Schema/RemoteTrack.pm:496``).  The value is the logo filed for the
entry URL (``Slim/Player/Protocols/HTTP.pm:1092``), else the handler's
``cover => $cover || $icon`` fallback ``html/images/radio.png``
(``:1140-1147``).

The fix reuses the JSON-RPC status' resolution
(``lyrion.web.api._stream_artwork_url`` / ``_perl_proxied_image`` /
``REMOTE_ART_FALLBACK``) instead of a second logo lookup.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.control import cli_commands
from lyrion.control.cli import CLIContext
from lyrion.control.queries import escape, render_line
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import (REMOTE_ART_FALLBACK, JSONRPCAPI, _remote_track_id)

MAC = "1c:87:2c:47:fc:36"
# The favourite's row URL and the URL the scan resolves the play to.
ENTRY_URL = ("http://opml.radiotime.com/Tune.ashx?id=s355203&formats=aac,ogg,"
             "mp3,wmpro,wma,wmvoice&partnerId=15&serial="
             "89d36ef4510ad7d85b912b3d0fee8bad")
RESOLVED_URL = "https://fr2.1mix.co.uk:8000/320h"
STATION = "1Mix Radio EDM Stream"
RAW_TUNEIN = "http://cdn-profiles.tunein.com/s355203/images/logoq.jpg?t=1"
PROXIED_TUNEIN = ("/imageproxy/http%3A%2F%2Fcdn-profiles.tunein.com%2Fs355203"
                  "%2Fimages%2Flogoq.jpg%3Ft%3D1/image.jpg")


@pytest.fixture(autouse=True)
def _isolated_players():
    prev = getattr(PlayerManager, "_instance", None)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    yield
    PlayerManager._instance = prev


def _playing_stream(image: str = RAW_TUNEIN, title: str = STATION) -> PlayerState:
    """A player whose playlist entry was rewritten to the RESOLVED url.

    Mirrors the live order: the registration happens for the row's URL
    (``music/favorites.py`` → ``_set_stream_title`` / ``_set_stream_image``)
    and ``networking/protocol.py`` replaces the playlist entry with the
    resolved URL afterwards (see tests/test_stream_artwork_resolution.py).
    """
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.power = True
    player.mode = "play"
    player.playlist = [RESOLVED_URL]
    player.playlist_position = 0
    player.playlist_total = 1
    player.current_url = RESOLVED_URL
    player.remote = 1
    player.stream_baseline_title = title
    if title:
        JSONRPCAPI._set_stream_title(player, ENTRY_URL, title)
    if image:
        JSONRPCAPI._set_stream_image(player, ENTRY_URL, image)
    return player


def _loop(player: PlayerState, tags: str) -> list[dict]:
    return asyncio.run(cli_commands._status_playlist_loop(player, tags))


def test_stream_entry_carries_the_registered_logo_for_tag_k():
    """``tags:K`` → ``artwork_url`` = the proxied row logo (live Perl)."""
    entry = _loop(_playing_stream(), "K")[0]
    assert entry["artwork_url"] == PROXIED_TUNEIN, entry
    assert entry["url"] == RESOLVED_URL, entry
    # 'c' was not requested → no coverid (Perl's tag map is per tag).
    assert "coverid" not in entry, entry


def test_stream_entry_carries_coverid_for_tag_c():
    """``tags:c`` → ``coverid`` is the RemoteTrack id, and nothing else."""
    entry = _loop(_playing_stream(), "c")[0]
    assert entry["coverid"] == _remote_track_id(RESOLVED_URL), entry
    assert "artwork_url" not in entry, entry


def test_logo_less_stream_keeps_perls_placeholder():
    """No registration for the URL → Perl's ``html/images/radio.png``."""
    entry = _loop(_playing_stream(image=""), "K")[0]
    assert entry["artwork_url"] == REMOTE_ART_FALLBACK, entry


def test_want_all_carries_the_artwork_fields():
    """An empty tag code adds every known field (this port's rule)."""
    entry = _loop(_playing_stream(), "")[0]
    assert entry["artwork_url"] == PROXIED_TUNEIN, entry
    assert entry["coverid"] == _remote_track_id(RESOLVED_URL), entry


def test_rendered_cli_line_carries_artwork_url():
    """The wire form: ``artwork_url:<escaped value>`` inside the loop."""
    loop = _loop(_playing_stream(), "galdK")
    line = render_line(clientid=MAC.upper(), terms=["status"],
                       params=[("_index", "0"), ("_quantity", "3"),
                               ("tags", "galdK")],
                       results=[("playlist_loop", loop)])
    assert escape(f"artwork_url:{PROXIED_TUNEIN}") in line, line
    assert escape(f"url:{RESOLVED_URL}") in line, line


def test_whole_status_line_emits_the_loop_for_the_artwork_tag_alone():
    """``status … tags:K`` (no ``l``) must answer the loop like Perl does.

    Live Perl 9.1.1, read-only 2026-09-20: ``<mac> status 0 3 tags:K`` →
    ``playlist index:0 id:… title:A-Frame artwork_url:…``.  Our CLI used to
    drop the whole ``playlist_loop`` for a tag set without ``l`` (and with it
    every artwork field), while the JSON-RPC status of the same state
    answered it (``Queries.pm:4348-4353`` gates the loop on nothing but a
    non-empty playlist).
    """
    player = _playing_stream()
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC.upper().replace(":", ""): player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    ctx = CLIContext(client_id="test", player_id=MAC.upper())
    line = asyncio.run(cli_commands.cmd_status(None, ctx, ["0", "5", "tags:K"]))[0]
    assert escape(f"artwork_url:{PROXIED_TUNEIN}") in line, line


def test_local_track_entry_is_untouched(monkeypatch):
    """Counter-check: a local (DB) entry keeps exactly its old fields.

    ``_addSong``'s local branch is not part of this change — no
    ``artwork_url``/``coverid`` is added there, whatever the tags ask for.
    """
    async def fake_query_db(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM tracks WHERE id" in sql:
            return [{"id": 7, "title": "Sonnentanz", "url": "file:///m/1.mp3",
                     "duration": 213, "genre": "Electronic", "year": 2013,
                     "tracknum": 1}]
        if "FROM contributors c JOIN tracks_contributors" in sql:
            return [{"name": "Klangkarussell"}]
        if "FROM albums al JOIN tracks_albums" in sql:
            return [{"title": "Netzwerk"}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", fake_query_db)
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.playlist = [7]
    player.playlist_position = 0
    # 'galdK' → the same five fields as before the change, artwork tags ignored.
    assert _loop(player, "galdK")[0] == {
        "playlist index": 0,
        "duration": 213,
        "genre": "Electronic",
        "artist": "Klangkarussell",
        "album": "Netzwerk",
    }
    # …and the tag set that would carry them for a stream stays artwork-free.
    full = _loop(player, "tuyngaldK")[0]
    assert full == {
        "playlist index": 0,
        "title": "Sonnentanz",
        "duration": 213,
        "url": "file:///m/1.mp3",
        "genre": "Electronic",
        "year": 2013,
        "tracknum": 1,
        "artist": "Klangkarussell",
        "album": "Netzwerk",
    }, full
