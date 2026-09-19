"""Radio directory: our answers' structure vs. the live Perl LMS.

Symptom: "radios suchen und hinzufügen geht nach wie vor nicht mit den
Controllern".  Cause: our ``radios`` node answered the *favourites* streams
instead of Perl's sub-node menu, so no controller ever reached
``local``/``music``/… or the search input.

Perl reference (read-only checkout ``/tmp/lms91/slimserver-public-9.1``, plus
word-for-word answers of the live server in
``tests/fixtures/perl_radio_structure.json``, fetched by
``tools/_fetch_radio_fixture.py``):

* ``Slim/Plugin/InternetRadio/Plugin.pm:42-59``/``:78-205`` — one dynamic
  OPMLBased plugin per TuneIn directory item (``tag => lc $subclass``,
  ``menu => 'radios'``, ``weight``, ``type``); ``Slim/Plugin/OPMLBased.pm:117-132``
  the CLI dispatch per tag plus the ``radios`` menu query, :181-280
  ``cliRadiosQuery`` (the answer shape of both forms), :200-247 the jive item
  with ``window {titleStyle: album}`` and the ``input`` block of the search
  entry; ``Slim/Plugin/InternetRadio/TuneIn.pm:33-81`` the tag → {icon, weight}
  table; ``Slim/Control/Queries.pm:5384``/``:5443`` the ``radioss_loop`` name
  and ``count`` last.
* ``Slim/Control/XMLBrowser.pm:274-1450`` — the sub-feed renderer:
  ``:960-990`` the ``base.actions`` table, :1053-1270 the item shapes,
  :1119-1126 + :1434-1441 the ``windowStyle`` rule, :837-846 the ``Leer``
  placeholder, :1811-1900 the play-control menu of a tapped row.

The stations behind the rows come from radio-browser.info instead of TuneIn
(user decision, approved deviation) — these tests therefore compare
*structure* (field sets, types, ``cmd`` arrays, weights, order), never the
data.  The HTTP layer is mocked so the suite stays offline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lyrion.web import radiobrowser
from lyrion.web.api import JSONRPCAPI

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "perl_radio_structure.json"
PLAYER = "00:04:20:2b:88:c8"


def perl(case: str) -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))[case]
    assert data["result"], f"{case} carries no result"
    return data


def loop_of(case: str) -> list[dict]:
    result = perl(case)["result"]
    return result.get("item_loop") or result.get("radioss_loop") or []


# ── canned radio-browser payloads (no network) ────────────────────────────

STATIONS = [
    {
        "stationuuid": "aaa-1", "name": "Test FM Eins",
        "url": "http://example.invalid/one.m3u",
        "url_resolved": "http://example.invalid/one",
        "favicon": "http://example.invalid/one.png", "country": "Germany",
        "countrycode": "DE", "language": "german", "tags": "pop",
        "codec": "MP3", "bitrate": 128,
    },
    {
        "stationuuid": "aaa-2", "name": "Test FM Zwei",
        "url": "https://example.invalid/two",
        "favicon": "", "country": "Germany", "countrycode": "DE",
        "language": "german", "tags": "jazz", "codec": "AAC", "bitrate": 0,
    },
]

COUNTRIES = [
    {"name": "Germany", "iso_3166_1": "DE", "stationcount": 100},
    {"name": "The United States Of America", "iso_3166_1": "US",
     "stationcount": 900},
]

LANGUAGES = [
    {"name": "german", "stationcount": 50},
    {"name": "#english", "stationcount": 3},
]


async def _fake_get_json(path: str, params: dict | None = None) -> Any:
    if path.startswith("/json/stations/"):
        return [dict(s) for s in STATIONS]
    if path == "/json/countries":
        return [dict(c) for c in COUNTRIES]
    if path == "/json/languages":
        return [dict(l) for l in LANGUAGES]
    return []


@pytest.fixture(autouse=True)
def mock_http(monkeypatch):
    """Every test in this module talks to a faked radio-browser."""
    monkeypatch.setattr(radiobrowser, "_get_json", _fake_get_json)
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()


def run(coro):
    return asyncio.run(coro)


def radios(args: list[str]) -> dict:
    return run(JSONRPCAPI()._json_radios("radios", [str(a) for a in args]))


def feed(name: str, rest: list[Any]) -> dict:
    return run(JSONRPCAPI()._json_radio_feed(
        name, [str(a) for a in rest], PLAYER))


# ── the root node ─────────────────────────────────────────────────────────

#: Perl's directory order and weights (``TuneIn.pm:33-81`` MENUS + the
#: unshifted presets entry :156-164, live ``radios 0 20`` 2026-09-14).
ROOT_TAGS = ["presets", "local", "music", "sports", "news", "talk",
             "location", "language", "podcast", "search"]
ROOT_WEIGHTS = [5, 20, 30, 40, 45, 50, 55, 56, 70, 110]
ROOT_NAMES = ["Eigene Voreinstellungen", "Lokales Radio", "Musik", "Sport",
              "Nachrichten", "Talksendungen", "Nach Standort",
              "Nach Sprache", "Podcasts", "TuneIn durchsuchen"]
ROOT_ICONS = [f"/plugins/TuneIn/html/images/{n}.png" for n in (
    "radiopresets", "radiolocal", "radiomusic", "radiosports", "radionews",
    "radiotalk", "radioworld", "radioworld", "podcasts", "radiosearch")]


def test_root_plain_form_matches_perl():
    """``radios 0 3`` — ``count`` + ``radioss_loop``, Perl's item fields."""
    got = radios(["0", "3"])
    want = perl("radios_plain")["result"]
    assert list(got) == list(want), (list(got), list(want))
    assert got["count"] == want["count"] == 10
    assert len(got["radioss_loop"]) == len(want["radioss_loop"]) == 3
    for ours, theirs in zip(got["radioss_loop"], want["radioss_loop"]):
        assert set(ours) == set(theirs), (ours, theirs)
        for key in theirs:
            assert type(ours[key]) is type(theirs[key]), (key, ours, theirs)


def test_root_node_table_is_perls_directory():
    """Tags, labels, weights, icons and order — Perl's MENUS table."""
    items = radios(["0", "20"])["radioss_loop"]
    assert [i["cmd"] for i in items] == ROOT_TAGS
    assert [i["weight"] for i in items] == ROOT_WEIGHTS
    assert [i["name"] for i in items] == ROOT_NAMES
    assert [i["icon"] for i in items] == ROOT_ICONS
    # OPMLBased.pm:249-256 — ``link`` renders as xmlbrowser, ``search`` as
    # xmlbrowser_search (live Perl's last row).
    assert [i["type"] for i in items] == ["xmlbrowser"] * 9 + ["xmlbrowser_search"]


def test_root_menu_form_matches_perl():
    """``radios 0 3 menu:radio`` — the jive form (no ``radioss_loop``)."""
    got = radios(["0", "3", "menu:radio"])
    want = perl("radios_menu")["result"]
    assert list(got) == list(want) == ["count", "item_loop", "offset"]
    assert got["count"] == 10 and got["offset"] == 0
    for ours, theirs in zip(got["item_loop"], want["item_loop"]):
        assert set(ours) == set(theirs), (ours, theirs)
        assert ours["text"] == theirs["text"]
        assert ours["weight"] == theirs["weight"]
        assert ours["icon-id"] == theirs["icon-id"]
        assert ours["window"] == theirs["window"] == {"titleStyle": "album"}
        assert ours["actions"] == theirs["actions"], ours["actions"]


def test_root_search_item_carries_perls_input_block():
    """OPMLBased.pm:221-246 — ``search: __TAGGEDINPUT__`` + ``input``."""
    theirs = perl("radios_menu_search_item")["result"]["item_loop"][0]
    ours = radios(["9", "1", "menu:radio"])["item_loop"][0]
    assert set(ours) == set(theirs), (ours, theirs)
    assert ours["actions"]["go"] == theirs["actions"]["go"]
    assert ours["actions"]["go"]["cmd"] == ["search", "items"]
    # ``len`` 3 comes from cliRadiosQuery (:236), the generic XMLBrowser
    # search item uses 1 (:1211-1224).
    assert isinstance(ours["input"]["len"], int)
    assert set(ours["input"]) == set(theirs["input"])
    assert set(ours["input"]["help"]) == set(theirs["input"]["help"])
    assert ours["input"]["softbutton1"] and ours["input"]["softbutton2"]


# ── the Radio node's children in the home menu ────────────────────────────

def test_home_menu_carries_the_radio_nodes_children():
    """Perl's ``@pluginMenus`` rows — the ones that make SqueezePlay show Radio.

    SqueezePlay creates the Radio entry itself
    (``share/jive/jive/JiveMain.lua:464``) and *ignores* the server's
    ``id: 'radios'`` row (``applets/SlimMenus/SlimMenusApplet.lua:569-570``);
    ``ui/HomeMenu.lua:567-580`` only puts that local node into the home menu
    once an item whose ``node`` is ``'radios'`` exists.  Perl sends one per
    generated plugin (``Slim/Plugin/InternetRadio/Plugin.pm:150-153`` with
    ``menu => 'radios'``, registered at ``Slim/Plugin/OPMLBased.pm:48-52``,
    item shape ``:66-91``, ``input`` block ``:93-106``, collected in
    ``Slim/Control/Jive.pm:478-529`` and emitted by ``mainMenu``
    ``Slim/Control/Jive.pm:286``).  Ten of Perl's live 54 home items are these
    rows; without them the client shows no Radio entry at all.
    """
    items = JSONRPCAPI()._home_menu(None)
    by_id = {i["id"]: i for i in items}

    # the node itself (internetRadioMenu, Jive.pm:1360-1393)
    assert by_id["radios"]["node"] == "home"
    assert by_id["radios"]["weight"] == 20
    assert by_id["radios"]["window"] == {"menuStyle": "album"}

    children = [i for i in items if i.get("node") == "radios"]
    assert [c["id"] for c in children] == \
        [f"opml{n['tag']}" for n in radiobrowser.ROOT_NODES]
    for node, child in zip(radiobrowser.ROOT_NODES, children):
        assert child["text"] == node["text"]
        assert child["weight"] == node["weight"]
        assert child["uuid"] is None              # OPMLBased.pm:70 (no manifest)
        assert child["displayWhenOff"] == 0       # :73
        assert child["window"] == {"icon-id": node["icon"],
                                   "titleStyle": "album"}
        go = child["actions"]["go"]
        assert go["player"] == 0
        assert go["cmd"] == [node["tag"], "items"]      # :78-85
        assert go["params"]["menu"] == node["tag"]

    # the search row additionally carries Perl's input block (:93-106)
    search = by_id["opmlsearch"]
    assert search["actions"]["go"]["params"]["search"] == "__TAGGEDINPUT__"
    assert set(search["input"]) == {"len", "processingPopup", "softbutton1",
                                    "softbutton2", "title", "help"}
    assert search["input"]["len"] == 1
    assert search["input"]["title"] == "TuneIn durchsuchen"


def test_home_menu_children_reach_the_menustatus_notification():
    """The same rows are what a controller receives as a menustatus push.

    SqueezePlay subscribes to ``/<cid>/slim/menustatus/<mac>`` and merges the
    pushed items by their ``node`` (``SlimMenusApplet.lua:414-470`` →
    ``ui/HomeMenu.lua:addItem``); the seed payload of that subscription is our
    ``menustatus`` answer (``Jive.pm:150-155`` dispatch/subscription,
    ``:1972``/``:2399-2409`` the notification).
    """
    async def _run():
        api = JSONRPCAPI()
        body = json.dumps({"id": 1, "method": "slim.request",
                           "params": [PLAYER, ["menustatus"]]}).encode()
        return json.loads(await api.handle_request(body))["result"]

    data = asyncio.run(_run())
    assert isinstance(data, list) and len(data) == 4
    assert data[2] == "add" and data[3] == PLAYER
    nodes = [i["node"] for i in data[1] if i.get("node") == "radios"]
    assert len(nodes) == len(radiobrowser.ROOT_NODES)


# ── the sub-feeds ─────────────────────────────────────────────────────────

def test_local_index_matches_perl():
    """``local items 0 6 menu:local`` — Perl's two drill-down rows."""
    got = feed("local", ["0", "6", "menu:local"])
    want = perl("local_index")["result"]
    assert set(got) == set(want), (set(got), set(want))
    assert set(got["base"]["actions"]) == set(want["base"]["actions"])
    # Row 0 is Perl's literal "Sender"; row 1 is the ``Alle <Land>`` entry —
    # the *name* comes from the directory (TuneIn "Deutschland", radio-browser
    # "Germany"/"The United States Of America"), the form is identical.
    assert got["item_loop"][0]["text"] == want["item_loop"][0]["text"] == "Sender"
    assert got["item_loop"][1]["text"].startswith("Alle ")
    assert want["item_loop"][1]["text"].startswith("Alle ")
    for ours, theirs in zip(got["item_loop"], want["item_loop"]):
        # TuneIn's OPML marks *some* category items ``type="link"`` and leaves
        # others untyped (live: "Sender" untyped, "Alle Deutschland" typed);
        # radio-browser has no such attribute, so every drill-down row is typed
        # ``link`` — the superset of both live forms, hence the extra key.
        assert set(ours) ^ set(theirs) <= {"type"}, (ours, theirs)
        assert ours["type"] == "link"
        assert ours["addAction"] == theirs["addAction"] == "go"
        assert ours["actions"]["go"]["cmd"] == theirs["actions"]["go"]["cmd"]
        assert set(ours["actions"]["go"]["params"]) == \
            set(theirs["actions"]["go"]["params"])
    assert got["window"] == want["window"] == {"windowStyle": "text_list"}


class _IdlePlayer:
    """An idle, named client — Perl's ``$client`` for an idle controller."""

    playerprefs: dict = {}
    mode = "stop"
    current_url = ""
    duration = 0.0
    playlist_total = 0


@pytest.fixture()
def idle_client(monkeypatch):
    """Make the request's player a *known*, idle client (Play branch)."""
    from lyrion.player import manager as player_manager

    monkeypatch.setattr(player_manager.PlayerManager, "get_player",
                        lambda self, pid, *a, **k: _IdlePlayer() if pid else None)


def test_station_list_matches_perl(idle_client):
    """The level below a drill-down: Perl's ``audio`` rows + ``base``.

    The fixture's player is a connected, idle client, i.e. Perl's
    ``_defeatDestructiveTouchToPlay`` answers the plain ``play`` row
    (``XMLBrowser.pm:1259-1267``) — our own resolution matches that for a
    known idle client and falls back to the defeated row without one
    (:1976 ``|| !$client``, see :func:`test_station_row_defeated_branch`).
    """
    sid = feed("local", ["0", "6", "menu:local"])["item_loop"][0][
        "actions"]["go"]["params"]["item_id"]
    got = feed("local", ["0", "4", "menu:local", f"item_id:{sid}"])
    want = perl("local_stations")["result"]
    assert set(got) == set(want), (set(got), set(want))
    assert got["title"] == want["title"] == "Sender"
    assert got["offset"] == want["offset"] == 0
    assert set(got["base"]["actions"]) == set(want["base"]["actions"])
    ours, theirs = got["item_loop"][0], want["item_loop"][0]
    assert set(ours) == set(theirs), (ours, theirs)
    assert ours["type"] == theirs["type"] == "audio"
    assert ours["goAction"] == theirs["goAction"] == "play"
    assert ours["style"] == theirs["style"] == "itemplay"
    assert ours["icon"].startswith("/imageproxy/") and \
        ours["icon"].endswith("/image.png")
    assert set(ours["params"]) == set(theirs["params"])
    assert ours["params"]["item_id"] == ours["params"]["touchToPlay"]
    assert ours["params"]["touchToPlaySingle"] == 1
    # ``presetParams`` is the "add to favourites" source (XMLBrowser.pm:1925).
    assert set(ours["presetParams"]) == set(theirs["presetParams"])
    assert ours["presetParams"]["favorites_url"] == STATIONS[0]["url_resolved"]
    assert ours["presetParams"]["favorites_type"] == "audio"


def test_station_row_defeated_branch_without_client():
    """No attributed client → Perl's defeated row (``:1268-1272``)."""
    sid = feed("local", ["0", "6", "menu:local"])["item_loop"][0][
        "actions"]["go"]["params"]["item_id"]
    got = run(JSONRPCAPI()._json_radio_feed(
        "local", ["0", "4", "menu:local", f"item_id:{sid}"], None))
    row = got["item_loop"][0]
    assert row["goAction"] == "playControl"
    assert row["playControlParams"] == {"xmlbrowserPlayControl": "0"}
    assert "style" not in row and "touchToPlay" not in row["params"]
    # XMLBrowser.pm:1430 — every row defeated ⇒ base.go = base.playControl
    assert got["base"]["actions"]["go"] == got["base"]["actions"]["playControl"]


def test_base_actions_match_perls_commands():
    """XMLBrowser.pm:960-990 — every ``cmd`` is built from the feed tag."""
    for name in ("local", "music", "location", "language", "search"):
        got = feed(name, ["0", "2", f"menu:{name}"])
        actions = got["base"]["actions"]
        assert actions["go"]["cmd"] == [name, "items"]
        assert actions["play"]["cmd"] == [name, "playlist", "play"]
        assert actions["play"]["nextWindow"] == "nowPlaying"
        assert actions["add"]["cmd"] == [name, "playlist", "add"]
        assert actions["add-hold"]["cmd"] == [name, "playlist", "insert"]
        assert actions["more"]["cmd"] == [name, "items"]
        assert actions["more"]["window"] == {"isContextMenu": 1}
        assert actions["playControl"]["cmd"] == [name, "items"]
        assert actions["playControl"]["itemsParams"] == "playControlParams"
        assert "player" not in actions["go"]
        # ``playControl`` echoes the request's tagged params (:978).
        assert actions["playControl"]["params"]["menu"] == name


def test_index_levels_match_perl_item_shape():
    """music/location/language: Perl's ``link`` rows + ``windowStyle``.

    The row *count* differs by data (Perl's TuneIn directory vs.
    radio-browser's tag/country/language lists), so the Perl rows are compared
    against ours row for row up to the shorter side — field set, item type,
    ``cmd``/``params`` shape and the ``windowStyle`` rule are the parity part.
    """
    for case, name, n in (("music_index", "music", 4),
                          ("location_index", "location", 4),
                          ("language_index", "language", 4)):
        want = perl(case)["result"]
        got = feed(name, ["0", str(n), f"menu:{name}"])
        assert set(got) == set(want), (name, set(got), set(want))
        assert got["title"] == want["title"]
        assert got["window"] == want["window"] == {"windowStyle": "text_list"}
        assert got["item_loop"], f"{name}: no rows"
        assert len(got["item_loop"]) <= n
        for ours, theirs in zip(got["item_loop"], want["item_loop"]):
            extra = set(ours) - set(theirs)
            assert extra <= {"type"}, (name, ours, theirs)
            assert ours.get("type") == theirs.get("type", "link") == "link"
            assert ours["addAction"] == theirs["addAction"] == "go"
            assert ours["actions"]["go"]["cmd"] == \
                theirs["actions"]["go"]["cmd"]


def test_titles_match_perls_feed_titles():
    """Perl's feed titles (TuneIn OPML ``<title>``) — measured live."""
    for name, title in (("local", "Lokale Sender"), ("music", "Musik"),
                        ("sports", "Sport"), ("news", "Nachrichten & Talk"),
                        ("talk", "Talk"), ("location", "Orte"),
                        ("language", "Sprachen"), ("podcast", "Podcasts")):
        got = feed(name, ["0", "4", f"menu:{name}"])
        assert got["title"] == title, (name, got["title"])


def test_tag_index_is_served_by_bytagexact(monkeypatch):
    """``Musik`` → tag index → ``bytagexact(<tag>)`` stations."""
    calls: list[tuple] = []

    async def fake(tag, limit=20, offset=0):
        calls.append((tag, limit, offset))
        return [radiobrowser.Station.from_json(STATIONS[0])]

    monkeypatch.setattr(radiobrowser, "stations_by_tag", fake)
    index = feed("music", ["0", "3", "menu:music"])
    assert [i["text"] for i in index["item_loop"]][:3] == ["Pop", "Rock", "Jazz"]
    sid = index["item_loop"][1]["actions"]["go"]["params"]["item_id"]
    level = feed("music", ["0", "3", "menu:music", f"item_id:{sid}"])
    assert calls and calls[0][0] == "rock", calls
    assert level["title"] == "Rock"
    assert level["item_loop"][0]["type"] == "audio"


def test_country_and_language_leaves_use_their_endpoints(monkeypatch):
    """``Nach Standort`` → ``/json/countries`` → ``bycountrycodeexact``."""
    seen: list[tuple] = []

    async def fake_country(code, limit=20, offset=0):
        seen.append(("country", code))
        return [radiobrowser.Station.from_json(STATIONS[0])]

    async def fake_language(lang, limit=20, offset=0):
        seen.append(("language", lang))
        return [radiobrowser.Station.from_json(STATIONS[0])]

    monkeypatch.setattr(radiobrowser, "stations_by_country", fake_country)
    monkeypatch.setattr(radiobrowser, "stations_by_language", fake_language)

    loc = feed("location", ["0", "2", "menu:location"])
    assert loc["item_loop"][0]["text"] == "Germany"
    sid = loc["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    assert feed("location", ["0", "2", "menu:location", f"item_id:{sid}"])
    assert seen == [("country", "DE")], seen

    lang = feed("language", ["0", "2", "menu:language"])
    # the ``#english`` artefact of /json/languages is dropped
    assert [i["text"] for i in lang["item_loop"]] == ["german"]
    sid = lang["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    assert feed("language", ["0", "2", "menu:language", f"item_id:{sid}"])
    assert seen[-1] == ("language", "german"), seen


def test_local_uses_the_locale_country(monkeypatch):
    """``local`` → ``bycountrycodeexact(<Land des Servers>)``."""
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)  # nie gesetzt
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    seen: list[str] = []

    async def fake(code, limit=20, offset=0):
        seen.append(code)
        return [radiobrowser.Station.from_json(STATIONS[0])]

    monkeypatch.setattr(radiobrowser, "stations_by_country", fake)
    assert radiobrowser.local_country() == "DE"
    rows = radiobrowser.RadioNode("index", "local")
    sid = radiobrowser.node_sid(rows)
    feed("local", ["0", "4", "menu:local", f"item_id:{sid}.0"])
    feed("local", ["0", "4", "menu:local", f"item_id:{sid}.1"])
    assert seen == ["DE", "DE"], seen


def test_podcast_and_sounds_answer_perls_empty_feed():
    """radio-browser has no podcast/sounds data → Perl's ``Leer`` row.

    Perl's podcast feed carries TuneIn category rows (type-less link rows,
    fixture ``podcast_index``) and the Sounds app ships files with its plugin;
    this port has neither, so the honest answer is Perl's *empty feed* form
    (``XMLBrowser.pm:837-846``) — exactly what the live server answers for an
    empty feed (``presets_empty``/``search_plain`` in the fixture).
    """
    from lyrion.web import menus

    placeholder = perl("search_plain")["result"]["item_loop"][0]
    for name in ("podcast", "sounds"):
        got = feed(name, ["0", "3", f"menu:{name}"])
        assert set(got) == {"offset", "title", "base", "item_loop", "count",
                            "window"}
        assert got["count"] == 1
        # The row *text* is the translated EMPTY string (Perl's DE answer says
        # "Leer", our EN server locale says "Empty"); style/action/type are the
        # parity part.
        assert got["item_loop"][0]["text"] == menus.menu_title("EMPTY")
        assert set(got["item_loop"][0]) == set(placeholder)
        assert got["item_loop"][0]["style"] == placeholder["style"]
        assert got["item_loop"][0]["action"] == placeholder["action"]
        assert got["item_loop"][0]["type"] == placeholder["type"]
        assert got["window"] == {"windowStyle": "text_list"}


def test_empty_feeds_match_perls_placeholder():
    """XMLBrowser.pm:837-846 — ``count`` 1 and the placeholder row."""
    from lyrion.web import menus

    want = perl("search_plain")["result"]
    got = feed("search", ["0", "6", "menu:search"])
    assert set(got) == set(want), (set(got), set(want))
    assert got["count"] == want["count"] == 1
    assert set(got["item_loop"][0]) == set(want["item_loop"][0])
    assert got["item_loop"][0]["text"] == menus.menu_title("EMPTY")
    assert got["item_loop"][0]["action"] == \
        want["item_loop"][0]["action"] == "none"


def test_presets_without_favourites_matches_perl():
    """Perl's empty TuneIn preset feed → the same ``Leer`` answer."""
    want = perl("presets_empty")["result"]
    got = feed("presets", ["0", "3", "menu:presets"])
    assert set(got) == set(want), (set(got), set(want))
    assert got["count"] == want["count"] == 1
    assert set(got["item_loop"][0]) == set(want["item_loop"][0])
    assert got["window"] == want["window"] == {"windowStyle": "text_list"}
    assert set(got["base"]["actions"]) == set(want["base"]["actions"])


# ── search, station rows and the play-control menu ────────────────────────

def test_search_feed_returns_perl_shaped_station_rows(idle_client):
    """The search feed answers Perl's envelope with touch-to-play rows.

    The *fixture* answer of the live server held TuneIn's artist/category
    ``link`` rows only (TuneIn's search is not a pure station list); with
    radio-browser the feed is a station list, so the parity claim is the
    envelope (``offset``/``title``/``base``/``item_loop``/``count``/``window``),
    Perl's ``base`` commands and a station row of the shape Perl renders in
    ``local_stations`` — plus Perl's ``_jivePresetBase`` rule: ``set-preset-*``
    exactly when the answered loop carries station rows.
    """
    want = perl("search_rock")["result"]
    got = feed("search", ["0", "4", "menu:search", "search:rock"])
    assert set(got) == set(want), (set(got), set(want))
    ours_actions = set(got["base"]["actions"])
    theirs_actions = set(want["base"]["actions"])
    assert {"go", "play", "add", "add-hold", "more", "playControl"} <= \
        ours_actions & theirs_actions
    # every answered row is a station row ⇒ Perl adds the preset slots
    # (XMLBrowser.pm:1426-1427 ``_jivePresetBase``), the fixture had link rows
    # only and therefore none.
    assert {f"set-preset-{n}" for n in range(10)} <= ours_actions
    assert not any(a.startswith("set-preset") for a in theirs_actions)
    station = perl("local_stations")["result"]["item_loop"][0]
    ours = got["item_loop"][0]
    assert ours["type"] == station["type"] == "audio"
    assert set(ours["params"]) == set(station["params"])
    assert set(ours["presetParams"]) == set(station["presetParams"])
    assert got["title"] == "Suchergebnisse: rock"
    assert got["offset"] == 0


def test_preset_slots_only_when_station_rows_are_answered():
    """The ``set-preset-*`` rule of XMLBrowser.pm:1426-1427 both ways."""
    index = feed("music", ["0", "3", "menu:music"])
    assert not any(k.startswith("set-preset") for k in index["base"]["actions"])
    stations = feed("search", ["0", "2", "menu:search", "search:rock"])
    assert {f"set-preset-{n}" for n in range(10)} <= set(
        stations["base"]["actions"])


def test_query_params_reach_the_radio_browser_endpoints(monkeypatch):
    seen: list[tuple] = []

    async def fake_search(name, limit=20, offset=0):
        seen.append((name, limit, offset))
        return [radiobrowser.Station.from_json(STATIONS[0])]

    monkeypatch.setattr(radiobrowser, "search_stations", fake_search)
    got = feed("search", ["6", "4", "menu:search", "search:jazz"])
    # A *name search* is the one level radio-browser publishes no total for:
    # the level fetches its whole (bounded) list and slices the requested window
    # out of it — Perl's model, whose feed holds the cached document and slices
    # it per request (``XMLBrowser.pm:353-371``, ``normalize()``).  The page
    # offset must NOT travel to the endpoint: ``count`` may not depend on the
    # page, or every page invalidates the client's list (``DB.lua:125-136``).
    assert seen == [("jazz", radiobrowser._MAX_LIMIT, 0)], seen
    # start 6 of a one-row list = a window past the end: the total (1) stays,
    # no row and no ``Leer`` placeholder (Perl's invalid ``normalize()``
    # branch, ``Request.pm:1805-1839``).
    assert got["count"] == 1 and got["item_loop"] == []


def test_station_leaf_and_interim_context_menu():
    """The tap on a row: Perl's info rows / the play-control menu."""
    sid = feed("local", ["0", "6", "menu:local"])["item_loop"][0][
        "actions"]["go"]["params"]["item_id"]
    stations = feed("local", ["0", "4", "menu:local", f"item_id:{sid}"])
    station_id = stations["item_loop"][0]["params"]["item_id"]

    info = feed("local", ["0", "4", "menu:local", f"item_id:{station_id}"])
    want_info = perl("local_station_leaf")["result"]
    assert set(info) == set(want_info) == {"offset", "count", "item_loop"}
    assert set(info["item_loop"][0]) == set(want_info["item_loop"][0])
    assert info["item_loop"][0]["style"] == "itemNoAction"
    assert info["item_loop"][0]["action"] == "none"

    cm = feed("local", ["0", "4", "menu:local", f"item_id:{station_id}",
                        "isContextMenu:1", "xmlBrowseInterimCM:1"])
    want_cm = perl("local_station_interim_cm")["result"]
    assert set(cm) == set(want_cm), (set(cm), set(want_cm))
    assert set(cm["item_loop"][0]) == set(want_cm["item_loop"][0])
    assert [r["style"] for r in cm["item_loop"]] == \
        [r["style"] for r in want_cm["item_loop"]]
    # rows 1-3 are the playlist-control rows for *this* feed
    assert [r["actions"]["go"]["cmd"] for r in cm["item_loop"][:3]] == [
        ["local", "playlist", "add"], ["local", "playlist", "insert"],
        ["local", "playlist", "play"]]
    assert cm["item_loop"][2]["actions"]["go"]["nextWindow"] == "nowPlaying"
    # row 4 is the favourites row (XMLBrowser.pm:1875-1900)
    fav = cm["item_loop"][3]
    assert fav["actions"]["go"]["cmd"] == ["jivefavorites", "add"]
    assert fav["actions"]["go"]["params"]["url"] == STATIONS[0]["url_resolved"]
    assert fav["actions"]["go"]["params"]["type"] == "audio"


# ── the dispatch ──────────────────────────────────────────────────────────

def test_radio_feed_tags_are_registered():
    assert set(radiobrowser.RADIO_FEEDS) == set(ROOT_TAGS) | {"sounds"}
    # ``search`` must not shadow the library search: the library form is
    # ``search <start> <count> term:<x>`` (Request.pm:610), the radio form
    # carries the literal ``items`` (OPMLBased.pm:117-120).
    assert "search" in radiobrowser.RADIO_FEEDS


def test_bare_feed_tag_without_items_is_not_answered():
    """Perl registers only ``items``/``playlist`` per radio tag (OPMLBased.pm).

    ``['music','0','5']`` matches no dispatch entry → Perl closes the socket
    without a body; this port answers the empty map (never an error body the
    controllers would try to parse as a loop).
    """
    async def run_dispatch():
        api = JSONRPCAPI()
        return await api.handle_request(json.dumps({
            "id": 1, "method": "slim.request",
            "params": [PLAYER, ["music", "0", "5"]],
        }).encode())

    body = json.loads(asyncio.run(run_dispatch()))
    assert body["result"] == {}
