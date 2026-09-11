"""`contextmenu` — SqueezePlay press-and-hold menus (Perl parity).

Regression: SqueezePlay opens the context window for a tapped/hold item with
``['contextmenu', 0, 200, 'playlist_index:0', 'menu:track', …]``; this server
answered ``{}`` (command unimplemented), so the modal dialog opened **empty**
(black window, only the X button).

Fixture origin — every ``tests/fixtures/perl_contextmenu_*.json`` is the
**verbatim answer of the real Perl LMS** (public/9.2, player
``24:0a:c4:29:77:90``), captured read-only against ``192.168.1.90:9000`` with::

    POST http://192.168.1.90:9000/jsonrpc.js
    {"id":1,"method":"slim.request",
     "params":["24:0a:c4:29:77:90", ["contextmenu", <index>, <quantity>,
                                    <params…>]]}

Each file records its exact request under ``params``; ``_perl_result()``
verifies it against the request the test actually sends, and ``PROBES`` below
spells out the token list per fixture.  The Perl source reference is
``/tmp/lms-ref`` (read-only): ``Slim/Control/Queries.pm:6171``
(``contextMenuQuery``, the wrapper), ``Slim/Control/Request.pm:487`` (dispatch),
``Slim/Menu/TrackInfo.pm:1378`` (``cliQuery`` context resolution) and
``Slim/Control/XMLBrowser.pm:805`` (the play-control branch).

Divergences that are *deliberate* and asserted as such below:

* a ``track_id``/playlist row maps onto **our** temp library (album 45 →
  tracks 10/11, artist 1) — ids and info texts differ, the shapes do not;
* the live player's playlist item 0 is a **favourite radio stream**, so its
  ``playlist_index:0`` fixture is the *remote* menu (7 items, "Favorit
  löschen"); our library track yields the library menu (9 items) for the same
  request — the core play-control entries are compared, the provider tail is
  not (different underlying track);
* the remote provider tail (``Titel:``/``Interpret:`` lines from the radio
  protocol handler) is not reproduced; a remote menu therefore has fewer
  provider items than Perl's;
* Perl's ``item_id`` crumb is a per-session SID; we reuse the request crumb or
  ``ffffffff`` (key parity, value differs);
* ``contextmenu`` with **no** ``menu`` param makes Perl close the connection
  on this build (probe: ``RemoteDisconnected``) — the source path is
  ``setStatusBadParams`` (Queries.pm:6220), i.e. an empty result, which is
  what we return; no fixture is carried because Perl sent no answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest

from lyrion.web import api as api_mod
from lyrion.web import contextmenu
from lyrion.web.api import JSONRPCAPI

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLAYER = "24:0a:c4:29:77:90"

# fixture name → the exact parameter tokens the Perl probe sent (after the
# player id); asserted against the file's recorded `params`.
PROBES: dict[str, list] = {
    "perl_contextmenu_track_playlist_index0":
        [0, 200, "playlist_index:0", "menu:track"],
    "perl_contextmenu_track_usecontextmenu":
        [0, 200, "playlist_index:0", "menu:track", "useContextMenu:1"],
    "perl_contextmenu_track_index_abc":
        [0, 200, "playlist_index:abc", "menu:track"],
    "perl_contextmenu_track_index_empty":
        [0, 200, "playlist_index:", "menu:track"],
    "perl_contextmenu_track_index_negative":
        [0, 200, "playlist_index:-1", "menu:track"],
    "perl_contextmenu_track_index_one":
        [0, 200, "playlist_index:1", "menu:track"],
    "perl_contextmenu_track_index_out_of_range":
        [0, 200, "playlist_index:9999", "menu:track"],
    "perl_contextmenu_track_no_context": [0, 200, "menu:track"],
    "perl_contextmenu_track_track_id": [0, 200, "track_id:51102", "menu:track"],
    "perl_contextmenu_album_id45": [0, 200, "album_id:45", "menu:album"],
    "perl_contextmenu_artist_id146": [0, 200, "artist_id:146", "menu:artist"],
    "perl_contextmenu_genre_id2": [0, 200, "genre_id:2", "menu:genre"],
    "perl_contextmenu_album_playlist_index0":
        [0, 200, "playlist_index:0", "menu:album"],
    "perl_contextmenu_artist_playlist_index0":
        [0, 200, "playlist_index:0", "menu:artist"],
    "perl_contextmenu_unknown_menu":
        [0, 200, "playlist_index:0", "menu:bogus"],
    "perl_contextmenu_track_xmlbrowserplaycontrol":
        [0, 200, "playlist_index:0", "menu:track", "useContextMenu:1",
         "xmlbrowserPlayControl:0"],
    "perl_contextmenu_track_index_quantity_tagged":
        [0, 200, "playlist_index:0", "menu:track", "useContextMenu:1",
         "_index:0", "_quantity:200"],
}

# Texts the Perl library-track menu carries verbatim (localised strings).
LIBRARY_FIXED_TEXTS = ["Am Ende hinzufügen", "Als nächstes wiedergeben",
                       "Wiedergabe", "In Favoriten speichern",
                       "Plattenhülle anzeigen", "Weitere Infos"]
LABELS = ["Interpret", "Album", "Stilrichtung"]

# Temp library (NOT Perl data): album 45 → tracks 10/11, artist 1.
LIB_ALBUM = 45
LIB_TRACK = 10
LIB_ARTIST = 1


def _fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.suffix:
        path = path.with_suffix(".json")
    return json.loads(path.read_text(encoding="utf-8"))


def _perl_result(name: str, args: list) -> dict:
    data = _fixture(name)
    assert [str(t) for t in data["params"][1]] == ["contextmenu"] + \
        [str(t) for t in PROBES[name]], f"{name}: recorded request drifted"
    return data["result"]


def _db(tmp_path) -> str:
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, genre TEXT,
                             year INTEGER, url TEXT, tracknum INTEGER,
                             duration REAL, bitrate INTEGER,
                             samplerate INTEGER, bitspersample INTEGER,
                             cover TEXT, remote INTEGER, disc INTEGER,
                             filesize INTEGER, comment TEXT, lyrics TEXT,
                             content_type TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, year INTEGER,
                             artwork TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
                                          role INTEGER);
        INSERT INTO tracks (id, title, genre, year, url, tracknum, duration,
                            remote) VALUES
            (10, 'Track One', 'Rock', 2000, 'file:///m/A/one.mp3', 1, 180, 0),
            (11, 'Track Two', 'Rock', 2000, 'file:///m/A/two.mp3', 2, 190, 0),
            (12, 'Other', 'Jazz', 2001, 'file:///m/B/other.mp3', 1, 200, 0);
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
    path = _db(tmp_path)
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    return path


class _FakePlayer:
    def __init__(self, playlist, current_title=""):
        self.playlist = list(playlist)
        self.current_title = current_title


class _FakePM:
    def __init__(self, player):
        self._player = player

    def get_player(self, pid):
        return self._player

    def get_all_players(self):
        return [self._player]


def _run(args: list, *, playlist=(), current_title="", api=None) -> dict:
    api = api or JSONRPCAPI()
    pm = _FakePM(_FakePlayer(playlist, current_title))
    return asyncio.run(
        contextmenu.handle_contextmenu(api, pm, PLAYER, args))


def _track_args(tokens: list) -> list:
    return ["contextmenu", 0, 200, *tokens]


# ---------------------------------------------------------------------------
# Fixture integrity + split_request
# ---------------------------------------------------------------------------


def test_every_fixture_records_its_probe():
    for name in PROBES:
        data = _fixture(name)
        assert data["method"] == "slim.request"
        assert data["params"][0] == PLAYER
        assert [str(t) for t in data["params"][1]] == \
            ["contextmenu"] + [str(t) for t in PROBES[name]]
        assert "result" in data, f"{name} has no Perl result"


def test_split_request_tagged_index_tokens():
    """Perl's cliQuery hack: a tagged token in the index/quantity slot is a
    real param (TrackInfo.pm:1382)."""
    index, quantity, params = contextmenu.split_request(
        ["playlist_index:0", 200, "menu:track"])
    assert (index, quantity) == (0, 200)
    assert params == {"playlist_index": "0", "menu": "track"}

    index, quantity, params = contextmenu.split_request(
        [0, "menu:track", "playlist_index:5"])
    assert (index, quantity) == (0, 200)  # Perl resets quantity to 200
    assert params == {"menu": "track", "playlist_index": "5"}


def test_perl_number_semantics_of_playlist_index():
    """``playlist_index`` is numified with ``0 +`` (abc/empty → 0), negatives
    count from the end (Perl array indexing)."""
    index, quantity, params = contextmenu.split_request(
        [0, 200, "playlist_index:0", "menu:track"])
    assert (index, quantity) == (0, 200)
    assert params == {"playlist_index": "0", "menu": "track"}
    assert api_mod._playctl_index("abc") == 0
    assert api_mod._playctl_index("") == 0
    assert api_mod._playctl_index("-1") == -1


# ---------------------------------------------------------------------------
# Empty results — exactly where Perl answers {}
# ---------------------------------------------------------------------------

#: Fixtures whose Perl ``result`` is ``{}``; every one must stay empty here.
EMPTY_CASES = (
    "perl_contextmenu_track_index_one",
    "perl_contextmenu_track_index_out_of_range",
    "perl_contextmenu_track_no_context",
    "perl_contextmenu_album_playlist_index0",
    "perl_contextmenu_artist_playlist_index0",
    "perl_contextmenu_unknown_menu",
)


@pytest.mark.parametrize("name", EMPTY_CASES)
def test_empty_results_match_perl(name, _lib_db):
    perl = _perl_result(name, PROBES[name])
    assert perl == {}, f"{name} is not an empty Perl answer"
    # The probe's playlist has one row; the test playlist mirrors that.
    assert _run(_track_args(PROBES[name][2:]), playlist=[10]) == {}


def test_out_of_range_and_alias_indices(_lib_db):
    """``abc``/``""``/``-1`` all address item 0 (Perl ``0 +`` / negative from
    the end); ``1`` is out of range for the 1-item playlist.  The alias token
    itself is echoed verbatim in ``playControl.params.playlist_index`` (live
    ``-1``/``abc`` fixtures), so it is normalised away here."""
    base = _run(_track_args(["playlist_index:0", "menu:track"]),
                playlist=[10])
    assert base and base["item_loop"]
    for token in ("playlist_index:abc", "playlist_index:",
                  "playlist_index:-1"):
        assert _strip_session(_run(_track_args([token, "menu:track"]),
                                   playlist=[10])) == _strip_session(base)
    assert _run(_track_args(["playlist_index:1", "menu:track"]),
                playlist=[10]) == {}
    assert _run(_track_args(["playlist_index:9999", "menu:track"]),
                playlist=[10]) == {}


def _strip_session(resp: dict) -> dict:
    """Normalise the fields that legitimately differ between equivalent Perl
    answers: the per-session ``item_id`` crumb, the echoed ``useContextMenu``
    and the verbatim ``playlist_index`` token in ``playControl.params``."""
    data = json.loads(json.dumps(resp))
    for item in data.get("item_loop", []):
        for action in (item.get("actions") or {}).values():
            (action.get("params") or {}).pop("item_id", None)
    pc = data.get("base", {}).get("actions", {}).get("playControl", {})
    for key in ("useContextMenu", "playlist_index"):
        (pc.get("params") or {}).pop(key, None)
    return data


# ---------------------------------------------------------------------------
# Track menu — library track (track_id / playlist_index onto a library row)
# ---------------------------------------------------------------------------


def test_library_track_menu_matches_perl_shape(_lib_db):
    """``track_id`` onto our library track → Perl's 9-item ``trackinfo`` menu.

    The Perl fixture answers ``track_id:51102`` (its own library); ids/info
    texts are normalised, everything structural is compared verbatim.
    """
    perl = _perl_result("perl_contextmenu_track_track_id",
                        PROBES["perl_contextmenu_track_track_id"])
    res = _run(_track_args(["track_id:10", "menu:track"]))

    assert set(res) == set(perl) == {"base", "count", "item_loop", "offset",
                                     "title", "window"}
    assert res["window"] == perl["window"] == {"windowStyle": "text_list"}
    assert res["offset"] == perl["offset"] == 0
    assert res["count"] == perl["count"] == 9
    assert len(res["item_loop"]) == len(perl["item_loop"]) == 9


def test_library_track_menu_item_keys_and_texts(_lib_db):
    perl = _perl_result("perl_contextmenu_track_track_id",
                        PROBES["perl_contextmenu_track_track_id"])
    items = _run(_track_args(["track_id:10", "menu:track"]))["item_loop"]
    want = perl["item_loop"]

    # fixed localised texts are identical, library-dependent rows by label
    fixed = {0, 1, 2, 3, 7, 8}
    for i in fixed:
        assert items[i]["text"] == want[i]["text"]
    for i, label in zip((4, 5, 6), LABELS):
        assert items[i]["text"].startswith(label + ": ")
        assert want[i]["text"].startswith(label + ": ")
    # Perl row kinds/styles, per index
    assert [it["style"] for it in items[:4]] == \
        [it["style"] for it in want[:4]] == \
        ["item_add", "item_insert", "itemplay", "item_fav"]
    assert [it.get("type") for it in items] == \
        [it.get("type") for it in want]


def _norm_cmd(cmd: list) -> list:
    """Normalise a cmd list for fixture diffing: library ids differ (Perl's
    album 45/track 51102 vs our temp tracks 10/11), so numeric tokens become
    a placeholder."""
    return ["ID" if str(tok).lstrip("-").isdigit() else tok for tok in cmd]


def test_library_track_menu_action_key_sets(_lib_db):
    """No invented item keys: our item/action key sets equal Perl's per row."""
    perl = _perl_result("perl_contextmenu_track_track_id",
                        PROBES["perl_contextmenu_track_track_id"])
    items = _run(_track_args(["track_id:10", "menu:track"]))["item_loop"]

    for got, want in zip(items, perl["item_loop"]):
        assert set(got) == set(want), (got.get("text"), set(got) ^
                                       set(want))
        if "actions" in want:
            assert set(got["actions"]) == set(want["actions"])
            for name, waction in want["actions"].items():
                gaction = got["actions"][name]
                assert set(gaction) == set(waction), (got["text"], name)
                if "cmd" in waction:
                    assert _norm_cmd(gaction["cmd"]) == _norm_cmd(
                        waction["cmd"])
                if "params" in waction:
                    assert set(gaction["params"]) == set(waction["params"]), \
                        (got["text"], name)


def test_library_track_menu_entity_link_params(_lib_db):
    """The Interpret/Album/Stilrichtung rows drill through browselibrary with
    Perl's fixedParams + ``menu:1``; the ids come from our temp library."""
    perl = _perl_result("perl_contextmenu_track_track_id",
                        PROBES["perl_contextmenu_track_track_id"])
    items = _run(_track_args(["track_id:10", "menu:track"]))["item_loop"]
    want = perl["item_loop"]

    for i, key, mode in ((4, "artist_id", "albums"), (5, "album_id", "tracks"),
                         (6, "genre_id", "artists")):
        go = items[i]["actions"]["go"]
        wgo = want[i]["actions"]["go"]
        assert go["cmd"] == wgo["cmd"] == ["browselibrary", "items"]
        assert go["params"]["mode"] == wgo["params"]["mode"] == mode
        assert go["params"]["menu"] == wgo["params"]["menu"] == 1
        assert go["params"]["library_id"] == wgo["params"]["library_id"] == ""
        assert key in go["params"] and key in wgo["params"]
        more = items[i]["actions"]["more"]
        assert more["cmd"] == want[i]["actions"]["more"]["cmd"]
        assert more["window"] == {"isContextMenu": 1}
    # our library data, so the ids are ours (album 45, artist 1, genre 1)
    assert items[4]["actions"]["go"]["params"]["artist_id"] == LIB_ARTIST
    assert items[5]["actions"]["go"]["params"]["album_id"] == LIB_ALBUM
    assert items[4]["presetParams"]["favorites_title"] == "Artist One"


def test_library_track_menu_base_actions(_lib_db):
    """Perl's feed actions: go/add/add-hold/play/more/playControl plus the ten
    favourite slots (XMLBrowser only adds them when presetParams exist)."""
    perl = _perl_result("perl_contextmenu_track_track_id",
                        PROBES["perl_contextmenu_track_track_id"])
    actions = _run(_track_args(["track_id:10", "menu:track"]))["base"]["actions"]
    want = perl["base"]["actions"]

    assert set(actions) == set(want)
    assert set(actions) == {"go", "add", "add-hold", "play", "more",
                            "playControl"} | {f"set-preset-{n}"
                                              for n in range(10)}
    for name in ("go", "add", "add-hold", "play", "more", "playControl"):
        assert set(actions[name]) == set(want[name]), name
    assert actions["go"]["cmd"] == want["go"]["cmd"] == ["trackinfo", "items"]
    assert actions["play"]["cmd"] == ["trackinfo", "playlist", "play"]
    assert actions["play"]["nextWindow"] == "nowPlaying"

    pc = actions["playControl"]
    wpc = want["playControl"]
    assert pc["itemsParams"] == wpc["itemsParams"] == "playControlParams"
    assert pc["window"] == wpc["window"] == {"isContextMenu": 1}
    assert set(pc["params"]) == set(wpc["params"]) == \
        {"_index", "_quantity", "menu", "track_id"}
    assert pc["params"]["menu"] == "track"
    # Perl ships the id as a STRING in the feed actions (the item params use
    # the numeric library id — see test_library_track_menu_item_keys_and_texts)
    assert pc["params"]["track_id"] == str(LIB_TRACK)


def test_library_track_item_track_id_is_the_library_number(_lib_db):
    items = _run(_track_args(["track_id:10", "menu:track"]))["item_loop"]
    assert items[0]["actions"]["go"]["params"]["track_id"] == LIB_TRACK
    assert items[0]["actions"]["go"]["cmd"] == ["playlistcontrol"]
    assert items[0]["actions"]["go"]["params"]["cmd"] == "add"
    assert items[1]["actions"]["go"]["params"]["cmd"] == "insert"
    assert items[2]["actions"]["go"]["params"]["cmd"] == "load"
    assert items[2]["actions"]["go"]["nextWindow"] == "nowPlaying"


# ---------------------------------------------------------------------------
# Track menu via playlist_index
# ---------------------------------------------------------------------------


def test_playlist_index_menu_core_matches_perl(_lib_db):
    """A playlist row: Perl forwards ``playlist_index`` to ``trackinfo``.
    The live player's item 0 is a radio stream (remote menu); our playlist
    row is a library track, so only the core play-control entries are compared
    — plus the top-level shape, which must match exactly."""
    perl = _perl_result("perl_contextmenu_track_playlist_index0",
                        PROBES["perl_contextmenu_track_playlist_index0"])
    res = _run(_track_args(["playlist_index:0", "menu:track"]), playlist=[10])

    assert set(res) == set(perl)
    assert res["window"] == perl["window"] == {"windowStyle": "text_list"}
    assert res["offset"] == perl["offset"] == 0
    for got, want in zip(res["item_loop"][:3], perl["item_loop"][:3]):
        assert got["text"] == want["text"]
        assert got["style"] == want["style"]
        assert got["type"] == want["type"]
        assert set(got["actions"]) == set(want["actions"])
        assert got["actions"]["go"]["cmd"] == \
            want["actions"]["go"]["cmd"] == ["playlistcontrol"]
        assert got["actions"]["go"]["nextWindow"] == \
            want["actions"]["go"]["nextWindow"]
        assert set(got["actions"]["go"]["params"]) == \
            set(want["actions"]["go"]["params"])


def test_playlist_index_menu_playcontrol_carries_url_and_index(_lib_db):
    """Perl repeats ``url``/``playlist_index`` in playControl for a playlist
    context (live ``playlist_index:0`` fixture)."""
    perl = _perl_result("perl_contextmenu_track_playlist_index0",
                        PROBES["perl_contextmenu_track_playlist_index0"])
    actions = _run(_track_args(["playlist_index:0", "menu:track"]),
                   playlist=[10])["base"]["actions"]
    pc = actions["playControl"]["params"]
    wpc = perl["base"]["actions"]["playControl"]["params"]
    assert set(pc) == set(wpc) == {"_index", "_quantity", "url",
                                   "playlist_index", "menu", "track_id"}
    assert pc["playlist_index"] == wpc["playlist_index"] == "0"
    assert pc["url"] == "file:///m/A/one.mp3"
    assert pc["track_id"] == str(LIB_TRACK)


def test_remote_stream_menu_core_and_base(_lib_db):
    """A playlist row with no library match (a radio URL): the core entries
    and the (preset-less) base actions match Perl's remote menu."""
    perl = _perl_result("perl_contextmenu_track_playlist_index0",
                        PROBES["perl_contextmenu_track_playlist_index0"])
    res = _run(_track_args(["playlist_index:0", "menu:track"]),
               playlist=["http://hirschmilch.de:7000/chillout.mp3"],
               current_title="Hirschmilch Chillout")

    assert set(res) == set(perl)
    assert res["window"] == perl["window"]
    assert res["title"] == "Hirschmilch Chillout"
    for got, want in zip(res["item_loop"][:3], perl["item_loop"][:3]):
        assert got["text"] == want["text"]
        assert got["style"] == want["style"]
        assert got["actions"]["go"]["cmd"] == ["playlistcontrol"]
    # no presetParams on a remote stream → no set-preset slots (Perl: 6 keys)
    assert set(res["base"]["actions"]) == set(perl["base"]["actions"]) == \
        {"go", "add", "add-hold", "play", "more", "playControl"}
    # the remote item id is a string, as in Perl
    assert isinstance(res["item_loop"][0]["actions"]["go"]["params"]
                      ["track_id"], str)


def test_usecontextmenu_and_tagged_index_do_not_change_the_menu(_lib_db):
    """`useContextMenu:1`, the swapped token order and `_index:`/`_quantity:`
    tagged tokens all yield the same menu; Perl's own answers for them differ
    only in the session crumb and the echoed `playControl` params."""
    base = _run(_track_args(["playlist_index:0", "menu:track"]), playlist=[10])
    for tokens in (
        ["playlist_index:0", "menu:track", "useContextMenu:1"],
        ["menu:track", "playlist_index:0"],
        ["playlist_index:0", "menu:track", "_index:0", "_quantity:200"],
    ):
        # only the echoed playControl params may differ (see _strip_session)
        assert _strip_session(_run(_track_args(tokens), playlist=[10])) == \
            _strip_session(base)
        assert [it["text"] for it in _run(_track_args(tokens),
                                          playlist=[10])["item_loop"]] == \
            [it["text"] for it in base["item_loop"]]
    # ...and the Perl fixtures normalise to the playlist_index:0 answer
    a = _strip_session(_perl_result(
        "perl_contextmenu_track_playlist_index0",
        PROBES["perl_contextmenu_track_playlist_index0"]))
    for name in ("perl_contextmenu_track_usecontextmenu",
                 "perl_contextmenu_track_index_quantity_tagged"):
        assert _strip_session(_perl_result(name, PROBES[name])) == a


def test_playcontrol_echoes_usecontextmenu_and_playlist_index(_lib_db):
    """Live: `playControl.params` copies the request's tagged params verbatim
    (`useContextMenu: "1"`, `playlist_index: "abc"`)."""
    perl = _perl_result("perl_contextmenu_track_usecontextmenu",
                        PROBES["perl_contextmenu_track_usecontextmenu"])
    res = _run(_track_args(["playlist_index:0", "menu:track",
                            "useContextMenu:1"]), playlist=[10])
    pc = res["base"]["actions"]["playControl"]["params"]
    wpc = perl["base"]["actions"]["playControl"]["params"]
    assert set(pc) == set(wpc)
    assert pc["useContextMenu"] == wpc["useContextMenu"] == "1"
    assert pc["playlist_index"] == wpc["playlist_index"] == "0"


def test_xmlbrowserplaycontrol_wins_over_the_menu(_lib_db):
    """An extra ``xmlbrowserPlayControl`` token routes Perl into XMLBrowser's
    play-control branch (:805), which yields the empty window for a trackinfo
    feed (live-probed 0/1/5/-1)."""
    perl = _perl_result("perl_contextmenu_track_xmlbrowserplaycontrol",
                        PROBES["perl_contextmenu_track_xmlbrowserplaycontrol"])
    res = _run(_track_args(PROBES[
        "perl_contextmenu_track_xmlbrowserplaycontrol"][2:]), playlist=[10])
    assert res == perl == {"window": {"windowStyle": "text_list"},
                           "offset": 0, "count": 0}
    assert "item_loop" not in res


# ---------------------------------------------------------------------------
# Entity menus — album / artist / genre
# ---------------------------------------------------------------------------


ENTITY_CASES = [
    ("perl_contextmenu_album_id45", "album_id:45", "album"),
    ("perl_contextmenu_artist_id146", "artist_id:1", "artist"),
    ("perl_contextmenu_genre_id2", "genre_id:0", "genre"),
]


@pytest.mark.parametrize("name,token,menu", ENTITY_CASES)
def test_entity_menu_shape_matches_perl(name, token, menu, _lib_db):
    perl = _perl_result(name, PROBES[name])
    res = _run(_track_args([token, f"menu:{menu}"]))

    assert set(res) == set(perl) == {"base", "count", "item_loop", "offset",
                                     "title", "window"}
    assert res["window"] == perl["window"] == {"windowStyle": "text_list"}
    assert res["offset"] == perl["offset"] == 0
    # the three play-control entries are identical
    for got, want in zip(res["item_loop"][:3], perl["item_loop"][:3]):
        assert got["text"] == want["text"]
        assert got["style"] == want["style"]
        assert got["type"] == want["type"]
        assert set(got["actions"]) == set(want["actions"])
        assert got["actions"]["go"]["cmd"] == \
            want["actions"]["go"]["cmd"] == ["playlistcontrol"]
        assert set(got["actions"]["go"]["params"]) == \
            set(want["actions"]["go"]["params"])


@pytest.mark.parametrize("name,token,menu", ENTITY_CASES)
def test_entity_menu_base_actions_match_perl(name, token, menu, _lib_db):
    perl = _perl_result(name, PROBES[name])
    actions = _run(_track_args([token, f"menu:{menu}"]))["base"]["actions"]
    want = perl["base"]["actions"]
    assert set(actions) == set(want) == {"go", "add", "add-hold", "play",
                                         "more", "playControl"}
    for key in ("go", "add", "add-hold", "play", "more", "playControl"):
        assert set(actions[key]) == set(want[key]), key
    assert actions["go"]["cmd"] == want["go"]["cmd"]
    assert actions["play"]["nextWindow"] == "nowPlaying"
    pc = actions["playControl"]
    assert pc["itemsParams"] == "playControlParams"
    assert pc["window"] == {"isContextMenu": 1}
    assert set(pc["params"]) == set(want["playControl"]["params"])
    assert pc["params"]["menu"] == menu


def test_entity_menu_favorites_entry(_lib_db):
    """album/artist menus carry Perl's "In Favoriten speichern" row; the genre
    menu does not (live genre fixture: randomplay rows instead)."""
    for token, menu in (("album_id:45", "album"),
                        ("artist_id:1", "artist")):
        items = _run(_track_args([token, f"menu:{menu}"]))["item_loop"]
        assert items[3]["text"] == "In Favoriten speichern"
        assert items[3]["style"] == "item_fav"
        assert items[3]["type"] == "text"
        assert items[3]["actions"]["go"]["cmd"] == ["jivefavorites", "add"]
    genre = _run(_track_args(["genre_id:0", "menu:genre"]))["item_loop"]
    assert all(it["style"] != "item_fav" for it in genre)


def test_entity_menu_missing_id_is_empty():
    assert _run(_track_args(["menu:album"])) == {}
    assert _run(_track_args(["menu:artist"])) == {}
    assert _run(_track_args(["menu:genre"])) == {}


# ---------------------------------------------------------------------------
# Dispatch — end to end through slim.request
# ---------------------------------------------------------------------------


def test_slim_request_dispatch_reaches_contextmenu(_lib_db, monkeypatch):
    """The real JSON-RPC entry must route ``contextmenu`` to the new handler
    (this used to fall through to the CLI passthrough and answer {})."""
    import lyrion.player.manager as manager_mod

    monkeypatch.setattr(
        manager_mod, "PlayerManager",
        lambda: _FakePM(_FakePlayer([10])))

    api = JSONRPCAPI()
    command: list = ["contextmenu", 0, 200, "playlist_index:0", "menu:track"]
    res = asyncio.run(api._slim_request(PLAYER, command))

    assert res["window"] == {"windowStyle": "text_list"}
    assert res["item_loop"]
    assert [it["text"] for it in res["item_loop"][:3]] == \
        ["Am Ende hinzufügen", "Als nächstes wiedergeben", "Wiedergabe"]
    # unknown menu / no menu still answer {}
    assert asyncio.run(api._slim_request(
        PLAYER, ["contextmenu", 0, 200, "playlist_index:0",
                 "menu:bogus"])) == {}
    assert asyncio.run(api._slim_request(
        PLAYER, ["contextmenu", 0, 200, "playlist_index:0"])) == {}


def test_dispatch_without_player_is_empty():
    """No registered player → the playlist context cannot resolve → {}."""
    assert _run(_track_args(["playlist_index:0", "menu:track"])) == {}


def test_menu_names_map_to_their_info_feeds():
    """Only the info feeds Perl dispatches to exist; everything else is a bad
    dispatch → {} (live: ``menu:trackinfo``/``menu:albuminfo`` → {})."""
    assert contextmenu._MENU_INFO == {
        "track": "trackinfo", "album": "albuminfo",
        "artist": "artistinfo", "genre": "genreinfo"}
    for bogus in ("trackinfo", "albuminfo", "artistinfo", "genreinfo",
                  "playlist", "year", "bogus"):
        assert _run(_track_args(["playlist_index:0", f"menu:{bogus}"]),
                    playlist=[10]) == {}


def test_perl_empty_flag_matches_fixture_regex():
    """Guard the documented divergence list: a fixture recorded with
    ``result == {}`` must be one of the EMPTY_CASES."""
    for name in PROBES:
        result = _fixture(name)["result"]
        if result == {}:
            assert name in EMPTY_CASES, name
        else:
            assert re.fullmatch(r"[0-9a-f_:]+", PLAYER)
            assert isinstance(result, dict) and result
