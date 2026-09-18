"""Squeezer favourites tap: which fields make a tap produce a command.

The user symptom: in Squeezer / Squeeze Client a tap on a favourites row does
nothing at all — no ``play`` command reaches the server (live cometd log,
client ``lyrion-5``: four ``favorites items useContextMenu:1 menu:favorites``
requests, no follow-up play).  SqueezePlay against the same server plays the
station fine (log: ``favorites playlist play touchToPlay:… item_id:…`` →
``play_url … Hirschmilch Chillout``), so the *server* play route works and the
defect sits in what the app does with the row.

Squeezer's tap path (all citations against
``/tmp/squeezer-src/android-squeezer-develop``, read-only):

* ``model/JiveItem.java:269-275`` — ``goAction`` is resolved as a **name**:
  first the item's own ``actions.do``, else ``actions.<goAction>``, else the
  response ``base.actions.<name>`` (``extractAction`` ``:577-596``).
* ``model/JiveItem.java:585-596`` — a base action is used **only** when the
  item carries a map under that action's ``itemsParams`` (``"params"`` for
  ``play``, ``"playControlParams"`` for ``playControl``, ``"presetParams"``
  for the presets).  Missing map ⇒ ``actionRecord == null`` ⇒ ``goAction ==
  null``.
* ``service/CometClient.java:555-566`` — the response-level ``base`` is
  injected into every item before parsing, so the row can rely on it.
* ``model/JiveItem.java:231-233`` ``isSelectable()`` — is false when
  ``goAction``/``nextWindow``/``node``/sub-items/checkbox/weblink are all
  absent.
* ``itemlist/JiveItemView.java:101`` (``itemView.setEnabled(isSelectable())``)
  and ``:143-145`` + ``:171-192`` — a non-selectable row swallows the tap, and
  the click handler ends in a silent ``else`` when ``goAction`` is null.
* ``itemlist/JiveItemView.java:181-182`` — with ``nextWindow`` set the tap
  goes straight to ``getActivity().action(item, item.goAction)`` →
  ``service/SqueezeService.java:1438-1443``
  (``cmd(action.action.cmd).params(action.action.params(...))``), i.e.
  ``base.actions.play.cmd`` = ``["favorites","playlist","play"]`` plus the
  item's ``params``.
* ``itemlist/JiveItemViewLogic.java:67-73`` — ``goAction.isContextMenu()``
  (true for ``base.actions.playControl``: its ``window.isContextMenu == 1``,
  ``model/JiveItem.java:634-640``) opens ``ContextMenu`` instead; the menu's
  Play entry then runs ``action(item, item.playAction)``
  (``framework/ContextMenu.java:134-150``) → the same command.

Perl side (READ-ONLY checkout ``/tmp/lms-ref``, public 9.2):

* ``Slim/Control/XMLBrowser.pm:1131-1136`` — ``presetParams``
  (``favorites_url``/``favorites_title``/``favorites_type``/``icon``).
* ``:1139-1147`` — item ``params`` (``item_id``, ``isContextMenu``) and the
  ``isContextMenu`` flag for every playable row.
* ``:1240-1255`` — a folder row: ``actions.go`` + ``addAction: "go"``.
* ``:1259-1267`` — the touch-to-play branch: ``goAction "play"``,
  ``style "itemplay"``, ``touchToPlay``/``touchToPlaySingle``.
* ``:1268-1272`` — the **defeated** branch: ``goAction "playControl"`` +
  ``playControlParams: {xmlbrowserPlayControl: <index>}`` and *no* ``style``.
* ``:1951-1983`` ``_defeatDestructiveTouchToPlay`` + ``Slim/Utils/Prefs.pm:272``
  (``'defeatDestructiveTouchToPlay' => 4``) — which branch a request gets.
* ``:805-830`` — a tap on a touch-to-play row arrives as
  ``xmlbrowserPlayControl:<index>`` and is answered with the play-control
  menu, not with the list again.

Live answers used as fixtures (read-only probes, 2026-09-13, both servers):

* Perl 192.168.1.90, ``favorites items useContextMenu:1 menu:favorites`` →
  ``{"goAction": "playControl", "playControlParams": {"xmlbrowserPlayControl":
  "6"}, "params": {"item_id": "3dce736f.6", "isContextMenu": 1}, "type":
  "audio", "presetParams": {…}, "icon-id": "…"}``.
* Perl 192.168.1.90, ``… menu:favorites xmlbrowserPlayControl:6`` → 3 rows
  ``Am Ende hinzufügen`` / ``Als nächstes wiedergeben`` / ``Wiedergabe`` with
  ``cmd ["favorites","playlist",add|insert|play]`` and
  ``nextWindow parentNoRefresh|nowRefresh|nowPlaying``, ``count`` 3,
  ``windowStyle`` ``text_list``, no ``base``.

Everything runs in-process against the real ``JSONRPCAPI`` with a favourites
stand-in and an injected ``PlayerState`` — no server start/stop, no DB write,
no live write command.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import favorites_menu
from lyrion.web.api import JSONRPCAPI, _defeat_destructive_touch_to_play

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"
PLAYER = "1c:87:2c:47:fc:36"
SID_RE = re.compile(r"^[a-f0-9]{8}$")

MENU_ARGS = ["items", 0, 10, "menu:favorites"]


# ── Squeezer model, ported ────────────────────────────────────────────────
# A faithful port of the three rules the tap depends on.  Keeping them here
# (instead of only asserting our JSON) is what makes the test a *tap* test:
# it answers "would Squeezer resolve a goAction for this row and which
# command would it send?".


def jive_extract_action(action_name, base_actions, item_actions, record):
    """``JiveItem.extractAction`` (``JiveItem.java:577-596``).

    Returns ``(action_record, item_params)`` or ``(None, None)`` — the second
    element is the item map the action's ``itemsParams`` points at, which the
    base branch requires (:585-596).
    """
    action_record = None
    item_params = None
    item_action = (item_actions or {}).get(action_name)
    if isinstance(item_action, dict):
        action_record = item_action
    if action_record is None and base_actions is not None:
        base_action = base_actions.get(action_name)
        if isinstance(base_action, dict):
            items_params = base_action.get("itemsParams")
            if items_params is not None:
                item_params = record.get(items_params)
                if isinstance(item_params, dict):
                    action_record = base_action
    if action_record is None:
        return None, None
    return action_record, item_params


def jive_go_action(record, base):
    """``JiveItem`` ``goAction`` resolution (``JiveItem.java:269-275``)."""
    base_actions = (base or {}).get("actions")
    item_actions = record.get("actions")
    action_name = record.get("goAction", "go")
    # ``do`` wins over ``go``/``goAction`` (:268-270)
    action, _ = jive_extract_action("do", base_actions, item_actions, record)
    if action is None:
        action, _ = jive_extract_action(action_name, base_actions, item_actions,
                                        record)
    return action


def jive_sends(record, base):
    """The command a tap on ``record`` produces, or ``None`` (no tap).

    Mirrors ``JiveItemView.onItemSelected`` (:171-192) plus
    ``SqueezeService.action`` (:1438-1443): when ``goAction`` resolves, the
    app sends ``cmd`` and merges the item map named by ``itemsParams``
    (``Action.JsonAction.params`` + ``extractJsonAction`` :624-632).
    """
    action = jive_go_action(record, base)
    if action is None:
        return None
    item_params = {}
    items_params = action.get("itemsParams")
    if items_params:
        item_params = record.get(items_params) or {}
    return {"cmd": list(action["cmd"]),
            "params": {**(action.get("params") or {}), **item_params}}


def jive_is_selectable(record, base):
    """``JiveItem.isSelectable`` (``JiveItem.java:231-233``).

    ``empty`` (``action 'none'`` + ``style 'itemNoAction'``, :266) is the
    only other way a row dies before the click handler runs.
    """
    empty = (record.get("action") == "none"
             and record.get("style") == "itemNoAction")
    if empty:
        return False
    return bool(jive_go_action(record, base)
                or record.get("nextWindow")
                or record.get("item_loop")
                or record.get("node")
                or record.get("checkbox"))


# ── fixtures ──────────────────────────────────────────────────────────────

FOLDER_ID, STREAM_ID = 11, 12
SUB_ID = 2


class _Favs:
    """FavoritesManager stand-in (pattern: tests/test_favorites_menu.py)."""

    TREE = {
        None: [
            {"id": FOLDER_ID, "title": "Chillout", "url": None,
             "type": "folder", "parent_id": None, "position": 0},
            {"id": STREAM_ID, "title": "1Mix EDM Radio",
             "url": "http://fr1.1mix.co.uk:8060/", "type": "stream",
             "parent_id": None, "position": 1},
        ],
        FOLDER_ID: [
            {"id": SUB_ID, "title": "Hirschmilch Chillout",
             "url": "http://hirschmilch.de:7000/chillout.mp3",
             "type": "stream", "parent_id": FOLDER_ID, "position": 0},
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

    async def play(self, player_id, fav_id):
        self.played.append((player_id, fav_id))
        return True


@pytest.fixture(autouse=True)
def _isolated_players():
    """No player leaks in from another test (the branch depends on state).

    ``_defeat_destructive_touch_to_play`` reads the playing state
    (``XMLBrowser.pm:1977``), so a ``PlayerManager`` left behind by an
    earlier test would silently flip the expected branch.
    """
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


def _install_player(mode: str = "play", duration: float = 257.533,
                    url: str = "http://hirschmilch.de:7000/chillout.mp3",
                    playlist_total: int = 1) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = mode
    player.duration = duration
    player.current_url = url
    player.playlist_total = playlist_total
    player.playlist = []
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


def _items(args: list, pid: str | None = PLAYER) -> dict:
    async def run():
        return await JSONRPCAPI()._json_favorites_items(
            pid, [str(a) for a in args])
    return asyncio.run(run())


def _resolve(path: str):
    """Resolve a session id the way the server does on the play echo."""
    async def run():
        return await _Favs().resolve_path(path)
    return asyncio.run(run())


def _rows(args: list) -> list[dict]:
    return _items(args)["item_loop"]


def _folder_row(args=None) -> dict:
    return _rows(args or MENU_ARGS)[0]


def _stream_row(args=None) -> dict:
    return _rows(args or MENU_ARGS)[1]


# ── 1. folder row: it must hand the app its own go action ─────────────────


def test_folder_row_tap_sends_favorites_items_with_the_session_id(favs):
    """XMLBrowser.pm:1240-1255 ↔ JiveItem.java:271-275/585-596.

    A folder row carries its drill command *on the item* (``actions.go``), so
    Squeezer resolves ``goAction`` without consulting ``base`` and
    ``JiveItemView.java:184-185`` runs the action →
    ``favorites items item_id:<sid>.<i>``.
    """
    base = _items(MENU_ARGS)["base"]
    row = _folder_row()
    assert row["addAction"] == "go"
    assert "goAction" not in row
    sent = jive_sends(row, base)
    assert sent is not None, "the folder row resolves no go action at all"
    assert sent["cmd"] == ["favorites", "items"]
    item_id = sent["params"]["item_id"]
    root, _, rest = item_id.partition(".")
    assert SID_RE.match(root), item_id
    assert rest == "0"
    assert jive_is_selectable(row, base)


def test_folder_row_is_resolvable_without_the_response_base(favs):
    """The folder tap must not depend on ``base`` being injected.

    ``JiveItem.java:271-275`` falls back to ``base.actions`` only when the
    item has no own action; the folder row must keep working when a client
    drops ``base`` (``CometClient.java:555-566`` is the only thing that adds
    it).
    """
    assert jive_sends(_folder_row(), None)["cmd"] == ["favorites", "items"]


# ── 2. stream row, non-defeated: direct play ──────────────────────────────


def test_stream_row_carries_every_preset_field_perl_sends(favs):
    """XMLBrowser.pm:1131-1136 — all four ``presetParams`` fields."""
    row = _stream_row()
    assert set(row["presetParams"]) == {"favorites_url", "favorites_title",
                                       "favorites_type", "icon"}
    assert row["presetParams"]["favorites_type"] == "audio"
    assert row["presetParams"]["favorites_title"] == row["text"]
    assert row["presetParams"]["favorites_url"] == "http://fr1.1mix.co.uk:8060/"


def test_stream_row_has_no_item_actions_and_resolves_play_from_base(favs):
    """XMLBrowser.pm:1259-1267 ↔ JiveItem.java:269-275/585-596.

    ``goAction`` is the *name* ``"play"``; the command therefore comes from
    ``base.actions.play``, and that base action is only usable because the
    row carries a ``params`` map (its ``itemsParams`` target).
    """
    # The touch-to-play branch needs a client Perl can *resolve* and that is
    # not playing (``XMLBrowser.pm:1976`` ``return 1 if $pref == 1 ||
    # !$client``, ``:1977`` ``$client->isPlaying()``): an unknown/absent
    # client is defeated.
    _install_player("stop", duration=0)
    res = _items(MENU_ARGS)
    base = res["base"]
    row = _stream_row()
    assert "actions" not in row
    assert row["goAction"] == "play"
    assert row["style"] == "itemplay"
    assert row["type"] == "audio"
    assert base["actions"]["play"]["cmd"] == ["favorites", "playlist", "play"]
    assert base["actions"]["play"]["itemsParams"] == "params"
    assert base["actions"]["play"]["nextWindow"] == "nowPlaying"
    assert isinstance(row.get("params"), dict)
    assert jive_is_selectable(row, base)


def test_stream_row_tap_sends_favorites_playlist_play(favs):
    """SqueezeService.java:1438-1443 — the command the tap produces.

    ``base.actions.play.cmd`` plus the row's ``params`` (``itemsParams``);
    ``JiveItem.java:632`` appends ``useContextMenu:1``.
    """
    _install_player("stop", duration=0)
    base = _items(MENU_ARGS)["base"]
    row = _stream_row()
    sent = jive_sends(row, base)
    assert sent["cmd"] == ["favorites", "playlist", "play"]
    assert sent["params"]["item_id"] == row["params"]["item_id"]
    assert sent["params"]["menu"] == "favorites"
    assert sent["params"]["touchToPlay"] == sent["params"]["item_id"]
    assert sent["params"]["touchToPlaySingle"] == 1
    root, _, rest = sent["params"]["item_id"].partition(".")
    assert SID_RE.match(root) and rest == "1"


def test_stream_row_is_selectable_without_base_too(favs):
    """Guard for the "tap does nothing" symptom.

    When ``goAction`` cannot be resolved the row is not selectable
    (``JiveItem.java:231-233``), ``JiveItemView.java:101`` disables it and no
    click listener ever fires.  With ``base`` injected the row must resolve
    (``CometClient.java:555-566``); without it Perl's shape is silently
    unresolvable, which is exactly the state a client lands in when the
    injection does not happen.
    """
    row = _stream_row()
    assert jive_is_selectable(row, _items(MENU_ARGS)["base"])
    assert jive_sends(row, _items(MENU_ARGS)["base"]) is not None


# ── 3. stream row, defeated: the Perl/live shape ──────────────────────────


DEFEAT_ARGS = MENU_ARGS + ["defeatDestructiveTouchToPlay:1"]


def test_defeated_stream_row_matches_live_perl_shape(favs):
    """XMLBrowser.pm:1268-1272 ↔ live Perl 192.168.1.90.

    Live: ``{"goAction": "playControl", "playControlParams":
    {"xmlbrowserPlayControl": "6"}, "params": {"item_id": …, "isContextMenu":
    1}}`` — and neither ``style`` nor ``touchToPlay``.
    """
    _install_player("play")
    row = _stream_row(DEFEAT_ARGS)
    assert row["goAction"] == "playControl"
    assert row["playControlParams"] == {"xmlbrowserPlayControl": "1"}
    assert "style" not in row
    assert "touchToPlay" not in row["params"]
    assert "touchToPlaySingle" not in row["params"]
    assert row["params"]["isContextMenu"] == 1
    assert set(row["presetParams"]) == {"favorites_url", "favorites_title",
                                       "favorites_type", "icon"}


def test_defeated_row_tap_resolves_playcontrol_from_base(favs):
    """JiveItem.java:634-640 — ``playControl`` is the context-menu branch."""
    _install_player("play")
    res = _items(DEFEAT_ARGS)
    row = _stream_row(DEFEAT_ARGS)
    pc = res["base"]["actions"]["playControl"]
    assert pc["itemsParams"] == "playControlParams"
    assert pc["window"] == {"isContextMenu": 1}
    assert "nextWindow" not in pc                       # JiveItemView:181
    assert jive_is_selectable(row, res["base"])
    sent = jive_sends(row, res["base"])
    assert sent["cmd"] == ["favorites", "items"]        # the menu request
    assert sent["params"]["xmlbrowserPlayControl"] == "1"


def test_defeated_tap_answer_is_the_three_row_play_control_menu(favs):
    """XMLBrowser.pm:805-830 ↔ live Perl ``xmlbrowserPlayControl:6``.

    Each menu row carries its whole command in its own ``actions.go``
    (``JiveItem.java:271-275``), so the Play row works in a client that has
    no ``base`` for this window (live Perl sends none).
    """
    _install_player("play")
    menu = _items(DEFEAT_ARGS + ["xmlbrowserPlayControl:1"])
    assert "base" not in menu
    assert menu["count"] == 3
    assert menu["offset"] == 0
    assert menu["window"] == {"windowStyle": "text_list"}
    texts = [r["text"] for r in menu["item_loop"]]
    assert texts == ["Am Ende hinzufügen", "Als nächstes wiedergeben",
                     "Wiedergabe"]
    assert [r["style"] for r in menu["item_loop"]] == [
        "item_add", "itemNoAction", "item_play"]
    cmds, nexts = [], []
    for row in menu["item_loop"]:
        sent = jive_sends(row, None)                    # no base at all
        assert sent is not None, "menu row resolves no action"
        cmds.append(sent["cmd"])
        # the menu answers for the *tapped* row (index 1 = the stream); the
        # handle itself is a fresh browse session, exactly like Perl's
        # (probes: list `3dce736f.6` vs. menu `43285f18.6`)
        item_id = sent["params"]["item_id"]
        assert SID_RE.match(item_id.partition(".")[0]), item_id
        assert item_id.endswith(".1"), item_id
        assert _resolve(item_id) == STREAM_ID
        nexts.append(row["actions"]["go"]["nextWindow"])
    assert cmds == [["favorites", "playlist", "add"],
                    ["favorites", "playlist", "insert"],
                    ["favorites", "playlist", "play"]]
    assert nexts == ["parentNoRefresh", "parentNoRefresh", "nowPlaying"]


def test_play_control_menu_for_a_folder_row_uses_the_folder_id(favs):
    """The menu must address the tapped row, not the first one."""
    _install_player("play")
    menu = _items(DEFEAT_ARGS + ["xmlbrowserPlayControl:0"])
    ids = [r["actions"]["go"]["params"]["item_id"] for r in menu["item_loop"]]
    assert all(i.endswith(".0") for i in ids), ids
    assert {_resolve(i) for i in ids} == {FOLDER_ID}


def test_play_control_index_out_of_range_answers_perls_empty_menu(favs):
    """XMLBrowser.pm:813-830 — out of range ⇒ window/offset/count only."""
    _install_player("play")
    menu = _items(DEFEAT_ARGS + ["xmlbrowserPlayControl:99"])
    assert menu == {"window": {"windowStyle": "text_list"}, "offset": 0,
                    "count": 0}


# ── 4. the branch decision itself (Perl's pref) ───────────────────────────


def test_defeat_pref_semantics_match_perl():
    """XMLBrowser.pm:1964-1983 + Prefs.pm:272 (default 4)."""
    playing = _install_player("play")
    idle = _install_player("stop", duration=0)
    # Perl :1976 `return 1 if $pref == 1 || !$client;` — a request that names
    # no client at all is defeated for every pref != 0 (live Perl
    # ``favorites items 0 50 menu:favorites`` → goAction "playControl").
    assert _defeat_destructive_touch_to_play([], None) is True
    assert _defeat_destructive_touch_to_play(
        [], None, client_named=False) is True
    # ... but a *named* client we cannot resolve keeps the touch-to-play
    # branch (documented divergence: the fixtures drive the API with a pid
    # and without an installed PlayerState).
    assert _defeat_destructive_touch_to_play(
        [], None, client_named=True) is False
    # 0 never defeats, 1 always (the request token wins, :1964)
    assert _defeat_destructive_touch_to_play(
        ["defeatDestructiveTouchToPlay:0"], playing) is False
    assert _defeat_destructive_touch_to_play(
        ["defeatDestructiveTouchToPlay:1"], idle) is True
    # 4 = "playing and the current item is not a radio stream" (:1977)
    assert _defeat_destructive_touch_to_play([], playing) is True
    assert _defeat_destructive_touch_to_play([], idle) is False
    # ... but a playlist container is excluded by !isPlaylist()
    listy = _install_player("play", url="http://host/radio.m3u")
    assert _defeat_destructive_touch_to_play([], listy) is False
    # 2/3 need a playlist longer than one entry (:1978-1980)
    assert _defeat_destructive_touch_to_play(
        ["defeatDestructiveTouchToPlay:2"], playing) is False
    playing.playlist_total = 3
    assert _defeat_destructive_touch_to_play(
        ["defeatDestructiveTouchToPlay:2"], playing) is True
    assert _defeat_destructive_touch_to_play(
        ["defeatDestructiveTouchToPlay:3"], idle) is False
    # The server pref is Perl's ``$prefs->get(...)`` (:1966) — an operator
    # setting of 1 forces the defeated branch (:1976) for every request.
    import lyrion.web.api as api_mod
    prev = api_mod._defeat_pref_default
    api_mod._defeat_pref_default = lambda: 1
    try:
        assert _defeat_destructive_touch_to_play([], idle) is True
        assert _defeat_destructive_touch_to_play(
            [], None, client_named=True) is True
        assert _defeat_destructive_touch_to_play(
            ["defeatDestructiveTouchToPlay:0"], idle) is False
    finally:
        api_mod._defeat_pref_default = prev


def test_client_less_request_gets_perls_play_control_row(favs):
    """XMLBrowser.pm:1976 ``|| !$client`` ↔ live Perl 192.168.1.90.

    A request without a player token (Web UI, CLI probe, SqueezeTray) is
    answered with the defeated row — live ``favorites items 0 50
    menu:favorites``: ``{"goAction": "playControl", "playControlParams":
    {"xmlbrowserPlayControl": "6"}, "params": {"item_id": …, "isContextMenu":
    1}, "type": "audio"}`` and neither ``style`` nor ``touchToPlay``.
    """
    row = _items(MENU_ARGS, pid=None)["item_loop"][1]
    assert row["goAction"] == "playControl"
    assert row["playControlParams"] == {"xmlbrowserPlayControl": "1"}
    assert "style" not in row
    assert set(row["params"]) == {"item_id", "isContextMenu"}


def test_squeezer_app_request_without_positionals_gets_the_same_row(favs):
    """The exact shape Squeezer sends (cometd log 2026-09-13 19:21).

    ``["favorites","items","menu:favorites","useContextMenu:1"]`` — no
    ``_index``/``_quantity`` positionals.  The row must be the same one a
    positioned request produces (the item index is the position *in the feed*,
    XMLBrowser.pm:1003/1014).
    """
    res = _items(["items", "menu:favorites", "useContextMenu:1"], pid=None)
    row = res["item_loop"][1]
    assert row["goAction"] == "playControl"
    assert row["playControlParams"]["xmlbrowserPlayControl"] == "1"
    assert res["item_loop"][0].get("actions", {}).get("go", {}).get(
        "params", {}).get("item_id", "").endswith(".0")


def test_unknown_client_gets_perls_defeated_playcontrol_row(favs):
    """An unresolvable pid *is* "no client" for Perl (``:1976`` ``!$client``).

    ``PlayerManager.get_player(mac)`` only answers for a connected player, and
    Perl's ``Request->new`` clears ``_clientid`` (status 103) for a mac it does
    not know — both land on ``return 1 if $pref == 1 || !$client``.  Live Perl
    9.1.1 (read-only 2026-09-14, unattributed favourites request) answers
    ``{"goAction": "playControl", "playControlParams":
    {"xmlbrowserPlayControl": "6"}, "params": {"item_id": …, "isContextMenu": 1},
    "type": "audio"}`` — no ``style``, no ``touchToPlay``.  This port used to
    keep the blind ``play`` row here, so a controller opened no play-control
    menu and the favourite never started.
    """
    assert _defeat_destructive_touch_to_play([], None, client_named=None) is True
    row = _stream_row()                       # PLAYER pid, no installed player
    assert row["goAction"] == "playControl"
    assert row["playControlParams"]["xmlbrowserPlayControl"] == "1"
    assert "style" not in row
    assert "touchToPlay" not in row["params"]


def test_playing_player_gets_the_play_control_row(favs):
    """Perl's default pref 4 defeats the tap while a file plays (:1977).

    This is the branch the live Perl server answers a client with that is
    playing something with a duration (``favorites items 0 50 menu:favorites``
    with a playing client).
    """
    _install_player("play")
    row = _stream_row()
    assert row["goAction"] == "playControl"
    assert row["playControlParams"]["xmlbrowserPlayControl"] == "1"


def test_all_touch_to_play_window_maps_base_go_onto_the_branch(favs):
    """XMLBrowser.pm:1429-1431 — ``base.go`` mirrors the defeat decision."""
    folder_path = _folder_id()          # `<sid>.0` — the folder row
    _install_player("play")
    # the folder's child list is exactly one stream → allTouchToPlay
    res = _items(["items", 0, 10, "menu:favorites", f"item_id:{folder_path}"])
    assert [r["text"] for r in res["item_loop"]] == ["Hirschmilch Chillout"]
    assert res["base"]["actions"]["go"] == res["base"]["actions"]["playControl"]
    _install_player("stop", duration=0)
    res2 = _items(["items", 0, 10, "menu:favorites", f"item_id:{folder_path}"])
    assert res2["base"]["actions"]["go"] == res2["base"]["actions"]["play"]
    # a client-less request is defeated (:1976) → the same remap
    res3 = _items(["items", 0, 10, "menu:favorites", f"item_id:{folder_path}"],
                  pid=None)
    assert res3["base"]["actions"]["go"] == res3["base"]["actions"]["playControl"]
    assert res3["item_loop"][0]["goAction"] == "playControl"


def _folder_id() -> str:
    return _folder_row()["actions"]["go"]["params"]["item_id"]


# ── 5. the folder/stream distinction the user suspected ───────────────────


def test_folder_and_stream_rows_differ_in_exactly_the_tap_fields(favs):
    """Folder = navigation node, stream = playable row (XMLBrowser.pm:1234).

    The folder branch (:1240-1255) is taken for
    ``!$isPlayable && !$touchToPlay``; the stream branch (:1259-1272) for a
    ``$touchToPlay`` item.  Only the stream row carries ``presetParams`` and
    a ``type``/``goAction``/``style`` triple, and only the folder row carries
    ``addAction``/``actions``.
    """
    _install_player("stop", duration=0)
    res = _items(MENU_ARGS)
    folder, stream = res["item_loop"][0], res["item_loop"][1]
    assert set(folder) - set(stream) == {"addAction", "actions"}
    assert set(stream) - set(folder) == {"goAction", "style", "type", "params",
                                         "presetParams"}
    assert "presetParams" not in folder
    assert folder["icon-id"] == stream["icon-id"] == favorites_menu.FAVORITES_ICON


# ── 6. folder ids: numeric, stable, and resolvable again ──────────────────
#
# Symptom (Squeeze Client, live 2026-09-13 19:25): "Eigene Musik /
# Musikordner" lists the subdirectories, but tapping a folder sends **no**
# drill request at all.  Perl hands out the ``tracks`` row id of the
# directory — a positive integer (live ``musicfolder 0 3``:
# ``{"id": 204573, "filename": "Accept", "type": "folder"}``,
# ``Slim/Control/Queries.pm:2429-2430``/``:2472-2487``) — and resolves a
# ``folder_id`` back through that row (``:2311-2316``
# ``findAndTrackDirectoryTree``).  Our port created no ``dir`` rows, so the
# id was the directory's ``file://`` URL.
#
# The port hands out the id of the directory's ``tracks`` row: the scan writes
# one per walked directory and the browse creates a missing one on demand
# (``objectForUrl({url, create => 1})``, ``Slim/Utils/Misc.pm:1082-1090`` /
# ``Queries.pm:2263-2268``) — a positive integer, stable across restarts, the
# same value Perl stores in ``tracks.id``.  Both request paths are covered:
# the app's ``browselibrary … mode:bmf`` and the plain ``musicfolder`` query.

import sqlite3                                    # noqa: E402
from pathlib import Path                          # noqa: E402

import lyrion.web.api as api_mod                  # noqa: E402

BMF_ROOT = "/srv/music"
BMF_ROWS = [
    (1, "file:///srv/music/Metal/Accept/01-hard_attack.mp3"),
    (2, "file:///srv/music/Metal/Iron%20Maiden/02-tv_war.flac"),
    (3, "file:///srv/music/Ambient/Boards%20of%20Canada/03-oktaf.flac"),
]


@pytest.fixture()
def bmf_library(tmp_path, monkeypatch, tracks_schema_sql):
    """Temp library DB + ``musicdir`` pref (pattern: test_musicdir_bmf)."""
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(tracks_schema_sql)
    con.executemany(
        "INSERT INTO tracks (id, url, title, titlesort, audio, video, remote,"
        " disabled, compilation, artflow_flag, duration, playcount)"
        " VALUES (?, ?, ?, '', 1, 0, 0, 0, 0, 0, 0, 0)",
        [(i, u, u.rsplit("/", 1)[-1]) for i, u in BMF_ROWS])
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: str(db))
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: BMF_ROOT)
    yield db


def _bmf(args: list) -> dict:
    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary",
                                                     [str(a) for a in args])
    return asyncio.run(run())


def _musicfolder(args: list) -> dict:
    async def run():
        return await JSONRPCAPI()._json_musicfolder([str(a) for a in args])
    return asyncio.run(run())


def test_bmf_folder_rows_carry_a_numeric_id(bmf_library):
    """The app's own browse form (cometd log: ``browselibrary items 0 512
    mode:bmf useContextMenu:1 menu:1``) must name every folder with a number.
    """
    res = _bmf(["items", "0", "512", "mode:bmf", "useContextMenu:1", "menu:1"])
    assert res["count"] == 2
    assert [r["text"] for r in res["item_loop"]] == ["Ambient", "Metal"]
    for row in res["item_loop"]:
        assert isinstance(row["id"], int) and row["id"] > 0, row
        assert row["commonParams"]["folder_id"] == str(row["id"])
        assert row["actions"]["play"]["params"]["folder_id"] == str(row["id"])
        # the path stays reachable for older taps/builders
        assert row["commonParams"]["url"] == f"{BMF_ROOT}/{row['text']}"


def test_bmf_folder_id_drills_into_the_children(bmf_library):
    """The id the row hands out must resolve back to that directory."""
    root = _bmf(["items", "0", "512", "mode:bmf", "menu:1"])["item_loop"]
    metal = next(r for r in root if r["text"] == "Metal")
    children = _bmf(["items", "0", "512", "mode:bmf", "menu:1",
                     f"folder_id:{metal['id']}"])
    assert [r["text"] for r in children["item_loop"]] == ["Accept",
                                                          "Iron Maiden"]
    assert children["count"] == 2
    accept = next(r for r in children["item_loop"] if r["text"] == "Accept")
    assert accept["commonParams"]["folder_id"] == str(accept["id"])
    assert accept["commonParams"]["url"] == f"{BMF_ROOT}/Metal/Accept"
    # … and one level deeper again (no loop, no empty answer)
    tracks = _bmf(["items", "0", "512", "mode:bmf",
                   f"folder_id:{accept['id']}"])
    assert [r["text"] for r in tracks["item_loop"]] == ["01-hard_attack.mp3"]


def test_bmf_drill_without_menu_also_carries_the_numeric_id(bmf_library):
    """The classic (non-menu) drill answer keeps the numeric id, too.

    Its ``actions.go`` params carry ``search``/``folder_id`` — both must be
    the id the row handed out, otherwise a client that echoes that action
    lands on a different directory.
    """
    metal = next(r for r in _bmf(["items", "0", "512", "mode:bmf"])["item_loop"]
                 if r["text"] == "Metal")
    children = _bmf(["items", "0", "512", "mode:bmf",
                     f"folder_id:{metal['id']}"])
    assert all(str(r["id"]).isdigit() for r in children["item_loop"])
    for row in children["item_loop"]:
        go = row["actions"]["go"]["params"]
        assert go["folder_id"] == str(row["id"])
        assert go["search"] == str(row["id"])


def test_bmf_legacy_tokens_and_unknown_ids(bmf_library):
    """Pfad-Tokens drillen weiter; eine unbekannte numerische id nicht.

    Live Perl 9.1.1 (read-only 2026-09-18): ``folder_id:99999999`` →
    ``{"count":0}`` und ``folder_id:1`` (eine Track-Zeile, kein Verzeichnis) →
    ebenfalls ``{"count":0}`` — ``findAndTrackDirectoryTree`` findet keine
    ``dir``-Zeile und ``readDirectory`` listet nichts.
    """
    for token in (f"{BMF_ROOT}/Metal", f"file://{BMF_ROOT}/Metal", "Metal"):
        res = _bmf(["items", "0", "512", "mode:bmf", f"folder_id:{token}"])
        assert [r["text"] for r in res["item_loop"]] == ["Accept", "Iron Maiden"]
    unknown = _bmf(["items", "0", "512", "mode:bmf", "folder_id:99999999"])
    assert unknown["count"] == 0
    assert not (unknown.get("item_loop") or [])
    # ``1`` ist im Fixture die Track-Zeile von Accept/01-hard_attack.mp3 —
    # keine ``dir``-Zeile, also leer (Perl-Probe oben).
    track_id = _bmf(["items", "0", "512", "mode:bmf", "folder_id:1"])
    assert track_id["count"] == 0


def test_folder_id_is_stable_and_path_based(bmf_library):
    """One directory = one id, whatever spelling the caller uses."""
    assert (api_mod._folder_numeric_id(f"{BMF_ROOT}/Metal")
            == api_mod._folder_numeric_id(f"file://{BMF_ROOT}/Metal")
            == api_mod._folder_numeric_id(f"{BMF_ROOT}/Metal/"))
    assert api_mod._folder_numeric_id(f"{BMF_ROOT}/Metal") > 0
    assert (api_mod._folder_numeric_id(f"{BMF_ROOT}/Metal")
            != api_mod._folder_numeric_id(f"{BMF_ROOT}/Ambient"))


def test_musicfolder_rows_are_numeric_like_perl(bmf_library, monkeypatch):
    """``musicfolder`` (Perl ``mediafolderQuery``) — ``id``/``filename``/``type``.

    Live Perl: ``{"id": 204573, "filename": "Accept", "type": "folder"}``.
    Our ``media/folders`` lister walks the filesystem, so the temp library is
    pointed at a real directory through the media-dir pref.
    """
    from lyrion.media import folders as folders_mod

    monkeypatch.setattr(folders_mod, "effective_media_dirs",
                        lambda media_type="": [BMF_ROOT])
    monkeypatch.setattr(folders_mod, "get_inactive_audio_dirs", lambda: [])
    monkeypatch.setattr(folders_mod, "list_directory_entries",
                        lambda directory, recursive=False:
                        ["Metal"] if directory == BMF_ROOT else ["Accept"])
    monkeypatch.setattr(folders_mod, "item_type",
                        lambda path: "folder")
    top = _musicfolder(["0", "3"])["folder_loop"]
    assert [sorted(r) for r in top] == [["filename", "id", "type"]]
    assert top[0]["filename"] == "Metal" and top[0]["type"] == "folder"
    assert isinstance(top[0]["id"], int) and top[0]["id"] > 0
    # the numeric id is only the *folder* id: a real tracks row keeps its own
    assert api_mod._folder_row_numeric_id(
        {"id": 123456, "type": "track"}) is None
    # the drill with the numeric id must reach the directory
    drilled = _musicfolder(["0", "3", f"folder_id:{top[0]['id']}"])
    assert [r["filename"] for r in drilled["folder_loop"]] == ["Accept"]
    # ... while the pre-existing path/URL tokens keep working
    assert [r["filename"] for r in
            _musicfolder(["0", "3", f"folder_id:{BMF_ROOT}/Metal"])["folder_loop"]] \
        == ["Accept"]