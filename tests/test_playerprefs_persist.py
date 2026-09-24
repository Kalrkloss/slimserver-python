"""Player-Prefs (``_client:<MAC>:<Pref>``) überleben einen Serverneustart.

Perl legt Client-Prefs im Namespace-Store ab — Schlüssel ``_client:<MAC>``
(``Slim/Utils/Prefs/Client.pm:30`` ``$clientPreferenceTag``, ``:44`` der Slot),
geschrieben bei jedem ``set`` (``Prefs/Base.pm:121-124`` ``$class->{'prefs'}->{$pref}
= $new; ... $root->save;``), gelesen beim Client-``init`` (``Player/Client.pm:334-349``
``initPrefs`` → ``Prefs/Base.pm:196-230`` ``init`` schreibt nur Fehlendes).

Der Neustart wird hier wie dort nachgestellt: ein **neues** ``PlayerState``-Objekt
(die Instanz nach dem Serverstart) gegen denselben Store — die Werte müssen
wieder da sein, ohne dass eine Einstellungsseite sie erneut setzt.

Perls Lebensdauer-Regeln, die hier mitgeprüft werden:

* ``client forget`` (``Player/Client.pm:539-568``) lässt die Prefs stehen.
* Nur ``resetPrefs`` (``:375-383``) räumt sie weg (``remove``,
  ``Prefs/Base.pm:241-257``).
* Server-Prefs und Client-Prefs liegen im selben Speicher nebeneinander
  (Perl: dieselbe ``server.prefs``), dürfen sich aber nicht vermischen.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.player.manager import PlayerManager
from lyrion.player.playerprefs import (
    CLIENT_PREF_TAG,
    load_player_prefs,
    player_pref_key,
    remove_player_prefs,
    set_player_pref,
    stored_player_prefs,
)
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI
from lyrion.web.app import create_app

MAC = "1c:87:2c:47:fc:36"
MAC2 = "00:04:20:2b:88:c8"


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """In-Memory-Prefs-Store (wie ``tests/test_web_settings_player.py``).

    Der ``PreferenceStore`` ist ein Singleton; vorher offene DB/Inhalt werden
    gesichert und am Modulende wiederhergestellt, damit die ``_client:``-Zeilen
    dieses Tests keine anderen Module treffen.
    """
    store = get_prefs()
    previous_db = store._db
    previous_cache = dict(store._cache)

    async def _open() -> None:
        if store._db is None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
        await store._db.executescript(_PREF_DB_SCHEMA)
        await store._db.commit()

    asyncio.run(_open())
    store._cache = {}
    yield store

    async def _cleanup() -> None:
        db = store._db
        if db is not None and previous_db is None:
            await db.execute("DELETE FROM prefhash WHERE name LIKE ?",
                             (CLIENT_PREF_TAG + ":%",))
            await db.commit()
            await db.close()
            store._db = None

    asyncio.run(_cleanup())
    store._cache = previous_cache


@pytest.fixture(autouse=True)
def _no_players_db_write(monkeypatch):
    """``register_player`` schreibt sonst eine Zeile in die echte ``players.db``.

    Dieser Test prüft die Player-Prefs, nicht die Namensablage.
    """
    monkeypatch.setattr(PlayerManager, "_save_player", lambda self, player: None)


def _player(mac: str = MAC, **kw) -> PlayerState:
    base = dict(mac=mac, name="Taverne", ip="192.168.1.130", port=0,
                model="squeezeplay", connected=True, power=True,
                digital_volume_control=True)
    base.update(kw)
    return PlayerState(**base)


async def _request(method: str, path: str, *, params=None, data=None) -> httpx.Response:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        return await client.request(method, path, params=params, data=data)


def _post_page(path: str, data: dict, mac: str = MAC) -> httpx.Response:
    return asyncio.run(_request("POST", path, params={"playerid": mac}, data=data))


def _store_rows(store) -> dict[str, str]:
    async def _read():
        async with store._db.execute("SELECT name, value FROM prefhash") as cur:
            return {r["name"]: r["value"] for r in await cur.fetchall()}

    return asyncio.run(_read())


# ── Ablage: Schlüssel = MAC + Pref ──────────────────────────────────────────

def test_key_is_the_mac_plus_the_pref_name():
    """Perl ``$parent->{'prefs'}->{"_client:$clientid"}`` (Prefs/Client.pm:44)."""
    assert player_pref_key("1C:87:2C:47:FC:36", "startDelay") == \
        "_client:1c:87:2c:47:fc:36:startDelay"
    # Gross-/Kleinschreibung derselben MAC trifft denselben Slot.
    assert player_pref_key("1c:87:2c:47:fc:36", "x") == player_pref_key(MAC, "x")


def test_a_set_pref_is_persisted_immediately():
    """Perl ``set`` schreibt sofort (``Prefs/Base.pm:121-124`` ``$root->save``)."""
    player = _player()
    asyncio.run(set_player_pref(player, "startDelay", "150"))

    assert player.playerprefs["startDelay"] == "150"
    assert _store_rows(get_prefs())[player_pref_key(MAC, "startDelay")] == "150"


def test_lists_round_trip_as_json():
    """``menuItem``/``disabledirsets`` sind Perl-Refs, unser Speicher ist Text."""
    player = _player()
    asyncio.run(set_player_pref(player, "menuItem", ["NOW_PLAYING", "RADIO"]))
    asyncio.run(set_player_pref(player, "disabledirsets", ["0", "0"]))

    assert stored_player_prefs(MAC)["menuItem"] == ["NOW_PLAYING", "RADIO"]
    assert stored_player_prefs(MAC)["disabledirsets"] == ["0", "0"]


# ── Laden nach „Neustart“ und beim Registrieren ─────────────────────────────

def test_prefs_survive_a_restart_like_player_object():
    """Der Kern: neues ``PlayerState`` (Serverstart) + ``initPrefs``-Laden."""
    writer = _player()
    asyncio.run(set_player_pref(writer, "startDelay", "175"))
    asyncio.run(set_player_pref(writer, "syncVolume", "1"))

    after_restart = _player()                      # neuer Prozess, leeres Dict
    assert after_restart.playerprefs == {}
    load_player_prefs(after_restart)               # Client.pm:334-349 initPrefs

    assert after_restart.playerprefs["startDelay"] == "175"
    assert after_restart.playerprefs["syncVolume"] == "1"


def test_load_reapplies_the_setchange_effect():
    """Perl liest ``digitalVolumeControl`` direkt aus der Pref (Squeezebox2.pm:283)."""
    writer = _player()
    asyncio.run(set_player_pref(writer, "digitalVolumeControl", "0"))

    after_restart = _player(digital_volume_control=True)
    load_player_prefs(after_restart)
    # Player.pm:79 setChange → audg-Frame liest das Feld, nicht die Pref.
    assert after_restart.playerprefs["digitalVolumeControl"] == "0"
    assert after_restart.digital_volume_control is False


def test_load_only_fills_missing_values():
    """``init`` schreibt nur Fehlendes (``Prefs/Base.pm:196-230``)."""
    writer = _player()
    asyncio.run(set_player_pref(writer, "playDelay", "20"))

    reconnect = _player()
    reconnect.playerprefs["playDelay"] = "99"      # schon in dieser Sitzung gesetzt
    load_player_prefs(reconnect)
    assert reconnect.playerprefs["playDelay"] == "99"


def test_registering_a_player_loads_its_stored_prefs():
    """``PlayerManager.register_player`` entspricht Perls ``init``/``initPrefs``."""
    writer = _player(MAC2)
    asyncio.run(set_player_pref(writer, "playername", "Küche"))

    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {}
    try:
        player = pm.register_player(MAC2, "Küche", "192.168.1.225", 0,
                                    model="squeezeplay")
        assert player.playerprefs.get("playername") == "Küche"
    finally:
        pm.players = saved


# ── Schreiben über die Einstellungsseiten ───────────────────────────────────

def test_settings_page_post_persists_the_value():
    """``/settings/player/synchronization.html`` (Perl ``Synchronization.pm``)."""
    player = _player()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {MAC: player}
    try:
        res = _post_page("/settings/player/synchronization.html",
                         {"saveSettings": "1", "pref_startDelay": "150"})
        assert res.status_code == 200
    finally:
        pm.players = saved

    assert player.playerprefs["startDelay"] == "150"
    assert _store_rows(get_prefs())[player_pref_key(MAC, "startDelay")] == "150"


def test_menu_page_post_persists_the_list():
    """``/settings/player/menu.html`` — Perl ``Menu.pm:93`` ``set('menuItem', ...)``."""
    player = _player()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {MAC: player}
    try:
        res = _post_page("/settings/player/menu.html",
                         {"saveSettings": "1", "Action0": "Remove"})
        assert res.status_code == 200
    finally:
        pm.players = saved

    assert stored_player_prefs(MAC)["menuItem"] == list(player.playerprefs["menuItem"])


def test_known_ir_sets_survive_a_restart():
    """``/settings/player/remote.html`` — Perl ``Remote.pm:70``."""
    player = _player()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {MAC: player}
    try:
        _post_page("/settings/player/remote.html",
                   {"saveSettings": "1", "pref_irsetlist0": "0",
                    "pref_irsetlist1": "0"})
    finally:
        pm.players = saved

    after_restart = _player()
    load_player_prefs(after_restart)
    assert after_restart.playerprefs["disabledirsets"] == ["0", "0"]


# ── Die DSTM-Provider-Pref (Jive-/JSON-RPC-Pfad) ────────────────────────────

def test_dontstopthemusic_provider_pref_survives_a_restart():
    """Perl ``$prefs->client($client)->get('provider')`` (RandomPlay ``Plugin.pm:110``)."""
    from lyrion.plugins import dontstopthemusic as dstm

    player = _player()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {MAC: player}
    try:
        async def _set():
            api = JSONRPCAPI()
            await api._slim_request(MAC, [
                "playerpref", f"{dstm.PREF_CATEGORY}:{dstm.PREF_PROVIDER}",
                "PLUGIN_RANDOM"])
            await asyncio.sleep(0.05)      # Schreib-Task aus apply_player_pref

        asyncio.run(_set())
    finally:
        pm.players = saved

    key = f"{dstm.PREF_CATEGORY}:{dstm.PREF_PROVIDER}"
    assert _store_rows(get_prefs())[player_pref_key(MAC, key)] == "PLUGIN_RANDOM"

    # Nach dem Neustart emittiert DSTM dieselbe Pref wieder:
    after_restart = _player()
    pm2 = PlayerManager()
    pm2.players = {MAC: after_restart}
    load_player_prefs(after_restart)
    try:
        got = asyncio.run(dstm.DontStopTheMusic()._client_pref(after_restart,
                                                              dstm.PREF_PROVIDER))
    finally:
        pm2.players = saved
    assert got == "PLUGIN_RANDOM"


# ── Entfernen: wie Perl ─────────────────────────────────────────────────────

def test_forget_keeps_the_prefs_and_reconnect_restores_them():
    """``forgetClient`` (``Client.pm:539-568``) fasst die Prefs nicht an."""
    player = _player()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players = {MAC: player}
    try:
        asyncio.run(set_player_pref(player, "syncPower", "1"))
        pm.unregister_player(MAC)
        assert stored_player_prefs(MAC)["syncPower"] == "1"

        again = pm.register_player(MAC, "Taverne", "192.168.1.130", 0,
                                   model="squeezeplay")
        assert again.playerprefs["syncPower"] == "1"
    finally:
        pm.players = saved


def test_reset_prefs_removes_only_this_players_rows():
    """Perl ``resetPrefs`` (``Client.pm:375-383``) → ``remove`` (Base.pm:241-257)."""
    a, b = _player(), _player(MAC2)
    asyncio.run(remove_player_prefs(MAC))          # sauberer Ausgangszustand
    asyncio.run(set_player_pref(a, "startDelay", "150"))
    asyncio.run(set_player_pref(a, "playDelay", "20"))
    asyncio.run(set_player_pref(b, "startDelay", "30"))

    removed = asyncio.run(remove_player_prefs(MAC))
    assert removed == 2
    assert stored_player_prefs(MAC) == {}
    assert stored_player_prefs(MAC2)["startDelay"] == "30"


def test_removing_one_named_pref_matches_perls_remove():
    """``remove('currentSong')``-Form — ein Name, eine Zeile."""
    player = _player()
    asyncio.run(set_player_pref(player, "startDelay", "150"))
    asyncio.run(set_player_pref(player, "playDelay", "20"))

    assert asyncio.run(remove_player_prefs(MAC, ["startDelay"])) == 1
    assert "startDelay" not in stored_player_prefs(MAC)
    assert stored_player_prefs(MAC)["playDelay"] == "20"


# ── Server-Prefs bleiben Server-Prefs ───────────────────────────────────────

def test_player_prefs_do_not_leak_into_the_server_prefs():
    """Beide liegen in ``prefs.db`` (Perl: dieselbe ``server.prefs``), getrennt
    durch den ``_client:``-Schlüssel (Prefs/Client.pm:44)."""
    store = get_prefs()
    asyncio.run(store.set("artworkOnlineSearch", "1"))     # Server-Pref
    player = _player()
    asyncio.run(set_player_pref(player, "artworkOnlineSearch", "0"))  # gleichnamige Client-Pref

    assert store.get("artworkOnlineSearch") == "1"
    assert stored_player_prefs(MAC)["artworkOnlineSearch"] == "0"

    asyncio.run(remove_player_prefs(MAC))
    assert store.get("artworkOnlineSearch") == "1"
    assert player_pref_key(MAC, "artworkOnlineSearch") != "artworkOnlineSearch"


def test_load_player_prefs_ignores_other_players_and_server_prefs():
    asyncio.run(remove_player_prefs(MAC))          # sauberer Ausgangszustand
    asyncio.run(remove_player_prefs(MAC2))
    a, b = _player(), _player(MAC2)
    asyncio.run(set_player_pref(a, "startDelay", "150"))
    asyncio.run(set_player_pref(b, "startDelay", "30"))
    asyncio.run(get_prefs().set("servername", "Pyrion"))

    fresh = _player()
    load_player_prefs(fresh)
    assert fresh.playerprefs == {"startDelay": "150"}
