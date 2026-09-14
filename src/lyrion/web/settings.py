"""Web-Settings-Seiten als ASGI-Routen (Perl ``Slim/Web/Settings/*``).

Perl-Referenz (``/tmp/lms-ref``, read-only)
===========================================

**Routing.** ``Slim/Web/Pages.pm:119-134`` (``addPageFunction``) registriert jede
Settings-Klasse unter ihrer ``page()``-URL in einem Regexp-Hash;
``Slim/Web/Settings.pm:47-66`` tut das für jede Unterklasse. ``Slim/Web/HTTP.pm:1160-1176``
schlägt den Pfad in diesem Hash nach, holt bei ``needsClient`` den Client über den
``playerid``-Parameter (``Slim/Web/HTTP.pm:1164-1169``) und ruft ``$class->handler(...)``.

**Schreiben.** Der Basis-Handler ``Slim/Web/Settings.pm:135-287``: bei ``saveSettings``
schreibt er für jede in ``prefs()`` gemeldete Pref den Parameter ``pref_<name>`` über
``$prefsClass->set($pref, ...)`` (``Settings.pm:154-171``) und rendert danach die Seite
neu (``Settings.pm:198-200`` „SETUP_CHANGES_SAVED"); ``useAJAX`` rendert stattdessen das
Fragment ``settings/ajaxSettings.txt`` (``Settings.pm:286``, Inhalt
``HTML/EN/settings/ajaxSettings.txt``). ``Slim/Web/Pages/Home.pm:66`` verweist auf
``/settings/server/wizard.html``; die klassische UI baut ihre Settings-Links in
``HTML/Default/html/Settings.js:143,152,159`` (``settings/player/basic.html?``,
``settings/server/formatting.html?``, ``settings/server/basic.html?``).

Seiten + Feldlisten (je ``page()``/``prefs()``/``handler()``)
------------------------------------------------------------

* ``GET|POST /settings/server/basic.html`` — ``Slim/Web/Settings/Server/Basic.pm``:
  ``page`` :23-25, ``prefs`` :27-29 (``language playlistdir libraryname``),
  ``mediadirs``/``ignoreInAudioScan`` :88-121, Vorlage
  ``HTML/EN/settings/server/basic.html:37,48,63,66,80``.
  Zusatzfeld dieses Ports: ``pref_radiobrowser_country`` (Land des
  ``local``-Radio-Knotens). Perl hat dafür KEINE Pref (``Slim/Utils/Prefs.pm:132-279``
  kennt kein ``country``; ``local`` ist TuneIns geolokalisierter Knoten,
  ``Slim/Plugin/InternetRadio/TuneIn.pm:38-40``) und keine Server-Seite — wir
  hängen das Feld an dieselbe Seite, auf der Perls vergleichbare
  Locale-abgeleitete Pref ``language`` liegt (``Slim/Utils/Prefs.pm:161`` →
  ``:676-678`` → ``Slim/Utils/OS.pm:399-414``). Form (2-stelliges ISO-Feld) und
  Beschriftung kommen aus Perls einziger „country“-Einstellung, dem
  Plugin-Feld ``pref_country`` (``Slim/Plugin/Podcast/Settings.pm:19,29-31``,
  ``HTML/EN/plugins/Podcast/settings/basic.html:81-87``,
  ``Slim/Plugin/Podcast/strings.txt`` ``PLUGIN_PODCAST_COUNTRY``); dessen
  String-Tabelle ist in Perls globale Tabelle eingemischt (``Slim/Utils/Strings.pm``
  lädt auch ``Slim/Plugin/*/strings.txt``), der Schlüssel ist also global gültig.
  Die Auswahlliste ist eine Zutat dieses Ports (radio-browser statt TuneIn) und
  kommt aus ``lyrion.web.radiobrowser.countries()``.
* ``GET|POST /settings/player/audio.html`` — ``.../Player/Audio.pm``: ``page`` :22-24,
  ``needsClient`` :26-28, ``prefs`` :30-120 (hier die unbedingten: :33),
  ``HTML/EN/settings/player/audio.html:4,144,348,367``.
* ``GET|POST /settings/player/display.html`` — ``.../Player/Display.pm``: ``page`` :22-24,
  ``needsClient`` :26-28, ``validFor`` :30-35, ``prefs`` :37-64;
  Helligkeit 0..4 = ``Slim/Display/Display.pm:401-411``,
  ``HTML/EN/settings/player/display.html:16,128,143,158,172``.
* ``GET|POST /settings/player/alarm.html`` — ``.../Player/Alarm.pm``: ``page`` :33-35,
  ``prefs`` :41-53, und ``pref_alarmSnoozeMinutes``/``pref_alarmTimeoutMinutes`` →
  ``alarmSnoozeSeconds``/``alarmTimeoutSeconds`` ×60 (:76-77);
  ``HTML/EN/settings/player/alarm.html:107,144,149,153,157``.
* ``GET /settings/server/status.html`` — ``.../Server/Status.pm``: ``name`` :15-17
  (``INFORMATION``), ``page`` :19-21.

**Die Aufgabe nennt ``/settings/information.html`` — diese Route gibt es in Perl NICHT**
(``Slim/Web/Settings/Server/Status.pm:20`` liefert ``settings/server/status.html``). Wir
bedienen deshalb beide Pfade; massgeblich ist ``settings/server/status.html``, der Alias
ist eine Zutat der Aufgabenstellung, kein Perl-Fund.

UNKLAR / bewusst nicht nachgebildet (kein Blindflug, keine erfundenen Felder)
-----------------------------------------------------------------------------
* ``pref_rescan``/``pref_rescantype`` und der ``Slim::Music::Import``-Scan-Trigger
  (``Server/Basic.pm:39-62,118-127``) hängen an der Scan-Queue; die Felder fehlen hier.
* ``mediadirs``/``ignoreInAudioScan`` speichern wir kommasepariert — das ist das
  Listen-Format unseres Stores (``lyrion/config.py:271-273`` ``_coerce_type``), nicht
  Perls Array-Ref. Die Änderungs-Erkennung aus ``Basic.pm:118`` (nur sie steuert den
  Rescan) entfällt: wir schreiben die Liste immer.
* ``radiobrowser_country`` wird hier wie jeder andere Pref geschrieben, hat aber
  (noch) keinen Eintrag in ``lyrion/config.py`` ``_register_known_prefs``. Perl
  registriert den abgeleiteten Default in der ``%defaults``-Tabelle
  (``Slim/Utils/Prefs.pm:161``) und wertet ihn in ``Slim/Utils/Prefs/Base.pm:178-234``
  beim Start EINMAL aus; unser Erst-Wert entsteht deshalb beim ersten Lesen
  (``lyrion/web/radiobrowser.py`` ``ensure_country_pref``). Die Validierung des
  Formularwerts läuft hier (``Settings.pm:154-171``) — die Pref selbst hat in
  unserem Store keine Validator-Registrierung wie Perls ``setValidate``.
* ``Player/Audio`` + ``Player/Display`` blenden Felder abhängig von den
  Player-Fähigkeiten ein (``Audio.pm:35-117``, ``Display.pm:44-61``); wir liefern die
  unbedingten Prefs beider ``prefs()``-Listen.
* ``Player/Alarm`` legt Alarme selbst an/entfernt sie (``Alarm.pm:60-108,139-177``,
  ``Slim::Utils::Alarm``); hier werden nur die Alarm-Prefs geschrieben.
* Die Gruppen der Status-Seite (``INFORMATION_MENU_SERVER/PERL/LIBRARY/PLAYER``,
  ``Status.pm:30-56``) füllen wir mit echten Werten unseres Servers;
  ``INFORMATION_MENU_PERL`` (Perl- und Modulversionen) hat in der Python-
  Neuimplementierung kein Gegenstück.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, parse_qs

from lyrion.config import get_prefs
from lyrion.player.manager import PlayerManager
from lyrion.player.playerprefs import apply_player_pref
from lyrion.utils.strings import get_string

logger = logging.getLogger(__name__)

#: Perl ``Slim/Web/Settings/Server/Basic.pm:76-77`` des Alarm-Handlers: Minuten im
#: Formular, Sekunden in der Pref.
_MINUTES_TO_SECONDS = {
    "alarmSnoozeMinutes": "alarmSnoozeSeconds",
    "alarmTimeoutMinutes": "alarmTimeoutSeconds",
}

#: Die Pref dieses Ports für das Land des ``local``-Radio-Knotens
#: (``lyrion/web/radiobrowser.py`` ``COUNTRY_PREF``).
_RADIO_COUNTRY_PREF = "radiobrowser_country"


def _string(key: str, default: str) -> str:
    """Beschriftung eines Feldes aus der ``strings.txt``-Tabelle dieses Ports.

    Perl rendert Settings-Seiten mit ``Slim::Utils::Strings::string`` in der
    Server-Sprache (``Slim/Utils/Strings.pm:525-536``, Sprache =
    ``language``-Pref, ``Strings.pm:622-624`` ``getLanguage`` → ``$prefs->get
    ('language') || $failsafeLang``); ``lyrion.i18n`` ist genau diese Tabelle
    (``EN`` failsafe :62, ``DE``-Spalte) und liefert bei fehlender Zeile den
    übergebenen Literal — dieselbe Kette wie ``Strings.pm:414-416``.
    """
    from lyrion.i18n import get_string, resolve_language

    return get_string(key, resolve_language(), default=default)


def _is_country_code(value: str) -> bool:
    """Leer oder ISO-3166-1 alpha-2 in Grossbuchstaben.

    Perls ``set`` prüft Werte gegen den Validator der Pref
    (``Slim/Utils/Prefs/Base.pm:83-100`` ``validate``); der Podcast-Country
    kennt keine eigene Regel (``Slim/Plugin/Podcast/Settings.pm:19`` listet
    ``country`` nur als *hidden*), und das Feld selbst ist ein 2-Zeichen-Feld
    (``HTML/EN/plugins/Podcast/settings/basic.html:84`` ``size="2"``).  Eine
    leere Auswahl heisst hier „alle Länder“.
    """
    text = (value or "").strip()
    if not text:
        return True
    return len(text) == 2 and text.isalpha() and text.isupper()


@dataclass(frozen=True)
class Field:
    """Ein Formularfeld mit dem Parameternamen ``pref_<pref>`` (Perl ``Settings.pm:157``)."""

    pref: str
    label_key: str
    label_default: str
    kind: str = "text"                       # text | select
    options: tuple[tuple[str, str, str], ...] = ()   # (value, label_key, label_default)
    perl_source: str = ""
    #: Werte-Prüfung des Feldes; ``None`` = jeder Wert wird geschrieben.  Perl
    #: prüft beim ``set`` gegen den Validator der Pref und meldet einen
    #: abgelehnten Wert als ``SETTINGS_INVALIDVALUE`` (``Settings.pm:162-169``).
    validator: Optional[Callable[[str], bool]] = None

    def label(self) -> str:
        return _string(self.label_key, self.label_default)

    def option_label(self, key: str, default: str) -> str:
        return _string(key, default)


@dataclass(frozen=True)
class SettingsPage:
    """Eine ``Slim::Web::Settings``-Seite (``page()`` + ``prefs()``)."""

    route: str
    perl_class: str
    page_name: str
    title_key: str
    title_default: str
    needs_client: bool
    fields: tuple[Field, ...] = ()
    scope: str = "server"                    # "server" | "client"
    perl_source: str = ""


# ── Feldlisten: je Seite die ``prefs()``-Liste der Perl-Klasse ───────────────

_BASIC_SERVER = SettingsPage(
    route="/settings/server/basic.html",
    perl_class="Slim::Web::Settings::Server::Basic",
    page_name="BASIC_SERVER_SETTINGS",       # Basic.pm:20
    title_key="BASIC_SERVER_SETTINGS",
    title_default="Basic Settings",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Basic.pm:23-29",
    fields=(
        Field("language", "SETUP_LANGUAGE", "Language",
              perl_source="Basic.pm:28; basic.html:37 (name=pref_language)"),
        Field("libraryname", "SETUP_LIBRARY_NAME", "Media Library Name",
              perl_source="Basic.pm:28; basic.html:48 (name=pref_libraryname)"),
        Field("playlistdir", "SETUP_PLAYLISTDIR", "Playlists Folder",
              perl_source="Basic.pm:28; basic.html:80 (name=pref_playlistdir)"),
        # Perl's own locale-derived pref lives on this page (Basic.pm:28) — the
        # country this port derives from the same locale goes next to it.
        # Label/form: Perl's only country setting, the Podcast plugin's field.
        Field(_RADIO_COUNTRY_PREF, "PLUGIN_PODCAST_COUNTRY", "Country",
              "select", options=(),
              validator=_is_country_code,
              perl_source=("Basic.pm:23-29 (page/prefs) + Prefs.pm:161 (locale-default);"
                           " Podcast/Settings.pm:19,29-31 + HTML/EN/plugins/Podcast/settings/"
                           "basic.html:81-87 + Podcast/strings.txt PLUGIN_PODCAST_COUNTRY"
                           " (no Perl country pref for TuneIn's local node, TuneIn.pm:38-40)")),
    ),
)

_PLAYER_AUDIO = SettingsPage(
    route="/settings/player/audio.html",
    perl_class="Slim::Web::Settings::Player::Audio",
    page_name="AUDIO_SETTINGS",              # Audio.pm:19
    title_key="AUDIO_SETTINGS",
    title_default="Audio Settings",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Audio.pm:22-33",
    fields=(
        Field("powerOnResume", "SETUP_POWERONRESUME", "Power On Resume", "select",
              options=(
                  ("PauseOff-NoneOn", "SETUP_POWERONRESUME_PAUSEOFF_NONEON",
                   "Pause at power off / Remain paused at power on"),
                  ("PauseOff-PlayOn", "SETUP_POWERONRESUME_PAUSEOFF_PLAYON",
                   "Pause at power off / Resume at power on"),
                  ("StopOff-PlayOn", "SETUP_POWERONRESUME_STOPOFF_PLAYON",
                   "Stop at power off / Restart song at power on"),
                  ("StopOff-NoneOn", "SETUP_POWERONRESUME_STOPOFF_NONEON",
                   "Stop at power off / Remain stopped at power on"),
                  ("StopOff-ResetPlayOn", "SETUP_POWERONRESUME_STOPOFF_RESETPLAYON",
                   "Stop at power off / Restart playlist at power on"),
                  ("StopOff-ResetOn", "SETUP_POWERONRESUME_STOPOFF_RESETON",
                   "Stop at power off / Reset playlist at power on"),
              ),
              perl_source="Audio.pm:33; audio.html:4-14 (pref_powerOnResume, 6 Optionen)"),
        Field("maxBitrate", "SETUP_MAXBITRATE", "Bitrate Limiting", "select",
              options=tuple((str(b), str(b), str(b))
                            for b in (64, 96, 128, 160, 192, 224, 256, 320)),
              perl_source="Audio.pm:33; audio.html:348-359 (pref_maxBitrate)"),
        Field("lameQuality", "SETUP_LAMEQUALITY", "LAME Quality Level", "select",
              options=tuple((str(n), str(n), str(n)) for n in range(10)),
              perl_source="Audio.pm:33; audio.html:367-378 (pref_lameQuality 0..9)"),
        Field("fadeInDuration", "SETUP_FADEINDURATION", "Play or Resume fade-in duration",
              perl_source="Audio.pm:33; audio.html:144 (pref_fadeInDuration)"),
    ),
)

_BRIGHTNESS = tuple(
    (str(n), key, default)
    for n, key, default in (
        (0, "BRIGHTNESS_DARK", "Dark"),
        (1, "BRIGHTNESS_DIMMEST", "Dimmest"),
        (2, "SETUP_BRIGHTNESS_2", "2"),
        (3, "SETUP_BRIGHTNESS_3", "3"),
        (4, "BRIGHTNESS_BRIGHTEST", "Brightest"),
    )
)

_PLAYER_DISPLAY = SettingsPage(
    route="/settings/player/display.html",
    perl_class="Slim::Web::Settings::Player::Display",
    page_name="DISPLAY_SETTINGS",            # Display.pm:19
    title_key="DISPLAY_SETTINGS",
    title_default="Display Settings",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Display.pm:22-64",
    fields=(
        Field("powerOnBrightness", "SETUP_POWERONBRIGHTNESS", "Power On Brightness",
              "select", _BRIGHTNESS,
              perl_source="Display.pm:45; display.html:16 + Display/Display.pm:401-411"),
        Field("powerOffBrightness", "SETUP_POWEROFFBRIGHTNESS", "Power Off Brightness",
              "select", _BRIGHTNESS,
              perl_source="Display.pm:45; display.html:22 + Display/Display.pm:401-411"),
        Field("idleBrightness", "SETUP_IDLEBRIGHTNESS", "Idle Brightness",
              "select", _BRIGHTNESS,
              perl_source="Display.pm:45; display.html:28 + Display/Display.pm:401-411"),
        Field("scrollMode", "SETUP_SCROLLMODE", "Scroll Mode", "select",
              options=(
                  ("0", "SETUP_SCROLLMODE_DEFAULT", "Standard scrolling"),
                  ("1", "SETUP_SCROLLMODE_SCROLLONCE", "Scroll once and stop"),
                  ("2", "SETUP_SCROLLMODE_NOSCROLL", "Do not scroll"),
              ),
              perl_source="Display.pm:46; display.html:143-151 (pref_scrollMode)"),
        Field("scrollRate", "SETUP_SCROLLRATE", "Scroll Rate",
              perl_source="Display.pm:46; display.html:158 (pref_scrollRate)"),
        Field("scrollRateDouble", "SETUP_SCROLLRATE", "Scroll Rate (Double Size)",
              perl_source="Display.pm:46; display.html:163 (pref_scrollRateDouble)"),
        Field("scrollPause", "SETUP_SCROLLPAUSE", "Scroll Pause",
              perl_source="Display.pm:46; display.html:172 (pref_scrollPause)"),
        Field("scrollPauseDouble", "SETUP_SCROLLPAUSE", "Scroll Pause (Double Size)",
              perl_source="Display.pm:46; display.html:177 (pref_scrollPauseDouble)"),
        Field("alwaysShowCount", "SETUP_SHOWCOUNT", "Show Menu Count", "select",
              options=(
                  ("0", "SETUP_SHOWCOUNT_TEMP", "While Scrolling"),
                  ("1", "SETUP_SHOWCOUNT_ALWAYS", "Show Always"),
              ),
              perl_source="Display.pm:46; display.html:128-138 (pref_alwaysShowCount)"),
    ),
)

_PLAYER_ALARM = SettingsPage(
    route="/settings/player/alarm.html",
    perl_class="Slim::Web::Settings::Player::Alarm",
    page_name="ALARM",                       # Alarm.pm:30
    title_key="ALARM",
    title_default="Alarms",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Alarm.pm:33-53",
    fields=(
        Field("alarmsEnabled", "ALARM_ALL_ALARMS", "All Alarms", "select",
              options=(("1", "ALARM_ON", "On"), ("0", "ALARM_OFF", "Off")),
              perl_source="Alarm.pm:44; alarm.html:107-115 (pref_alarmsEnabled)"),
        Field("alarmDefaultVolume", "ALARM_VOLUME", "Alarm Volume",
              perl_source="Alarm.pm:49; alarm.html:144 (pref_alarmDefaultVolume)"),
        Field("alarmfadeseconds", "ALARM_FADE", "Fade Alarms In", "select",
              options=(("1", "YES", "Yes"), ("0", "NO", "No")),
              perl_source="Alarm.pm:44; alarm.html:157-163 (pref_alarmfadeseconds)"),
        Field("alarmSnoozeMinutes", "SETUP_SNOOZE_MINUTES", "Snooze Length (in minutes)",
              perl_source="Alarm.pm:76,110 (pref_alarmSnoozeMinutes → alarmSnoozeSeconds)"),
        Field("alarmTimeoutMinutes", "SETUP_ALARM_TIMEOUT", "Alarm Timeout (in minutes)",
              perl_source="Alarm.pm:77,111 (pref_alarmTimeoutMinutes → alarmTimeoutSeconds)"),
    ),
)

_INFORMATION = SettingsPage(
    route="/settings/server/status.html",
    perl_class="Slim::Web::Settings::Server::Status",
    page_name="INFORMATION",                 # Status.pm:16
    title_key="INFORMATION",
    title_default="Information",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Status.pm:19-21",
)

#: Perl-Route → Seite. Schlüssel ist der HTTP-Pfad (ohne Slash am Ende).
PAGES: dict[str, SettingsPage] = {
    _BASIC_SERVER.route: _BASIC_SERVER,
    _PLAYER_AUDIO.route: _PLAYER_AUDIO,
    _PLAYER_DISPLAY.route: _PLAYER_DISPLAY,
    _PLAYER_ALARM.route: _PLAYER_ALARM,
    _INFORMATION.route: _INFORMATION,
    # Aufgaben-Alias (kein Perl-Fund, siehe Modul-Docstring „UNKLAR").
    "/settings/information.html": _INFORMATION,
}

#: Perl ``Slim/Web/Settings/Server/Basic.pm:106,120``: Liste der ignorierten Pfade.
_MEDIADIR_IGNORE_PREF = "ignoreInAudioScan"
_MEDIADIR_PREF = "mediadirs"


# ── Parameter/Client ────────────────────────────────────────────────────────

async def _read_body(receive: Callable) -> bytes:
    body = b""
    more = True
    while more:
        event = await receive()
        if event.get("type") == "http.request":
            body += event.get("body", b"")
            more = event.get("more_body", False)
        elif event.get("type") == "http.disconnect":
            break
    return body


def _parse_params(scope: dict, body: bytes) -> dict[str, str]:
    """Query- und Form-Parameter (POST gewinnt, wie CGI.pm ``param``).

    Perl liest beides aus einem ``%$paramRef`` (``Slim/Web/HTTP.pm``); der
    Parametername ist jeweils ``pref_<pref>`` (``Settings.pm:157``).
    """
    params: dict[str, str] = {}
    for source in (scope.get("query_string", b""), body):
        if not source:
            continue
        for key, values in parse_qs(
                source.decode("utf-8", "replace"), keep_blank_values=True).items():
            params[key] = values[-1]
    return params


def _resolve_player(params: dict[str, str]):
    """Client über ``playerid`` (Perl ``Slim/Web/HTTP.pm:1164-1169``).

    Ohne ``playerid`` bleibt der Client in Perl ``undef`` (die Handler der
    Player-Seiten laufen dann in den ``SETUP_NO_PREFS``-Zweig,
    ``Player/Display.pm:101-105``); wir geben hier ebenfalls ``None`` zurück.
    """
    pid = params.get("playerid") or params.get("player") or ""
    if not pid:
        return None
    return PlayerManager().get_player(pid)


def _field_id(pref: str) -> str:
    return pref


async def _radio_country_options() -> tuple[tuple[str, str, str], ...]:
    """``(value, label_key, label_default)``-Zeilen des Länder-``select``.

    Erste Zeile ist der leere Wert — „alle Länder“ — mit Perls ``ALL``-Token
    (``strings.txt`` ``ALL``: EN ``all``, DE ``Alle``).  Danach radio-browsers
    Länderliste (``/json/countries``, ``lyrion.web.radiobrowser.countries()``);
    deren Namen sind *Daten* der API, keine ``strings.txt``-Zeile, deshalb
    stehen sie als Label-Default drin.  Ist radio-browser nicht erreichbar,
    bleibt nur die „alle“-Zeile: die Seite rendert trotzdem, und der
    gespeicherte Wert bleibt wählbar (:func:`_options_for`) — statt eines
    Fehlers oder einer erfundenen Liste.
    """
    from lyrion.web import radiobrowser

    options: list[tuple[str, str, str]] = [("", "ALL", "all")]
    try:
        rows = await asyncio.wait_for(radiobrowser.countries(),
                                      timeout=radiobrowser.HTTP_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — die Seite darf daran nicht scheitern
        logger.debug("settings: Länderliste nicht verfügbar: %s", exc)
        rows = []
    options.extend((r["code"], r["code"], r["name"]) for r in rows)
    return tuple(options)


def _options_for(f: Field, value: str, extra: dict[str, tuple[tuple[str, str, str], ...]]
                 ) -> tuple[tuple[str, str, str], ...]:
    """Optionen eines ``select`` — der gespeicherte Wert ist immer dabei.

    Perls Vorlagen listen die Optionen fest auf, ein gespeicherter Wert kommt
    darin vor.  Eine *dynamische* Liste (unsere Länderliste — ``/json/countries``
    kann ein Land jederzeit fallen lassen) würde den Wert sonst nicht rendern,
    und der Browser schickte beim nächsten Speichern die erste Option.  Fehlt
    der Wert in der Liste, wird er angehängt.
    """
    options = extra.get(f.pref) or f.options
    if not options or not value:
        return options
    if any(str(opt[0]) == str(value) for opt in options):
        return options
    return tuple(options) + ((str(value), str(value), str(value)),)


# ── Pref-Werte lesen/schreiben ──────────────────────────────────────────────

def _server_value(pref: str) -> str:
    value = get_prefs().get(pref)
    if value is None:
        if pref == _RADIO_COUNTRY_PREF:
            # Never-set pref: show the first-start derivation even if storing it
            # failed (no pref DB yet).  Perl's read-side rule is the same order —
            # Light.pm:95 ``$language ||= getPref('language') || $os->getSystemLanguage()``.
            from lyrion.web import radiobrowser

            return radiobrowser.derived_country()
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _client_value(page: SettingsPage, f: Field, player) -> str:
    """Pref-Wert eines Spielers + Minuten↔Sekunden-Umrechnung (Alarm.pm:110-111)."""
    if player is None:
        return ""
    real = _MINUTES_TO_SECONDS.get(f.pref, f.pref)
    raw = getattr(player, "playerprefs", {}).get(real)
    if raw is None:
        return ""
    if f.pref in _MINUTES_TO_SECONDS:
        try:
            # Perl teilt nur für die Anzeige (Alarm.pm:110) — "%g" wie Perl.
            return "%g" % (float(raw) / 60)
        except (TypeError, ValueError):
            return str(raw)
    return str(raw)


async def _save_simple_prefs(page: SettingsPage, params: dict[str, str],
                             player, invalid: Optional[list[tuple[str, str]]] = None
                             ) -> list[str]:
    """``Settings.pm:154-171``: ``pref_<name>`` → ``$prefsClass->set``.

    ``invalid`` sammelt die abgelehnten ``(pref, wert)``-Paare: Perl prüft jeden
    Wert beim ``set`` (``Base.pm:83-100`` ``validate``), meldet einen Fehlschlag
    als ``SETTINGS_INVALIDVALUE`` und setzt ``validated`` auf 0
    (``Settings.pm:162-169``); der Wert wird dann NICHT gespeichert.
    """
    written: list[str] = []
    for f in page.fields:
        key = "pref_" + f.pref
        if key not in params:
            continue
        raw = params[key]
        if page.scope == "client":
            if player is None:
                continue
            real = _MINUTES_TO_SECONDS.get(f.pref, f.pref)
            value = raw
            if f.pref in _MINUTES_TO_SECONDS:
                try:
                    value = str(int(float(raw)) * 60)   # Alarm.pm:76-77
                except (TypeError, ValueError):
                    logger.warning("settings: %s=%r ist keine Zahl — übersprungen",
                                   f.pref, raw)
                    continue
            apply_player_pref(player, real, value)
            written.append(real)
        else:
            if f.validator is not None and not f.validator(raw):
                if invalid is not None:
                    invalid.append((f.pref, raw))
                continue
            await get_prefs().set(f.pref, raw)
            written.append(f.pref)
    return written


def _as_list(value: Any) -> list[str]:
    """Listen-Pref lesen (unser Store kodiert listen kommasepariert)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value)
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (ValueError, TypeError):
            pass
    return [p for p in (part.strip() for part in text.split(",")) if p]


async def _save_server_basic(params: dict[str, str], page: SettingsPage,
                             player, invalid: Optional[list[tuple[str, str]]] = None
                             ) -> list[str]:
    """Zusatz-Schreibpfade von ``Server/Basic.pm:81-128``.

    ``mediadirs`` / ``ignoreInAudioScan`` kommen als ``pref_mediadirs0``,
    ``pref_mediadirs1`` … (Template ``basic.html:63``: ``name="pref_mediadirs[% loop.index %]"``).
    """
    written = await _save_simple_prefs(page, params, player, invalid)

    paths: list[str] = []
    ignored: list[str] = []
    index = 0
    while f"pref_{_MEDIADIR_PREF}{index}" in params:
        path = params[f"pref_{_MEDIADIR_PREF}{index}"]
        if path:                                            # Basic.pm:89
            paths.append(path)
            if f"pref_{_MEDIADIR_IGNORE_PREF}{index}" not in params:
                ignored.append(path)                        # Basic.pm:102
        index += 1

    if index:
        prefs = get_prefs()
        await prefs.set(_MEDIADIR_IGNORE_PREF, ",".join(ignored))   # Basic.pm:106
        await prefs.set(_MEDIADIR_PREF, ",".join(paths))            # Basic.pm:120
        written.extend((_MEDIADIR_PREF, _MEDIADIR_IGNORE_PREF))
    return written


# ── HTML ────────────────────────────────────────────────────────────────────

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render_input(f: Field, value: str,
                  options: tuple[tuple[str, str, str], ...] = ()) -> str:
    fid = _field_id(f.pref)
    name = "pref_" + f.pref
    if f.kind == "select" and options:
        opts = []
        for val, key, default in options:
            sel = " selected" if str(value) == val else ""
            label = _string(key, default)
            opts.append(f'<option value="{_esc(val)}"{sel}>{_esc(label)}</option>')
        return (f'<select class="stdedit" name="{name}" id="{_esc(fid)}">'
                + "".join(opts) + "</select>")
    return (f'<input type="text" class="stdedit" name="{name}" '
            f'id="{_esc(fid)}" value="{_esc(value)}" size="40">')


def _render_mediadirs() -> str:
    """Medienordner-Zeilen wie ``basic.html:52-76`` (Parameter ``pref_mediadirs<i>``)."""
    prefs = get_prefs()
    paths = _as_list(prefs.get(_MEDIADIR_PREF))
    ignored = set(_as_list(prefs.get(_MEDIADIR_IGNORE_PREF)))
    rows = []
    for i, path in enumerate(paths):
        checked = "" if path in ignored else " checked"   # basic.html:66 (audio ⇒ aus)
        rows.append(
            f'<tr><td><input type="text" class="stdedit" name="pref_{_MEDIADIR_PREF}{i}" '
            f'id="{_MEDIADIR_PREF}{i}" value="{_esc(path)}" size="40"></td>'
            f'<td><input type="checkbox" name="pref_{_MEDIADIR_IGNORE_PREF}{i}" '
            f'id="{_MEDIADIR_IGNORE_PREF}{i}" value="1"{checked}></td></tr>')
    # Leerzeile für einen weiteren Ordner (Basic.pm:146-148)
    i = len(paths)
    rows.append(
        f'<tr><td><input type="text" class="stdedit" name="pref_{_MEDIADIR_PREF}{i}" '
        f'id="{_MEDIADIR_PREF}{i}" value="" size="40"></td>'
        f'<td><input type="checkbox" name="pref_{_MEDIADIR_IGNORE_PREF}{i}" '
        f'id="{_MEDIADIR_IGNORE_PREF}{i}" value="1"></td></tr>')
    return ('<table border="0" cellspacing="0"><tr><th>'
            + _esc(get_string("SETUP_MEDIADIRS", default="Media Folders"))
            + '</th><th>' + _esc(get_string("MUSIC", default="Music"))
            + "</th></tr>" + "".join(rows) + "</table>")


def _information_sections() -> str:
    """Echte Server-Werte unter den Perl-Gruppen-Tokens (``Status.pm:30-56``).

    ``INFORMATION_MENU_PERL`` hat kein Gegenstück (Python statt Perl) — wir
    zeigen dort die Interpreter-Version und sagen das im Text.
    """
    from lyrion import __version__

    players = PlayerManager().get_all_players()
    server_items = [
        f"<li>{_esc(get_string('INFORMATION_VERSION', default='Version'))}: "
        f"{_esc(__version__)}</li>",
        f"<li>Lyrion::Web (ASGI): {_esc(sys.version.split()[0])}</li>",
    ]
    player_items = [
        f"<li>{_esc(p.name)} — {_esc(p.mac)} "
        f"({'connected' if p.connected else 'disconnected'})</li>"
        for p in players
    ] or ["<li>-</li>"]

    def section(key: str, default: str, items: list[str]) -> str:
        return (f'<div class="settingSection"><div class="prefHead">'
                f'{_esc(get_string(key, default=default))}</div><ul>'
                + "".join(items) + "</ul></div>")

    return (
        section("INFORMATION_MENU_SERVER", "Lyrion Music Server", server_items)
        + section("INFORMATION_MENU_PERL", "Perl and Module Versions",
                  ["<li>Python-Neuimplementierung — Perl-Modulversionen entfallen</li>"])
        + section("INFORMATION_MENU_PLAYER", "Player Information", player_items)
    )


def _render_page(page: SettingsPage, params: dict[str, str], player,
                 warning: Optional[str],
                 extra_options: Optional[dict[str, tuple[tuple[str, str, str], ...]]] = None
                 ) -> bytes:
    action = page.route
    pid = params.get("playerid", "")
    if pid:
        action += "?playerid=" + quote(pid, safe="")

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(get_string(page.title_key, default=page.title_default))}</title>",
        "</head><body>",
        # ``warning`` wird WIE IN PERL roh eingesetzt — die Vorlage schreibt
        # ``[% warning %]`` ohne Escaping (``HTML/EN/settings/header.html:42-43``)
        # und Perl legt HTML hinein (``Settings.pm:193-195`` baut die Scan-Warnung
        # als ``<span id="rescanWarning">…</span>``, ``:167-168`` hängt ``<br/>``
        # an).  Der eingesetzte *Wert* einer abgelehnten Pref wird beim Bauen der
        # Warnung escaped; Perls ``sprintf`` setzt ihn roh ein.
        f'<div id="statusarea" class="statusarea">{warning or ""}</div>',
    ]

    if page is _INFORMATION:
        parts.append('<div id="settingsRegion">')
        parts.append(_information_sections())
        parts.append("</div></body></html>")
        return "\n".join(parts).encode("utf-8")

    parts.append(f'<form name="settingsForm" id="settingsForm" method="post" action="{_esc(action)}">')
    parts.append('<input type="hidden" name="useAJAX" value="0">')      # header.html:54
    parts.append(f'<input type="hidden" name="page" value="{_esc(page.page_name)}">')
    if page.needs_client and pid:
        parts.append(f'<input type="hidden" name="playerid" value="{_esc(pid)}">')
    parts.append('<div id="settingsRegion">')

    for f in page.fields:
        value = (_client_value(page, f, player) if page.scope == "client"
                 else _server_value(f.pref))
        options = _options_for(f, value, extra_options or {})
        parts.append(f'<div class="settingGroup"><label for="{_esc(_field_id(f.pref))}">'
                     f'{_esc(f.label())}</label>{_render_input(f, value, options)}</div>')

    if page is _BASIC_SERVER:
        parts.append(_render_mediadirs())

    parts.append("</div>")
    parts.append('<div id="prefsSubmit">'
                 + f'<input name="saveSettings" id="saveSettings" type="submit" '
                   f'class="stdclick" value="{_esc(get_string("SAVE_SETTINGS", default="Save Settings"))}">'
                 + '<input type="hidden" name="saveSettings" value="1"></div>')  # footer.html:38-39
    parts.append("</form></body></html>")
    return "\n".join(parts).encode("utf-8")


def _render_ajax(warning: Optional[str], written: list[str],
                 invalid: Optional[list[tuple[str, str]]] = None) -> bytes:
    """Fragment ``settings/ajaxSettings.txt`` (``Settings.pm:286``).

    Zeile 1 ``warning|<text>``; danach eine Zeile ``<pref>|<valid>`` je
    validierter Pref (``Settings.pm:164-168`` ``$paramRef->{'validated'}->{$pref}``
    = 1 bzw. 0, Vorlage ``HTML/EN/settings/ajaxSettings.txt:2-4``).
    """
    lines = ["warning|" + (warning or "")]
    lines.extend(f"{pref}|1" for pref in written)
    lines.extend(f"{pref}|0" for pref, _value in (invalid or []))
    return ("\n".join(lines) + "\n").encode("utf-8")


# ── ASGI ────────────────────────────────────────────────────────────────────

async def _send(send, status: int, content_type: str, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"Content-Type", content_type.encode()),
            (b"Content-Length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def is_settings_path(path: str) -> bool:
    """Pfade, die dieser Handler übernimmt (Perl ``settings/``-Seiten)."""
    return path.startswith("/settings/")


async def handle_settings_request(scope: dict, receive, send) -> None:
    """Bedient ``/settings/…`` und schreibt ``pref_*`` bei ``saveSettings``.

    Perl: ``Slim/Web/HTTP.pm:1160-1176`` (Dispatch) + ``Slim/Web/Settings.pm:135-287``
    (Basis-Handler). Unbekannte Settings-Pfade bekommen 404 wie Perls
    ``html/errors/404.html`` (``Slim/Web/HTTP.pm:1128-1131``).
    """
    method = scope.get("method", "GET")
    path = scope.get("path", "")
    body = await _read_body(receive)
    params = _parse_params(scope, body)

    page = PAGES.get(path)
    if page is None:
        await _send(send, 404, "text/html", b"<html><body>404 Not Found</body></html>")
        return

    player = _resolve_player(params) if page.needs_client else None

    warning: Optional[str] = None
    written: list[str] = []
    invalid: list[tuple[str, str]] = []

    # Der Erst-Wert der Radio-Pref entsteht beim ersten Lesen, nicht im
    # Pref-Init (``config.py`` liegt ausserhalb dieser Änderung) — Perl wertet
    # den abgeleiteten Default beim Start in ``Slim/Utils/Prefs/Base.pm:178-234``
    # aus. Ohne das zeigte das Formular beim ersten Aufruf einen leeren Wert
    # statt der Server-Einstellung.
    extra_options: dict[str, tuple[tuple[str, str, str], ...]] = {}
    if any(f.pref == _RADIO_COUNTRY_PREF for f in page.fields):
        from lyrion.web import radiobrowser

        await radiobrowser.ensure_country_pref()
        extra_options[_RADIO_COUNTRY_PREF] = await _radio_country_options()

    if method == "POST" and "saveSettings" in params:
        if page.needs_client and player is None:
            # Perl: Player/Display.pm:101-105 — ohne Client keine Einstellungen.
            warning = get_string("SETUP_NO_PREFS",
                                 default="There are no settings for this player on this page")
        else:
            invalid = []
            if page is _BASIC_SERVER:
                written = await _save_server_basic(params, page, player, invalid)
            else:
                written = await _save_simple_prefs(page, params, player, invalid)
            # Perl ``Settings.pm:167-169`` hängt je abgelehntem Wert
            # ``sprintf(SETTINGS_INVALIDVALUE, $wert, $pref) . '<br/>'`` an die
            # Warnung; ``:198-200`` setzt SETUP_CHANGES_SAVED nur, wenn noch
            # KEINE Warnung steht — die Fehlermeldung ersetzt also die
            # Erfolgsmeldung (Perl-Verhalten, nicht additiv).
            for pref, value in invalid:
                warning = (warning or "") + (
                    _string("SETTINGS_INVALIDVALUE",
                            'Invalid value "%s" for %s') % (_esc(value), pref)) + "<br/>"
            if not warning:
                warning = get_string("SETUP_CHANGES_SAVED",
                                     default="Changes have been saved.")
            logger.info("settings: %s saved %s", page.route, written)
    elif page.needs_client and player is None:
        warning = get_string("SETUP_NO_PREFS",
                             default="There are no settings for this player on this page")

    if str(params.get("useAJAX", "0")) == "1":
        await _send(send, 200, "text/plain", _render_ajax(warning, written, invalid))
        return

    await _send(send, 200, "text/html",
                _render_page(page, params, player, warning, extra_options))
