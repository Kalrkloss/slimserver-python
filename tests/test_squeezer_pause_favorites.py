"""Squeezer: paused playback kept ticking and favourites taps did nothing.

Two user-visible symptoms, both fixed on the SERVER side:

1. **Pause does not switch the play/pause icon and the clock keeps running.**
   Server state was correct (``status - 1`` → ``mode: pause`` + frozen
   ``time``, verified live), but the *pushed* status was stale: the 1 s poll
   cache (``api.py`` ``_status_cache``, added for SqueezeTray flood clients)
   was keyed on the request only, so the push that follows a state change
   reproduced the pre-change answer.  Perl re-runs every subscribed request
   after each command (``@notificationQueue`` → ``Request::notify``,
   ``Slim/Control/Request.pm:1872-1876`` / ``:2005-2100``) and therefore
   always pushes fresh state.

   Client side, for the record — Squeezer decides the icon AND the local
   progress ticker from the ``mode`` field of the pushed frame:

   * ``service/BaseClient.java:120`` (``changedPlayStatus =
     updatePlayStatus(playerState, Util.getStringOrEmpty(tokenMap, "mode"))``)
     — a payload without ``mode`` (or with a stale ``play``) leaves the icon
     on "pause" (``NowPlayingFragment.updatePlayPauseIcon``, ``:483-484``).
   * ``service/CometClient.java:605-611`` (``postSongTimeChanged``) — while
     ``playerState.isPlaying()`` the app re-arms ``MSG_TIME_UPDATE`` every
     1000 ms and interpolates the position locally, i.e. exactly the
     "Zeit läuft weiter" symptom.
   * ``service/CometClient.java:18205ff`` live log: the subscription is
     ``status - 1 useContextMenu:1 subscribe:0 menu:menu`` on
     ``/<cid>/slim/playerstatus/<mac>`` — the very request whose answer the
     cache was replaying.

2. **Favourite taps were ineffective / ids not Perl's.**
   Perl hands out browse-session ids ``<sid>.<index>[.<index>…]``:
   ``createUUID`` = ``substr(sha1_hex(time . $$ . hostname), 0, 8)``
   (``Slim/Utils/Misc.pm:1557-1560``), recognised by ``getSID``
   (``Slim/Control/XMLBrowser.pm:1739-1741``), prefixed to the crumb path
   (``:353``) and put on the item as ``item_id``/``touchToPlay``
   (``:1142``/``:1261``); the tap echoes it back and the server strips the
   handle again (``:334-336``).  We used to answer the synthetic root ``0``
   ('0.0'), which no Perl client ever sees.  The classic (no ``menu:``)
   shape additionally announced ``["playlist","play"]`` for a stream — a
   command that cannot resolve a favourites id at all (Perl's only
   favourites play route is ``['favorites','playlist','_method']``,
   ``Slim/Plugin/Favorites/Plugin.pm:81``), so that tap stayed a no-op.

Live probes used for the reference data (read-only, ``?``-style queries,
both servers): ``favorites items 0 10`` / ``… menu:favorites`` /
``… '0' '10' 'menu:favorites' 'item_id:<sid>.0'`` against
``http://127.0.0.1:9000/jsonrpc.js`` and ``http://192.168.1.90:9000/jsonrpc.js``.
No command that changes state was sent to either server.

Everything here runs in-process against ``CometdManager`` + the real
``JSONRPCAPI`` with an injected ``PlayerState`` / favourites stand-in — no
server start/stop, no DB write.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI
from lyrion.web.cometd import CometdManager

MAC = "1c:87:2c:47:fc:36"          # clients register the lower-case spelling
MAC_CLEAN = "1C872C47FC36"
PLAYER = "1c:87:2c:47:fc:36"

#: Verbatim from the live cometd log: Squeezer's playerstatus subscription.
SQUEEZER_STATUS_REQUEST = [PLAYER, ["status", "-", 1, "useContextMenu:1",
                                    "subscribe:0", "menu:menu"]]

#: Perl's browse-session handle (getSID, XMLBrowser.pm:1739-1741).
SID_RE = re.compile(r"^[a-f0-9]{8}$")


def _install_player(mode: str = "play", elapsed: float = 128.271) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.power = True
    player.volume = 47
    player.mode = mode
    player.seq_no = 780000
    player.playlist = []
    player.playlist_position = 0
    player.elapsed = elapsed
    player.duration = 257.533
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


async def _subscribe(mgr: CometdManager, request: list) -> tuple[str, str]:
    hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
    cid = hs[0]["clientId"]
    channel = f"/{cid}/slim/playerstatus/{PLAYER}"
    await mgr.handle_messages([{
        "channel": "/slim/subscribe", "id": 2,
        "data": {"request": request, "response": channel},
    }])
    await mgr.wait_for_events(cid, timeout=0)   # drain the seed event
    return cid, channel


def _pushed(cid: str, events: list) -> dict:
    for event in events:
        if isinstance(event.get("data"), dict):
            return event["data"]
    raise AssertionError(f"no status payload pushed: {events}")


# ── A. pause: the pushed status must not be a cached pre-pause answer ─────


def test_pause_push_carries_mode_pause_and_frozen_time():
    """The push after a pause is fresh — not the cached pre-pause frame.

    Regression guard for the live symptom: Squeezer's icon and its local
    1 s progress ticker read ``mode``/``time`` from THIS frame
    (BaseClient.java:120, CometClient.java:605-611).  Both were served from
    a <1 s old cache entry (``mode: play``) before.
    """
    async def run():
        player = _install_player("play")
        mgr = CometdManager(JSONRPCAPI())
        cid, _ = await _subscribe(mgr, SQUEEZER_STATUS_REQUEST)
        # the pause command froze the position and the STAT (STMp,
        # elapsed=128.272 — live log) flipped the mode
        player.mode = "pause"
        await mgr.notify_player_status(MAC)
        return _pushed(cid, await mgr.wait_for_events(cid, timeout=0))

    pushed = asyncio.run(run())
    assert pushed["mode"] == "pause", (
        "a pushed status that still says 'play' leaves the icon and the "
        "client-side timer running")
    assert pushed["time"] == pytest.approx(128.271)
    # The stream reports no length, so `duration` must be ABSENT — Perl adds
    # the key only for a truthy $song->duration() (Queries.pm:4100-4102), and
    # live Perl 9.1.1 answers exactly that for a metadata-less radio stream
    # (read-only probe 2026-09-14).  The old port put the frozen position in
    # it, which is what killed SqueezeClient's seek slider
    # (Slider value … vs valueTo …).
    assert "duration" not in pushed, (
        f"no known stream length ⇒ no duration key, got {pushed['duration']!r}")
    assert pushed["power"] == 1


def test_same_request_repeats_cheaply_while_the_state_is_unchanged():
    """The flood guard stays: identical polls in one state hit the cache."""
    api = JSONRPCAPI()
    _install_player("play")

    async def run():
        first = await api._slim_request(PLAYER, ["status", "-", 1, "menu:menu"])
        second = await api._slim_request(PLAYER, ["status", "-", 1, "menu:menu"])
        return first, second

    first, second = asyncio.run(run())
    assert first is second, "an unchanged state must still be served cached"


def test_status_cache_is_not_reused_after_a_mode_change():
    """The cache must never mask a state change (Perl pushes fresh state)."""
    api = JSONRPCAPI()
    player = _install_player("play")

    async def run():
        before = await api._slim_request(PLAYER, ["status", "-", 1, "menu:menu"])
        player.mode = "pause"                      # what pause_player does
        after = await api._slim_request(PLAYER, ["status", "-", 1, "menu:menu"])
        return before, after

    before, after = asyncio.run(run())
    assert before["mode"] == "play"
    assert after["mode"] == "pause"


# ── B. favourites: Perl's session ids, resolvable when echoed back ────────

FOLDER_ID, STREAM_ID = 11, 12          # root: folder "Chillout", stream
SUB_ID = 2                             # the folder's only child


class _Favs:
    """FavoritesManager stand-in (pattern: tests/test_favorites_menu.py)."""

    TREE = {
        None: [
            {"id": FOLDER_ID, "title": "Chillout", "url": None, "type": "folder",
             "parent_id": None, "position": 0},
            {"id": STREAM_ID, "title": "1Mix EDM Radio",
             "url": "http://fr1.1mix.co.uk:8060/", "type": "stream",
             "parent_id": None, "position": 1},
        ],
        FOLDER_ID: [
            {"id": SUB_ID, "title": "Hirschmilch Chillout",
             "url": "http://hirschmilch.de:7000/chillout.mp3", "type": "stream",
             "parent_id": FOLDER_ID, "position": 0},
        ],
    }

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []

    async def list_items(self, parent_id=None):
        return [dict(x) for x in self.TREE.get(parent_id, [])]

    async def resolve_path(self, path):
        """Same contract as FavoritesManager.resolve_path (Perl session id)."""
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


@pytest.fixture()
def favs(monkeypatch):
    fake = _Favs()
    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: fake)
    return fake


def _items(args: list) -> dict:
    async def run():
        return await JSONRPCAPI()._json_favorites_items(PLAYER, [str(a) for a in args])
    return asyncio.run(run())


def _split_id(item_id: str) -> tuple[str, str]:
    root, _, rest = str(item_id).partition(".")
    return root, rest


def test_root_ids_carry_a_perl_session_handle(favs):
    """``<8-hex sid>.<index>`` — Perl's id, not our old synthetic root '0'."""
    loop = [
        _items(["items", 0, 10, "menu:favorites"])["item_loop"],
        _items(["items", 0, 10])["loop_loop"],
    ]
    for items in loop:
        for index, item in enumerate(items):
            item_id = (item.get("actions", {}).get("go", {}).get("params", {})
                       or item.get("params", {})).get("item_id") or item["id"]
            root, rest = _split_id(item_id)
            assert SID_RE.match(root), (
                f"item {index} id {item_id!r} is not rooted in a Perl "
                f"browse-session handle (getSID, XMLBrowser.pm:1739-1741)")
            assert rest == str(index), item_id
        roots = {_split_id((it.get("actions", {}).get("go", {}).get("params", {})
                            or it.get("params", {})).get("item_id") or it["id"])[0]
                 for it in items}
        assert len(roots) == 1, "one browse answer = one session handle"


def test_menu_items_keep_item_id_and_touch_to_play_on_the_same_id(favs):
    """XMLBrowser.pm:1142/1261 — the tap id and the touch id are identical."""
    items = _items(["items", 0, 10, "menu:favorites"])["item_loop"]
    folder, stream = items[0], items[1]
    folder_id = folder["actions"]["go"]["params"]["item_id"]
    stream_id = stream["params"]["item_id"]
    assert SID_RE.match(_split_id(folder_id)[0]), folder_id
    assert stream["params"]["touchToPlay"] == stream_id
    assert stream["params"]["touchToPlaySingle"] == 1
    assert _split_id(folder_id)[1] == "0"
    assert _split_id(stream_id)[1] == "1"


def test_folder_tap_resolves_and_echoes_the_handle(favs):
    """The id the client echoes back must address the same folder."""
    folder_id = _items(["items", 0, 10, "menu:favorites"])["item_loop"][0][
        "actions"]["go"]["params"]["item_id"]
    sub = _items(["items", 0, 10, "menu:favorites",
                  f"item_id:{folder_id}"])["item_loop"]
    assert [it["text"] for it in sub] == ["Hirschmilch Chillout"]
    # the session handle is kept → nested ids stay in one session
    assert _split_id(sub[0]["params"]["item_id"])[0] == _split_id(folder_id)[0]
    assert sub[0]["params"]["item_id"].endswith(".0.0")


def test_tap_command_plays_the_favorite_for_the_perl_id(favs):
    """The command Squeezer builds from the item (base.actions.play +
    ``itemsParams``, JiveItem.java:577-660) must resolve."""
    stream_id = _items(["items", 0, 10, "menu:favorites"])["item_loop"][1][
        "params"]["item_id"]
    request = ["favorites", "playlist", "play", "0", "255", "menu:favorites",
               f"item_id:{stream_id}", f"touchToPlay:{stream_id}",
               "touchToPlaySingle:1", "isContextMenu:1", "useContextMenu:1"]
    asyncio.run(JSONRPCAPI()._slim_request(PLAYER, request))
    assert favs.played == [(PLAYER, STREAM_ID)]


def test_plain_shape_play_action_uses_the_favorites_route(favs):
    """Perl's only favourites play route is ``favorites playlist play``
    (Favorites/Plugin.pm:81) — ``playlist play`` cannot resolve an
    ``item_id`` (Commands.pm:1313-1323), so the tap was a no-op."""
    item = _items(["items", 0, 10])["loop_loop"][1]
    assert item["actions"]["play"]["cmd"] == ["favorites", "playlist", "play"]
    assert item["actions"]["do"]["cmd"] == ["favorites", "playlist", "play"]
    stream_id = item["actions"]["play"]["params"]["item_id"]
    asyncio.run(JSONRPCAPI()._slim_request(PLAYER, [
        "favorites", "playlist", "play", "0", "255",
        f"item_id:{stream_id}"]))
    assert favs.played == [(PLAYER, STREAM_ID)]


def test_legacy_virtual_root_still_resolves(favs):
    """Responses cached by older clients stay resolvable ('0.1')."""
    async def run():
        return await _Favs().resolve_path("0.1")

    assert asyncio.run(run()) == STREAM_ID
    # and the response for a legacy id keeps working
    sub = _items(["items", 0, 10, "menu:favorites", "item_id:0.0"])
    assert [it["text"] for it in sub["item_loop"]] == ["Hirschmilch Chillout"]


def test_real_resolve_path_accepts_perl_session_ids(monkeypatch):
    """The real ``FavoritesManager.resolve_path`` (not the stand-in) must
    strip a Perl session handle and walk the crumbs below it."""
    from lyrion.music.favorites import FavoritesManager

    tree = _Favs.TREE

    async def list_items(self, parent_id=None):           # noqa: ANN001
        return [dict(x) for x in tree.get(parent_id, [])]

    monkeypatch.setattr(FavoritesManager, "list_items", list_items)
    mgr = object.__new__(FavoritesManager)

    async def run():
        return (
            await mgr.resolve_path("deadbeef.0"),      # session root → folder
            await mgr.resolve_path("deadbeef.0.0"),    # + child
            await mgr.resolve_path("0.1"),             # legacy root
            await mgr.resolve_path("deadbeef.9"),      # out of range
            await mgr.resolve_path("notasid.0"),       # no session, no '0'
            await mgr.resolve_path("0"),               # root alone is no item
        )

    assert asyncio.run(run()) == (FOLDER_ID, SUB_ID, STREAM_ID, None, None, None)


def test_new_sid_is_a_perl_getSID_token():
    """createUUID shape: 8 hex chars (Misc.pm:1557-1560, XMLBrowser.pm:1739)."""
    from lyrion.web.api import _is_fav_sid, _new_fav_sid

    for _ in range(20):
        sid = _new_fav_sid()
        assert SID_RE.match(sid), sid
        assert _is_fav_sid(sid)
    assert not _is_fav_sid("0")
    assert not _is_fav_sid("ab9c31e")            # 7 chars
    assert not _is_fav_sid("zzzzzzzz")           # not hex


def test_item_ids_are_json_stable(favs):
    """Sanity: an id survives a JSON round trip (Squeezer parses strictly)."""
    items = _items(["items", 0, 10, "menu:favorites"])["item_loop"]
    for item in items:
        assert json.loads(json.dumps(item)) == item
