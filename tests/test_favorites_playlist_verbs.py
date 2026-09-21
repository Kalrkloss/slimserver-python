"""``favorites playlist add|insert|play`` — the Play-Control menu verbs.

User symptom: in the Play-Control menu of a favourites row ("Am Ende
hinzufügen" / "Als nächstes wiedergeben") nothing happened for a favourite
**inside a folder**; "Wiedergabe" worked.  Measured on the running server
(raw, before the fix, player PyTest)::

    favorites playlist add    item_id:<sid>.2.0 menu:1  -> playlist_tracks 1 -> 1
    favorites playlist insert item_id:<sid>.2.0 menu:1  -> playlist_tracks 1 -> 1
    favorites playlist play   item_id:<sid>.2.0 menu:1  -> played "SWR 3"

Perl side (READ-ONLY checkout ``/tmp/lms-ref``, live Perl 192.168.1.90):

* ``Slim/Plugin/Favorites/Plugin.pm:81`` registers
  ``['favorites','playlist','_method']`` on ``cliBrowse``; ``:746-764`` hands
  the request to ``Slim::Control::XMLBrowser::cliQuery`` (:763).
* ``Slim/Control/XMLBrowser.pm:331-340`` splits the item id on ``.`` and shifts
  the browse-session handle off; ``:373-405`` descends **one crumb per level**::

      $subFeed = $subFeed->{'items'}->[$in - $subFeed->{'offset'}];   (:384)

  so the last crumb selects the row at any depth — root or folder.
* ``:667-702`` — for every ``_method`` in add/insert/play the row's URL is the
  same, only the verb differs: ``$client->execute([ 'playlist', $method, $url ])``
  (:702), with the row's name/logo filed under the URL first (:693-700).
* ``Slim/Control/Commands.pm:1483-1491`` (play/load = clear + start),
  ``:1495-1503`` (add = append → ``Slim/Player/Playlist.pm:239``),
  ``:1535-1560`` + ``Playlist.pm:265-284`` (insert = append, then move the new
  block to ``playingSongIndex + 1``).

Live Perl 9.2 (read-only, 2026-09-21) resolves both ids to the row Perl feeds
into that command::

    favorites items … useContextMenu:1 item_id:32f2829e.2.0
      -> {"text":"Titel: SWR 3"} {"text":"URL: https://liveradio.swr.de/sw282p3/swr3/"}
    favorites items … useContextMenu:1 item_id:32f2829e.6
      -> {"text":"Titel: 1Mix Radio EDM Stream"} {"text":"URL: http://opml.radiotune…"}

Everything here runs in-process against the real ``CLIHandler``/``_fav_playlist``
with a favourites stand-in and an injected ``PlayerState`` — no server
start/stop, no DB write, no live command.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"
SID = "98ed1835"
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"

F1, F2, S1, S2, S3 = 11, 13, 12, 14, 15


class _Favs:
    """FavoritesManager stand-in with the real tree walk.

    ``resolve_path`` mirrors ``music/favorites.py`` (which mirrors Perl
    ``XMLBrowser.pm:331-405``): the 8-hex session handle is dropped, every
    remaining crumb is an index into the sorted child list of its parent.
    """

    TREE = {
        None: [
            {"id": F1, "title": "Chillout", "url": None, "type": "folder",
             "parent_id": None, "position": 0,
             "icon": "html/images/favorites.png"},
            {"id": S1, "title": "1Mix EDM Radio", "url": STREAM_URL,
             "type": "stream", "parent_id": None, "position": 1,
             "icon": "html/images/radio.png"},
        ],
        F1: [
            {"id": F2, "title": "Unterordner", "url": None, "type": "folder",
             "parent_id": F1, "position": 0,
             "icon": "html/images/favorites.png"},
            {"id": S2, "title": "Absolut relax",
             "url": "https://absolut-relax.example/stream/mp3",
             "type": "stream", "parent_id": F1, "position": 1,
             "icon": "/imageproxy/https%3A%2F%2Fabsolut-relax.example/image.png"},
        ],
        F2: [
            {"id": S3, "title": "SWR 3",
             "url": "https://liveradio.swr.de/sw282p3/swr3/",
             "type": "stream", "parent_id": F2, "position": 0,
             "icon": "html/images/favorites.png"},
        ],
    }

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []

    async def list_items(self, parent_id=None):
        return [dict(x) for x in self.TREE.get(parent_id, [])]

    async def resolve_path(self, path):
        from lyrion.music.favorites import _is_session_root

        crumbs = [c for c in str(path).split(".") if c]
        if crumbs and (_is_session_root(crumbs[0]) or crumbs[0] == "0"):
            crumbs = crumbs[1:]
        if not crumbs:
            return None
        parent = None
        for crumb in crumbs:
            if not str(crumb).lstrip("-").isdigit():
                return None
            idx = int(crumb)
            items = await self.list_items(parent)
            if idx < 0 or idx >= len(items):
                return None
            parent = int(items[idx]["id"])
        return parent

    async def get(self, fav_id):
        for rows in self.TREE.values():
            for row in rows:
                if int(row["id"]) == int(fav_id):
                    return dict(row)
        return None

    async def play(self, player_id, fav_id):
        self.played.append((player_id, fav_id))
        return True


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


@pytest.fixture()
def favs(monkeypatch):
    fake = _Favs()
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: fake)
    return fake


def _install_player(playlist=None, position: int = 0) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = "stop"
    player.playlist = list(playlist or [])
    player.playlist_position = position
    player.playlist_total = len(player.playlist)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


def _cli(args, pid=MAC, cmd="favorites"):
    from lyrion.control.cli import CLIContext, CLIHandler

    ctx = CLIContext(player_id=pid)
    ctx.command = cmd
    return asyncio.run(CLIHandler().dispatch(ctx, (cmd, args)))


def _jsonrpc(params, pid=MAC):
    """The client path: /jsonrpc.js -> slim.request -> the CLI handler."""
    from lyrion.web.api import JSONRPCAPI

    return asyncio.run(JSONRPCAPI()._slim_request(pid, params))


# ── 1. add: the row's URL is appended, root AND folder rows ───────────────


def test_root_row_add_appends_the_row_url(favs):
    """XMLBrowser.pm:667-702 + Commands.pm:1495-1503 (append)."""
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.1", "menu:1"])
    assert player.playlist == [STREAM_URL]
    assert player.playlist_total == 1


def test_folder_row_add_appends_the_row_url(favs):
    """The regression: a row two levels deep (``<sid>.0.1`` = F1 → S2).

    The former branch searched ``fm.list_items(None)`` (root only), never found
    the row and left the playlist untouched.
    """
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.0.1", "menu:1"])
    assert player.playlist == ["https://absolut-relax.example/stream/mp3"]


def test_three_level_row_add_appends_the_row_url(favs):
    """Perl descends one crumb per level (:384) — no depth limit."""
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.0.0.0", "menu:1"])
    assert player.playlist == ["https://liveradio.swr.de/sw282p3/swr3/"]


def test_add_registers_the_rows_title_and_logo_under_the_url(favs):
    """XMLBrowser.pm:693-700 — setRemoteMetadata for add/insert/play alike."""
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.0.1", "menu:1"])
    assert player.stream_titles["https://absolut-relax.example/stream/mp3"] \
        == "Absolut relax"
    assert player.stream_images["https://absolut-relax.example/stream/mp3"] \
        == "/imageproxy/https%3A%2F%2Fabsolut-relax.example/image.png"


# ── 2. insert: appended, then moved behind the running song ───────────────


def test_insert_lands_directly_behind_the_playing_song(favs):
    """Commands.pm:1535-1560 → Playlist.pm:265-284 ``_insert_done``."""
    player = _install_player(["a.mp3", "b.mp3"], position=0)
    _cli(["playlist", "insert", f"item_id:{SID}.1", "menu:1"])
    assert player.playlist == ["a.mp3", STREAM_URL, "b.mp3"]
    assert player.playlist_total == 3


def test_folder_row_insert_lands_directly_behind_the_playing_song(favs):
    player = _install_player(["a.mp3", "b.mp3"], position=1)
    _cli(["playlist", "insert", f"item_id:{SID}.0.0.0", "menu:1"])
    assert player.playlist == ["a.mp3", "b.mp3",
                               "https://liveradio.swr.de/sw282p3/swr3/"]


def test_insert_into_an_empty_playlist_starts_it(favs):
    """Perl: append + move with count == size is a no-op (Playlist.pm:280)."""
    player = _install_player()
    _cli(["playlist", "insert", f"item_id:{SID}.1", "menu:1"])
    assert player.playlist == [STREAM_URL]


# ── 3. play / load: unchanged, the DB id goes to the favourite manager ────


def test_root_row_play_plays_the_row(favs):
    _install_player()
    _cli(["playlist", "play", f"item_id:{SID}.1", "menu:1"])
    assert favs.played == [(MAC, S1)]


def test_folder_row_play_plays_the_row(favs):
    """Was already working before the fix — the guard against a regression."""
    _install_player()
    _cli(["playlist", "play", f"item_id:{SID}.0.1", "menu:1"])
    assert favs.played == [(MAC, S2)]


def test_load_behaves_like_play(favs):
    _install_player()
    _cli(["playlist", "load", f"item_id:{SID}.0.0.0", "menu:1"])
    assert favs.played == [(MAC, S3)]


# ── 4. unknown / folder rows: nothing is appended ────────────────────────


def test_unknown_item_id_changes_nothing(favs):
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.0.9", "menu:1"])
    assert player.playlist == []


def test_a_folder_row_itself_is_not_a_stream(favs):
    """Documented gap: Perl's folder branch (XMLBrowser.pm:710-779) collects the
    folder's children and runs ``playlist addtracks``; no client offers the
    play-control menu for a folder row and this port has no ``listref`` form of
    the ``playlist`` command, so a folder id stays untouched (never a wrong
    stream)."""
    player = _install_player()
    _cli(["playlist", "add", f"item_id:{SID}.0", "menu:1"])
    assert player.playlist == []


# ── 5. the client path reaches the same handler (no special route) ───────


def test_jsonrpc_route_adds_a_folder_row_too(favs):
    player = _install_player()
    _jsonrpc(["favorites", "playlist", "add", f"item_id:{SID}.0.1", "menu:1"])
    assert player.playlist == ["https://absolut-relax.example/stream/mp3"]


def test_jsonrpc_route_inserts_a_folder_row_behind_the_song(favs):
    player = _install_player(["a.mp3"], position=0)
    _jsonrpc(["favorites", "playlist", "insert", f"item_id:{SID}.0.0.0",
              "menu:1"])
    assert player.playlist == ["a.mp3",
                               "https://liveradio.swr.de/sw282p3/swr3/"]


def test_the_verb_is_case_insensitive(favs):
    player = _install_player()
    _cli(["playlist", "ADD", f"item_id:{SID}.1", "menu:1"])
    assert player.playlist == [STREAM_URL]
