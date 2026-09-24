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


# ── Server: Software-Updates (Server/Software.pm:23-31, Update.pm:30-141) ───
#
# Perl zeigt ``pref_checkVersion`` und ``pref_checkVersionInterval``
# (``software.html:4-18``), dazu ``pref_autoDownloadUpdate`` nur bei
# ``canAutoUpdate`` (``Software.pm:26-28``) und den Knopf ``checkForUpdateNow``
# nur, wenn NICHT aus dem Quellbaum gestartet (``software.html:35-38``).  Die
# Prüfung selbst liest ``servers.json`` (``Update.pm:17,84``) und vergleicht die
# Version (``Update.pm:104-111``); **ein Selbst-Update führen wir nicht aus**.


def test_software_get_has_the_perl_pref_fields():
    body = _request("GET", "/settings/server/software.html").text

    assert 'name="pref_checkVersion"' in body
    assert 'name="pref_checkVersionInterval"' in body
    # Software.pm:24 — die vier Intervalle aus software.html:15-18
    for value in ("3600", "86400", "604800", "2592000"):
        assert f'value="{value}"' in body
    # Vorbelegung Prefs.pm:170-171
    assert '<option value="1" selected>' in body
    assert '<option value="86400" selected>' in body
    # Versionsanzeige (Software.pm:40-42 via $::newVersion/main::VERSION)
    assert "9.2.0" in body
    # Knopf nur ohne Quellbaum-Start (software.html:35-38); unser Default ist
    # „nicht aus dem Quellbaum" (OSDetect hat kein running_from_source).
    assert 'name="checkForUpdateNow"' in body


def test_software_post_writes_checkversion_prefs():
    res = _request("POST", "/settings/server/software.html",
                   data={"saveSettings": "1", "pref_checkVersion": "0",
                         "pref_checkVersionInterval": "604800"})

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("checkVersion") == "0"                 # Prefs.pm:170
    assert prefs.get("checkVersionInterval") == "604800"    # Prefs.pm:171


def test_software_can_auto_update_is_false_like_perl_on_linux():
    """``Slim/Utils/OS.pm:451`` ``sub canAutoUpdate { 0 }`` — Linux: kein Feld."""
    from lyrion.web.settings import _can_auto_update

    assert _can_auto_update() is False
    # ``software.html:24-32`` rendert das Feld dann nicht.
    body = _request("GET", "/settings/server/software.html").text
    assert 'name="pref_autoDownloadUpdate"' not in body


def test_software_version_compare_matches_update_pm():
    """``Update.pm:111`` — Repo-Version muss höher sein als die laufende."""
    from lyrion.web.settings import _evaluate_update_payload, _version_greater

    assert _version_greater("9.3.0", "9.2.0") is True
    assert _version_greater("9.2.0", "9.2.0") is False
    assert _version_greater("9.1.9", "9.2.0") is False
    assert _version_greater("10.0.0", "9.9.9") is True

    # Ein höheres Repo-Ergebnis ergibt SERVER_UPDATE_AVAILABLE (Update.pm:117)
    payload = {"latest": {"default": {"version": "9.9.9", "url": "http://x/y.tgz"}}}
    msg = _evaluate_update_payload(payload, "9.2.0")
    assert "9.9.9" in msg and "y.tgz" in msg
    # Kein Update → leere Meldung (Aufrufer setzt CONTROLPANEL_NO_UPDATE_AVAILABLE)
    assert _evaluate_update_payload(
        {"latest": {"default": {"version": "9.2.0"}}}, "9.2.0") == ""


# ── Server: Netzwerk (Server/Network.pm:27-41) ─────────────────────────────

def test_network_get_has_the_unconditional_perl_prefs():
    body = _request("GET", "/settings/server/networking.html").text

    # Network.pm:28 — die sieben unbedingten Prefs in der Vorlagenreihenfolge
    for name in ("webproxy", "httpport", "maxRedirects", "bufferSecs",
                 "remotestreamtimeout", "maxWMArate", "useEnhancedHTTP"):
        assert f'name="pref_{name}"' in body, name
    # Vorbelegungen aus Prefs.pm:210-216
    assert 'value="9000"' in body         # httpport
    assert 'value="3"' in body            # bufferSecs
    assert 'value="15"' in body           # remotestreamtimeout
    assert 'value="7"' in body            # maxRedirects
    # networking.html:25 — die WMA-Raten
    for rate in ("9999", "128", "320"):
        assert f'value="{rate}"' in body


def test_network_post_writes_the_prefs_and_reports_port_change():
    res = _request("POST", "/settings/server/networking.html",
                   data={"saveSettings": "1", "pref_httpport": "9123",
                         "pref_bufferSecs": "10", "pref_useEnhancedHTTP": "2"})

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("httpport") == "9123"
    assert prefs.get("bufferSecs") == "10"
    assert prefs.get("useEnhancedHTTP") == "2"
    # Network.pm:53-60 — SETUP_HTTPPORT_OK mit der Server-URL
    assert "Now using port:" in res.text


def test_network_rejects_out_of_range_buffer_secs():
    """``Prefs.pm:319`` ``intlimit 3..30`` → SETTINGS_INVALIDVALUE, nicht schreiben."""
    _set_pref("bufferSecs", "5")
    res = _request("POST", "/settings/server/networking.html",
                   data={"saveSettings": "1", "pref_bufferSecs": "99"})

    assert 'Invalid value "99" for bufferSecs' in res.text
    assert get_prefs().get("bufferSecs") == "5"           # unverändert


def test_network_hides_syncstartdelay_without_multiple_players():
    """``Network.pm:36-38`` — ``syncStartDelay`` nur bei ``clients() > 1``."""
    body = _request("GET", "/settings/server/networking.html").text
    assert 'name="pref_syncStartDelay"' not in body


# ── Server: Sicherheit (Server/Security.pm:26-64) ──────────────────────────

def test_security_get_lists_the_perl_prefs_and_password_fields():
    body = _request("GET", "/settings/server/security.html").text

    # Security.pm:27 ``prefs()`` + die zwei Passwortfelder der Vorlage
    for name in ("authorize", "username", "password", "password_repeat",
                 "filterHosts", "allowedHosts", "csrfProtectionLevel",
                 "corsAllowedHosts", "insecureHTTPS"):
        assert f'name="pref_{name}"' in body, name
    assert '<input type="password"' in body               # security.html:18,22


def test_security_hashes_the_password_like_perl():
    """``Security.pm:57-58`` — ``sha1_base64``, Klartext wird nie gespeichert."""
    from lyrion.web.settings import perl_sha1_base64

    expected = perl_sha1_base64("geheim")
    res = _request("POST", "/settings/server/security.html",
                   data={"saveSettings": "1", "pref_username": "admin",
                         "pref_password": "geheim",
                         "pref_password_repeat": "geheim"})

    assert res.status_code == 200
    stored = get_prefs().get("password")
    assert stored == expected
    assert stored != "geheim"
    # Der Hash darf nicht im Formular stehen (wir geben ihn nicht heraus).
    body = _request("GET", "/settings/server/security.html").text
    assert expected not in body
    # ``password_repeat`` ist reines Vergleichsfeld (Security.pm:46)
    assert get_prefs().get("password_repeat") is None


def test_security_password_mismatch_warns_and_disables_authorize():
    """``Security.pm:46-51`` — ``SETUP_PASSWORD_MISMATCH``, ``authorize = 0``."""
    res = _request("POST", "/settings/server/security.html",
                   data={"saveSettings": "1", "pref_authorize": "1",
                         "pref_username": "admin",
                         "pref_password": "a", "pref_password_repeat": "b"})

    assert "The passwords you entered do not match." in res.text
    assert get_prefs().get("authorize") == "0"


def test_security_authorize_without_username_warns():
    """``Security.pm:34-39`` — ``SETUP_MISSING_USERNAME``, ``authorize = 0``."""
    res = _request("POST", "/settings/server/security.html",
                   data={"saveSettings": "1", "pref_authorize": "1",
                         "pref_username": ""})

    assert "You can't enable authorization without a password." in res.text
    assert get_prefs().get("authorize") == "0"


# ── Server: Dateitypen (Server/FileTypes.pm:27-133) ────────────────────────

def test_filetypes_get_has_the_perl_fields_and_profile_table():
    body = _request("GET", "/settings/server/filetypes.html").text

    # FileTypes.pm:28,37-38 — die Vorlage gibt die Endungen OHNE ``pref_`` aus
    assert 'name="disabledextensionsaudio"' in body
    assert 'name="disabledextensionsplaylist"' in body
    assert 'name="pref_prioritizeNative"' in body         # :28
    # filetypes.html:21 — die Tabellenköpfe
    for token in ("File Format", "Stream Format", "From", "Decoder"):
        assert token in body
    # filetypes.html:37,39-40 — je Profil ein ``select`` mit ``DISABLED``
    assert 'value="DISABLED"' in body


def test_filetypes_post_writes_extensions_and_disabledformats():
    res = _request("POST", "/settings/server/filetypes.html",
                   data={"saveSettings": "1",
                         "disabledextensionsaudio": "cue, mp4",
                         "disabledextensionsplaylist": "m3u",
                         "pref_prioritizeNative": "0",
                         "flc-mp3-*-*": "DISABLED"})

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("disabledextensionsaudio") == "cue, mp4"      # :37
    assert prefs.get("disabledextensionsplaylist") == "m3u"        # :38
    assert prefs.get("prioritizeNative") == "0"                    # :68
    assert prefs.get("disabledformats") == "flc-mp3-*-*"           # :40,67


# ── Server: Logging (Server/Debugging.pm:24-78) ───────────────────────────

def test_debugging_get_has_the_levels_groups_and_logfiles():
    body = _request("GET", "/settings/server/debugging.html").text

    # Log.pm:64 — die sechs Stufen
    for level in ("OFF", "FATAL", "ERROR", "WARN", "INFO", "DEBUG"):
        assert f'value="{level}"' in body, level
    # Log.pm:66-102 — die vier Protokollsätze
    for group in ("SERVER", "RADIO", "TRANSCODING", "SCANNER", "DEFAULT"):
        assert f'value="{group}"' in body, group
    # Log.pm:645-651 — die Logdatei-Links der Vorlage (debugging.html:21-27)
    assert "/lyrion.log?lines=100" in body
    assert "/lyrion.log?full=1" in body
    assert "/lyrion.log?zip=1" in body
    # debugging.html:47-55 — Kategoriename als Feldname, plus Beschriftung
    assert 'name="network.http"' in body
    assert '(network.http)' in body


def test_debugging_post_writes_single_category_level():
    """``Debugging.pm:40-46`` — je Kategorie der Formularwert."""
    res = _request("POST", "/settings/server/debugging.html",
                   data={"saveSettings": "1", "logging_group": "",
                         "persist": "1", "network.http": "DEBUG",
                         "server": "INFO"})

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("log.level.network.http") == "DEBUG"
    assert prefs.get("log.level.server") == "INFO"
    assert prefs.get("log.persist") == "1"                 # :48


def test_debugging_post_applies_a_logging_group():
    """``Debugging.pm:32-36`` — ``setLogGroup`` setzt ALLE Kategorien."""
    _request("POST", "/settings/server/debugging.html",
             data={"saveSettings": "1", "logging_group": "RADIO"})

    prefs = get_prefs()
    assert prefs.get("log.group") == "RADIO"
    # Log.pm:74-80 — der RADIO-Satz hebt ``formats.audio``/``network.asyncdns``
    assert prefs.get("log.level.formats.audio") == "DEBUG"
    assert prefs.get("log.level.network.asyncdns") == "DEBUG"
    # Kategorien außerhalb des Satzes behalten ihre Vorbelegung
    assert prefs.get("log.level.server") == "ERROR"


# ── Server: Leistung (Server/Performance.pm:26-30) ────────────────────────

def test_performance_get_has_the_perl_prefs():
    body = _request("GET", "/settings/server/performance.html").text

    for name in ("dbhighmem", "dontTriggerScanOnPrefChange", "useBalancedShuffle",
                 "disableStatistics", "precacheArtwork", "useLocalImageproxy",
                 "serverPriority", "scannerPriority", "maxPlaylistLength"):
        assert f'name="pref_{name}"' in body, name
    # Prefs.pm:221 — ``precacheArtwork`` vorbelegt AN
    assert 'value="1" selected' in body
    # Performance.pm:82-91 — die fünf Prioritätslabels
    for token in ("Above Normal", "Below Normal"):
        assert token in body


def test_performance_hides_autorescan_without_os_support():
    """``Performance.pm:28`` — nur bei ``canAutoRescan`` (``OS.pm:455`` = 0)."""
    from lyrion.web.settings import _can_auto_rescan

    assert _can_auto_rescan() is False
    body = _request("GET", "/settings/server/performance.html").text
    assert 'name="pref_autorescan"' not in body


def test_performance_post_writes_the_prefs():
    res = _request("POST", "/settings/server/performance.html",
                   data={"saveSettings": "1", "pref_dbhighmem": "1",
                         "pref_maxPlaylistLength": "2500",
                         "pref_scannerPriority": "5",
                         "pref_dontTriggerScanOnPrefChange": "0"})

    assert res.status_code == 200
    prefs = get_prefs()
    assert prefs.get("dbhighmem") == "1"                    # Prefs.pm:139
    assert prefs.get("maxPlaylistLength") == "2500"         # Prefs.pm:223
    assert prefs.get("scannerPriority") == "5"              # Prefs.pm:220
    assert prefs.get("dontTriggerScanOnPrefChange") == "0"  # Prefs.pm:167
