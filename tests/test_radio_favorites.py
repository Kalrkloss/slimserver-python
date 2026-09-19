"""Radio search → play → "add to favourites" — the controller's flow.

What a controller does with a found radio station (Perl reference, read-only
checkout ``/tmp/lms91/slimserver-public-9.1``):

1. the Radio menu's search entry carries ``search: __TAGGEDINPUT__`` and an
   ``input`` block (``Slim/Plugin/OPMLBased.pm:221-246``); the client
   substitutes the typed text and sends
   ``search items 0 <n> menu:search search:<term>`` → the feed of
   ``Slim/Control/XMLBrowser.pm:274-1450``;
2. a station row carries ``presetParams`` (``:1925-1944``) and either
   ``goAction: "play"`` (``:1259-1267``) or the defeated ``playControl``
   (``:1268-1272``); the tap sends
   ``<feed> playlist play menu:<feed> item_id:<sid>.<i>`` (``OPMLBased.pm:122-125``,
   ``_cliQuery_done`` :291-293/:778-790);
3. the row's context menu offers "In Favoriten speichern" →
   ``['jivefavorites','add']`` (``XMLBrowser.pm:1875-1900``), which answers
   Perl's confirmation menu (two rows, ``Slim/Control/Jive.pm:2657-2687``);
4. the confirmation row sends ``favorites add url:<url> title:<title>`` →
   ``cliAdd`` (``Slim/Plugin/Favorites/Plugin.pm:76``, :821-922), which stores
   the station and answers ``count`` 1; the station is then in the favourites
   list — *and* under ``Radios → Eigene Voreinstellungen`` (the ``presets``
   feed, ``Slim/Plugin/InternetRadio/TuneIn.pm:85``).

Favourites data must never be emptied by any of this.  The favourites manager
and the radio-browser HTTP layer are faked, so nothing here touches a real DB
or the network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from lyrion.web import radiobrowser
from lyrion.web.api import JSONRPCAPI

PLAYER = "00:04:20:2b:88:c8"
STATION_URL = "http://stream.invalid/live.mp3"
FAVICON = "http://stream.invalid/logo.png"

ROW = {
    "stationuuid": "uuid-1",
    "name": "Test FM",
    "url": "http://playlist.invalid/list.m3u",
    "url_resolved": STATION_URL,
    "favicon": FAVICON,
    "country": "Germany",
    "countrycode": "DE",
    "language": "german",
    "tags": "jazz",
    "codec": "MP3",
    "bitrate": 128,
}


class _Favs:
    """In-memory favourites manager (same surface the API uses)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [
            {"id": 1, "title": "Chill", "url": None, "type": "folder",
             "parent_id": None, "position": 0},
            {"id": 2, "title": "Old Station",
             "url": "http://old.example/stream", "type": "stream",
             "parent_id": None, "position": 1},
        ]
        self._next = 3
        self.played: list[tuple[str, int]] = []

    async def list_items(self, parent_id=None):
        return [dict(r) for r in self.rows if r["parent_id"] == parent_id]

    async def list_tree(self):
        out = []
        for row in await self.list_items(None):
            item = dict(row)
            item["children"] = await self.list_items(row["id"])
            out.append(item)
        return out

    async def resolve_path(self, path):
        crumbs = [c for c in str(path).split(".") if c]
        if crumbs and len(crumbs[0]) == 8:
            crumbs = crumbs[1:]
        if not crumbs:
            return None
        parent = None
        for crumb in crumbs:
            if not crumb.isdigit():
                return None
            items = await self.list_items(parent)
            index = int(crumb)
            if index < 0 or index >= len(items):
                return None
            parent = items[index]["id"]
        return parent

    async def get(self, fav_id):
        for row in self.rows:
            if row["id"] == fav_id:
                return dict(row)
        return None

    async def find_url(self, url):
        """Perl ``findUrl`` — the first entry with that URL (None for folders)."""
        url = (url or "").strip()
        if not url:
            return None
        for row in self.rows:
            if row.get("url") == url:
                return row["id"]
        return None

    async def delete(self, fav_id):
        for index, row in enumerate(self.rows):
            if row["id"] == fav_id:
                del self.rows[index]
                return True
        return False

    async def add(self, title, url=None, parent_id=None, icon=None):
        title = (title or "").strip()
        if not title:
            return None
        new_id = self._next
        self._next += 1
        url_value = url.strip() if url else None
        from lyrion.music.favorites import favorite_icon

        self.rows.append({"id": new_id, "title": title,
                          "url": url_value,
                          "icon": (str(icon or "").strip()
                                   or favorite_icon(url_value)),
                          "type": "folder" if url_value is None else "stream",
                          "parent_id": parent_id, "position": len(self.rows)})
        return new_id

    async def play(self, player_id, fav_id):
        self.played.append((player_id, fav_id))
        return True


@pytest.fixture()
def client(monkeypatch):
    """A known, idle client — Perl's ``$request->client`` for Jive commands.

    ``['jivefavorites','_cmd']`` is registered with ``needClient = 1``
    (``Slim/Control/Jive.pm:102-103``), so without an attributed client Perl
    answers nothing and this port answers ``{}`` as well.
    """
    class _Stub:
        playerprefs: dict = {}
        mode = "stop"
        current_url = ""
        duration = 0.0
        playlist_total = 0

    class _Pm:
        def get_player(self, pid, *a, **k):
            return _Stub() if pid else None

    return _Pm()


@pytest.fixture()
def favs(monkeypatch):
    fake = _Favs()
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: fake)
    monkeypatch.setattr(radiobrowser, "_get_json",
                        _static_json([dict(ROW)]))
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()
    return fake


def _static_json(payload: Any):
    async def _fake(path: str, params: dict | None = None) -> Any:
        return payload
    return _fake


def call(coro):
    return asyncio.run(coro)


def rpc(*args: Any, pid: str = PLAYER) -> Any:
    """One full request through the JSON-RPC dispatcher."""
    async def _run():
        api = JSONRPCAPI()
        body = await api.handle_request(json.dumps({
            "id": 1, "method": "slim.request",
            "params": [pid, [str(a) for a in args]],
        }).encode())
        return json.loads(body)["result"]

    return call(_run())


# ── 1./2. search and play ─────────────────────────────────────────────────

def test_search_finds_a_station_and_playing_it_starts_the_stream(
        favs, monkeypatch):
    """The search result row plays the station behind it."""
    played: list[tuple] = []

    async def fake_play_url(self, player_id, url, title=""):
        played.append((player_id, url, title))
        return True

    monkeypatch.setattr("lyrion.player.manager.PlayerManager.play_url",
                        fake_play_url)

    found = rpc("search", "items", 0, 10, "menu:search", "search:jazz")
    row = found["item_loop"][0]
    assert row["type"] == "audio"
    assert row["presetParams"]["favorites_url"] == STATION_URL
    assert row["params"]["item_id"].endswith(".0")
    assert row["icon"].startswith("/imageproxy/") and \
        row["icon"].endswith("/image.png")
    assert found["base"]["actions"]["play"]["cmd"] == \
        ["search", "playlist", "play"]

    # the tap: base.actions.play.cmd + itemsParams ("params")
    item_id = row["params"]["item_id"]
    assert rpc("search", "playlist", "play", "menu:search",
               f"item_id:{item_id}") == {}
    assert played == [(PLAYER, STATION_URL, "Test FM")], played


def test_playing_a_station_from_a_tag_feed(favs, monkeypatch):
    played: list[tuple] = []

    async def fake_play_url(self, player_id, url, title=""):
        played.append((player_id, url, title))
        return True

    monkeypatch.setattr("lyrion.player.manager.PlayerManager.play_url",
                        fake_play_url)
    index = rpc("music", "items", 0, 5, "menu:music")
    sid = index["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    stations = rpc("music", "items", 0, 5, "menu:music", f"item_id:{sid}")
    item_id = stations["item_loop"][0]["params"]["item_id"]
    rpc("music", "playlist", "play", "menu:music", f"item_id:{item_id}")
    assert played == [(PLAYER, STATION_URL, "Test FM")], played


def test_playing_an_unknown_item_id_plays_nothing(favs, monkeypatch):
    played: list = []

    async def fake_play_url(self, player_id, url, title=""):
        played.append((player_id, url, title))
        return True

    monkeypatch.setattr("lyrion.player.manager.PlayerManager.play_url",
                        fake_play_url)
    rpc("music", "playlist", "play", "menu:music", "item_id:deadbeef.9")
    assert played == []


# ── 3./4. add to favourites ───────────────────────────────────────────────

def test_interim_context_menu_offers_save_to_favorites(favs):
    """``XMLBrowser.pm:1875-1900`` — the row that adds the station."""
    found = rpc("search", "items", 0, 10, "menu:search", "search:jazz")
    item_id = found["item_loop"][0]["params"]["item_id"]
    menu = rpc("search", "items", 0, 10, "menu:search", f"item_id:{item_id}",
               "isContextMenu:1", "xmlBrowseInterimCM:1")
    go = menu["item_loop"][3]["actions"]["go"]
    assert go["cmd"] == ["jivefavorites", "add"]
    assert go["params"]["url"] == STATION_URL
    assert go["params"]["title"] == "Test FM"
    assert go["params"]["type"] == "audio"
    assert go["params"]["icon"] == FAVICON
    assert "item_id" not in go["params"]      # not a favourite yet


def test_jivefavorites_add_confirms_with_the_favorites_add_command(favs, client):
    """``Jive.pm:2657-2687`` — the confirmation that writes the favourite."""
    from lyrion.web.api import JSONRPCAPI as API

    menu = call(API()._jive_menu_query(
        "jivefavorites", client, PLAYER,
        ["add", f"url:{STATION_URL}", "title:Test FM", "type:audio",
         f"icon:{FAVICON}"]))
    # without an attributed client Perl needs a client and answers nothing
    assert call(API()._jive_menu_query(
        "jivefavorites", None, PLAYER, ["add", "title:Test FM"])) == {}
    assert menu["count"] == 2 and menu["offset"] == 0
    assert menu["item_loop"][0]["actions"]["go"]["cmd"] == ["jiveblankcommand"]
    confirm = menu["item_loop"][1]
    go = confirm["actions"]["go"]
    assert go["cmd"] == ["favorites", "add"]
    assert go["params"]["url"] == STATION_URL
    assert go["params"]["title"] == "Test FM"
    assert go["params"]["type"] == "audio"
    assert go["params"]["icon"] == FAVICON
    assert confirm["nextWindow"] == "grandparent"


def test_favorites_add_stores_the_station_and_answers_count_1(favs):
    """``Favorites/Plugin.pm:821-922`` — ``count`` 1, appended at the end."""
    before = [dict(r) for r in favs.rows]
    result = rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM",
                 "type:audio", f"icon:{FAVICON}")
    assert result == {"count": 1}
    assert len(favs.rows) == len(before) + 1
    new = favs.rows[-1]
    assert new["title"] == "Test FM" and new["url"] == STATION_URL
    # the existing favourites are untouched
    assert [r["title"] for r in favs.rows[:len(before)]] == \
        [r["title"] for r in before]


def test_favorites_add_without_url_or_title_is_rejected(favs):
    """Perl: ``setStatusBadParams`` (:847, :872-877) — nothing is stored."""
    before = len(favs.rows)
    assert rpc("favorites", "add", "title:Only Title") == {}
    assert rpc("favorites", "add", f"url:{STATION_URL}") == {}
    assert len(favs.rows) == before


def test_favorites_addlevel_creates_a_folder(favs):
    """``favorites addlevel`` (:861-870) — a folder, not a stream."""
    assert rpc("favorites", "addlevel", "title:Neuer Ordner") == {"count": 1}
    folder = favs.rows[-1]
    assert folder["title"] == "Neuer Ordner" and folder["url"] is None


def test_added_station_shows_up_in_the_favorites_list(favs):
    """The favourite list keeps its data and shows the new station."""
    rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM")
    plain = rpc("favorites", "items", 0, 50)
    names = [i["name"] for i in plain["loop_loop"]]
    assert names == ["Chill", "Old Station", "Test FM"], names

    menu = rpc("favorites", "items", 0, 50, "menu:favorites")
    titles = [i.get("text") for i in menu["item_loop"]]
    assert titles == ["Chill", "Old Station", "Test FM"], titles
    station = menu["item_loop"][-1]
    assert station["type"] == "audio"
    assert station["presetParams"]["favorites_url"] == STATION_URL


def test_added_station_shows_up_under_radio_presets(favs):
    """``Radios → Eigene Voreinstellungen`` (Perl's presets feed)."""
    rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM")
    presets = rpc("presets", "items", 0, 50, "menu:presets")
    titles = [i.get("text") for i in presets["item_loop"]]
    assert titles == ["Chill", "Old Station", "Test FM"], titles
    station = presets["item_loop"][-1]
    assert station["type"] == "audio"
    assert station["presetParams"]["favorites_url"] == STATION_URL
    # every command of that feed keeps the client inside ``presets``
    assert presets["base"]["actions"]["play"]["cmd"] == \
        ["presets", "playlist", "play"]
    assert presets["base"]["actions"]["go"]["cmd"] == ["presets", "items"]


def test_preset_rows_play_through_the_favorites_manager(favs):
    """``presets playlist play item_id:<sid>.<i>`` → the stored station."""
    rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM")
    presets = rpc("presets", "items", 0, 50, "menu:presets")
    item_id = presets["item_loop"][-1]["params"]["item_id"]
    rpc("presets", "playlist", "play", "menu:presets", f"item_id:{item_id}")
    assert favs.played == [(PLAYER, 3)], favs.played


def test_presets_survive_a_dead_radio_browser(favs, monkeypatch):
    """A dead data source must not break the favourites path.

    ``/json/*`` answering nothing (``None``) is the degradation the data
    source promises; the ``presets`` node renders from our own favourites and
    must stay complete.
    """
    rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM")

    async def dead(path: str, params: dict | None = None):
        return None

    monkeypatch.setattr(radiobrowser, "_get_json", dead)
    radiobrowser._cache.clear()
    presets = rpc("presets", "items", 0, 50, "menu:presets")
    assert [i.get("text") for i in presets["item_loop"]] == \
        ["Chill", "Old Station", "Test FM"]
    # a station level degrades to Perl's empty feed instead of an error
    station_feed = rpc("search", "items", 0, 5, "menu:search", "search:x")
    assert station_feed["item_loop"][0]["style"] == "itemNoAction"
    # the static tag index still lists its drill-downs (no API needed)
    music = rpc("music", "items", 0, 5, "menu:music")
    assert {i["type"] for i in music["item_loop"]} == {"link"}


# ── the client's own request line, end to end ─────────────────────────────

def test_client_built_add_chain_ends_in_the_favorites_list(favs, client):
    """The whole chain with the tokens the *client* builds.

    SqueezePlay turns an action's ``params`` into ``key:value`` tokens and
    always appends ``useContextMenu:1``
    (``share/jive/applets/SlimBrowser/SlimBrowserApplet.lua:735-762``); the
    server side is Perl's ``_playlistControlContextMenu`` favourites row
    (``XMLBrowser.pm:1875-1900``) → ``jiveFavoritesCommand``
    (``Slim/Control/Jive.pm:2657-2687``) → ``cliAdd``
    (``Slim/Plugin/Favorites/Plugin.pm:821-922``).  Step 1 is skipped for the
    row's ``item_id`` (the sid lives in the feed), steps 2-4 are replayed
    verbatim.
    """
    from lyrion.web.api import JSONRPCAPI as API

    before = [r["title"] for r in favs.rows]

    # 2. the row's action: ['jivefavorites','add'] + params → tokens
    confirm = call(API()._jive_menu_query(
        "jivefavorites", client, PLAYER,
        ["add", f"url:{STATION_URL}", "title:Test FM", "type:audio",
         f"icon:{FAVICON}", "isContextMenu:1", "useContextMenu:1"]))
    assert confirm["count"] == 2

    # 3. the confirmation row's own tokens (params + trailing useContextMenu)
    go = confirm["item_loop"][1]["actions"]["go"]
    request = [str(c) for c in go["cmd"]]
    for key, value in go["params"].items():
        if value is not None:              # the client skips json.null
            request.append(f"{key}:{value}")
    request.append("useContextMenu:1")
    assert request[0] == "favorites" and request[1] == "add"
    assert rpc(*request) == {"count": 1}

    # 4. the favourites list grew by exactly this station — nothing was emptied
    plain = rpc("favorites", "items", 0, 50)
    names = [i["name"] for i in plain["loop_loop"]]
    assert names[:len(before)] == before, names
    assert names[-1] == "Test FM" and names.count("Test FM") == 1
    menu = rpc("favorites", "items", 0, 50, "menu:favorites")
    assert [i.get("text") for i in menu["item_loop"]][-1] == "Test FM"


# ── favourites delete (Perl cliDelete) ────────────────────────────────────

def test_favorites_delete_removes_by_url_and_keeps_the_rest(favs):
    """``Slim/Plugin/Favorites/Plugin.pm:925-970`` — ``deleteUrl``.

    The radio feeds' play-control row and the ``jivefavorites`` confirmation
    both address the entry by URL (``XMLBrowser.pm:1884-1899``,
    ``Jive.pm:2670-2690``); Perl's answer carries no result keys at all
    (``setStatusDone``).
    """
    rpc("favorites", "add", f"url:{STATION_URL}", "title:Test FM")
    before = [r["title"] for r in favs.rows]

    assert rpc("favorites", "delete", f"url:{STATION_URL}",
               "title:Test FM") == {}
    assert [r["title"] for r in favs.rows] == before[:-1]
    assert favs.rows[0]["title"] == "Chill"       # nothing else was touched


def test_favorites_delete_by_index_deletes_exactly_that_entry(favs):
    """The ``item_id`` form: a *valid* index wins over the URL (``:946-952``)."""
    assert rpc("favorites", "delete", "url:http://old.example/stream",
               "item_id:2") == {}
    assert [r["title"] for r in favs.rows] == ["Chill"]


def test_favorites_delete_without_url_or_valid_index_is_a_no_op(favs):
    """Perl: no index and no URL → ``setStatusBadParams``; unknown URL → warn."""
    before = [dict(r) for r in favs.rows]
    assert rpc("favorites", "delete", "title:Test FM") == {}
    assert rpc("favorites", "delete", "url:http://nowhere.invalid/x") == {}
    assert rpc("favorites", "delete", "item_id:999") == {}
    assert [r["title"] for r in favs.rows] == [r["title"] for r in before]


# ── Logo der Zeile: Perl setRemoteMetadata(cover => …) ────────────────────
#
# Perl registriert beim Start einer Zeile Name UND Logo unter der URL
# (`setRemoteMetadata($url, {title => …, cover => $subFeed->{cover} ||
# {image} || {icon}}, …)`, `Slim/Control/XMLBrowser.pm:693-700` →
# `Slim/Music/Info.pm:489-492` cacht es als `remote_image_$url`); `_songData`
# reicht es als `artwork_url`/`icon` weiter (Queries.pm:5618-5633,
# `Protocols/HTTP.pm:1133` `cover => $cover || $icon`).  Live Perl 9.1.1
# (read-only 2026-09-19) liefert genau die proxied Form:
# `/imageproxy/https%3A%2F%2F…%2Flogog.png%3Ft%3D155074/image.png`.

def _feed_player(monkeypatch, playlist, **kw):
    """Ein echter PlayerState im Singleton-PlayerManager (für `stream_images`)."""
    from lyrion.player.manager import PlayerManager
    from lyrion.player.state import PlayerState

    player = PlayerState(mac=PLAYER, name="Taverne", ip="192.168.1.130",
                         port=58044)
    player.power = True
    player.playlist = list(playlist)
    player.playlist_position = 0
    player.playlist_total = len(player.playlist)
    for key, value in kw.items():
        setattr(player, key, value)
    pm = PlayerManager()
    pm.players = {PLAYER: player}
    return player


def test_playing_a_feed_station_registers_the_row_logo(favs, monkeypatch):
    """Der Start registriert das Logo der Zeile unter der URL (Perl: cover)."""
    async def fake_play_url(self, player_id, url, title=""):
        return True

    monkeypatch.setattr("lyrion.player.manager.PlayerManager.play_url",
                        fake_play_url)
    player = _feed_player(monkeypatch, [STATION_URL])

    index = rpc("music", "items", 0, 5, "menu:music")
    sid = index["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    stations = rpc("music", "items", 0, 5, "menu:music", f"item_id:{sid}")
    item_id = stations["item_loop"][0]["params"]["item_id"]
    # Die Browse-Zeile trägt das Logo bereits proxied (XMLBrowser.pm:1171).
    row_icon = stations["item_loop"][0]["icon"]
    assert row_icon == "/imageproxy/http%3A%2F%2Fstream.invalid%2Flogo.png/image.png"

    rpc("music", "playlist", "play", "menu:music", f"item_id:{item_id}")

    assert player.stream_images[STATION_URL] == row_icon
    # Der Status reicht es als `artwork_url`/`icon` weiter — MIT führendem
    # Schrägstrich (so liefert Perl es live; `/html/EN/imageproxy/…` wäre 404).
    item = call(JSONRPCAPI()._json_player_status(
        _FeedPm(player), PLAYER, ["-", "1", "menu:menu"]))["item_loop"][0]
    assert item["artwork_url"] == row_icon
    assert item["icon"] == row_icon


class _FeedPm:
    def __init__(self, player):
        self._player = player

    def get_player(self, mac, *a, **k):
        return self._player if mac == self._player.mac else None

    def get_all_players(self):
        return [self._player]


def test_feed_row_without_a_logo_keeps_the_default_image(favs, monkeypatch):
    """Ohne Logo bleibt Perl's Default (`html/images/radio.png`)."""
    from lyrion.web.api import REMOTE_ART_FALLBACK

    async def fake_play_url(self, player_id, url, title=""):
        return True

    monkeypatch.setattr("lyrion.player.manager.PlayerManager.play_url",
                        fake_play_url)
    radiobrowser._cache.clear()
    row = dict(ROW)
    row["favicon"] = ""
    monkeypatch.setattr(radiobrowser, "_get_json", _static_json([row]))
    player = _feed_player(monkeypatch, [STATION_URL])

    index = rpc("music", "items", 0, 5, "menu:music")
    sid = index["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    stations = rpc("music", "items", 0, 5, "menu:music", f"item_id:{sid}")
    item_id = stations["item_loop"][0]["params"]["item_id"]
    assert "icon" not in stations["item_loop"][0]
    rpc("music", "playlist", "play", "menu:music", f"item_id:{item_id}")

    assert player.stream_images == {}
    item = call(JSONRPCAPI()._json_player_status(
        _FeedPm(player), PLAYER, ["-", "1"]))["playlist_loop"][0]
    assert item["artwork_url"] == REMOTE_ART_FALLBACK == "html/images/radio.png"
