"""Player-Settings-Seiten aus ``web/settings_player.py`` (Perl ``Slim/Web/Settings/Player/*``).

Perl-Belege (``/tmp/lms-ref``, read-only)
=========================================

* ``Slim/Web/Settings/Player/Menu.pm``: ``name`` :16-18 ``MENU_SETTINGS``,
  ``page`` :20-22 ``settings/player/menu.html``, ``needsClient`` :24-26,
  ``validFor`` :28-33 (``display->isa('Slim::Display::NoDisplay')``), Handler
  :35-109.  Pref ``menuItem`` je Client (:41,:93); Parameter ``Action<i>``
  (:45-60), ``removeItems``+``menuItemRemove<i>`` (:63-66), ``addItems``+
  ``nonMenuItemAdd<i>`` (:69-77).  Fallback ``NOW_PLAYING`` (:88-91).
  Vorlage ``HTML/EN/settings/player/menu.html:4-34``.
* ``Slim/Web/Settings/Player/Remote.pm``: ``name`` :18-20 ``REMOTE_SETTINGS``,
  ``page`` :22-24, ``needsClient`` :26-28, ``validFor`` :30-35 (``hasIR``),
  ``prefs`` :37-45 (``irmap`` nur bei >1 Map-Datei), Handler :47-85
  (``disabledirsets`` aus ``pref_irsetlist<i>``, :53-71).  Vorlage
  ``HTML/EN/settings/player/remote.html:3-19``.
* ``Slim/Web/Settings/Player/Synchronization.pm``: ``name`` :18-20
  ``SETUP_SYNCHRONIZE``, ``page`` :22-24, ``needsClient`` :26-28, ``validFor``
  :30-35, ``prefs`` :37-42, Handler :44-73 (``synchronize`` ist KEINE Pref,
  :50-58; ``-1`` = keine Synchronisation :66,:92).  Vorlage
  ``HTML/EN/settings/player/synchronization.html:5-60``.

Die Tests fahren die Seiten **in-process** Ã¼ber die ASGI-App; die Dispatch-Zeile
in ``settings.py`` ist noch nicht verdrahtet (parallel arbeitender Agent), also
baut der Test denselben Weg Ã¼ber ``settings_player.handle_player_page_request``
auf â€” dieselbe Funktion, die der Registry-Patch (Modul-Docstring) aufruft.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import settings_player as sp
from lyrion.web.app import create_app

MAC = "00:04:20:2b:88:c8"
MAC2 = "00:04:20:2b:88:c9"


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """Globalen Prefs-Store auf In-Memory-DB hÃ¤ngen und am Modulende schlieÃŸen."""
    store = get_prefs()
    opened = False
    if getattr(store, "_db", None) is None:
        async def _open() -> None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
            await store._db.executescript(_PREF_DB_SCHEMA)

        asyncio.run(_open())
        opened = True
    yield store
    db = getattr(store, "_db", None)
    if opened and db is not None:
        try:
            asyncio.run(db.close())
        except Exception:  # noqa: BLE001 — AufrÃ¤umen darf nicht scheitern
            pass
        store._db = None


@pytest.fixture
def players():
    """Zwei Squeezebox2-artige Clients (Display + IR; synchronisierbar)."""
    pm = PlayerManager()
    a = PlayerState(mac=MAC, name="KÃ¼che", ip="192.168.1.225", port=48512,
                    model="squeezebox2", connected=True, power=True)
    b = PlayerState(mac=MAC2, name="Bad", ip="192.168.1.226", port=48512,
                    model="squeezebox2", connected=True, power=True)
    old = dict(pm.players)
    pm.players = {MAC: a, MAC2: b}
    try:
        yield a, b
    finally:
        pm.players = old


async def _asgi_request(method: str, path: str, *, params=None, data=None) -> httpx.Response:
    """Anfrage gegen die ASGI-App, deren ``/settings/``-Dispatch der Patch-Zeile entspricht."""
    app = create_app()

    # Denselben Weg wie der empfohlene settings.py-Dispatch aufbauen: vor dem
    # Standard-Handler prÃ¼fen, ob der Pfad eine Player-Seite dieses Moduls ist.
    transport = httpx.ASGITransport(app=app)

    # Ein kleiner ASGI-Wrapper, der die Player-Seiten an unser Modul gibt.
    from lyrion.web import settings as S

    orig = S.handle_settings_request

    async def dispatch(scope, receive, send):
        if scope.get("path") in sp.PLAYER_PAGES:
            await sp.handle_player_page_request(
                scope, receive, send, sp.PLAYER_PAGES[scope["path"]])
            return
        await orig(scope, receive, send)

    S.handle_settings_request = dispatch
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.request(method, path, params=params, data=data)
    finally:
        S.handle_settings_request = orig


def _request(method: str, path: str, *, params=None, data=None) -> httpx.Response:
    return asyncio.run(_asgi_request(method, path, params=params, data=data))


# ── /settings/player/menu.html (Menu.pm) ───────────────────────────────────

def test_menu_get_renders_the_current_items_with_actions(players):
    a, _b = players
    a.playerprefs["menuItem"] = ["NOW_PLAYING", "BROWSE_MUSIC", "RADIO"]
    res = _request("GET", "/settings/player/menu.html", params={"playerid": MAC})

    assert res.status_code == 200
    body = res.text
    # menu.html:9-12 — je Zeile Up/Down/Remove + menuItemRemove<i>
    for i in range(3):
        assert f"Action{i}=Up" in body
        assert f"Action{i}=Down" in body
        assert f"Action{i}=Remove" in body
        assert f'name="menuItemRemove{i}"' in body
    # menu.html:16,32 — die beiden KnÃ¶pfe
    assert 'name="removeItems"' in body
    assert 'name="addItems"' in body


def test_menu_get_without_items_falls_back_to_now_playing(players):
    a, _b = players
    a.playerprefs.pop("menuItem", None)
    body = _request("GET", "/settings/player/menu.html",
                    params={"playerid": MAC}).text
    # Menu.pm:88-91 — leere Liste â†’ NOW_PLAYING
    assert "Action0=Up" in body
    assert "NOW_PLAYING" in body or "Now Playing" in body


def test_menu_post_reorders_items(players):
    a, _b = players
    a.playerprefs["menuItem"] = ["NOW_PLAYING", "BROWSE_MUSIC"]

    res = _request("POST", "/settings/player/menu.html",
                   params={"playerid": MAC},
                   data={"saveSettings": "1", "Action1": "Up"})

    assert res.status_code == 200
    # Menu.pm:51-55 — BROWSE_MUSIC rÃ¼ckt auf Position 0
    assert a.playerprefs["menuItem"] == ["BROWSE_MUSIC", "NOW_PLAYING"]


def test_menu_post_removes_and_adds_items(players):
    a, _b = players
    a.playerprefs["menuItem"] = ["NOW_PLAYING", "BROWSE_MUSIC"]

    # Menu.pm:63-66 — removeItems + menuItemRemove1 entfernt BROWSE_MUSIC
    _request("POST", "/settings/player/menu.html", params={"playerid": MAC},
             data={"saveSettings": "1", "removeItems": "1", "menuItemRemove1": "1"})
    assert a.playerprefs["menuItem"] == ["NOW_PLAYING"]

    # Menu.pm:69-77 — addItems + nonMenuItemAdd0 hÃ¤ngt ihn wieder an
    _request("POST", "/settings/player/menu.html", params={"playerid": MAC},
             data={"saveSettings": "1", "addItems": "1",
                   "nonMenuItems": "0", "nonMenuItemAdd0": "BROWSE_MUSIC"})
    assert a.playerprefs["menuItem"] == ["NOW_PLAYING", "BROWSE_MUSIC"]


def test_menu_post_empty_result_keeps_now_playing(players):
    a, _b = players
    a.playerprefs["menuItem"] = ["NOW_PLAYING"]
    _request("POST", "/settings/player/menu.html", params={"playerid": MAC},
             data={"saveSettings": "1", "removeItems": "1", "menuItemRemove0": "1"})
    # Menu.pm:88-91
    assert a.playerprefs["menuItem"] == ["NOW_PLAYING"]


def test_menu_valid_for_needs_a_display():
    """Menu.pm:28-33 — NoDisplay-Clients haben die Seite nicht."""
    from lyrion.web.settings_player import valid_for, _PLAYER_MENU

    jive = PlayerState(mac=MAC, name="x", ip="1.1.1.1", port=1,
                       model="squeezeplay", connected=True)   # NoDisplay
    hw = PlayerState(mac=MAC2, name="y", ip="1.1.1.2", port=1,
                     model="squeezebox2", connected=True)     # Squeezebox2
    assert valid_for(_PLAYER_MENU, jive) is False
    assert valid_for(_PLAYER_MENU, hw) is True
    # Ohne Client (Perl ``validFor`` mit undef) â†’ False
    assert valid_for(_PLAYER_MENU, None) is False


# ── /settings/player/remote.html (Remote.pm) ───────────────────────────────

def test_remote_fields_match_perls_prefs_list():
    """Remote.pm:37-45 — ``disabledirsets``; ``irmap`` nur bei >1 Map-Datei."""
    fields = [f.pref for f in sp._PLAYER_REMOTE.fields]
    assert fields == ["disabledirsets", "irmap"]


def test_remote_get_without_ir_files_shows_no_invented_sets(players):
    a, _b = players
    body = _request("GET", "/settings/player/remote.html",
                    params={"playerid": MAC}).text
    # Unser Port hat keinen IR-Dateibaum (Slim/Hardware/IR.pm:165-243): keine Sets.
    assert 'name="pref_irsetlist0"' not in body
    assert "SETUP_IRMAP" not in body or 'name="pref_irmap"' not in body


def test_remote_post_writes_disabledirsets(players):
    a, _b = players
    _request("POST", "/settings/player/remote.html", params={"playerid": MAC},
             data={"saveSettings": "1", "pref_irsetlist0": "0",
                   "pref_irsetlist1": "0"})
    # Remote.pm:70 — die abgewÃ¤hlten Sets landen in disabledirsets
    assert a.playerprefs["disabledirsets"] == ["0", "0"]


def test_remote_valid_for_needs_ir():
    from lyrion.web.settings_player import valid_for, _PLAYER_REMOTE

    hw = PlayerState(mac=MAC, name="y", ip="1.1.1.2", port=1,
                     model="squeezebox2", connected=True)      # hasIR
    sw = PlayerState(mac=MAC2, name="z", ip="1.1.1.3", port=1,
                     model="squeezelite", connected=True)      # kein IR
    assert valid_for(_PLAYER_REMOTE, hw) is True
    assert valid_for(_PLAYER_REMOTE, sw) is False


# ── /settings/player/synchronization.html (Synchronization.pm) ─────────────

def test_sync_fields_match_perls_prefs_list():
    """Synchronization.pm:39 — genau diese sieben Prefs, in Perls Reihenfolge."""
    fields = [f.pref for f in sp._PLAYER_SYNC.fields]
    assert fields == ["syncVolume", "syncPower", "maintainSync", "startDelay",
                      "playDelay", "minSyncAdjust", "packetLatency"]
    # Synchronization.pm:82-92 — die Auswahl nutzt die eigenen Namen.
    assert sp.SYNC_PREFS == ("syncVolume", "syncPower", "startDelay",
                             "maintainSync", "playDelay", "packetLatency",
                             "minSyncAdjust")


def test_sync_get_lists_the_perl_fields_and_group_select(players):
    _a, _b = players
    res = _request("GET", "/settings/player/synchronization.html",
                   params={"playerid": MAC})
    body = res.text
    for name in ("syncVolume", "syncPower", "maintainSync", "startDelay",
                 "playDelay", "minSyncAdjust", "packetLatency"):
        assert f'name="pref_{name}"' in body, name
    # Synchronization.pm:66 — ``synchronize`` ist keine Pref, sondern die Auswahl
    assert 'name="synchronize"' in body
    # Synchronization.pm:92 — "keine Synchronisation" ist immer dabei
    assert 'value="-1"' in body


def test_sync_post_writes_the_client_prefs(players):
    a, _b = players
    res = _request("POST", "/settings/player/synchronization.html",
                   params={"playerid": MAC},
                   data={"saveSettings": "1", "pref_syncVolume": "1",
                         "pref_syncPower": "0", "pref_startDelay": "150",
                         "pref_maintainSync": "1", "pref_playDelay": "20",
                         "pref_minSyncAdjust": "50", "pref_packetLatency": "30"})
    assert res.status_code == 200
    assert a.playerprefs["syncVolume"] == "1"
    assert a.playerprefs["syncPower"] == "0"
    assert a.playerprefs["startDelay"] == "150"
    assert a.playerprefs["maintainSync"] == "1"
    assert a.playerprefs["playDelay"] == "20"
    assert a.playerprefs["minSyncAdjust"] == "50"
    assert a.playerprefs["packetLatency"] == "30"


def test_sync_pref_roundtrip_shows_the_saved_value(players):
    """Nach dem Speichern zeigt die Seite die Werte wieder an (Roundtrip)."""
    a, _b = players
    _request("POST", "/settings/player/synchronization.html",
             params={"playerid": MAC},
             data={"saveSettings": "1", "pref_startDelay": "175"})
    body = _request("GET", "/settings/player/synchronization.html",
                    params={"playerid": MAC}).text
    assert 'name="pref_startDelay"' in body
    assert 'value="175"' in body
    assert a.playerprefs["startDelay"] == "175"


def test_sync_post_unsynced_choice_keeps_players_together_apart(players):
    """Synchronization.pm:50-58 — ``synchronize`` lÃ¶st ``sync`` aus/ab."""
    a, b = players
    # An b synchronisieren
    _request("POST", "/settings/player/synchronization.html",
             params={"playerid": MAC},
             data={"saveSettings": "1", "synchronize": MAC2})
    assert a.sync_master == MAC2
    assert MAC in b.sync_slaves

    # ``-1`` = keine Synchronisation (Synchronization.pm:66,:92)
    _request("POST", "/settings/player/synchronization.html",
             params={"playerid": MAC},
             data={"saveSettings": "1", "synchronize": "-1"})
    assert a.sync_master is None
    assert MAC not in b.sync_slaves


def test_sync_valid_for_needs_a_partner_or_group():
    """Synchronization.pm:30-35 — ``isSynced() || canSyncWith() > 0``."""
    from lyrion.web.settings_player import valid_for, _PLAYER_SYNC

    solo = PlayerState(mac=MAC, name="y", ip="1.1.1.2", port=1,
                       model="squeezebox2", connected=True)
    # Ein einzelner, nicht synchronisierter Client hat die Seite nicht.
    assert valid_for(_PLAYER_SYNC, solo) is False
    # Ein synchronisierter Client (sync_master gesetzt) hat sie.
    solo.sync_master = MAC2
    assert valid_for(_PLAYER_SYNC, solo) is True


# ── NachzÃ¼gler-Felder (ErgÃ¤nzungsvorschlag fÃ¼r die bestehenden Seiten) ──────

def test_audio_extra_fields_match_the_live_perl_page():
    """Live-Gegenprobe 192.168.1.90 (Squeezebox Radio) audio.html: diese Namen."""
    names = {f.pref for f in sp.AUDIO_EXTRA_FIELDS}
    for expected in ("digitalVolumeControl", "mp3SilencePrelude",
                     "mp3StreamingMethod", "balance", "outputChannels",
                     "polarityInversion", "replayGainMode", "remoteReplayGain",
                     "transitionDuration", "transitionSampleRestriction",
                     "transitionSmart", "transitionType"):
        assert expected in names, expected


def test_audio_extra_fields_cover_perls_conditional_branches():
    """Audio.pm:35-117 — die bedingten Zweige sind abgedeckt."""
    names = {f.pref for f in sp.AUDIO_EXTRA_FIELDS}
    for expected in ("powerOffDac", "disableDac", "preampVolumeControl",
                     "digitalOutputEncoding", "clockSource", "fxloopSource",
                     "fxloopClock", "wordClockOutput", "rolloffSlow",
                     "analogOutMode", "bass", "treble", "stereoxl",
                     "lineInLevel", "lineInAlwaysOn", "localReplayGain"):
        assert expected in names, expected


def test_display_extra_fields_cover_the_class_branches():
    """Display.pm:49-61 — Graphics-, Text- und Boom-Zweig."""
    names = {f.pref for f in sp.DISPLAY_EXTRA_FIELDS}
    for expected in ("activeFont_curr", "idleFont_curr", "scrollPixels",
                     "scrollPixelsDouble",            # Graphics (:51)
                     "doublesize", "offDisplaySize", "largeTextFont",  # Text (:55)
                     "minAutoBrightness", "sensAutoBrightness"):       # Boom (:58-61)
        assert expected in names, expected


def test_alarm_extra_fields_adds_the_conditional_volume_pref():
    """Alarm.pm:46-50 — ``alarmDefaultVolume`` nur ohne Digital-LautstÃ¤rke."""
    assert [f.pref for f in sp.ALARM_EXTRA_FIELDS] == ["alarmDefaultVolume"]


# ── Gegenproben: bestehende Seiten und unbekannte Pfade unverÃ¤ndert ────────

def test_existing_server_page_is_unchanged(players):
    res = _request("GET", "/settings/server/basic.html")
    assert res.status_code == 200
    assert 'name="pref_language"' in res.text


def test_unknown_settings_path_is_still_404():
    res = _request("GET", "/settings/player/does-not-exist.html")
    assert res.status_code == 404


def test_player_pages_are_not_in_the_reference_registry_yet():
    """Solange der Patch nicht eingetragen ist, bleiben die Pfade frei (Gegenprobe)."""
    from lyrion.web import settings as S

    for route in sp.PLAYER_PAGES:
        assert route not in S.PAGES
