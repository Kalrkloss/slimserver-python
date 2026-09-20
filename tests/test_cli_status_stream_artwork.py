"""CLI status ``playlist_loop``: Perl's ``_songData`` field set per tag letter.

Perl builds the loop with ``_addSong`` → ``_songData`` and starts every item
with ``'playlist index'`` (unshifted, ``Slim/Control/Queries.pm:5461-5479`` +
``:4410-4414``).  ``id`` and ``title`` are added BEFORE the tag loop and are
NOT gated by a tag letter (``Queries.pm:5971-5972``); every other field follows
the letters the client sent, in that order (``split //, $tags``, ``:5964``),
and is omitted when empty (``:6103-6108``).

Raw Perl evidence (live LMS 9.1.1 at 192.168.1.90:9090, read-only, 2026-09-20;
the CLI takes the player id FIRST: ``<mac> status <start> <count> [tags:]``) —
the playing stream ``http://hirschmilch.de:7000/chillout.mp3``::

    <mac> status 0 5              → playlist index:0 id:-94115167819792
                                    title:A-Frame
    <mac> status 0 5 tags:a       → … title:A-Frame artist:Unknown Reality
    <mac> status 0 5 tags:d       → … title:A-Frame duration:0
    <mac> status 0 5 tags:u       → … title:A-Frame
                                    url:http://hirschmilch.de:7000/chillout.mp3
    <mac> status 0 5 tags:o       → … title:A-Frame type:MP3 Radio
    <mac> status 0 5 tags:N       → … title:A-Frame
                                    remote_title:Hirschmilch Chillout
    <mac> status 0 5 tags:K       → … title:A-Frame artwork_url:/imageproxy/…
    <mac> status 0 5 tags:c       → … title:A-Frame coverid:-94115167819792
    <mac> status 0 5 tags:al      → … title:A-Frame artist:Unknown Reality
                                    (NO album — RemoteTrack has none)
    <mac> status 0 5 tags:utado   → … title:A-Frame url:… artist:…
                                    duration:0   (tag order, 't' empty)

and, for a LOCAL track (same ``%tagMap``; live via ``titles 0 1 <tags>``)::

    tags:t → id:201214 title:… tracknum:7        ('t' => tracknum)
    tags:n → id:201214 title:… modificationTime:1723554429
    tags:i → disc:1     tags:g → genre:Psychedelic    tags:d → duration:1195.075

The stream logo resolution is the JSON-RPC status' own
(``lyrion.web.api._stream_artwork_url`` / ``_perl_proxied_image`` /
``REMOTE_ART_FALLBACK``) — one resolution, both protocols — with Perl's
``html/images/radio.png`` fallback (``Protocols/HTTP.pm:1140-1147``).
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.control import cli_commands
from lyrion.control.cli import CLIContext
from lyrion.control.queries import escape, render_line
from lyrion.music import favorites as favorites_mod
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import (
    REMOTE_ART_FALLBACK,
    JSONRPCAPI,
    _register_favorites_row_images,
    _remote_track_id,
    _stream_artwork_url,
)

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
ICY_TITLE = "Unknown Reality - A-Frame"


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


def _playing_stream(image: str = RAW_TUNEIN, title: str = STATION,
                    icy: str = "") -> PlayerState:
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
    player.current_title = icy
    if title:
        JSONRPCAPI._set_stream_title(player, ENTRY_URL, title)
    if image:
        JSONRPCAPI._set_stream_image(player, ENTRY_URL, image)
    return player


def _loop(player: PlayerState, tags: str) -> list[dict]:
    return asyncio.run(cli_commands._status_playlist_loop(player, tags))


def _fields(entry: dict) -> list[str]:
    return list(entry)


# ── id/title are NOT tag-gated (Queries.pm:5971-5972) ────────────────────────

def test_empty_tag_code_answers_id_and_title_only():
    """Perl's ``$tags = $request->getParam('tags') || ''`` (Queries.pm:4008).

    Live Perl ``<mac> status 0 5`` on the stream: ``playlist index:0
    id:-94115167819792 title:A-Frame`` — the former ``want_all`` here (every
    known field) was this port's invention.
    """
    entry = _loop(_playing_stream(), "")[0]
    assert entry == {"playlist index": 0,
                     "id": _remote_track_id(RESOLVED_URL),
                     "title": STATION}, entry


def test_tag_k_does_not_carry_the_url():
    """``tags:K`` → artwork_url only (live Perl: no ``url`` without ``u``)."""
    entry = _loop(_playing_stream(), "K")[0]
    assert entry["artwork_url"] == PROXIED_TUNEIN, entry
    assert "url" not in entry, entry
    assert "coverid" not in entry, entry      # 'c' was not requested


def test_tag_u_carries_the_url():
    """``tags:u`` → ``url`` = the playlist entry's URL (live Perl)."""
    entry = _loop(_playing_stream(), "u")[0]
    assert entry["url"] == RESOLVED_URL, entry
    assert "artwork_url" not in entry, entry


def test_tags_are_served_in_the_clients_order():
    """``split //, $tags`` — the field order follows the tag string."""
    entry = _loop(_playing_stream(), "uad")[0]
    assert _fields(entry) == ["playlist index", "id", "title", "url",
                              "duration"], entry


# ── the stream's own fields ─────────────────────────────────────────────────

def test_stream_entry_carries_the_registered_logo_for_tag_k():
    """``tags:K`` → ``artwork_url`` = the proxied row logo (live Perl)."""
    entry = _loop(_playing_stream(), "K")[0]
    assert entry["artwork_url"] == PROXIED_TUNEIN, entry


def test_stream_entry_carries_coverid_for_tag_c():
    """``tags:c`` → ``coverid`` is the RemoteTrack id, and nothing else."""
    entry = _loop(_playing_stream(), "c")[0]
    assert entry["coverid"] == _remote_track_id(RESOLVED_URL), entry
    assert "artwork_url" not in entry, entry


def test_logo_less_stream_keeps_perls_placeholder():
    """No registration for the URL → Perl's ``html/images/radio.png``."""
    entry = _loop(_playing_stream(image=""), "K")[0]
    assert entry["artwork_url"] == REMOTE_ART_FALLBACK, entry


def test_stream_duration_is_zero_for_tag_d():
    """Live Perl ``tags:d`` → ``duration:0`` (RemoteTrack::secs)."""
    entry = _loop(_playing_stream(), "d")[0]
    assert entry["duration"] == 0, entry


def test_stream_year_is_zero_for_tag_y():
    """Live Perl ``tags:y`` → ``year:0`` (RemoteTrack's numeric default).

    ``tracknum``/``disc`` are undef on a RemoteTrack, so they stay absent
    (live ``tags:t``/``tags:i`` → nothing).
    """
    assert _loop(_playing_stream(), "y")[0]["year"] == 0
    for code in ("t", "i"):
        entry = _loop(_playing_stream(), code)[0]
        assert _fields(entry) == ["playlist index", "id", "title"], entry


def test_icy_title_split_gives_artist_and_title():
    """Perl's ``$remoteMeta->{title}`` split (``HTTP.pm:1076-1083``).

    Live Perl on the Hirschmilch stream (ICY ``Unknown Reality - A-Frame``):
    ``title:A-Frame artist:Unknown Reality`` for ``tags:a``.
    """
    entry = _loop(_playing_stream(icy=ICY_TITLE), "a")[0]
    assert entry["title"] == "A-Frame", entry
    assert entry["artist"] == "Unknown Reality", entry


def test_stream_without_icy_title_omits_the_artist():
    """A RemoteTrack without metadata has no artist (Queries.pm:5999-6007)."""
    entry = _loop(_playing_stream(), "a")[0]
    assert entry["title"] == STATION, entry
    assert "artist" not in entry, entry


def test_remote_title_is_the_registered_station_name():
    """``tags:N`` → ``remote_title`` (live Perl: ``Hirschmilch Chillout``)."""
    entry = _loop(_playing_stream(), "N")[0]
    assert entry["remote_title"] == STATION, entry


def test_remote_title_is_omitted_without_a_registration():
    """Nothing registered for the URL ⇒ Perl omits the field."""
    player = _playing_stream(title="")
    entry = _loop(player, "N")[0]
    assert "remote_title" not in entry, entry


def test_tag_x_marks_a_stream_as_remote():
    """``'x' => ['remote', '', 'remote']`` — only emitted when true (:5994)."""
    entry = _loop(_playing_stream(), "x")[0]
    assert entry["remote"] == 1, entry


# ── the wire form ───────────────────────────────────────────────────────────

def test_rendered_cli_line_carries_artwork_url():
    """The wire form: ``artwork_url:<escaped value>`` inside the loop."""
    loop = _loop(_playing_stream(), "galdK")
    line = render_line(clientid=MAC.upper(), terms=["status"],
                       params=[("_index", "0"), ("_quantity", "3"),
                               ("tags", "galdK")],
                       results=[("playlist_loop", loop)])
    assert escape(f"artwork_url:{PROXIED_TUNEIN}") in line, line
    assert escape(f"title:{STATION}") in line, line
    assert escape(f"id:{_remote_track_id(RESOLVED_URL)}") in line, line


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


# ── local (DB) entries: Perl's letters, not this port's older guesses ────────

def _local_loop(monkeypatch, tags: str) -> dict:
    async def fake_query_db(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM tracks WHERE id" in sql:
            return [{"id": 7, "title": "Sonnentanz", "url": "file:///m/1.mp3",
                     "duration": 213, "genre": "Electronic", "year": 2013,
                     "tracknum": 1, "modtime": 1711053369, "disc": 1,
                     "remote": 0}]
        if "FROM contributors c JOIN tracks_contributors" in sql:
            return [{"name": "Klangkarussell"}]
        if "FROM albums al JOIN tracks_albums" in sql:
            return [{"title": "Netzwerk"}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", fake_query_db)
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.playlist = [7]
    player.playlist_position = 0
    return _loop(player, tags)[0]


def test_local_track_id_and_title_are_always_present(monkeypatch):
    """``id``/``title`` before the tag loop — live ``titles 0 1 tags:t``."""
    assert _local_loop(monkeypatch, "t") == {
        "playlist index": 0,
        "id": 7,
        "title": "Sonnentanz",
        "tracknum": 1,
    }


def test_local_track_tag_letters_match_perls_tag_map(monkeypatch):
    """``'t' => tracknum``, ``'n' => modificationTime``, ``'i' => disc`` …"""
    assert _local_loop(monkeypatch, "n") == {
        "playlist index": 0,
        "id": 7,
        "title": "Sonnentanz",
        "modificationTime": 1711053369,
    }
    assert _local_loop(monkeypatch, "tuyngaldix") == {
        "playlist index": 0,
        "id": 7,
        "title": "Sonnentanz",
        "tracknum": 1,
        "url": "file:///m/1.mp3",
        "year": 2013,
        "modificationTime": 1711053369,
        "genre": "Electronic",
        "artist": "Klangkarussell",
        "album": "Netzwerk",
        "duration": 213,
        "disc": 1,
    }


def test_local_track_keeps_no_artwork_fields(monkeypatch):
    """``K``/``c`` stay unserved for local tracks (unchanged)."""
    entry = _local_loop(monkeypatch, "Kc")
    assert entry == {"playlist index": 0, "id": 7, "title": "Sonnentanz"}


# ── a merely ADDED favourite keeps its row logo ─────────────────────────────

def test_favorites_render_files_the_row_logo_under_the_row_url():
    """``XMLBrowser.pm:1043-1049`` for the favourites list's own rows."""
    from types import SimpleNamespace

    player = SimpleNamespace(stream_images={}, stream_titles={},
                             current_url="")
    items = [
        {"id": 27, "title": STATION, "url": ENTRY_URL,
         "icon": PROXIED_TUNEIN, "type": "stream"},
        {"id": 1, "title": "Chill", "url": None,
         "icon": "html/images/favorites.png", "type": "folder"},
        {"id": 29, "title": "80s80s", "url": "http://regiocast.invalid/x",
         "icon": "html/images/radio.png", "type": "stream"},
    ]
    _register_favorites_row_images(player, items)
    # The row with a logo is filed — that is what a later status reads back
    # for a playlist entry that was only ADDED, never played.
    assert _stream_artwork_url(player, ENTRY_URL) == PROXIED_TUNEIN
    # Folders file nothing; a row with only the handler placeholder keeps
    # Perl's default (html/images/radio.png) instead of a stripped copy.
    assert player.stream_images == {ENTRY_URL: PROXIED_TUNEIN}


def test_cli_favorites_list_files_the_row_logo(monkeypatch):
    """The CLI list render goes through the same XMLBrowser loop.

    ``addDispatch(['favorites','items',…], […, \\&cliBrowse])``
    (``Plugin/Favorites/Plugin.pm:75``) → ``cliBrowse`` (:763) →
    ``Slim::Control::XMLBrowser::cliQuery`` — the very loop that caches the
    row image.  Both protocols must therefore answer a later status with the
    row logo.
    """
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.stream_images = {}
    player.stream_titles = {}
    player.current_url = ""
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC.upper(): player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    class _FM:
        async def list_items(self, parent=None):
            return [{"id": 27, "title": STATION, "url": ENTRY_URL,
                     "icon": PROXIED_TUNEIN, "type": "stream"}]

    monkeypatch.setattr(favorites_mod, "get_favorites_manager", lambda: _FM())
    ctx = CLIContext(client_id="test", player_id=MAC.upper())
    line = asyncio.run(cli_commands.cmd_favorites(None, ctx, ["items"]))[0]
    assert escape(f"image:{PROXIED_TUNEIN}") in line, line
    assert _stream_artwork_url(player, ENTRY_URL) == PROXIED_TUNEIN
