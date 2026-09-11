"""R0-A — SqueezePlay song tap in My Music (MENU-01 / MENU-02).

Regression (gap analysis 03-menus-jive.md): a tap on a song row in
"Eigene Musik" was a silent no-op.

* MENU-01 — SqueezePlay rewrites the tap action name ``go`` through
  ``_actionAliasMap`` onto ``item.goAction``
  (SlimBrowserApplet.lua:1846-1853) and then aborts with ``EVENT_UNUSED``
  when the item has no ``playControlParams``
  (SlimBrowserApplet.lua:1993-2025). Perl puts ``goAction="playControl"``
  + ``playControlParams={xmlbrowserPlayControl => <itemIndex>}`` on every
  track item (Slim/Control/XMLBrowser.pm:1270-1271).
* MENU-02 — the server must answer ``menu:1`` +
  ``xmlbrowserPlayControl:N`` — with or without ``useContextMenu:1`` —
  with the play-control context menu for that row
  (Slim/Control/XMLBrowser.pm:805-830, ``_playlistControlContextMenu``).

Fixture origin (be precise about what is Perl and what is not):

* The ``tests/fixtures/perl_browselibrary_*.json`` files are **verbatim
  responses of the real Perl LMS** (public/9.2 @ d1d0a683d, player
  ``1c:87:2c:47:fc:36``, read-only ``slim.request`` probes against
  192.168.1.90:9000). Each file records the exact request it answers in
  ``params``. On the Perl side ``album_id:45`` is the pseudo album
  "Kein Album" with **6143** tracks, so the fixture's item 0 is
  ``track_id 51102`` (``007(Molotow Soda)``).
* The rows/track ids the assertions run against are **not** Perl data:
  they come from the local temp DB built by ``_db()`` (album 45 →
  tracks 10/11). Only the structural fields (text, style, params key
  sets, types of the Perl item shape) are compared verbatim; ids are
  compared against the temp DB, which is why ``track_id`` is normalised
  away when a Perl fixture is diffed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CM_FIXTURE = "perl_browselibrary_tracks_usecontextmenu.json"
TAP_FIXTURE = "perl_browselibrary_playcontrol_tap.json"
TAP_MULTI_FIXTURE = "perl_browselibrary_playcontrol_tap_multi.json"
NO_UCM_FIXTURE = "perl_browselibrary_playcontrol_no_usecontextmenu.json"
ALBUM_TAP_FIXTURE = "perl_browselibrary_playcontrol_albums.json"
OUT_OF_RANGE_FIXTURE = "perl_browselibrary_playcontrol_index_out_of_range.json"
NON_NUMERIC_FIXTURE = "perl_browselibrary_playcontrol_index_non_numeric.json"

# Temp-DB rows (NOT Perl data): album_id:45 → tracks 10 then 11, ordered by
# tracknum. The Perl library's album 45 ("Kein Album", 6143 tracks) starts
# with track_id 51102 instead, so fixture ids are never compared verbatim.
TRACK_IDS = [10, 11]

MENU_TEXTS_3 = ["Am Ende hinzufügen", "Als nächstes wiedergeben", "Wiedergabe"]
MENU_TEXTS_4 = ["Am Ende hinzufügen", "Als nächstes wiedergeben",
                "Diesen Titel wiedergeben", "Alle Titel wiedergeben"]


def _fixture(name: str) -> dict:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    # keep the params of the real request next to the result for reference
    assert data["result"], f"{name} carries no result"
    return data


def _perl_result(name: str, args: list[str]) -> dict:
    """Perl fixture result, with the recorded request verified against the
    request the test actually sends (token-wise, counts may be str/int)."""
    data = _fixture(name)
    recorded = [str(tok) for tok in data["params"][1]]
    expected = ["browselibrary"] + [str(tok) for tok in args]
    assert recorded == expected, \
        f"{name} answers {recorded}, test sends {expected}"
    return data["result"]


def _no_track_id(params: dict) -> dict:
    """Drop the library-dependent track id so a Perl fixture can be diffed."""
    return {k: v for k, v in params.items() if k != "track_id"}


def _db(tmp_path) -> str:
    """Temp library DB: album 45 → tracks 10/11, album 46 → track 12."""
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT, tracknum INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        INSERT INTO tracks (id, title, genre, year, url, tracknum) VALUES
            (10, 'Track One', 'Rock', 2000, 'file:///m/A/one.mp3', 1),
            (11, 'Track Two', 'Rock', 2000, 'file:///m/A/two.mp3', 2),
            (12, 'Other', 'Jazz', 2001, 'file:///m/B/other.mp3', 1);
        INSERT INTO albums (id, title, year, artwork) VALUES
            (45, 'Album A', 2000, 'art1'), (46, 'Album B', 2001, NULL);
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One');
        INSERT INTO tracks_albums (track, album) VALUES (10, 45), (11, 45),
                                                        (12, 46);
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (10, 1, 1), (11, 1, 1), (12, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _db_without_track_url(tmp_path) -> str:
    """Same library, but ``tracks`` has no ``url`` column at all.

    This is the defensive branch the ``track_urls`` lookup is written for:
    the preset lookup must fail soft without leaking its sqlite handle.
    """
    db = tmp_path / "lib_no_url.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, tracknum INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        INSERT INTO tracks (id, title, genre, year, tracknum) VALUES
            (10, 'Track One', 'Rock', 2000, 1),
            (11, 'Track Two', 'Rock', 2000, 2);
        INSERT INTO albums (id, title, year, artwork) VALUES
            (45, 'Album A', 2000, 'art1');
        INSERT INTO contributors (id, name) VALUES (1, 'Artist One');
        INSERT INTO tracks_albums (track, album) VALUES (10, 45), (11, 45);
        INSERT INTO tracks_contributors (track, contributor, role) VALUES
            (10, 1, 1), (11, 1, 1);
        """
    )
    con.commit()
    con.close()
    return str(db)


def _count_open_connections(monkeypatch) -> dict:
    """Instrument ``sqlite3.connect`` and track the open/close balance."""
    state = {"open": 0, "opened": 0}
    real_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, *args, **kwargs):
            object.__setattr__(self, "_closed", False)
            object.__setattr__(self, "_con", real_connect(*args, **kwargs))
            state["open"] += 1
            state["opened"] += 1

        def close(self):
            if not object.__getattribute__(self, "_closed"):
                object.__getattribute__(self, "_con").close()
                object.__setattr__(self, "_closed", True)
                state["open"] -= 1

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_con"), name)

        def __setattr__(self, name, value):
            if name in ("_con", "_closed"):
                object.__setattr__(self, name, value)
            else:
                setattr(object.__getattribute__(self, "_con"), name, value)

    monkeypatch.setattr(sqlite3, "connect", TrackingConnection)
    return state


@pytest.fixture()
def _lib_db(tmp_path, monkeypatch):
    """One temp library DB per test, patched into ``api._library_db_path``.

    Must be built ONCE: the menu path queries the DB repeatedly
    (rows + album artist lookup), and re-running the schema would raise.
    """
    path = _db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    return path


def _browse(args: list[str]) -> dict:
    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


TRACK_LIST_ARGS = ["items", "0", "10", "menu:1", "mode:tracks",
                   "album_id:45"]
CM_ARGS = TRACK_LIST_ARGS + ["useContextMenu:1"]
# One-item window: exactly the request the Perl Tap fixture answers.
ONE_ITEM_ARGS = ["items", "0", "1", "menu:1", "mode:tracks", "album_id:45"]
TAP_ARGS = ONE_ITEM_ARGS + ["useContextMenu:1"]


def test_track_items_carry_goaction_and_playcontrolparams(_lib_db):
    """MENU-01: every track row must be tappable (no silent no-op)."""
    res = _browse(CM_ARGS)
    items = res["item_loop"]
    assert [it["text"] for it in items] == ["Track One", "Track Two"]

    for idx, it in enumerate(items):
        assert it["goAction"] == "playControl", \
            f"row {idx} has no goAction → SqueezePlay tap returns EVENT_UNUSED"
        assert it["playControlParams"] == {"xmlbrowserPlayControl": str(idx)}
        assert it["commonParams"]["track_id"] == TRACK_IDS[idx]
        assert it["playallParams"] == {"play_index": idx}
        assert it["presetParams"]["favorites_title"] == it["text"]
        assert it["presetParams"]["favorites_type"] == "audio"
        assert it["presetParams"]["favorites_url"] == (
            "file:///m/A/one.mp3" if idx == 0 else "file:///m/A/two.mp3")


def test_track_items_expose_perl_item_keys(_lib_db):
    """The Perl fixture item's fields must all be present on our items."""
    perl_item = _fixture(CM_FIXTURE)["result"]["item_loop"][0]
    ours = _browse(CM_ARGS)["item_loop"][0]
    missing = set(perl_item) - set(ours)
    assert not missing, f"track item misses Perl fields {sorted(missing)}"
    assert ours["type"] == perl_item["type"] == "audio"


def test_base_actions_expose_playcontrol_alias(_lib_db):
    """SqueezePlay looks up base.actions[item.goAction]; the alias target
    base.actions.playControl must exist and merge the playControlParams."""
    res = _browse(CM_ARGS)
    actions = res["base"]["actions"]
    assert "playControl" in actions, sorted(actions)
    pc = actions["playControl"]
    assert pc["itemsParams"] == "playControlParams"
    assert pc["window"] == {"isContextMenu": 1}
    assert pc["cmd"] == ["browselibrary", "items"]
    # The tap request repeats these; without the drill filter the follow-up
    # request would list the whole library instead of the tapped album.
    assert pc["params"]["mode"] == "tracks"
    assert pc["params"]["album_id"] == "45"
    assert pc["params"]["useContextMenu"] in (1, "1")


def test_single_item_window_matches_perl_three_item_fixture(_lib_db):
    """MENU-02: a one-item window ("items 0 1") → Perl's 3-item menu."""
    args = TAP_ARGS + ["xmlbrowserPlayControl:0"]
    res = _browse(args)
    perl = _perl_result(TAP_FIXTURE, args)

    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_3
    assert res["count"] == len(perl["item_loop"]) == 3
    assert res["window"] == perl["window"]
    assert len(res["item_loop"]) == len(perl["item_loop"])

    for got, want in zip(res["item_loop"], perl["item_loop"]):
        assert got["text"] == want["text"]
        assert got["style"] == want["style"]
        wgo, ggo = want["actions"]["go"], got["actions"]["go"]
        assert ggo["cmd"] == wgo["cmd"] == ["playlistcontrol"]
        assert ggo.get("nextWindow") == wgo.get("nextWindow")
        assert ggo["player"] == 0
        # track_id differs by library — compare the rest verbatim
        assert _no_track_id(ggo["params"]) == _no_track_id(wgo["params"])

    assert [i["actions"]["go"]["params"]["cmd"] for i in res["item_loop"]] == \
        ["add", "insert", "load"]
    # every entry targets the tapped row (Perl fixture: track_id 51102 → 10)
    assert {i["actions"]["go"]["params"]["track_id"] for i in res["item_loop"]} \
        == {"10"}


def test_multi_item_window_matches_perl_four_item_fixture(_lib_db):
    """P2: a window with >1 item → Perl adds the 4th "Alle Titel
    wiedergeben" entry and switches the 3rd text to "Diesen Titel
    wiedergeben" (XMLBrowser.pm:1839-1844 + :1866/:1874)."""
    args = CM_ARGS + ["xmlbrowserPlayControl:0"]  # count=10 → 2 rows
    res = _browse(args)
    perl = _perl_result(TAP_MULTI_FIXTURE,
                        ["items", "5", "3", "menu:1", "mode:tracks",
                         "album_id:45", "useContextMenu:1",
                         "xmlbrowserPlayControl:5"])

    assert res["count"] == perl["count"] == 4
    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_4
    assert [it["text"] for it in res["item_loop"]] == \
        [it["text"] for it in perl["item_loop"]]
    assert [it["style"] for it in res["item_loop"]] == \
        [it["style"] for it in perl["item_loop"]] == \
        ["item_add", "itemNoAction", "item_play", "itemNoAction"]

    # The play-all entry replays the request's drill filter and the tapped
    # absolute index (Perl probe: play_index 5, album_id "45").
    got_go = res["item_loop"][3]["actions"]["go"]
    want_go = perl["item_loop"][3]["actions"]["go"]
    assert got_go["cmd"] == want_go["cmd"] == ["playlistcontrol"]
    assert got_go["nextWindow"] == want_go["nextWindow"] == "nowPlaying"
    assert {k: v for k, v in got_go["params"].items()
            if k != "play_index"} == \
        {k: v for k, v in want_go["params"].items() if k != "play_index"}
    assert got_go["params"]["play_index"] == 0   # tapped index in our window
    # ... target the tapped song, not the play-all
    assert {i["actions"]["go"]["params"].get("track_id")
            for i in res["item_loop"][:3]} == {"10"}


def test_tap_without_usecontextmenu_still_serves_the_menu(_lib_db):
    """P2: Perl gates on ``$menuMode && defined $xmlbrowserPlayControl``
    (XMLBrowser.pm:805) — ``useContextMenu`` is not required."""
    args = ONE_ITEM_ARGS + ["xmlbrowserPlayControl:0"]
    res = _browse(args)
    perl = _perl_result(NO_UCM_FIXTURE, args)

    assert res["count"] == perl["count"] == 3
    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_3
    assert [it["style"] for it in res["item_loop"]] == \
        [it["style"] for it in perl["item_loop"]]
    assert {i["actions"]["go"]["params"]["track_id"]
            for i in res["item_loop"]} == {"10"}


def test_tap_on_album_row_serves_the_menu(_lib_db):
    """P2: Perl serves the menu for album rows too (probe: ``mode:albums``
    + ``xmlbrowserPlayControl:0`` → 3 items carrying ``album_id``)."""
    args = ["items", "0", "1", "menu:1", "mode:albums", "useContextMenu:1",
            "xmlbrowserPlayControl:0"]
    res = _browse(args)
    perl = _perl_result(ALBUM_TAP_FIXTURE, args)

    assert res["count"] == perl["count"] == 3
    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_3
    assert [it["text"] for it in res["item_loop"]] == \
        [it["text"] for it in perl["item_loop"]]
    # our temp album 45 is the same id as Perl's album 45 → full diff
    for got, want in zip(res["item_loop"], perl["item_loop"]):
        assert got["style"] == want["style"]
        assert got["actions"]["go"]["cmd"] == \
            want["actions"]["go"]["cmd"] == ["playlistcontrol"]
        assert got["actions"]["go"]["params"] == want["actions"]["go"]["params"]
    # album rows are not audio rows: no play-all even in wider windows
    assert all("goAction" not in it for it in _browse(
        ["items", "0", "10", "menu:1", "mode:albums"])["item_loop"])


def test_negative_index_returns_empty_menu(_lib_db):
    """P3: Perl computes ``$i = -1 - offset < 0`` → count 0, empty menu."""
    args = TAP_ARGS + ["xmlbrowserPlayControl:-1"]
    res = _browse(args)
    perl = _perl_result(OUT_OF_RANGE_FIXTURE, args)

    assert res == {"window": perl["window"], "offset": 0, "count": 0,
                   "item_loop": []}
    assert perl["count"] == 0


def test_non_numeric_index_addresses_item_zero(_lib_db):
    """P3: Perl does arithmetic on the token, so "abc" → item 0."""
    args = TAP_ARGS + ["xmlbrowserPlayControl:abc"]
    res = _browse(args)
    perl = _perl_result(NON_NUMERIC_FIXTURE, args)

    assert res["count"] == perl["count"] == 3
    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_3
    for got, want in zip(res["item_loop"], perl["item_loop"]):
        assert got["style"] == want["style"]
        assert _no_track_id(got["actions"]["go"]["params"]) == \
            _no_track_id(want["actions"]["go"]["params"])
    assert {i["actions"]["go"]["params"]["track_id"]
            for i in res["item_loop"]} == {"10"}


def test_playcontrol_index_selects_the_tapped_row(_lib_db):
    """xmlbrowserPlayControl:N is an ABSOLUTE row index: the window's
    offset is subtracted first (Perl ``$i = $idx - offset``)."""
    first = _browse(ONE_ITEM_ARGS + ["xmlbrowserPlayControl:0"])
    # paged window start=1 → the absolute index 1 addresses its first row
    second = _browse(["items", "1", "1", "menu:1", "mode:tracks",
                      "album_id:45", "useContextMenu:1",
                      "xmlbrowserPlayControl:1"])

    def target(resp):
        return {i["actions"]["go"]["params"]["track_id"]
                for i in resp["item_loop"]}

    assert target(first) == {"10"}
    assert target(second) == {"11"}
    # an index outside the fetched window is out of range → empty
    assert _browse(ONE_ITEM_ARGS + ["xmlbrowserPlayControl:1"])["count"] == 0


def test_usecontextmenu_without_index_keeps_the_list(_lib_db):
    """A plain list request (no xmlbrowserPlayControl) is not a tap."""
    res = _browse(CM_ARGS)
    assert [it["text"] for it in res["item_loop"]] == ["Track One", "Track Two"]
    assert "style" not in res["item_loop"][0]


def test_album_list_is_unchanged_without_the_playcontrol_token(_lib_db):
    """Album rows without a tap token keep drilling (only audio rows get
    playControl); a tap token now serves the menu (see above)."""
    res = _browse(["items", "0", "10", "menu:1", "mode:albums",
                   "useContextMenu:1"])
    assert res["item_loop"], "albums menu must still return its rows"
    assert all("goAction" not in it for it in res["item_loop"])
    assert res["item_loop"][0]["commonParams"] == {"album_id": "45"}


def test_track_without_url_column_keeps_preset_params_and_closes_db(
        tmp_path, monkeypatch):
    """P3: the defensive ``track_urls`` lookup must not leak its sqlite
    handle and must still emit ``presetParams`` (Perl always ships them)."""
    path = _db_without_track_url(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    state = _count_open_connections(monkeypatch)

    res = _browse(TRACK_LIST_ARGS)

    assert state["opened"] >= 2, "expected several read-only queries"
    assert state["open"] == 0, \
        f"{state['open']} of {state['opened']} library DB connections left open"
    item = res["item_loop"][0]
    assert "favorites_url" not in item["presetParams"]
    assert item["presetParams"]["favorites_type"] == "audio"
    assert item["presetParams"]["favorites_title"] == "Track One"


def test_slim_request_dispatch_reaches_the_play_control_menu(_lib_db):
    """End-to-end over the real JSON-RPC entry (slim.request): the tap's
    request must survive the dispatch with both tokens intact."""
    res = asyncio.run(JSONRPCAPI()._slim_request(
        "1c:87:2c:47:fc:36",
        ["browselibrary", "items", "0", "1", "menu:1", "mode:tracks",
         "album_id:45", "useContextMenu:1", "xmlbrowserPlayControl:0"]))
    assert [it["text"] for it in res["item_loop"]] == MENU_TEXTS_3
    assert {it["actions"]["go"]["params"]["track_id"]
            for it in res["item_loop"]} == {"10"}
