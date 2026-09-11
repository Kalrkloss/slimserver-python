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
* MENU-02 — the server must answer ``useContextMenu:1`` +
  ``xmlbrowserPlayControl:N`` with the play-control context menu
  (Add / Play-next / Play) for that row
  (Slim/Control/XMLBrowser.pm:805-830, ``_playlistControlContextMenu``).

The fixtures under ``tests/fixtures/`` are the **real Perl LMS
responses** (LMS public/9.2, player 1c:87:2c:47:fc:36, read-only probes):

    browselibrary items 0 1 menu:1 mode:tracks album_id:45 useContextMenu:1
    browselibrary items 0 1 menu:1 mode:tracks album_id:45 useContextMenu:1
        xmlbrowserPlayControl:0

so the assertions below are grounded in Perl output, not in guesses. The
probe rows/track ids come from the Perl library, hence only structural
fields are compared verbatim; ids are checked against the local temp DB.
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

# album_id:45 drill → tracks 10 then 11 (ordered by tracknum).
TRACK_IDS = [10, 11]


def _fixture(name: str) -> dict:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    # keep the params of the real request next to the result for reference
    assert data["result"], f"{name} carries no result"
    return data


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


def test_usecontextmenu_returns_playcontrol_context_menu(_lib_db):
    """MENU-02: the tap's context menu must match the Perl fixture."""
    res = _browse(CM_ARGS + ["xmlbrowserPlayControl:0"])
    perl = _fixture(TAP_FIXTURE)["result"]

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
        assert {k: v for k, v in ggo["params"].items() if k != "track_id"} == \
            {k: v for k, v in wgo["params"].items() if k != "track_id"}

    assert [i["actions"]["go"]["params"]["cmd"] for i in res["item_loop"]] == \
        ["add", "insert", "load"]
    # every entry targets the tapped row (Perl fixture: track_id 51102 → 10)
    assert {i["actions"]["go"]["params"]["track_id"] for i in res["item_loop"]} \
        == {"10"}


def test_playcontrol_index_selects_the_tapped_row(_lib_db):
    """xmlbrowserPlayControl:N addresses the tapped row, not always row 0."""
    first = _browse(CM_ARGS + ["xmlbrowserPlayControl:0"])
    second = _browse(CM_ARGS + ["xmlbrowserPlayControl:1"])

    def target(resp):
        return {i["actions"]["go"]["params"]["track_id"]
                for i in resp["item_loop"]}

    assert target(first) == {"10"}
    assert target(second) == {"11"}


def test_usecontextmenu_without_index_keeps_the_list(_lib_db):
    """A plain list request (no xmlbrowserPlayControl) is not a tap."""
    res = _browse(CM_ARGS)
    assert [it["text"] for it in res["item_loop"]] == ["Track One", "Track Two"]
    assert "style" not in res["item_loop"][0]


def test_plain_list_is_unchanged_for_non_audio_modes(_lib_db):
    """Album rows must keep drilling (only audio rows get playControl)."""
    res = _browse(["items", "0", "10", "menu:1", "mode:albums",
                   "useContextMenu:1", "xmlbrowserPlayControl:0"])
    assert res["item_loop"], "albums menu must still return its rows"
    assert all("goAction" not in it for it in res["item_loop"])


def test_slim_request_dispatch_reaches_the_play_control_menu(_lib_db):
    """End-to-end over the real JSON-RPC entry (slim.request): the tap's
    request must survive the dispatch with both tokens intact."""
    res = asyncio.run(JSONRPCAPI()._slim_request(
        "1c:87:2c:47:fc:36",
        ["browselibrary", "items", "0", "1", "menu:1", "mode:tracks",
         "album_id:45", "useContextMenu:1", "xmlbrowserPlayControl:0"]))
    assert [it["text"] for it in res["item_loop"]] == [
        "Am Ende hinzufügen", "Als nächstes wiedergeben", "Wiedergabe"]
    assert {it["actions"]["go"]["params"]["track_id"]
            for it in res["item_loop"]} == {"10"}
