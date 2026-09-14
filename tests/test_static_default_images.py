"""Default images + browse-tap shape.

Two user-visible symptoms of the same class — "the client asks for something
the server does not answer" and "the client's tap builds a request the server
does not understand":

1. **SqueezePlay shows no covers / no default images.**  The client asks for
   its two spellings of every skin image, live log 2026-09-14:

   * ``GET /html/images/radio.png`` → **404** (Perl: 200, 16749 B)
   * ``GET /html/EN/html/images/favorites.png`` → **404** (Perl: 404 too, but
     SqueezePlay builds it from a *skin-relative* item field, so it must
     answer here)
   * ``GET /html/EN/html/images/radio.png`` → 200 (that is why the radio
     placeholder used to be emitted in that spelling)

   Perl's rule: the web root is ``HTML/`` and a URL is resolved against the
   skin directory, i.e. ``HTML/EN/<path>`` (``Slim/Web/HTTP.pm:466-490``
   feeds ``$uri->path()`` into the SkinManager).  Live Perl 9.1.1 proves the
   pairing (curl, 2026-09-14):

   ====================================  =======  =========================
   URL                                   status   note
   ====================================  =======  =========================
   ``/html/images/radio.png``            200      16749 B (HTML/EN/…)
   ``/html/EN/html/images/radio.png``    404      skin doubled
   ``/html/images/favorites.png``        200      14815 B
   ``/html/EN/html/images/favorites.png`` 404     skin doubled
   ``/html/images/genres_40x40_m.png``   200      1514 B, 40x40 RGBA PNG
   ====================================  =======  =========================

   ``app._static_path_variants`` reproduces that order for our layout
   (static root = ``html/``) and additionally tolerates the doubled ``EN/``
   spelling, and ``api._serve_static`` scales Jive's size-encoded names
   instead of shipping the 512x512 original.

2. **„Musikordner sieht nur die folder, kann sie aber nicht öffnen."**  Perl's
   bmf feed carries ``params`` (its own ``commonVariables`` name) and its
   ``base.actions.go`` names ``itemsParams: "params"``; the tap therefore
   sends ``browselibrary items … mode:bmf item_id:<index> isContextMenu:1``.
   Our base action pointed ``go`` at ``playControlParams`` + a context window
   and the rows carried ``commonParams`` only, so a plain tap never drilled.

Perl citations are the read-only checkout ``/tmp/lms-ref`` plus live 9.1.1
answers captured read-only from ``192.168.1.90:9000``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import menus
from lyrion.web.api import (JSONRPCAPI, RADIO_PLACEHOLDER_ICON, REMOTE_ART_FALLBACK,
                            WebAPIHandler, _bmf_index_dir)
from lyrion.web.app import _static_path_variants

REPO_HTML = Path(__file__).resolve().parent.parent / "html"

MAC = "1c:87:2c:47:fc:36"
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


# ── 1. the static resolver ────────────────────────────────────────────────


def test_variants_put_perls_skin_fallback_after_the_plain_path():
    """Perl's order: the path as sent, then ``HTML/EN/<path>``."""
    variants = _static_path_variants("/html/images/radio.png")
    assert variants[0] == "html/images/radio.png"
    assert "EN/html/images/radio.png" in variants
    # our historical layout (static root already *is* ``HTML/``)
    assert "images/radio.png" in variants


def test_variants_drop_a_doubled_skin_segment():
    """``/html/EN/html/images/x`` → ``html/images/x`` (SqueezePlay's spelling)."""
    variants = _static_path_variants("/html/EN/html/images/favorites.png")
    assert "html/images/favorites.png" in variants
    assert "images/favorites.png" in variants


def test_variants_do_not_escape_the_root():
    api = WebAPIHandler()
    api.set_static_dir(str(REPO_HTML))
    for path in ("/../../etc/passwd", "/html/../../etc/passwd"):
        status, _headers, _body = api._serve_static(path)
        assert status in (403, 404), (path, status)


TAGS = "ABCDEKJZljcuxyrtS"


def _static(path: str) -> tuple[int, dict, bytes]:
    api = WebAPIHandler()
    api.set_static_dir(str(REPO_HTML))
    return api._serve_static(path)


@pytest.mark.parametrize("path", [
    # Perl's spelling (used to 404: "keine Standardbilder").
    "/html/images/radio.png",
    "/html/images/favorites.png",
    "/html/images/albums.png",
    "/html/images/artists.png",
    "/html/images/genres.png",
    "/html/images/musicfolder.png",
    "/html/images/cover.png",
    # the skin-qualified spellings SqueezePlay also asks for
    "/html/EN/html/images/radio.png",
    "/html/EN/html/images/favorites.png",
    "/html/EN/html/images/albums.png",
])
def test_default_images_answer_with_real_bytes(path):
    status, headers, body = _static(path)
    assert status == 200, f"{path} → {status}"
    assert headers.get("Content-Type", "").startswith("image/"), headers
    assert len(body) > 500, f"{path} → {len(body)} bytes"
    # a real image, not an HTML error page
    assert body.startswith(PNG_MAGIC), body[:8]


def test_jive_sized_skin_name_is_scaled_not_the_original():
    """Live Perl answers ``genres_40x40_m.png`` as a 40x40 PNG (1514 B)."""
    status, headers, body = _static("/html/images/genres_40x40_m.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body.startswith(PNG_MAGIC)
    from PIL import Image
    import io
    with Image.open(io.BytesIO(body)) as im:
        assert im.size == (40, 40), im.size


def test_unknown_static_path_stays_404():
    status, _headers, _body = _static("/html/images/does-not-exist.png")
    assert status == 404


def test_radio_placeholder_is_perls_literal_path():
    """Perl: ``_addJiveSong`` emits ``/html/images/radio.png`` (Queries.pm:5633)."""
    assert RADIO_PLACEHOLDER_ICON == "/html/images/radio.png"
    assert REMOTE_ART_FALLBACK == "html/images/radio.png"
    status, _h, body = _static(RADIO_PLACEHOLDER_ICON)
    assert status == 200 and body.startswith(PNG_MAGIC)


# ── 2. playlist item image fields (status / playlist_loop) ────────────────


class _PM:
    def __init__(self, player: PlayerState) -> None:
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _status(player: PlayerState, args: list, tracks: dict | None = None):
    api = JSONRPCAPI()

    async def load(_ids):
        return {i: dict(v) for i, v in (tracks or {}).items()}

    async def run():
        api._load_tracks = load          # type: ignore[method-assign]
        return await api._json_player_status(_PM(player), MAC,
                                             [str(a) for a in args])

    return asyncio.run(run())


TAGS = "ABCDEKJZljcuxyrtS"


def _player(playlist: list, url: str = STREAM_URL) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = "play"
    player.playlist = list(playlist)
    player.playlist_position = 0
    player.playlist_total = len(playlist)
    player.current_url = url
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC.upper().replace(":", ""): player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


def test_remote_playlist_item_carries_the_perl_artwork_fields():
    """Live Perl (``status - 1 tags:ABCDEKJZlcuxyrtS`` on a stream):

    ``{"title": "Life Breath (oct12)", "artwork_url":
    "html/images/favorites.png", "coverid": "-94115167939280",
    "id": "-94115167939280", "remote": 1, "url": …, "year": "0"}``.

    Our item used to have NO image field at all — the exact field the
    controllers read (and crash on when it is missing) on a stream start.
    """
    player = _player([STREAM_URL])
    item = _status(player, ["-", 1, f"tags:{TAGS}"])["playlist_loop"][0]
    assert item["artwork_url"] == REMOTE_ART_FALLBACK
    assert item["coverid"] == item["id"]
    assert isinstance(item["coverid"], int) and item["coverid"] < 0
    assert item["remote"] == 1
    assert item["icon-id"] == RADIO_PLACEHOLDER_ICON
    assert "icon" not in item


def test_remote_playlist_item_uses_a_stored_station_logo():
    """A stored ``image:`` path is data — Perl's ``$track->coverurl``."""
    player = _player([STREAM_URL])
    player.stream_images = {STREAM_URL: "/html/images/favorites.png"}
    item = _status(player, ["-", 1, f"tags:{TAGS}"])["playlist_loop"][0]
    assert item["artwork_url"] == "html/images/favorites.png"   # skin-relative
    assert item["icon"] == "html/images/favorites.png"


def test_remote_meta_mirrors_the_item_field_set():
    """Perl: ``remoteMeta`` IS ``_songData`` of the stream (Queries.pm:4385-4391).

    Die angeforderten Tags entscheiden Feldmenge UND Reihenfolge (``_songData``
    bindet seinen Hash an Tie::IxHash, :5898-5900).  Live Perl 9.1.1
    ``status - 1 tags:ABCDEKJZlcuxyrtS`` auf dem 1.FM-Stream: ``id, title,
    artist, artwork_url, coverid, url, remote, year, bitrate, duration`` —
    ``artist`` erscheint nur, weil die ICY-Metadaten einen liefern
    (:5925-5935), und ``bitrate`` kommt aus dem ICY-Header (``HTTP.pm:731-734``)
    bzw. ist ohne Wert ``0`` (``Track.pm:362``).
    """
    player = _player([STREAM_URL])
    player.current_title = "Dj Fada 2 - Life Breath (oct12)"
    res = _status(player, ["-", 1, f"tags:{TAGS}"])
    meta = res["remoteMeta"]
    # Live Perl 9.1.1, derselbe Tag-Satz auf dem 1.FM-Stream:
    # {"id","title","artist","addedTime","artwork_url","coverart":"0",
    #  "coverid","url","remote","year","bitrate"} — genau diese Reihenfolge
    # (Tag 'd' ist in TAGS nicht enthalten, deshalb kein "duration").
    assert list(meta) == ["id", "title", "artist", "addedTime",
                          "artwork_url", "coverart", "coverid", "url",
                          "remote", "year", "bitrate"], meta
    assert meta["artwork_url"] == REMOTE_ART_FALLBACK
    assert meta["coverid"] == meta["id"]
    assert meta["coverart"] == "0"        # RemoteTrack::coverArtExists = 0
    assert meta["remote"] == 1 and meta["year"] == "0"
    assert isinstance(meta["addedTime"], str) and ", " in meta["addedTime"]


def test_remote_meta_without_tags_is_id_and_title_only():
    """Live Perl ``status - 1`` (kein ``tags:``): ``{id, title}``.

    ``Queries.pm:4012`` setzt ``$tags = $request->getParam('tags') || ''``;
    die Zeile ``$tags = 'gald' if !defined $tags`` (:4363) greift nie.
    """
    meta = _status(_player([STREAM_URL]), ["-", 1])["remoteMeta"]
    assert list(meta) == ["id", "title"], meta


def test_remote_meta_added_time_appears_for_tag_d():
    """Tag 'D' → ``addedTime`` aus ``$track->addedTime`` (``Track.pm:325-341``).

    Ein ``RemoteTrack`` hat nie ein ``added_time``, also formatiert Perl die
    QUERY-Zeit (``longDateF(undef) . ', ' . timeF(undef)`` — beide fallen auf
    ``time()`` zurück, ``DateTime.pm:53``).  Live Perl 9.1.1:
    ``"addedTime": "Montag, 14. September 2026, 14:19"``.
    """
    meta = _status(_player([STREAM_URL]), ["-", 1, "tags:rD"])["remoteMeta"]
    assert list(meta) == ["id", "title", "bitrate", "addedTime"], meta
    assert isinstance(meta["addedTime"], str) and ", " in meta["addedTime"]


def test_local_playlist_item_carries_artwork_ids():
    """Perl ``songinfo … tags:KJcj`` on a track with art (live 9.1.1):
    ``artwork_track_id`` + ``coverid`` + ``coverart`` "1"."""
    tracks = {4242: {"title": "Local Song", "artist": "Someone",
                     "album": "Some Album", "duration": 200.0,
                     "url": "file:///music/local.mp3", "album_id": 4243,
                     "artwork_url": "/music/4243/cover.jpg",
                     "cover": None}}
    res = _status(_player([4242], url="file:///music/local.mp3"),
                  ["-", 1, f"tags:{TAGS}"], tracks=tracks)
    item = res["playlist_loop"][0]
    assert item["coverid"] == 4243
    assert item["artwork_track_id"] == 4243
    assert item["coverart"] == 1
    assert item["icon"] == "/music/4243/cover.jpg"


def test_local_playlist_item_without_album_art_has_no_ids_but_coverart_zero():
    """``addResultLoopIfValueDefined`` (Queries.pm:864) omits the ids; the
    ``coverart`` flag stays (Perl: ``tracks.cover ? 1 : 0``)."""
    tracks = {4242: {"title": "Local Song", "duration": 200.0,
                     "url": "file:///music/local.mp3", "cover": None}}
    res = _status(_player([4242], url="file:///music/local.mp3"),
                  ["-", 1, f"tags:{TAGS}"], tracks=tracks)
    item = res["playlist_loop"][0]
    assert "artwork_track_id" not in item
    assert "coverid" not in item
    assert item["coverart"] == 0
    assert "icon" not in item and "icon-id" not in item


# ── 3. the „Musikordner“ tap ──────────────────────────────────────────────


def test_bmf_base_action_go_names_perls_items_params():
    """Live Perl ``browselibrary items 0 3 menu:1 mode:bmf``:

    ``base.actions.go = {"cmd": ["browselibrary", "items"],
    "itemsParams": "params", "params": {"mode": "bmf",
    "menu": "browselibrary"}}``.
    """
    actions = menus.base_actions("folder")
    go = actions["go"]
    assert go["itemsParams"] == "params"
    assert go["cmd"] == ["browselibrary", "items"]
    assert go["params"] == {"mode": "bmf", "menu": "browselibrary"}
    # the press-and-hold variant stays the context-menu one (Perl keeps both)
    assert actions["playControl"]["itemsParams"] == "playControlParams"
    assert actions["playControl"]["window"] == {"isContextMenu": 1}
    assert actions["play"]["cmd"] == ["browselibrary", "playlist", "play"]
    assert actions["add-hold"]["cmd"] == ["browselibrary", "playlist", "insert"]


def test_bmf_folder_row_carries_params_with_the_drill_target():
    """Live Perl bmf row: ``"params": {"item_id": "0", "isContextMenu": 1}``."""
    rows = [{"id": 123, "path": "/media/music/Folder", "name": "Folder",
             "type": "folder"}]
    items = JSONRPCAPI()._browselibrary_menu_items("folder", rows, "bmf",
                                                  "", 0)
    item = items[0]
    assert item["params"]["item_id"] == "0"
    assert item["params"]["isContextMenu"] == 1
    # the tokens this server resolves without Perl's feed cache (see
    # _bmf_index_dir): the tap therefore drills with either spelling
    assert item["params"]["folder_id"] == "123"
    assert item["params"]["url"] == "/media/music/Folder"


def test_bmf_index_path_resolves_the_tapped_folder(monkeypatch):
    """Perl resolves ``params.item_id`` ("0", "0.0", …) against its feed.

    Without that cache we resolve it against the folder tree —
    ``_bmf_children`` supplies the same order the rows were listed in.
    """
    from lyrion.web import api as api_mod

    def fake_children(directory, start=0, count=200):
        tree = {
            "/root": [{"id": 1, "path": "/root/A", "name": "A",
                       "type": "folder"},
                      {"id": 2, "path": "/root/B", "name": "B",
                       "type": "folder"}],
            "/root/A": [{"id": 3, "path": "/root/A/X", "name": "X",
                         "type": "folder"}],
        }
        return tree.get(directory, [])[start:start + count], 0

    monkeypatch.setattr(api_mod, "_bmf_children", fake_children)
    assert _bmf_index_dir("0", "/root") == "/root/A"
    assert _bmf_index_dir("0.0", "/root") == "/root/A/X"
    assert _bmf_index_dir("1", "/root") == "/root/B"
    # a token that is no index path / no folder resolves to None
    assert _bmf_index_dir("/media/x", "/root") is None
    assert _bmf_index_dir("9", "/root") is None


def test_feed_playlist_action_routes_to_the_playlist_handler(monkeypatch):
    """Perl's ``browselibrary playlist add|insert|play`` (bmf base actions).

    Live Perl 9.1.1: ``base.actions.play = {"cmd": ["browselibrary",
    "playlist", "play"], "itemsParams": "params"}``.  The sub-command must
    reach this port's ``playlist`` handler (which resolves ``folder_id``) —
    it used to fall through to the ITEMS listing, so a folder's "play"
    answered the album list.
    """
    calls: list = []

    async def fake_control(pm, pid, cmd, args):
        calls.append((cmd, [str(a) for a in args]))

    api = JSONRPCAPI()
    api._json_control = fake_control          # type: ignore[method-assign]
    res = asyncio.run(api._json_browselibrary(
        "browselibrary", ["playlist", "insert", "folder_id:123"], "ab:cd"))
    assert res == {}
    assert calls == [("playlist", ["insert", "folder_id:123"])]

    calls.clear()
    asyncio.run(api._json_browselibrary(
        "browselibrary", ["playlist", "add", "folder_id:123"], None))
    assert calls == [("playlist", ["add", "folder_id:123"])]
    # an unknown sub-command answers empty instead of a library listing
    calls.clear()
    assert asyncio.run(api._json_browselibrary(
        "browselibrary", ["playlist", "zap"], None)) == {}
    assert calls == []
