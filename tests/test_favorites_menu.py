"""P1 — Favorites menus must answer in Perl's jive shape.

Symptom: tapping a favourite radio station plays the stream (server:
``mode=play``, ICY title) but SqueezePlay never switches to the
"Aktueller Titel" (Now Playing) screen.

Cause (Perl reference, **read-only**): the screen switch is decided
*client-side* from the browse response the client already holds — never
from the answer to the play command:

* ``share/jive/applets/SlimBrowser/SlimBrowserApplet.lua:1774-1779``
  maps the tap's ``go`` action through ``_actionAliasMap`` onto
  ``item.goAction``.  Perl puts ``goAction: "play"`` on every
  touch-to-play item (``Slim/Control/XMLBrowser.pm:1259-1267``).
* ``:1891-1922`` then picks ``base.actions.play`` for
  ``actionName == 'play'`` (the item itself carries no ``actions``).
* ``:1924-1928`` resolves ``nextWindow``: item action > base action >
  item > base.
* ``:2044-2048`` ``nextWindow == 'nowPlaying'`` → ``_goNowPlaying()``.
  That field lives in ``base.actions.play.nextWindow`` of the **items**
  response (``XMLBrowser.pm:956-958`` passes ``nowPlaying`` to
  ``_makeAction``).

Fixtures under ``tests/fixtures/perl_favorites_*.json`` are **word-for-
word answers of the live Perl LMS** (public/9.2, player
``24:0a:c4:29:77:90``) — read-only ``slim.request`` probes against
``http://192.168.1.90:9000/jsonrpc.js``.  Each file records the exact
request it answers in ``params``.  The requests were:

* ``perl_favorites_items_usecontextmenu.json``
  ``["favorites","items",0,200,"menu:favorites","useContextMenu:1"]``
* ``perl_favorites_items_menu_favorites.json``
  ``["favorites","items",0,200,"menu:favorites"]``
* ``perl_favorites_items_subfolder.json``
  ``["favorites","items",0,200,"menu:favorites","useContextMenu:1",
  "item_id:<sid>.0"]`` — the sub-folder holds *only* streams, which is
  the ``$allTouchToPlay`` branch (``XMLBrowser.pm:1429-1431``).
* ``perl_favorites_items_plain.json``
  ``["favorites","items",0,200]`` — no ``menu:`` → Perl's *classic*
  ``loop_loop`` item shape (``XMLBrowser.pm:1378-1410``).

What is deliberately **not** executed against the live Perl server: the
tap itself (``favorites playlist play …``).  That is a play command and
the server must never be touched that way; the request tokens are derived
from ``SlimBrowserApplet.lua:704-767`` (``_performJSONAction``) instead.

Documented deviations from Perl (our data model forces them):

* ``icon-id`` / ``presetParams.icon``: Perl proxies the station logo from
  the OPML ``image``/``icon`` attribute.  Our ``favorites`` table has no
  icon column, so both fall back to ``html/images/favorites.png``.
* ``base.actions.playControl.cmd`` is ``["favorites","items"]`` verbatim
  from Perl — we do not recompute it from the request tokens.
* ``defeatDestructiveTouchToPlay`` (``base.go = base.playControl`` for
  old clients) is not implemented; only the ``base.go = base.play``
  branch of ``XMLBrowser.pm:1430`` is.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from lyrion.web.api import JSONRPCAPI

FIXTURES = Path(__file__).resolve().parent / "fixtures"
USE_CM_FIXTURE = "perl_favorites_items_usecontextmenu.json"
MENU_ONLY_FIXTURE = "perl_favorites_items_menu_favorites.json"
SUBFOLDER_FIXTURE = "perl_favorites_items_subfolder.json"
PLAIN_FIXTURE = "perl_favorites_items_plain.json"

MENU_ARGS = ["items", 0, 200, "menu:favorites", "useContextMenu:1"]
PLAIN_ARGS = ["items", 0, 200]

# ── our temp favourites tree (NOT Perl data) ──────────────────────────────
# Root: index 0 = folder "Chill", index 1 = stream.  "Chill" holds only
# streams, i.e. Perl's $allTouchToPlay branch.
FOLDER_ID, STREAM_ID = 1, 2
SUB_IDS = (3, 4)


class _Favs:
    """Minimal FavoritesManager stand-in (pattern: tests/test_alarms.py)."""

    TREE: dict[Any, list[dict]] = {
        None: [
            {"id": FOLDER_ID, "title": "Chill", "url": None, "type": "folder",
             "parent_id": None, "position": 0},
            {"id": STREAM_ID, "title": "1Mix Radio EDM Stream",
             "url": "http://opml.radiotime.com/Tune.ashx?id=s355203",
             "type": "stream", "parent_id": None, "position": 1},
        ],
        FOLDER_ID: [
            {"id": SUB_IDS[0], "title": "Hirschmilch Chillout",
             "url": "http://relay1.hirschmilch.de:7000/chillout.mp3",
             "type": "stream", "parent_id": FOLDER_ID, "position": 0},
            {"id": SUB_IDS[1], "title": "Absolut relax",
             "url": "http://absolut-relax.live-sm.absolutradio.de/absolut-relax",
             "type": "stream", "parent_id": FOLDER_ID, "position": 1},
        ],
    }

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []

    async def list_items(self, parent_id=None):
        return [dict(x) for x in self.TREE.get(parent_id, [])]

    async def resolve_path(self, path):
        """Mirror of FavoritesManager.resolve_path.

        Walks the id ('<sid>.0', '<sid>.0.1', legacy '0.0') index by index
        through the sorted child lists and returns the DB id of the last
        element.  The leading crumb is Perl's browse-session handle
        (``getSID``, XMLBrowser.pm:1739-1741) — our old root ``0`` is still
        accepted.
        """
        from lyrion.music.favorites import _is_session_root

        crumbs = [c for c in str(path).split(".") if c]
        if crumbs and (_is_session_root(crumbs[0]) or crumbs[0] == "0"):
            crumbs = crumbs[1:]
        if not crumbs:
            return None
        try:
            parts = [int(p) for p in crumbs]
        except ValueError:
            return None
        parent: int | None = None
        for idx in parts:
            items = await self.list_items(parent)
            if idx < 0 or idx >= len(items):
                return None
            parent = int(items[idx]["id"])
        return parent

    async def play(self, player_id, fav_id):
        self.played.append((player_id, fav_id))
        return True


@pytest.fixture()
def favs(monkeypatch):
    fake = _Favs()
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: fake)
    return fake


def _items(args: list[Any], pid: str = "1c:87:2c:47:fc:36") -> dict:
    """``args`` are the full sub-command tokens (`favorites items …`)."""
    rest = [str(a) for a in args]
    assert rest[0] == "items", rest
    async def run():
        return await JSONRPCAPI()._json_favorites_items(pid, rest[1:])

    return asyncio.run(run())


def _perl(name: str) -> dict:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["result"], f"{name} carries no result"
    return data


def _perl_result(name: str) -> dict:
    return _perl(name)["result"]


def _perl_sid(name: str) -> str:
    """Perl's item_id prefix — a per-request ``createUUID`` session id.

    ``XMLBrowser.pm:341-353`` (``getSID``/``createUUID``): every fresh
    ``favorites items`` request gets a new 8-hex prefix
    (``Slim/Utils/Misc.pm:1557-1560``), so the ids inside a fixture are
    meaningless across requests.  We now hand out a handle of exactly that
    shape too (``lyrion.web.api._new_fav_sid``) — the value differs per
    request, the *form* must not.
    """
    for it in _perl_result(name)["item_loop"]:
        go = (it.get("actions") or {}).get("go")
        if go:
            return go["params"]["item_id"].split(".")[0]
        if it.get("params"):
            return it["params"]["item_id"].split(".")[0]
    raise AssertionError(f"{name}: no item_id found")


def _ours_sid(res: dict) -> str:
    """The 8-hex session handle of OUR answer ('<sid>.<index>')."""
    for it in res["item_loop"]:
        go = (it.get("actions") or {}).get("go")
        item_id = (go["params"]["item_id"] if go
                   else (it.get("params") or {}).get("item_id") or it.get("id"))
        return str(item_id).split(".")[0]
    raise AssertionError("no item_id found")


SID_RE = re.compile(r"^[a-f0-9]{8}$")


def _folder_item(res: dict, idx: int = 0) -> dict:
    it = res["item_loop"][idx]
    assert "actions" in it, f"item {idx} is not a folder item: {sorted(it)}"
    return it


def _audio_item(res: dict) -> dict:
    for it in res["item_loop"]:
        if it.get("goAction") == "play":
            return it
    raise AssertionError("no touch-to-play item in the response")


# ── 0. the fixtures really answer what this test sends ────────────────────


def test_fixture_requests_are_recorded():
    """Every Perl fixture must name the request it is the answer to."""
    for name, args in ((USE_CM_FIXTURE, MENU_ARGS),
                       (MENU_ONLY_FIXTURE, MENU_ARGS[:4]),
                       (PLAIN_FIXTURE, PLAIN_ARGS)):
        recorded = [str(t) for t in _perl(name)["params"][1]]
        assert recorded == ["favorites"] + [str(a) for a in args], \
            f"{name} answers {recorded}"

    sub = _perl(SUBFOLDER_FIXTURE)
    recorded = [str(t) for t in sub["params"][1]]
    sid = _perl_sid(SUBFOLDER_FIXTURE)
    assert recorded[:4] == ["favorites", "items", "0", "200"]
    assert recorded[4:] == ["menu:favorites", "useContextMenu:1",
                            f"item_id:{sid}.0"]


# ── 1. top level: base + window must be there ─────────────────────────────


def test_menu_mode_top_level_keys_match_perl(favs):
    perl = _perl_result(USE_CM_FIXTURE)
    ours = _items(MENU_ARGS)
    assert set(ours) == set(perl), (
        "menu-mode response must carry exactly Perl's top-level keys "
        "(base/window included)")


def test_menu_mode_omits_count_loop_and_keeps_offset(favs):
    ours = _items(MENU_ARGS)
    assert ours["offset"] == 0
    assert ours["count"] == len(ours["item_loop"]) == 2
    assert ours["title"] == "Favorites"


def test_window_is_home_menu(favs):
    """Perl: ``$hasImage`` → ``windowStyle: home_menu`` (XMLBrowser.pm:1434-1442)."""
    assert _items(MENU_ARGS)["window"] == {"windowStyle": "home_menu"}
    assert _perl_result(USE_CM_FIXTURE)["window"] == {"windowStyle": "home_menu"}


def test_plain_listing_keeps_classic_shape(favs):
    """Without ``menu:`` Perl answers the classic loop (no base/window)."""
    ours = _items(PLAIN_ARGS)
    assert set(_perl_result(PLAIN_FIXTURE)) == {"count", "loop_loop", "title"}
    assert "base" not in ours and "window" not in ours
    assert "loop_loop" in ours and "item_loop" in ours
    folder = _folder_item(ours, 0)
    for key in ("id", "name", "isaudio", "hasitems"):
        assert key in folder, f"classic shape lost '{key}'"


# ── 2. folder items ───────────────────────────────────────────────────────


def test_folder_item_key_set_matches_perl(favs):
    extra = {"icon-id", "text", "addAction", "actions"}
    assert set(_folder_item(_items(MENU_ARGS))) == extra
    assert set(_folder_item(_perl_result(USE_CM_FIXTURE))) == extra


def test_folder_item_go_action_navigates_with_item_id(favs):
    perl_id = _perl_sid(USE_CM_FIXTURE)
    perl = _folder_item(_perl_result(USE_CM_FIXTURE))["actions"]["go"]
    res = _items(MENU_ARGS)
    ours = _folder_item(res)["actions"]["go"]
    assert ours["cmd"] == perl["cmd"] == ["favorites", "items"]
    assert set(ours["params"]) == set(perl["params"]) == {"menu", "item_id"}
    assert ours["params"]["menu"] == perl["params"]["menu"] == "favorites"
    # both sides answer with '<sid>.0' (one browse-session handle per answer)
    ours_id = _ours_sid(res)
    assert SID_RE.match(ours_id), ours_id
    assert ours["params"]["item_id"] == f"{ours_id}.0"
    assert perl["params"]["item_id"] == f"{perl_id}.0"
    assert _folder_item(_items(MENU_ARGS))["addAction"] == "go"
    assert "player" not in ours, "Perl's base go has no player key"


def test_folder_tap_opens_the_subfolder(favs):
    """The folder item_id must resolve to the folder's children."""
    res = _items(MENU_ARGS)
    fid = _folder_item(res)["actions"]["go"]["params"]["item_id"]
    assert fid == f"{_ours_sid(res)}.0"
    sub = _items(["items", 0, 200, "menu:favorites",
                  "useContextMenu:1", f"item_id:{fid}"])
    assert [it["text"] for it in sub["item_loop"]] == [
        "Hirschmilch Chillout", "Absolut relax"]


# ── 3. touch-to-play items ────────────────────────────────────────────────


def test_audio_item_key_set_matches_perl(favs):
    ours = _audio_item(_items(MENU_ARGS))
    perl = _audio_item(_perl_result(USE_CM_FIXTURE))
    # icon-id / presetParams.icon differ on purpose (no logo in our model)
    assert set(ours) == set(perl), "touch-to-play item key set"


def test_audio_item_is_touch_to_play(favs):
    """XMLBrowser.pm:1259-1267 — the fields that make the tap a play."""
    ours = _audio_item(_items(MENU_ARGS))
    perl = _audio_item(_perl_result(USE_CM_FIXTURE))
    for key in ("goAction", "style", "type"):
        assert ours[key] == perl[key], key
    assert ours["goAction"] == "play"
    assert ours["style"] == "itemplay"
    assert ours["type"] == "audio"


def test_audio_item_params_match_perl(favs):
    """XMLBrowser.pm:1139-1147/1259-1263 — item_id + touchToPlay + isContextMenu."""
    res = _items(MENU_ARGS)
    ours = _audio_item(res)
    perl = _audio_item(_perl_result(USE_CM_FIXTURE))
    assert set(ours["params"]) == set(perl["params"]) == {
        "item_id", "touchToPlay", "touchToPlaySingle", "isContextMenu"}
    assert ours["params"]["touchToPlaySingle"] == 1
    assert ours["params"]["isContextMenu"] == 1
    ours_id = _ours_sid(res)
    assert SID_RE.match(ours_id), ours_id
    assert ours["params"]["item_id"] == ours["params"]["touchToPlay"]
    assert ours["params"]["item_id"] == f"{ours_id}.1"
    assert perl["params"]["item_id"] == perl["params"]["touchToPlay"]


def test_audio_item_carries_preset_params(favs):
    """required by the ``set-preset-*`` base actions (_jivePresetBase)."""
    ours = _audio_item(_items(MENU_ARGS))
    perl = _audio_item(_perl_result(USE_CM_FIXTURE))
    assert set(ours["presetParams"]) == set(perl["presetParams"])
    assert ours["presetParams"]["favorites_type"] == "audio"
    assert ours["presetParams"]["favorites_title"] == ours["text"]
    assert ours["presetParams"]["favorites_url"] == (
        "http://opml.radiotime.com/Tune.ashx?id=s355203")
    # deviation, documented: no logo column → favourites placeholder icon
    assert ours["icon-id"] == "html/images/favorites.png"


def test_audio_items_have_no_item_actions(favs):
    """Perl serves the play command from base.actions.play, not the item."""
    assert "actions" not in _audio_item(_items(MENU_ARGS))
    assert "actions" not in _audio_item(_perl_result(USE_CM_FIXTURE))


# ── 4. base actions ───────────────────────────────────────────────────────


def test_base_action_key_set_matches_perl(favs):
    perl = _perl_result(USE_CM_FIXTURE)["base"]["actions"]
    ours = _items(MENU_ARGS)["base"]["actions"]
    assert set(ours) == set(perl)
    assert {f"set-preset-{n}" for n in range(10)} <= set(ours)


def test_base_action_bodies_match_perl(favs):
    perl = _perl_result(USE_CM_FIXTURE)["base"]["actions"]
    ours = _items(MENU_ARGS)["base"]["actions"]
    for key in ("go", "add", "add-hold", "play", "more", "playControl"):
        assert ours[key] == perl[key], f"base.actions.{key}"


def test_base_go_stays_items_when_a_folder_is_listed(favs):
    """Root has a folder → $allTouchToPlay = 0 (XMLBrowser.pm:1256/1275)."""
    ours = _items(MENU_ARGS)["base"]["actions"]["go"]
    assert ours["cmd"] == ["favorites", "items"]
    assert "nextWindow" not in ours


# ── 5. THE screenswitch ───────────────────────────────────────────────────


def test_base_play_carries_nowplaying_nextwindow(favs):
    """SlimBrowserApplet.lua:2044-2048 — this switches to "Aktueller Titel".

    Precedence (``:1924-1928``): item action > base action > item > base.
    A touch-to-play favourite carries no ``actions``, so the base action's
    ``nextWindow`` is what the client sees.
    """
    perl = _perl_result(USE_CM_FIXTURE)["base"]["actions"]["play"]
    ours = _items(MENU_ARGS)["base"]["actions"]["play"]
    assert ours["nextWindow"] == perl["nextWindow"] == "nowPlaying"
    assert ours == perl


def test_nextwindow_is_reachable_for_a_touch_to_play_item(favs):
    """Replay SlimBrowserApplet.lua:1846-1928 for the item we emit."""
    item = _audio_item(_items(MENU_ARGS))
    base = _items(MENU_ARGS)["base"]["actions"]
    # :1846-1853 'go' → item.goAction
    action_name = item.get("goAction") or "go"
    assert action_name == "play"
    # :1891-1922 an item 'actions.play' would win — Perl/we ship none
    assert "actions" not in item
    action = base[action_name]
    assert action["nextWindow"] == "nowPlaying"
    # :1924-1928 + :2044
    assert action["itemsParams"] == "params" and item["params"]


def test_subfolder_only_streams_rewrites_base_go(favs):
    """XMLBrowser.pm:1429-1431 — all touch-to-play ⇒ base.go = base.play."""
    sub = _items(["items", 0, 200, "menu:favorites", "useContextMenu:1",
                  "item_id:0.0"])
    perl = _perl_result(SUBFOLDER_FIXTURE)["base"]["actions"]
    assert perl["go"] == perl["play"], "Perl fixture expectation"
    ours = sub["base"]["actions"]
    assert ours["go"] == ours["play"]
    assert ours["go"]["nextWindow"] == "nowPlaying"
    assert sub["window"] == _perl_result(SUBFOLDER_FIXTURE)["window"]


def test_subfolder_top_level_matches_perl(favs):
    sub = _items(["items", 0, 200, "menu:favorites", "useContextMenu:1",
                  "item_id:0.0"])
    perl = _perl_result(SUBFOLDER_FIXTURE)
    assert set(sub) == set(perl)
    assert set(sub["base"]["actions"]) == set(perl["base"]["actions"])


def test_playcontrol_params_echo_the_request(favs):
    """XMLBrowser.pm:973-979 — ``params => $request->getParamsCopy()``."""
    perl = _perl_result(USE_CM_FIXTURE)["base"]["actions"]["playControl"]
    ours = _items(MENU_ARGS)["base"]["actions"]["playControl"]
    assert ours["params"] == perl["params"] == {
        "menu": "favorites", "useContextMenu": "1",
        "_quantity": "200", "_index": "0"}
    assert ours["itemsParams"] == "playControlParams"
    assert ours["window"] == {"isContextMenu": 1}


def test_subfolder_playcontrol_echoes_item_id(favs):
    sub = _items(["items", 0, 200, "menu:favorites", "useContextMenu:1",
                  "item_id:0.0"])
    pc = sub["base"]["actions"]["playControl"]["params"]
    assert pc["item_id"] == "0.0"
    assert pc["menu"] == "favorites"


# ── 6. the command SqueezePlay derives from our answer ────────────────────


def test_tap_yields_the_perl_play_command(favs):
    """Rebuild _performJSONAction (SlimBrowserApplet.lua:704-767) by hand."""
    res = _items(MENU_ARGS)
    item = _audio_item(res)
    action = res["base"]["actions"]["play"]
    params = dict(action["params"])
    params.update(item[action["itemsParams"]])
    tokens = [f"{k}:{v}" for k, v in params.items()]
    request = list(action["cmd"]) + ["0", "200"] + tokens + ["useContextMenu:1"]
    assert request[:3] == ["favorites", "playlist", "play"]
    ours_id = _ours_sid(res)
    assert f"item_id:{ours_id}.1" in request
    assert f"touchToPlay:{ours_id}.1" in request
    assert "menu:favorites" in request and "useContextMenu:1" in request
    # the server must be able to answer it (Perl: cliBrowse, Plugin.pm:747)
    assert request[3:5] == ["0", "200"]


def test_tap_command_plays_the_right_favorite(favs):
    """``favorites playlist play`` (Favorites/Plugin.pm:81) must resolve the
    hierarchical item_id we hand out — not fall back to the current index."""
    async def run():
        return await JSONRPCAPI()._slim_request("1c:87:2c:47:fc:36", [
            "favorites", "playlist", "play", "0", "200", "menu:favorites",
            "item_id:0.1", "touchToPlay:0.1", "touchToPlaySingle:1",
            "isContextMenu:1", "useContextMenu:1"])

    asyncio.run(run())
    assert favs.played and favs.played[-1][1] == STREAM_ID


# ── 7. menu: is only a "menu mode" switch ─────────────────────────────────


@pytest.mark.parametrize("menu", ["menu:favorites", "menu:1", "menu:radio",
                                  "menu:apps"])
def test_any_menu_value_yields_the_perl_menu_shape(favs, menu):
    """Favorites/Plugin.pm:756 — only ``defined($menu)`` matters."""
    res = _items(["items", 0, 200, menu, "useContextMenu:1"])
    assert set(res["base"]["actions"]) == \
        set(_perl_result(USE_CM_FIXTURE)["base"]["actions"])
    assert "actions" in _folder_item(res)
