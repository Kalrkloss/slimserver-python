"""Web-Settings-Seiten über die ASGI-App (Perl ``Slim/Web/Settings/*``).

Perl-Belege (``/tmp/lms-ref``, read-only)
==========================================

**Routen** (jede Klasse liefert ihre URL aus ``page()``; ``Slim/Web/Pages.pm:119-134``
registriert sie in einem Regexp-Hash, ``Slim/Web/Settings.pm:47-66`` ruft das beim
Laden auf):

* ``settings/server/basic.html`` — ``Slim/Web/Settings/Server/Basic.pm:23-25``
  (``name`` :19-21 ``BASIC_SERVER_SETTINGS``).
* ``settings/player/audio.html`` — ``Slim/Web/Settings/Player/Audio.pm:22-24``
  (``name`` :18-20 ``AUDIO_SETTINGS``, ``needsClient`` :26-28).
* ``settings/player/display.html`` — ``.../Player/Display.pm:22-24``
  (``name`` :18-20 ``DISPLAY_SETTINGS``, ``needsClient`` :26-28).
* ``settings/player/alarm.html`` — ``.../Player/Alarm.pm:33-35``
  (``name`` :29-31 ``ALARM``, ``needsClient`` :37-39).
* ``settings/server/status.html`` — ``.../Server/Status.pm:19-21``
  (``name`` :15-17 ``INFORMATION``). Die Aufgaben-Route ``/settings/information.html``
  existiert in Perl NICHT; wir bedienen sie als Alias (siehe Modul-Docstring).

**Antwortform**: HTML-Seite (``Slim/Web/Settings.pm:286`` ``filltemplatefile``),
Content-Type ``text/html`` (``Slim/Web/HTTP.pm:1254,1293``); ``useAJAX=1`` rendert
stattdessen ``settings/ajaxSettings.txt`` (``Slim/Web/Settings.pm:286``, Inhalt
``HTML/EN/settings/ajaxSettings.txt``: ``warning|<text>`` + ``<pref>|<valid>``).

**Pref-Schreibpfad**: ``pref_<name>``-Parameter → ``$prefsClass->set($pref, …)``
(``Slim/Web/Settings.pm:154-171``), Client-Prefs über ``$prefs->client($client)``
(z. B. ``Player/Audio.pm:119``, ``Player/Display.pm:63``, ``Player/Alarm.pm:52``).
``Alarm.pm:76-77`` rechnet Minuten (Formular) in Sekunden (Pref) um.
``Server/Basic.pm:88-121`` schreibt zusätzlich ``mediadirs`` / ``ignoreInAudioScan``
(Parameter ``pref_mediadirs0``/``pref_ignoreInAudioScan0``, Vorlage
``HTML/EN/settings/server/basic.html:63,66``).

Die App läuft in-process (``httpx.ASGITransport`` gegen ``create_app()``); der
Pref-Store ist die In-Memory-Variante des globalen ``PreferenceStore`` (Muster
aus ``tests/test_prefs.py`` und ``tests/test_jive_settings_commands.py``).
"""

from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import radiobrowser
from lyrion.web.app import create_app

MAC = "1C:87:2C:47:FC:36"


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """Globalen Prefs-Store auf In-Memory-DB hängen und am Modulende schließen.

    Ohne das Schließen hängt aiosqlites Worker-Thread (``aiosqlite/core.py:59``)
    am Prozessende und pytest beendet sich nicht mehr (siehe
    ``tests/test_jive_settings_commands.py:125-143``).
    """
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
        except Exception:  # noqa: BLE001 — Aufräumen darf nicht scheitern
            pass
        store._db = None


@pytest.fixture
def player():
    """SqueezePlay-artiger Client (``needsClient``-Seiten brauchen ``playerid``)."""
    p = PlayerState(mac=MAC, name="Küche", ip="192.168.1.225", port=48512,
                    model="squeezeplay", connected=True, power=True)
    pm = PlayerManager()
    old = dict(pm.players)
    pm.players = {MAC: p}
    yield p
    pm.players = old


def _request(method: str, path: str, *, params=None, data=None) -> httpx.Response:
    """Eine Anfrage gegen die ASGI-App (in-process, kein Serverstart)."""
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.request(method, path, params=params, data=data)

    return asyncio.run(run())


def _set_pref(name: str, value) -> None:
    asyncio.run(get_prefs().set(name, value))


# ── /settings/server/basic.html (Server/Basic.pm:23-29, :88-121) ───────────

def test_server_basic_get_has_the_perl_pref_fields_and_form():
    _set_pref("language", "DE")
    _set_pref("libraryname", "Wohnzimmer")
    res = _request("GET", "/settings/server/basic.html")

    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html"
    body = res.text
    # Feldnamen = prefs().language/playlistdir/libraryname (Basic.pm:28)
    assert 'name="pref_language"' in body
    assert 'name="pref_libraryname"' in body
    assert 'name="pref_playlistdir"' in body
    # Formularwie in HTML/EN/settings/header.html:49 + footer.html:38-39
    assert 'name="settingsForm"' in body
    assert 'name="saveSettings"' in body
    # Die aktuellen Werte stehen drin
    assert 'value="DE"' in body
    assert 'value="Wohnzimmer"' in body


def test_server_basic_post_writes_the_prefs():
    res = _request("POST", "/settings/server/basic.html", data={
        "saveSettings": "1", "page": "BASIC_SERVER_SETTINGS", "useAJAX": "0",
        "pref_language": "FR", "pref_libraryname": "Keller",
        "pref_playlistdir": "/playlists",
    })

    assert res.status_code == 200
    assert "Changes have been saved." in res.text      # SETUP_CHANGES_SAVED
    prefs = get_prefs()
    assert prefs.get("language") == "FR"
    assert prefs.get("libraryname") == "Keller"
    assert prefs.get("playlistdir") == "/playlists"


def test_server_basic_post_writes_mediadirs_list():
    # Basic.pm:88-121; Parameternamen aus basic.html:63,66
    res = _request("POST", "/settings/server/basic.html", data={
        "saveSettings": "1",
        "pref_mediadirs0": "/music", "pref_mediadirs1": "/music2",
        "pref_ignoreInAudioScan1": "1",
    })

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("mediadirs") == "/music,/music2"
    # Basic.pm:102 — nicht angehakte Ordner landen in ignoreInAudioScan
    assert prefs.get("ignoreInAudioScan") == "/music"


def test_server_basic_get_renders_mediadir_rows():
    _set_pref("mediadirs", "/music,/music2")
    _set_pref("ignoreInAudioScan", "/music")
    body = _request("GET", "/settings/server/basic.html").text

    assert 'name="pref_mediadirs0" id="mediadirs0" value="/music"' in body
    assert 'name="pref_mediadirs1" id="mediadirs1" value="/music2"' in body
    # Leerzeile für einen weiteren Ordner (Basic.pm:146-148)
    assert 'name="pref_mediadirs2"' in body


# ── /settings/player/audio.html (Player/Audio.pm:22-33) ────────────────────

def test_player_audio_get_lists_the_unconditional_prefs(player):
    res = _request("GET", "/settings/player/audio.html", params={"playerid": MAC})

    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html"
    body = res.text
    for name in ("powerOnResume", "maxBitrate", "lameQuality", "fadeInDuration"):
        assert f'name="pref_{name}"' in body, name
    # Audio.pm:33 — powerOnResume-Optionen aus audio.html:6-13
    assert 'value="StopOff-ResetPlayOn"' in body


def test_player_audio_post_writes_the_client_pref(player):
    res = _request("POST", "/settings/player/audio.html",
                   params={"playerid": MAC},
                   data={"saveSettings": "1", "pref_powerOnResume": "PauseOff-PlayOn",
                         "pref_maxBitrate": "192"})

    assert res.status_code == 200
    assert "Changes have been saved." in res.text
    assert player.playerprefs["powerOnResume"] == "PauseOff-PlayOn"
    assert player.playerprefs["maxBitrate"] == "192"


# ── /settings/player/display.html (Player/Display.pm:22-64) ────────────────

def test_player_display_get_lists_the_unconditional_prefs(player):
    res = _request("GET", "/settings/player/display.html", params={"playerid": MAC})

    assert res.status_code == 200
    body = res.text
    for name in ("powerOnBrightness", "powerOffBrightness", "idleBrightness",
                 "scrollMode", "scrollPause", "scrollPauseDouble",
                 "scrollRate", "scrollRateDouble", "alwaysShowCount"):
        assert f'name="pref_{name}"' in body, name
    # Display/Display.pm:401-411 — Helligkeitsoptionen 0..4
    assert 'value="0"' in body and 'value="4"' in body
    # display.html:144-148 — scrollMode-Optionen
    assert 'value="2"' in body


def test_player_display_post_writes_the_client_pref(player):
    res = _request("POST", "/settings/player/display.html",
                   params={"playerid": MAC},
                   data={"saveSettings": "1", "pref_idleBrightness": "2",
                         "pref_scrollMode": "1"})

    assert res.status_code == 200
    assert player.playerprefs["idleBrightness"] == "2"
    assert player.playerprefs["scrollMode"] == "1"


# ── /settings/player/alarm.html (Player/Alarm.pm:33-53, :76-77) ────────────

def test_player_alarm_post_converts_minutes_to_seconds(player):
    res = _request("POST", "/settings/player/alarm.html",
                   params={"playerid": MAC},
                   data={"saveSettings": "1",
                         "pref_alarmsEnabled": "1",
                         "pref_alarmDefaultVolume": "30",
                         "pref_alarmfadeseconds": "0",
                         "pref_alarmSnoozeMinutes": "5",
                         "pref_alarmTimeoutMinutes": "10"})

    assert res.status_code == 200
    # Alarm.pm:76-77 — ×60 in die Sekunden-Prefs
    assert player.playerprefs["alarmSnoozeSeconds"] == "300"
    assert player.playerprefs["alarmTimeoutSeconds"] == "600"
    assert player.playerprefs["alarmsEnabled"] == "1"
    assert player.playerprefs["alarmDefaultVolume"] == "30"
    assert player.playerprefs["alarmfadeseconds"] == "0"


def test_player_alarm_get_shows_minutes_from_the_seconds_pref(player):
    player.playerprefs["alarmSnoozeSeconds"] = "300"      # Alarm.pm:110
    body = _request("GET", "/settings/player/alarm.html",
                    params={"playerid": MAC}).text

    assert 'name="pref_alarmSnoozeMinutes" id="alarmSnoozeMinutes" value="5"' in body


# ── Player-Seite ohne Client (Player/Display.pm:101-105) ───────────────────

def test_player_page_without_playerid_warns_and_does_not_write(player):
    res = _request("GET", "/settings/player/audio.html")

    assert res.status_code == 200
    assert "There are no settings for this player on this page" in res.text  # SETUP_NO_PREFS

    post = _request("POST", "/settings/player/audio.html",
                    data={"saveSettings": "1", "pref_powerOnResume": "StopOff-NoneOn"})
    assert post.status_code == 200
    assert player.playerprefs == {}


# ── Information / Status (Server/Status.pm:19-21) ────────────────────────

def test_status_page_and_information_alias():
    res = _request("GET", "/settings/server/status.html")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html"
    # Gruppentitel = Perl-Tokens (Status.pm:33-36)
    assert "Lyrion Music Server" in res.text
    assert "Player Information" in res.text

    alias = _request("GET", "/settings/information.html")
    assert alias.status_code == 200
    assert alias.headers["content-type"] == "text/html"


# ── AJAX-Fragment + 404 (Settings.pm:286, HTTP.pm:1128-1131) ──────────────

def test_ajax_post_returns_the_ajaxsettings_fragment():
    res = _request("POST", "/settings/server/basic.html", data={
        "saveSettings": "1", "useAJAX": "1", "pref_language": "EN",
    })

    assert res.status_code == 200
    assert res.headers["content-type"] == "text/plain"
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("warning|")          # ajaxSettings.txt
    assert "language|1" in lines                    # Settings.pm:165-166


def test_unknown_settings_path_is_404():
    res = _request("GET", "/settings/server/bogus.html")
    assert res.status_code == 404


# ── Das Radio-Land (``radiobrowser_country``, Feld auf Basic.pm:23-29) ──────
#
# Perl hat keine solche Pref (``Slim/Utils/Prefs.pm:132-279`` kennt kein
# ``country``; der ``local``-Knoten ist TuneIns geolokalisierter,
# ``TuneIn.pm:38-40``).  Form und Beschriftung kommen aus Perls einziger
# country-Einstellung (Podcast-Plugin: ``Settings.pm:19,29-31``,
# ``HTML/EN/plugins/Podcast/settings/basic.html:81-87``,
# ``strings.txt`` ``PLUGIN_PODCAST_COUNTRY``), der Erst-Wert aus den
# Server-Einstellungen (Locale → Zeitzone, ``Slim/Utils/OS.pm:399-414``).


def _clear_radio_country() -> None:
    """Pref ``radiobrowser_country`` aus Cache und DB entfernen (nie gesetzt)."""
    store = get_prefs()
    name = radiobrowser.COUNTRY_PREF
    store._cache.pop(name, None)
    if getattr(store, "_db", None) is not None:
        async def _delete() -> None:
            await store._db.execute("DELETE FROM prefhash WHERE name = ?", (name,))
            await store._db.commit()

        asyncio.run(_delete())


def _stub_countries(monkeypatch) -> None:
    """Länderliste ohne radio-browser-Roundtrip (deren Spiegel-Test liegt in
    ``tests/test_radiobrowser.py``)."""
    async def _countries():
        return [{"name": "Germany", "code": "DE", "stationcount": 9},
                {"name": "France", "code": "FR", "stationcount": 4}]

    monkeypatch.setattr(radiobrowser, "countries", _countries)


def test_radio_country_field_renders_the_country_list(monkeypatch):
    _stub_countries(monkeypatch)
    _clear_radio_country()
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)

    body = _request("GET", "/settings/server/basic.html").text

    assert 'name="pref_radiobrowser_country" id="radiobrowser_country"' in body
    # Erste Option = leerer Wert, Beschriftung aus Perls ``ALL``-Token.
    assert '<option value="">all</option>' in body
    # Die abgeleitete Server-Einstellung ist vorbelegt.
    assert '<option value="DE" selected>Germany</option>' in body
    assert '<option value="FR">France</option>' in body
    # Label aus der String-Tabelle (PLUGIN_PODCAST_COUNTRY).
    assert 'for="radiobrowser_country">Country</label>' in body


def test_radio_country_first_get_stores_the_derived_country(monkeypatch):
    """Erst-Vorbelegung: ``Slim/Utils/Prefs/Base.pm:178-234`` wertet den
    abgeleiteten Default einmal aus und speichert ihn."""
    _stub_countries(monkeypatch)
    _clear_radio_country()
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    assert get_prefs().get(radiobrowser.COUNTRY_PREF) is None
    _request("GET", "/settings/server/basic.html")
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == "US"

    # Danach gewinnt der gespeicherte Wert — eine andere Locale ändert ihn nicht.
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    _request("GET", "/settings/server/basic.html")
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == "US"


def test_radio_country_post_writes_the_pref(monkeypatch):
    _stub_countries(monkeypatch)

    res = _request("POST", "/settings/server/basic.html",
                   data={"saveSettings": "1", "pref_radiobrowser_country": "FR"})
    assert res.status_code == 200
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == "FR"
    assert radiobrowser.local_country() == "FR"

    # "alle Länder" ist der *gespeicherte* leere Wert, nicht "nie gesetzt".
    _request("POST", "/settings/server/basic.html",
             data={"saveSettings": "1", "pref_radiobrowser_country": ""})
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == ""
    assert radiobrowser.local_country() == ""


def test_radio_country_rejects_an_invalid_value(monkeypatch):
    """``Settings.pm:162-169``: nicht speichern + ``SETTINGS_INVALIDVALUE``."""
    _stub_countries(monkeypatch)
    _request("POST", "/settings/server/basic.html",
             data={"saveSettings": "1", "pref_radiobrowser_country": "FR"})

    res = _request("POST", "/settings/server/basic.html",
                   data={"saveSettings": "1", "pref_radiobrowser_country": "XYZ"})
    assert 'Invalid value "XYZ" for radiobrowser_country' in res.text   # strings.txt
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == "FR"           # unverändert

    ajax = _request("POST", "/settings/server/basic.html",
                    data={"saveSettings": "1", "useAJAX": "1",
                          "pref_radiobrowser_country": "de"})
    assert "radiobrowser_country|0" in ajax.text.splitlines()           # validated=0
    assert get_prefs().get(radiobrowser.COUNTRY_PREF) == "FR"


# ── Bildproxy-Schalter (``imageProxyFollowRedirects``, bewusste Abweichung) ──
#
# Perl hat KEINE solche Pref: ``Slim/Web/ImageProxy.pm:145-155`` antwortet auf
# ``/imageproxy/<url>/image.jpg`` mit 301 und lässt den Client laden.  Der
# Schalter (Vorbelegung AN) lässt den Server das Bild selbst holen; AUS stellt
# Perls 301 wieder her.  Die Bildproxy-Tests dazu liegen in
# ``tests/test_imageproxy.py``.


def test_imageproxy_follow_switch_is_on_by_default_and_writable():
    """Feld auf ``/settings/server/basic.html``: vorbelegt AN, per POST schaltbar."""
    from lyrion.web.settings import (
        IMAGEPROXY_FOLLOW_REDIRECTS_PREF as PREF,
        imageproxy_follow_redirects,
    )

    store = get_prefs()
    store._cache.pop(PREF, None)                 # nie gesetzt = frischer Server

    body = _request("GET", "/settings/server/basic.html").text
    assert f'name="pref_{PREF}" id="{PREF}"' in body
    assert '<option value="1" selected>' in body          # Vorbelegung AN
    assert '<option value="0">' in body
    assert imageproxy_follow_redirects() is True          # Default greift ohne Wert

    res = _request("POST", "/settings/server/basic.html",
                   data={"saveSettings": "1", f"pref_{PREF}": "0"})
    assert res.status_code == 200
    assert store.get(PREF) == "0"
    assert imageproxy_follow_redirects() is False         # AUS = Perls 301

    _request("POST", "/settings/server/basic.html",
             data={"saveSettings": "1", f"pref_{PREF}": "1"})
    assert store.get(PREF) == "1"
    assert imageproxy_follow_redirects() is True
