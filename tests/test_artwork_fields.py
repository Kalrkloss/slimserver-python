"""Artwork fields per context: radio, local, favourites, albums.

The symptom: the controllers show no preview images (thumbnails).  Our cover
route works (``/music/1/cover.jpg`` → 200, 453693 bytes, live curl), but the
items have to *name* an image field, and the name has to resolve.

Perl's rules, all cited against the READ-ONLY checkout ``/tmp/lms-ref``:

* ``Slim/Control/Queries.pm:5618-5630`` (``_addJiveSong``, the Menu-Status and
  item loop builder) — precedence and the radio fallback::

      my $iconId = $songData->{coverid} || $songData->{artwork_track_id};
      if ( defined($songData->{artwork_url}) ) {
          … 'icon', proxiedImage($songData->{artwork_url})
      } elsif ( defined $iconId ) {
          … 'icon-id', proxiedImage($iconId)
      } elsif ( $isRemote ) {
          # send radio placeholder art for remote tracks with no art
          … 'icon-id', '/html/images/radio.png'
      }

* ``Slim/Control/XMLBrowser.pm:1159-1169`` — browse items: ``$item->{icon}``
  non-http → ``icon-id``, ``$item->{icon}`` http(s) → ``icon``,
  ``$item->{image}`` → ``icon``, ``artwork_track_id`` → ``icon-id``.
* ``Slim/Control/XMLBrowser.pm:5628-5630``'s counterpart for the favourites
  feed is ``proxiedImage('html/images/favorites.png')`` (our
  ``favorites_menu.FAVORITES_ICON``); live Perl answers both the folder and
  the station row with ``"icon-id": "html/images/favorites.png"``.

Client side (``/tmp/squeezer-src/android-squeezer-develop``, read-only):

* ``itemlist/JiveItemViewLogic.java:99-105`` — ``icon()`` loads a remote image
  only when ``item.useIcon()`` (i.e. ``hasIconUri()``, ``model/JiveItem.java:
  170-191``) and otherwise uses the embedded drawable.
* ``model/JiveItem.java:250`` — reads ``icon-id`` first, else ``icon``.
* ``Util.java:238-257`` — ``getAbsoluteUrl`` prefixes the origin for any
  non-absolute path (``urlPrefix + ("/" + url)``), and a pure **hex** ``icon-id``
  is rewritten to ``/music/<id>/cover`` (``HEX_PATTERN`` :236).
* The live log shows the app asking for the favourites path verbatim:
  ``GET /html/images/favorites.png HTTP/1.1" 200 OK``.

Every path this module emits is verified with curl against the running dev
server (read-only, 2026-09-13):

===========================================  =======  ==================
path                                         status   note
===========================================  =======  ==================
``/html/images/favorites.png``               200      14815 B
``/html/images/radio.png``                   404      Perl's path, missing here
``/html/EN/html/images/radio.png``           200      image/png (emitted)
``/html/images/albums.png``                  200      23983 B
``/music/1/cover.jpg``                       200      453693 B
===========================================  =======  ==================

The live HTTP check is skipped when the dev server is not up, so the suite
stays runnable offline.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import favorites_menu
from lyrion.web.api import (RADIO_PLACEHOLDER_ICON, REMOTE_ART_FALLBACK,
                            JSONRPCAPI, _remote_track_id, _remote_url_for_id)

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
PLAYER = "1c:87:2c:47:fc:36"
BASE = "http://127.0.0.1:9000"
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"
MENU_ARGS = ["items", 0, 10, "menu:favorites"]


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


class _Favs:
    TREE = {
        None: [
            {"id": 11, "title": "Chillout", "url": None, "type": "folder",
             "parent_id": None, "position": 0},
            {"id": 12, "title": "1Mix EDM Radio", "url":
             "http://fr1.1mix.co.uk:8060/", "type": "stream",
             "parent_id": None, "position": 1},
        ],
    }

    async def list_items(self, parent_id=None):
        return [dict(x) for x in self.TREE.get(parent_id, [])]

    async def resolve_path(self, path):
        return None


@pytest.fixture()
def favs(monkeypatch):
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: _Favs())
    return _Favs()


class _PM:
    """Minimal PlayerManager stand-in (pattern: tests/test_status_nowplaying)."""

    def __init__(self, player: PlayerState) -> None:
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _status(player: PlayerState, args: list, tracks: dict | None = None):
    """Run a status request in-process with an injected player + track rows."""
    api = JSONRPCAPI()

    async def load(_ids):
        return {i: dict(v) for i, v in (tracks or {}).items()}

    async def run():
        api._load_tracks = load          # type: ignore[method-assign]
        return await api._json_player_status(_PM(player), MAC,
                                             [str(a) for a in args])

    return asyncio.run(run())


def _install_player(playlist: list, position: int = 0,
                    url: str = STREAM_URL) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = "play"
    player.duration = 281.69
    player.playlist = list(playlist)
    player.playlist_position = position
    player.playlist_total = len(playlist)
    player.current_url = url
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


# ── 1. favourites rows ────────────────────────────────────────────────────


def test_favorites_rows_name_the_perl_icon_field(favs):
    """XMLBrowser.pm:1159-1169 — every rows carries the icon Perl would send.

    The stub tree has no stored ``icon``, so the *derived* default appears:
    a folder gets ``html/images/favorites.png`` (``OpmlFavorites.pm:83-88``),
    an http stream ``html/images/radio.png`` (``HTTP.pm:1138-1148``) — exactly
    ``$favs->icon($url)``.  Stored logos (the OPML ``icon`` attribute) win, see
    ``tests/test_favorites_icons.py``.  Without an image field
    ``JiveItemViewLogic.java:99-105`` falls back to the embedded drawable and
    the controllers show nothing.
    """
    res = asyncio.run(JSONRPCAPI()._json_favorites_items(PLAYER,
                                                         list(MENU_ARGS)))
    rows = res["item_loop"]
    assert rows[0].get("icon-id") == favorites_menu.FAVORITES_ICON
    assert rows[1].get("icon-id") == "html/images/radio.png"
    for index, row in enumerate(rows):
        assert row.get("icon-id") in _VERIFIED_PATHS, (
            f"row {index} carries no resolvable icon: {sorted(row)}")


# ── 2. Menu status (the frame Squeezer's Now-Playing reads) ───────────────


def test_menu_status_remote_item_uses_the_radio_placeholder():
    """Queries.pm:5618-5633 — the cover-less remote fallback is Perl's ``icon``.

    Live Perl 9.1.1, read-only 2026-09-19, ``status - 1 menu:menu`` on a
    logo-less stream (the live server's ``http://192.168.1.90/alarm.mp3``)::

        {"icon": "html/images/radio.png", "text": "…", "style": "itemplay",
         "params": {"playlist_index": 0, "track_id": -94115161401160},
         "trackType": "radio"}

    — Perl puts the *skin-relative* ``html/images/radio.png`` into the
    ``icon`` field: the stream's ``artwork_url`` is that path
    (``Slim/Player/Protocols/HTTP.pm:1136-1147`` ``getIcon``) and
    ``_addJiveSong`` sends a defined ``artwork_url`` as ``icon``
    (``Queries.pm:5619-5633``).  ``icon-id`` is kept alongside pointing at the
    same path — the controllers read it first (``JiveItem.java:250``) and both
    spellings resolve to ``/html/images/radio_<size>_m.png`` (curl 200).
    """
    _install_player([STREAM_URL])
    res = _status(_current(), ["-", 10, "menu:menu", "tags:ABdejJKlrStTuxy"])
    item = res["item_loop"][0]
    assert item["trackType"] == "radio"
    assert item["icon"] == REMOTE_ART_FALLBACK == "html/images/radio.png"
    assert item["icon-id"] == RADIO_PLACEHOLDER_ICON
    # both spellings name a path the live server answers (see _VERIFIED_PATHS)
    assert "/" + item["icon"] in _VERIFIED_PATHS
    assert item["icon-id"] in _VERIFIED_PATHS


def test_menu_status_keeps_a_stored_stream_logo(monkeypatch):
    """A stream *with* a logo keeps naming it — nothing changes for it.

    ``player.stream_images`` is the logo the stored station entry /
    the metadata handler delivered (``_simg``, ``Queries.pm:5619-5627``);
    the default only applies when there is none (Perl's
    ``if (defined $songData->{artwork_url})`` branch).

    That logo is published through ``proxiedImage`` on the jive ``icon`` field
    (``Queries.pm:5626``); ``ImageProxy.pm:443-461`` wraps an external URL and
    leaves every other path alone (which is why the already-proxied value of a
    feed row survives unchanged).  Live Perl 9.1.1 (read-only 2026-09-20, the
    playing Hirschmilch stream, ``status - 1 menu:menu``) answers exactly the
    proxied form: ``item_loop[0].icon =
    "/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein.com%2Fs111987q.png/image.png"``.
    """
    player = _install_player([STREAM_URL])
    player.stream_images = {STREAM_URL: "http://logos.example/station.png"}
    res = _status(_current(), ["-", 10, "menu:menu", "tags:ABdejJKlrStTuxy"])
    item = res["item_loop"][0]
    assert item["icon"] == (
        "/imageproxy/http%3A%2F%2Flogos.example%2Fstation.png/image.png")
    assert "icon-id" not in item


def _current() -> PlayerState:
    return PlayerManager().get_player(PLAYER)


def test_menu_status_prefers_the_track_cover_over_the_album_cover():
    """Queries.pm:5618-5629 — ``artwork_url`` wins, then ``coverid``.

    ``icon`` for a stored ``artwork_url``, ``icon-id`` (plus the Jive
    ``music/<id>/cover`` path) for a DB cover id — a client may not read a
    full URL out of ``icon-id`` (``Util.java:249-257`` rewrites only pure hex
    ids into the cover route).
    """
    _install_player([4242], url="file:///music/local.mp3")
    res = _status(
        _current(), ["-", 10, "menu:menu", "tags:ABdejJKlrStTuxy"],
        tracks={4242: {"title": "Local Song", "artist": "Someone",
                       "album": "Some Album", "duration": 200.0,
                       "url": "file:///music/local.mp3", "cover": 4242}})
    item = res["item_loop"][0]
    assert item["trackType"] == "local"
    assert item["icon-id"] == "4242"
    assert item["icon"] == "music/4242/cover"


def test_menu_status_uses_artwork_url_when_present():
    """Queries.pm:5620-5622 — a stored ``artwork_url`` becomes ``icon``."""
    _install_player([4242], url="file:///music/local.mp3")
    res = _status(
        _current(), ["-", 10, "menu:menu", "tags:ABdejJKlrStTuxy"],
        tracks={4242: {"title": "Local Song", "url": "file:///music/local.mp3",
                       "cover": 4242,
                       "artwork_url": "/music/4243/cover.jpg"}})
    item = res["item_loop"][0]
    assert item["icon"] == "/music/4243/cover.jpg"
    assert "icon-id" not in item


def test_plain_status_has_no_item_loop_and_keeps_playlist_loop():
    """Queries.pm:4353 — ``item_loop`` exists only in menuMode.

    Live Perl answered a plain ``status - 1 tags:…`` with 25 keys and no
    ``item_loop`` where we sent 26 (parent measurement, 2026-09-13); our own
    playlist item is the tags window §4425-4470.
    """
    _install_player([STREAM_URL])
    res = _status(_current(), ["-", 1, "tags:ABdejJKlrStTuxy"])
    assert "item_loop" not in res
    assert res["playlist_loop"]
    menu = _status(_current(), ["0", "300", "menu:menu", "useContextMenu:1"])
    assert menu["item_loop"]


# ── 3. the remote-track id shape (Squeeze Client crash) ───────────────────


def test_playlist_loop_remote_id_is_a_number_not_the_url():
    """RemoteTrack.pm:317 — ``id => -int($self)``.

    Perl answers a stream with ``"id": "-94115161401160"`` (live probe); a
    client that parses the field numerically crashes on the URL string we
    used to send.
    """
    _install_player([STREAM_URL])
    res = _status(_current(), ["-", 1, "tags:ABdejJKlrStTuxy"])
    item = res["playlist_loop"][0]
    assert item["id"] != STREAM_URL
    assert isinstance(item["id"], int) and item["id"] < 0, item["id"]
    # Perl's magnitudes are pointer-derived (live: -94115161401160, 14
    # digits); ours is a 12-hex-digit hash → 14-15 decimal digits.
    assert 10**13 <= abs(item["id"]) < 10**16, item["id"]
    # the URL stays available on its own field
    assert item["url"] == STREAM_URL


def test_remote_id_resolves_back_to_the_url():
    """RemoteTrack.pm:412/439-451 — ``%idIndex`` / ``fetchById``."""
    tid = _remote_track_id(STREAM_URL)
    assert tid == _remote_track_id(STREAM_URL), "stable per process"
    assert _remote_url_for_id(tid) == STREAM_URL
    assert _remote_url_for_id("0") is None


def test_local_track_id_stays_the_database_id():
    """Queries.pm:5971 ``$returnHash{'id'} = $track->id`` for a local row."""
    _install_player([4242], url="file:///music/local.mp3")
    res = _status(_current(), ["-", 1, "tags:ABdejJKlrStTuxy"],
                  tracks={4242: {"title": "Local Song", "duration": 200.0,
                                 "url": "file:///music/local.mp3"}})
    assert res["playlist_loop"][0]["id"] == 4242


# ── 4. live check: every emitted path really answers ──────────────────────

_VERIFIED_PATHS = {
    "html/images/favorites.png",
    # ``HTTP.pm:1138-1148`` — the derived icon of a logo-less http stream
    # (``$favs->icon($url)``); the live check prepends the missing slash.
    "html/images/radio.png",
    "/html/EN/html/images/radio.png",
    "/music/1/cover.jpg",
    "/html/images/albums.png",
    # Perl's own spelling of the radio placeholder — served by the skin
    # fallback in app._static_path_variants since 2026-09-14 (curl: 200,
    # 16749 B, the same file Perl answers).
    "/html/images/radio.png",
    # …and the doubled-skin spelling SqueezePlay builds from a
    # skin-relative field (live client log 2026-09-14).
    "/html/EN/html/images/favorites.png",
}


def _get(path: str) -> tuple[int, str]:
    if not path.startswith("/"):
        path = "/" + path
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:  # 4xx/5xx are answers too
        return e.code, ""
    except OSError as e:
        pytest.skip(f"dev server not reachable: {e}")


@pytest.mark.parametrize("path", sorted(_VERIFIED_PATHS))
def test_emitted_artwork_paths_answer_on_the_live_server(path):
    """No invented paths: every path the items name must answer 200."""
    status, _ctype = _get(path)
    assert status == 200, f"{path} → {status}"


def test_perl_radio_placeholder_path_is_served_now():
    """``/html/images/radio.png`` is Perl's spelling and answers 200.

    It used to 404 (``_serve_static`` had no skin fallback), which is why
    ``RADIO_PLACEHOLDER_ICON`` carried the ``/html/EN/…`` spelling.  Perl
    resolves a skin-relative URL against ``HTML/EN/``
    (``Slim/Web/HTTP.pm`` + SkinManager); ``app._static_path_variants``
    mirrors that order, so the constant is Perl's literal string again.
    """
    assert RADIO_PLACEHOLDER_ICON == "/html/images/radio.png"
    status, ctype = _get("/html/images/radio.png")
    assert status == 200, f"/html/images/radio.png → {status}"
    assert "image/" in (ctype or "") or ctype == ""
