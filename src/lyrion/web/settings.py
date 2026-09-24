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
 **Zusatzfeld des Bildproxys** (``imageProxyFollowRedirects``, Vorbelegung AN):
 Perl antwortet auf ``/imageproxy/<url>/image.jpg`` mit 301 und lässt den Client
 das Logo selbst holen (``Slim/Web/ImageProxy.pm:145-155``); SqueezePlay folgt
 zwar, hat aber kein TLS, so dass der http→https-Sprung der imgur-Logos (1.FM
 u. a.) scheitert.  Das Feld schaltet die bewusste Abweichung ab: AUS = Perls
 301.  Die Umsetzung steht in ``lyrion/web/app.py``.
 **Zusatzfelder der Online-Cover-Suche** (``artworkOnline*``, siehe
 ``lyrion/media/art_online.py``): Perl hat dafür KEINE Pref und keinen Anbieter —
 seine Kette endet bei Tags/Ordnerbild (``Slim/Music/Artwork.pm:388-400``,
 ``:480-637``) und liefert sonst ``html/images/cover_*.png``
 (``Slim/Web/Graphics.pm:275-291``).  Wir hängen die Felder an dieselbe Seite,
 auf der Perls Artwork-nahe Prefs liegen (``coverArt``/``artfolder``/``thumbSize``;
 Perl zeigt sie in der Web-UI unter Formatting/Interface, nicht auf ``Basic`` —
 die Zuordnung hier ist die unsere).  Vorbild der Felder ist der Kodi-UAS
 (``metadata.album.universal`` ``resources/settings.xml``: Anbieter-Schalter,
 Sprache) plus die Key-/Cache-Felder unseres Ports.
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
* ``GET|POST /settings/server/status.html`` — ``.../Server/Status.pm``: ``name`` :15-17
  (``INFORMATION``), ``page`` :19-21.
* ``GET|POST /settings/server/software.html`` — ``.../Server/Software.pm``:
  ``page`` :19-21, ``prefs`` :23-31 (``checkVersion checkVersionInterval``,
  ``autoDownloadUpdate`` nur bei ``canAutoUpdate`` :26-28), Knopf
  ``checkForUpdateNow`` :44-60; Version über ``$::newVersion`` :40-42,
  Repo ``https://lms-community.github.io/lms-server-repository/servers.json``
  (``Slim/Utils/Update.pm:17``), Vergleich ``Update.pm:102-141``.
* ``GET|POST /settings/server/networking.html`` — ``.../Server/Network.pm``:
  ``page`` :23-25, ``prefs`` :27-41 (``webproxy httpport bufferSecs
  remotestreamtimeout maxRedirects maxWMArate useEnhancedHTTP``, bedingt
  ``udpChunkSize``/``syncStartDelay`` :31-38), ``SETUP_HTTPPORT_OK`` :46-63.
* ``GET|POST /settings/server/security.html`` — ``.../Server/Security.pm``:
  ``page`` :22-24, ``prefs`` :26-28, Passwort als ``sha1_base64`` :42-61.
* ``GET|POST /settings/server/filetypes.html`` — ``.../Server/FileTypes.pm``:
  ``page`` :23-25, ``prefs`` :27-29 (``prioritizeNative``), Endungen/Profile
  :31-133 (``conversion_table`` aus ``lyrion/media/convert.conf``, Muster von
  ``Slim/Player/TranscodingHelper.pm:51-134``).
* ``GET|POST /settings/server/debugging.html`` — ``.../Server/Debugging.pm``
  (kein ``prefs()``): Log-Sätze/Kategorien/persist im Handler :24-78; Stufen
  ``Slim/Utils/Log.pm:64``, Kategorien :876-948.
* ``GET|POST /settings/server/performance.html`` — ``.../Server/Performance.pm``:
  ``page`` :21-23, ``prefs`` :25-30, Prioritäten :82-91.

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
* **Software-Updates:** Perls Seite prüft auf Wunsch (``checkForUpdateNow``) die
  Repo-Datei und **lädt bei ``autoDownloadUpdate`` den Installer herunter**
  (``Update.pm:131-135`` ``getUpdate``, ``:54`` ``$os->initUpdate()``) und
  startet ihn.  Diese Portierung führt **nur die Prüfung** aus (Status
  „verfügbar/aktuell/Fehler"): kein Download, kein Selbst-Update, kein
  Neustart.  Grund: ein Selbst-Update des laufenden Python-Prozesses wäre eine
  neue, ungeprüfte Gefährdung und ist von der Aufgabe ausgeschlossen.  Perls
  Basisklasse meldet ``canAutoUpdate`` = ``0`` (``Slim/Utils/OS.pm:451``) und
  blendet das Download-Feld dann aus; die Live-Referenz läuft als
  Distributionspaket (``Slim/Utils/OS/Debian.pm`` überschreibt es), dort steht
  das Feld — wir folgen der Basisklasse, ``_can_auto_update`` prüft sie.
* **Netzwerk:** ``udpChunkSize`` (nur bei ``$Slim::Player::SLIMP3::SLIMP3Connected``,
  ``Network.pm:31-33``) fehlt — unser Port hat keinen SLIMP3-Client.  Die
  ``syncStartDelay``-Bedingung (``:36-38``, ``clients() > 1``) bilden wir über
  die Zahl bekannter Player nach.
* **Logging:** Es gibt in unserem Port keinen ``log4perl.conf``-Writer; Perls
  ``persist`` schreibt seine Stufen in diese Datei (``Log.pm:214-244``).  Wir
  legen die Stufen stattdessen als Prefs ``log.level.<kategorie>`` ab und
  setzen sie zur Laufzeit über ``lyrion.utils.log.set_level``.  Die Pref
  ``log.persist`` entspricht Perls ``persist``-Flag; die Plugin-Kategorien
  (``plugin.*``) zeigt Perl nur für geladene Plugins — wir tragen die unserer
  mitgelieferten Plugins aus ``PLUGIN_LOG_CATEGORIES`` bei, nicht die der 45
  Perl-Plugins, die dieser Port nicht hat.
* **Dateitypen:** Die Profile stammen aus ``lyrion/media/convert.conf``
  (:func:`conversion_table`); sie decken sich mit dem Live-Perl bis auf drei
  ``lpcm``-Profile (Perls Live-Seite 74, wir 77).
* **Grundversorgung:** Perls ``%defaults`` (``Prefs.pm:133-283``) wird hier beim
  Seitenaufruf registriert (:func:`register_server_page_prefs`), nicht in
  ``lyrion/config.py`` ``_register_known_prefs`` — diese Datei liegt ausserhalb
  der Änderung.  Die Werte sind dieselben; sie entstehen nur später.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, parse_qs

from lyrion.config import get_prefs
from lyrion.media.art_online import (
    DEFAULT_PROVIDER_ORDER,
    PREF_AUDIODB_KEY as _ART_AUDIODB_KEY_PREF,
    PREF_CACHE_DIR as _ART_CACHE_DIR_PREF,
    PREF_COUNTRY as _ART_COUNTRY_PREF,
    PREF_DEFAULTS as _ART_PREF_DEFAULTS,
    PREF_ENABLED as _ART_ENABLED_PREF,
    PREF_FANART_KEY as _ART_FANART_KEY_PREF,
    PREF_LANGUAGE as _ART_LANGUAGE_PREF,
    PREF_PROVIDERS as _ART_PROVIDERS_PREF,
    PREF_RETRY_DAYS as _ART_RETRY_DAYS_PREF,
    PREF_TIMEOUT as _ART_TIMEOUT_PREF,
    ArtOnlineSettings,
    normalize_provider_list,
)
from lyrion.media.art_online_wanted import (
    PREF_WANTED_AUTO as _ART_WANTED_AUTO_PREF,
    PREF_WANTED_PER_PASS as _ART_WANTED_PER_PASS_PREF,
    WANTED_PREF_DEFAULTS as _ART_WANTED_PREF_DEFAULTS,
)
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

# ── Bildproxy: Weiterleitungen serverseitig verfolgen (bewusste Abweichung) ──
#
# Perl hat dafür KEINE Pref: ``Slim/Web/ImageProxy.pm:145-155`` antwortet auf
# ``/imageproxy/<url>/image.jpg`` mit **301** und lässt den Client das Bild
# selbst holen.  SqueezePlay folgt dem 301, hat aber kein TLS — der übliche
# http→https-Sprung (imgur-Logos von 1.FM u. a.) scheitert, das Logo bleibt
# klein, obwohl die kleinen Varianten (``_40x40_m``, ``_100x100_m``) da sind
# (die holt der Server schon immer selbst).  Mit dieser Pref holt der Server
# auch das große Bild selbst, verfolgt Weiterleitungen serverseitig und liefert
# es als 200 aus; AUS stellt Perls 301 exakt wieder her.
#
# Die Umsetzung steht in ``lyrion/web/app.py`` (``_imageproxy_proxied``,
# ``_imageproxy_fetch_following_redirects``, ``_imageproxy_negative_put``).
IMAGEPROXY_FOLLOW_REDIRECTS_PREF = "imageProxyFollowRedirects"

#: Vorbelegung AN — die Abweichung ist gewollt (User-Entscheidung 2026-09-20),
#: deshalb wirkt sie ohne Einrichtung; abschaltbar bleibt sie über das Feld auf
#: ``/settings/server/basic.html``.
IMAGEPROXY_PREF_DEFAULTS: dict[str, str] = {
    IMAGEPROXY_FOLLOW_REDIRECTS_PREF: "1",
}


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
    kind: str = "text"                       # text | select | password
    options: tuple[tuple[str, str, str], ...] = ()   # (value, label_key, label_default)
    perl_source: str = ""
    #: Werte-Prüfung des Feldes; ``None`` = jeder Wert wird geschrieben.  Perl
    #: prüft beim ``set`` gegen den Validator der Pref und meldet einen
    #: abgelehnten Wert als ``SETTINGS_INVALIDVALUE`` (``Settings.pm:162-169``).
    validator: Optional[Callable[[str], bool]] = None
    #: Immer leer rendern, nie den gespeicherten Wert zeigen.  Perls
    #: Passwortfelder zeigen den gespeicherten SHA1-Hash
    #: (``security.html:18,22``); wir geben den Hash nicht heraus.
    never_show_value: bool = False
    #: Formularfeld ohne eigene Pref (z. B. ``password_repeat``, das Perl nur
    #: zum Vergleich liest, ``Security.pm:46``).  Wird nie gespeichert.
    not_stored: bool = False
    #: Formularname ohne ``pref_``-Präfix, wie Perls Vorlage ihn ausgibt
    #: (``filetypes.html:5,9`` ``name="disabledextensionsaudio"``; der Handler
    #: liest denselben Namen, ``FileTypes.pm:37-38``).  Gespeichert wird der
    #: Wert über den Zusatz-Handler der Seite.
    no_pref_prefix: bool = False

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

# ── Online-Cover-Suche (Zutat dieses Ports, KEIN Perl-Fund) ──────────────────
#
# Perl hat keinen Online-Cover-Anbieter; die Felder sind an den Kodi-UAS
# angelehnt (``metadata.album.universal`` ``resources/settings.xml``:
# ``fanarttvalbumthumbs``/``tadbalbumthumbs`` = Anbieter-Schalter,
# ``tadbalbumlanguage`` = Sprache, Default ``en``) und ergänzen nur, was dieser
# Port zusätzlich braucht (Cache-Verzeichnis, optionale Keys).
# Die Werte liest/schreibt ``lyrion.media.art_online`` (``PREF_DEFAULTS``).


def _is_provider_list(value: str) -> bool:
    """Anbieter-Liste: bekannte Namen, Komma/Leerzeichen-getrennt, oder leer."""
    text = (value or "").strip()
    if not text:
        return True
    names = [n for n in text.replace(",", " ").split() if n]
    return all(n.lower() in DEFAULT_PROVIDER_ORDER for n in names)


def _is_positive_int(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    return text.isdigit() and int(text) >= 0


def _is_non_negative_int(value: str) -> bool:
    """Ganzzahl ≥ 0 — ``artworkOnlineWantedPerPass`` (``0`` = alle fälligen).

    Dieselbe Prüfung wie ``_is_positive_int``, eigener Name, weil die
    ``0`` hier eine **Bedeutung** hat (unbegrenzt) und nicht nur „erlaubt“.
    """
    text = (value or "").strip()
    if not text:
        return True
    return text.isdigit()


def _is_positive_number(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    try:
        return float(text) > 0
    except ValueError:
        return False


_ART_ONLINE_FIELDS: tuple[Field, ...] = (
    Field(_ART_ENABLED_PREF, "SETUP_ART_ONLINE", "Online cover search",
          "select",
          options=(("1", "SETUP_ART_ONLINE_ON", "On"),
                   ("0", "SETUP_ART_ONLINE_OFF", "Off")),
          perl_source="kein Perl-Fund; Vorbild metadata.album.universal/resources/settings.xml:14-21"),
    Field(_ART_PROVIDERS_PREF, "SETUP_ART_PROVIDERS",
          "Providers (order = priority)",
          validator=_is_provider_list,
          perl_source="kein Perl-Fund; Reihenfolge wie metadata.album.universal/albumuniversal.xml:122-137"),
    Field(_ART_LANGUAGE_PREF, "SETUP_ART_LANGUAGE", "Cover search language",
          perl_source="Vorbild metadata.album.universal/resources/settings.xml:12 (tadbalbumlanguage)"),
    Field(_ART_COUNTRY_PREF, "SETUP_ART_COUNTRY", "Cover search country",
          validator=_is_country_code,
          perl_source="kein Perl-Fund (UAS kennt kein Land; freies Feld)"),
    Field(_ART_CACHE_DIR_PREF, "SETUP_ART_CACHE_DIR", "Cover cache folder",
          perl_source=("kein Perl-Fund; Perls Artwork-Cache liegt bei den "
                       "Bibliotheksdaten (Slim/Utils/ArtworkCache.pm:44-47)")),
    Field(_ART_RETRY_DAYS_PREF, "SETUP_ART_RETRY_DAYS", "Retry after no match (days)",
          validator=_is_positive_int,
          perl_source="kein Perl-Fund (Negative-Cache dieses Ports)"),
    Field(_ART_TIMEOUT_PREF, "SETUP_ART_TIMEOUT", "Provider timeout (seconds)",
          validator=_is_positive_number,
          perl_source="kein Perl-Fund (Frist je Anbieter)"),
    Field(_ART_AUDIODB_KEY_PREF, "SETUP_ART_AUDIODB_KEY", "TheAudioDB API key (optional)",
          perl_source="kein Perl-Fund; UAS trägt den Key im Addon (tadb.xml:677)"),
    Field(_ART_FANART_KEY_PREF, "SETUP_ART_FANART_KEY", "fanart.tv API key (optional)",
          perl_source="kein Perl-Fund; UAS-Key im Addon (fanarttv.xml:5)"),
)

#: Felder der wanted-Liste (``media/art_online_wanted.py``).  Perl hat dafür
#: kein Vorbild: ``Slim/Plugin/RadioArtwork/Plugin.pm:199-201`` cacht Cover
#: 30 Tage, kennt aber keine persistente Nachlade-Liste — die Arbeit läuft dort
#: ereignisgetrieben im Titelwechsel (``requestIsQueued``, ``:166-180``).
_ART_WANTED_FIELDS: tuple[Field, ...] = (
    Field(_ART_WANTED_AUTO_PREF, "SETUP_ART_WANTED_AUTO",
          "Background service starts with the server",
          "select",
          options=(("1", "SETUP_ART_ONLINE_ON", "On"),
                   ("0", "SETUP_ART_ONLINE_OFF", "Off")),
          perl_source="kein Perl-Fund (persistente wanted-Liste dieses Ports)"),
    Field(_ART_WANTED_PER_PASS_PREF, "SETUP_ART_WANTED_PER_PASS",
          "Albums per pass (0 = all)",
          validator=_is_non_negative_int,
          perl_source="kein Perl-Fund (Drossel des Durchlaufs)"),
)

#: Öffentlicher Name für Tests/Verdrahtung.
ART_ONLINE_FIELDS: tuple[Field, ...] = _ART_ONLINE_FIELDS

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
        *_ART_ONLINE_FIELDS,
        # Zusatzfeld dieses Ports, bewusste Abweichung von Perls ImageProxy
        # (siehe Block oben): Perl kennt keine solche Einstellung.
        Field(IMAGEPROXY_FOLLOW_REDIRECTS_PREF, "SETUP_IMAGEPROXY_FOLLOW",
              "Follow logo redirects (deviates from Perl)",
              "select",
              options=(("1", "YES", "Yes"), ("0", "NO", "No")),
              perl_source=("kein Perl-Fund; bewusste Abweichung von "
                           "Slim/Web/ImageProxy.pm:145-155 (Perl antwortet dort "
                           "mit 301 und lässt den Client laden)")),
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

#: Eigene Seite des Artwork-Downloaders (kein Perl-Vorbild: Perl kennt keinen
#: Online-Cover-Anbieter, siehe ``media/art_online.py`` Modul-Docstring).
#: Sie zeigt die bestehenden ``artworkOnline*``-Einstellungen, den Zustand der
#: wanted-Liste und den Knopf für einen erneuten Durchlauf; die Felder auf
#: ``/settings/server/basic.html`` bleiben unverändert bestehen.
_ART_ONLINE = SettingsPage(
    route="/settings/server/artworkonline.html",
    perl_class="Slim::Web::Settings::Server::ArtworkOnline",
    page_name="ARTWORK_ONLINE_SETTINGS",
    title_key="SETUP_ART_ONLINE_TITLE",
    title_default="Online cover search / Artwork-Downloader",
    needs_client=False,
    perl_source=("kein Perl-Fund; Anbietervorbild "
                 "metadata.album.universal/albumuniversal.xml:122-137"),
    fields=(*_ART_ONLINE_FIELDS, *_ART_WANTED_FIELDS),
)

#: Eigen-Kennzeichen: Seiten mit eigenem Handler statt Pref-Formular.
#: Perl ``Plugins.pm:48-145`` (handler) + ``:46-49`` (page) — die Seite hat
#: KEIN ``prefs()``; sie schreibt ``manual:<plugin>``-Parameter über
#: ``enablePlugin``/``disablePlugin`` (``:91-94``).
_PLUGINS = SettingsPage(
    route="/settings/server/plugins.html",
    perl_class="Slim::Web::Settings::Server::Plugins",
    page_name="SETUP_PLUGINS",               # Plugins.pm:40-42
    title_key="SETUP_PLUGINS",
    title_default="Manage Plugins",
    needs_client=False,
    perl_source=("Slim/Web/Settings/Server/Plugins.pm:44-46 (page), :40-42 (name);"
                 " Zustand/Regeln aus Slim/Utils/PluginManager.pm:535-585"),
)

# ── Server-Einstellungen: Software / Netzwerk / Sicherheit / Dateitypen / ──
# ── Logging / Leistung (Perl ``Slim/Web/Settings/Server/*.pm``) ────────────
#
# Die Feldlisten sind roh aus den ``prefs()``-Listen der Perl-Klassen
# übernommen; die Vorbelegungen sind ``Slim/Utils/Prefs.pm:133-283``
# (``%defaults``) entnommen, nicht erfunden.  Sie werden — wie bei den
# bestehenden Seiten — beim Aufruf der Seite registriert, damit das Formular
# beim ersten Aufruf die *wirksamen* Werte zeigt und nicht leere Felder.

#: ``Slim/Utils/Update.pm:17`` — die Repo-URL, gegen die Perl prüft.
UPDATE_REPOSITORY_URL = "https://lms-community.github.io/lms-server-repository/servers.json"

#: ``Slim/Web/Settings/Server/Software.pm:23-31`` — ``prefs()``.
#: ``autoDownloadUpdate`` steht dort nur, wenn ``$os->canAutoUpdate()``
#: (``Software.pm:26-28``); Perl antwortet auf Linux ``0``
#: (``Slim/Utils/OS.pm:451`` ``sub canAutoUpdate { 0 }``), deshalb blenden wir
#: das Feld auf ``can_auto_update() == False`` aus (siehe ``_render_page``).
_SOFTWARE_DEFAULTS: dict[str, tuple[str, str]] = {
    # name: (default, category)
    "checkVersion": ("1", "server"),                    # Prefs.pm:170
    "checkVersionInterval": ("86400", "server"),        # Prefs.pm:171 (60*60*24)
    "autoDownloadUpdate": ("0", "server"),              # Prefs.pm:173 (canAutoUpdate)
}
#: Die Seite registriert ``checkVersionLastTime`` NICHT in ``prefs()``, Perl
#: schreibt sie aber beim Prüfen (``Update.pm:86`` ``checkVersionLastTime``).
#: Ohne Registrierung wäre die Zeit nach dem Speichern verloren.
_SOFTWARE_AUX_DEFAULTS: dict[str, tuple[str, str]] = {
    "checkVersionLastTime": ("0", "server"),
    "serverUpdateAvailable": ("", "server"),            # Update.pm:139
}


def _can_auto_update() -> bool:
    """Perl ``$os->canAutoUpdate()`` — Basisklasse ``0`` (``OS.pm:451``).

    Die Basisklasse antwortet auf allen Nicht-Windows-Systemen ``0``; nur
    ``Slim/Utils/OS/Win32.pm`` und das Distributionspaket
    (``Slim/Utils/OS/Debian.pm:108`` ``sub canAutoUpdate { $_[0]->runningFromSource ? 0 : 1 }``)
    überschreiben sie.  Unser ``OSDetect``-Pendant heisst ``lyrion.utils.osdetect``;
    fehlt eine passende Antwort, bleibt es bei Perls Basisverhalten (``False``).
    """
    try:
        from lyrion.utils import osdetect

        fn = getattr(osdetect, "can_auto_update", None)
        if callable(fn):
            return bool(fn())
    except Exception as exc:  # noqa: BLE001 — die Seite darf daran nicht scheitern
        logger.debug("settings: canAutoUpdate nicht lesbar (%s)", exc)
    return False


def _running_from_source() -> bool:
    """Perl ``$os->runningFromSource`` (``Slim/Utils/OS.pm:510``).

    Perl vergleicht das Installationsverzeichnis mit dem Quellbaum; die
    Python-Fassung läuft als Modul aus ``src/`` oder als installiertes Paket.
    Ohne OSDetect-Antwort gilt: nicht aus dem Quellbaum.
    """
    try:
        from lyrion.utils import osdetect

        fn = getattr(osdetect, "running_from_source", None)
        if callable(fn):
            return bool(fn())
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings: runningFromSource nicht lesbar (%s)", exc)
    return False


def _can_auto_rescan() -> bool:
    r"""Perl ``$os->canAutoRescan`` — Basisklasse ``0`` (``Slim/Utils/OS.pm:455``).

    ``canAutoRescan`` ist keine Methode mit Klammern, sondern die Sub-Referenz
    ``\\&canAutoRescan``; ``Performance.pm:28`` ruft sie als ``getOS->canAutoRescan``
    auf.  Nur einige Plattformen (macOS, Windows) überschreiben sie mit ``1``.
    Ohne OSDetect-Antwort gilt der Basiswert ``False``.
    """
    try:
        from lyrion.utils import osdetect

        fn = getattr(osdetect, "can_auto_rescan", None)
        if callable(fn):
            return bool(fn())
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings: canAutoRescan nicht lesbar (%s)", exc)
    return False


def _multiple_clients() -> bool:
    """Perl ``Slim::Player::Client::clients() > 1`` (``Network.pm:36``).

    Bekannte Player unseres ``PlayerManager``; bei einem Fehler gilt „nur
    einer" (kein Feld) — Perls Vorlage prüft dasselbe über das Vorhandensein
    der Pref.
    """
    try:
        return len(PlayerManager().get_all_players()) > 1
    except Exception as exc:  # noqa: BLE001 — die Seite darf daran nicht scheitern
        logger.debug("settings: Playerliste nicht lesbar (%s)", exc)
        return False


#: ``Slim/Web/Settings/Server/Network.pm:28`` — ``prefs()``.
#: ``udpChunkSize`` (nur SLIMP3) und ``syncStartDelay`` (nur mehrere Player)
#: hängen an Laufzeitbedingungen (:31-38) und stehen deshalb NICHT in der
#: Liste; Perl zeigt sie nur, wenn ``$Slim::Player::SLIMP3::SLIMP3Connected``
#: bzw. mehr als ein Client existiert.  Unsere Seite zeigt die unbedingten
#: sieben Prefs — die zwei bedingten sind bewusst nicht dabei (siehe Modul-
#: Docstring „UNKLAR").
_NETWORK_DEFAULTS: dict[str, tuple[str, str]] = {
    "webproxy": ("", "server"),                          # Prefs.pm:209 (OS-Proxy)
    "httpport": ("9000", "server"),                      # Prefs.pm:210
    "bufferSecs": ("3", "server"),                       # Prefs.pm:211
    "remotestreamtimeout": ("15", "server"),             # Prefs.pm:212
    "maxRedirects": ("7", "server"),                     # Prefs.pm:216
    "maxWMArate": ("9999", "server"),                    # Prefs.pm:213
    "useEnhancedHTTP": ("0", "server"),                  # nicht in %defaults
    # ``Network.pm:36-38`` pusht ``syncStartDelay`` nur, wenn mehr als ein
    # Client verbunden ist; ``networking.html:32-36`` rendert es dann.
    "syncStartDelay": ("200", "server"),
}
#: Gültige Werte des ``maxWMArate``-Selects (``networking.html:25``).
_NETWORK_WMA_RATES = ("9999", "32", "64", "96", "128", "160", "192", "256", "320")
#: ``networking.html:67-69`` — die drei ``useEnhancedHTTP``-Stufen.
_NETWORK_HTTP_MODES = (
    ("0", "SETUP_DISABLE_ENHANCEDHTTP", "Normal streaming"),
    ("1", "SETUP_ENABLE_PERSISTENTHTTP", "Persistent mode"),
    ("2", "SETUP_ENABLE_BUFFEREDHTTP", "Cache HTTP(S) streams on disk"),
)


#: ``Slim/Web/Settings/Server/Security.pm:26-28`` — ``prefs()``.
_SECURITY_DEFAULTS: dict[str, tuple[str, str]] = {
    "filterHosts": ("0", "server"),                      # Prefs.pm:226
    "allowedHosts": ("", "server"),                      # Prefs.pm:227-230
    "corsAllowedHosts": ("", "server"),                  # nicht in %defaults
    "csrfProtectionLevel": ("0", "server"),              # Prefs.pm:231
    "authorize": ("0", "server"),                        # Prefs.pm:233
    "username": ("", "server"),                          # Prefs.pm:234
    "password": ("", "server"),                          # Prefs.pm:235 (SHA1-base64)
    "insecureHTTPS": ("0", "server"),                    # Prefs.pm:236
}


#: ``Slim/Web/Settings/Server/FileTypes.pm:27-29`` — ``prefs()`` nennt nur
#: ``prioritizeNative``; ``disabledextensionsaudio``/``disabledextensionsplaylist``
#: und ``disabledformats`` schreibt/liefert der Handler (:37-38, :67, :130-131).
_FILETYPES_DEFAULTS: dict[str, tuple[str, str]] = {
    "prioritizeNative": ("1", "server"),                 # Prefs.pm:205
    "disabledextensionsaudio": ("", "server"),           # Prefs.pm:203
    "disabledextensionsplaylist": ("", "server"),        # Prefs.pm:204
    "disabledformats": ("", "server"),                   # Prefs.pm:206 (Array)
}


#: ``Slim/Web/Settings/Server/Debugging.pm`` hat KEIN ``prefs()`` — die
#: Log-Stufen liegen in ``log4perl.conf`` (``Slim/Utils/Log.pm`` ``writeConfig``),
#: nicht in den Prefs.  Perl reicht die geforderten Werte an
#: ``setLogLevelForCategory`` weiter (:42-44) und schreibt sie über
#: ``persist`` in die Konfigurationsdatei (:48).  Wir speichern sie im
#: Pref-Store unter ``log.level.<category>``; die Stufen-Tabelle ist Perls
#: ``logLevels()`` (``Log.pm:876-948``), die Auswahlliste ``validLevels``
#: (``Log.pm:64``).  Die Anzeige eines ``select`` braucht trotzdem einen
#: Pref-Namen; die Perl-Kategorienamen sind selbst der Parametername
#: (``debugging.html:47`` ``name="[% category.name %]"``).
DEBUG_LEVELS = ("OFF", "FATAL", "ERROR", "WARN", "INFO", "DEBUG")   # Log.pm:64
#: ``Log.pm:66-102`` — die vier Protokollsätze mit ihren Kategorien.
DEBUG_LOG_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("SERVER", "DEBUG_SERVER_CHOOSE", "Server"),
    ("RADIO", "DEBUG_RADIO", "Internet Radio"),
    ("TRANSCODING", "DEBUG_TRANSCODING", "Transcoder"),
    ("SCANNER", "DEBUG_SCANNER_CHOOSE", "Media Scanner"),
)
#: ``Log.pm:879-948`` ``logLevels()`` — Kategorie → Vorbelegung.  ``perfmon``
#: ist über ``allCategories`` (:504-505) ausgeblendet und fehlt daher hier.
#: Die Liste ist zugleich die Kategorienliste, die Perl anzeigt — auf dem
#: Live-Server erscheinen zusätzlich die ``plugin.*``-Kategorien, weil Perl
#: ``_customCategories`` (:516-523) erst beim Laden der Plugins füllt.
DEBUG_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("server", "ERROR"),
    ("server.memory", "OFF"),
    ("server.plugins", "ERROR"),
    ("server.scheduler", "ERROR"),
    ("server.select", "ERROR"),
    ("server.timers", "ERROR"),
    ("server.update", "ERROR"),
    ("artwork", "ERROR"),
    ("artwork.imageproxy", "ERROR"),
    ("favorites", "ERROR"),
    ("prefs", "ERROR"),
    ("factorytest", "ERROR"),
    ("network.asyncdns", "ERROR"),
    ("network.asynchttp", "ERROR"),
    ("network.ws", "ERROR"),
    ("network.http", "ERROR"),
    ("network.protocol", "ERROR"),
    ("network.protocol.slimproto", "ERROR"),
    ("network.protocol.slimp3", "ERROR"),
    ("network.upnp", "ERROR"),
    ("network.jsonrpc", "ERROR"),
    ("network.cometd", "ERROR"),
    ("formats.audio", "ERROR"),
    ("formats.xml", "ERROR"),
    ("formats.playlists", "ERROR"),
    ("formats.metadata", "ERROR"),
    ("database.info", "ERROR"),
    ("database.sql", "ERROR"),
    ("database.virtuallibraries", "ERROR"),
    ("os.files", "ERROR"),
    ("os.paths", "ERROR"),
    ("control.command", "ERROR"),
    ("control.queries", "ERROR"),
    ("control.stdio", "ERROR"),
    ("menu.trackinfo", "ERROR"),
    ("player.alarmclock", "ERROR"),
    ("player.display", "ERROR"),
    ("player.fonts", "ERROR"),
    ("player.firmware", "ERROR"),
    ("player.jive", "ERROR"),
    ("player.ir", "ERROR"),
    ("player.menu", "ERROR"),
    ("player.playlist", "ERROR"),
    ("player.source", "ERROR"),
    ("player.streaming", "ERROR"),
    ("player.streaming.direct", "ERROR"),
    ("player.streaming.remote", "ERROR"),
    ("player.sync", "ERROR"),
    ("player.text", "ERROR"),
    ("player.ui", "ERROR"),
    ("player.ui.screensaver", "ERROR"),
    ("scan", "ERROR"),
    ("scan.auto", "DEBUG"),          # Log.pm:941 (XXX fest an)
    ("scan.scanner", "ERROR"),
    ("scan.import", "ERROR"),
    ("wizard", "ERROR"),
)
#: Pref-Namensraum der Log-Stufen (siehe ``DEBUG_CATEGORIES``-Kommentar).
_DEBUG_PREF_PREFIX = "log.level."

#: Von Plugins angemeldete Log-Kategorien (Perl ``addLogCategory``,
#: ``Log.pm:449-495``): Perls ``allCategories`` mischt ``_customCategories``
#: (``:516-523``) unter die Kernkategorien; auf dem Live-Server erscheinen so
#: die ``plugin.*``-Einträge (``Slim/Plugin/<X>/Plugin.pm`` je ein
#: ``addLogCategory``).  Dieser Port liefert die Kategorien der mitgelieferten
#: Plugins aus ``PLUGIN_LOG_CATEGORIES``; ein Plugin ohne Eintrag trägt keine
#: bei, und dann fehlt seine Zeile — wie in Perl, wenn das Plugin nicht lädt.
PLUGIN_LOG_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    # (Kategorie, Beschreibungstoken, Vorbelegung) — Vorlage der Perl-Datei.
    ("plugin.dontstopthemusic", "PLUGIN_DSTM", "ERROR"),   # DontStopTheMusic/Plugin.pm:41-45
)


def debug_categories() -> tuple[tuple[str, str], ...]:
    """Kern- plus Plugin-Kategorien (Perl ``allCategories``, ``Log.pm:496-514``).

    Reihenfolge: Kernkategorien wie in ``logLevels()``, danach die von den
    geladenen Plugins gemeldeten.  ``perfmon`` bleibt draußen — Perl blendet es
    über ``:504-505`` aus.
    """
    core = list(DEBUG_CATEGORIES)
    known = {name for name, _level in core}
    for category, _token, level in PLUGIN_LOG_CATEGORIES:
        if category not in known:
            core.append((category, level))
    return tuple(core)


def debug_description_token(category: str) -> str:
    """Perl ``descriptionForCategory`` (``Log.pm:583-595``).

    ``%descriptions`` wird in Perl von den Plugin-Tabellen gefüllt und ist für
    die Kernkategorien leer; der Rückfall ist ``uc("DEBUG_$category")`` mit
    ``.`` → ``_`` (``:591-593``) — genau das ist die ``strings.txt``-Zeile.
    Für von Plugins angemeldete Kategorien gewinnt das im Manifest genannte
    Token (``Slim/Plugin/<X>/Plugin.pm`` ``'description' => 'PLUGIN_X'``).
    """
    for name, token, _level in PLUGIN_LOG_CATEGORIES:
        if name == category:
            return token
    return "DEBUG_" + category.replace(".", "_").upper()


#: ``Slim/Web/Settings/Server/Performance.pm:26-27`` — ``prefs()``.
#: ``autorescan``/``autorescan_stat_interval`` pusht Perl nur, wenn
#: ``canAutoRescan`` (``:28``); die Basisklasse antwortet ``0``
#: (``Slim/Utils/OS.pm:455``), wir blenden die zwei Felder entsprechend aus.
_PERFORMANCE_DEFAULTS: dict[str, tuple[str, str]] = {
    "dbhighmem": ("0", "server"),                        # Prefs.pm:139
    "disableStatistics": ("0", "server"),                # Prefs.pm:218
    "serverPriority": ("", "server"),                    # Prefs.pm:219
    "scannerPriority": ("0", "server"),                  # Prefs.pm:220
    "useBalancedShuffle": ("0", "server"),               # Prefs.pm:224
    "precacheArtwork": ("1", "server"),                  # Prefs.pm:221
    "maxPlaylistLength": ("500", "server"),              # Prefs.pm:223
    "useLocalImageproxy": ("2", "server"),               # Prefs.pm:269
    "dontTriggerScanOnPrefChange": ("1", "server"),      # Prefs.pm:167
    "autorescan": ("0", "server"),                       # Prefs.pm:165
    "autorescan_stat_interval": ("10", "server"),        # Prefs.pm:166
}
#: ``Performance.pm:82-91`` — die Prioritäts-Auswahl (nur die fünf Labels,
#: die ``options`` setzt; ``-20..20`` bekommt sonst den leeren Text).
_PRIORITY_OPTIONS = (("-16", "SETUP_PRIORITY_HIGH", "High"),
                     ("-6", "SETUP_PRIORITY_ABOVE_NORMAL", "Above Normal"),
                     ("0", "SETUP_PRIORITY_NORMAL", "Normal"),
                     ("5", "SETUP_PRIORITY_BELOW_NORMAL", "Below Normal"),
                     ("15", "SETUP_PRIORITY_LOW", "Low"))


#: Alle ``%defaults``-Werte der hier gebauten Seiten (Registrierung beim
#: Seitenaufruf, siehe :func:`register_server_page_prefs`).
_SERVER_PAGE_DEFAULTS: dict[str, tuple[str, str]] = {
    **_SOFTWARE_DEFAULTS, **_SOFTWARE_AUX_DEFAULTS,
    **_NETWORK_DEFAULTS, **_SECURITY_DEFAULTS,
    **_FILETYPES_DEFAULTS, **_PERFORMANCE_DEFAULTS,
}


def _is_int_between(low: int, high: int) -> Callable[[str], bool]:
    """Perl ``setValidate({validator=>'intlimit',low=>..,high=>..})``.

    Z. B. ``httpport`` (``Prefs.pm:317`` ``1..65535``) und ``bufferSecs``
    (``:319`` ``3..30``).  Ein abgelehnter Wert wird wie in Perl NICHT
    geschrieben und als ``SETTINGS_INVALIDVALUE`` gemeldet (``Settings.pm:162-169``).
    """
    def _check(value: str) -> bool:
        text = (value or "").strip()
        if not text:
            return True                       # leer = Perl setzt den Default
        try:
            number = int(text)
        except ValueError:
            return False
        return low <= number <= high

    return _check


def _is_integer(value: str) -> bool:
    """Ganzzahl (Perls ``int``-Validator, ``Prefs.pm:309-311``)."""
    text = (value or "").strip()
    if not text:
        return True
    try:
        int(text)
    except ValueError:
        return False
    return True


def _is_number(value: str) -> bool:
    """Zahl (Perls ``num``-Validator, ``Prefs.pm:310-312``)."""
    text = (value or "").strip()
    if not text:
        return True
    try:
        float(text)
    except ValueError:
        return False
    return True


def _is_port(value: str) -> bool:
    """``intlimit 1..65535`` (``Prefs.pm:317``)."""
    return _is_int_between(1, 65535)(value)


#: Form eines gespeicherten Passworts: Perls ``Digest::SHA1::sha1_base64``
#: liefert 27 Zeichen base64 mit ``=``-Auffüllung (20 Byte), z. B.
#: ``5en6G6MezYKDBjFygIYk8SDJmyc=``.
_SHA1_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]{27}=$")


def perl_sha1_base64(plain: str) -> str:
    """Perl ``sha1_base64`` (``Security.pm:57-58``) — exakt derselbe Wert.

    ``Digest::SHA1::sha1_base64`` ist ``base64(sha1_raw)`` **ohne** Zeilenumbruch
    (``b64digest`` fügt keinen an).  Python: ``base64.b64encode(digest())``.
    Ein mit Perl gesetzter Hash und ein hier gesetzter sind damit identisch —
    Perls Login-Vergleich (``Slim/Web/HTTP.pm`` prüft ``sha1_base64($pw) eq
    $prefs->get('password')``) funktioniert also mit unserem Wert weiter.
    """
    digest = hashlib.sha1(plain.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _is_sha1_base64(value: str) -> bool:
    """Leer oder SHA1-base64 (Perl ``sha1_base64``, ``Security.pm:57``)."""
    text = (value or "").strip()
    return not text or bool(_SHA1_BASE64_RE.fullmatch(text))


_SOFTWARE = SettingsPage(
    route="/settings/server/software.html",
    perl_class="Slim::Web::Settings::Server::Software",
    page_name="SETUP_CHECKVERSION",              # Software.pm:16
    title_key="SETUP_CHECKVERSION",
    title_default="Software Updates",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Software.pm:15-31",
    fields=(
        Field("checkVersion", "SETUP_CHECKVERSION", "Software Updates", "select",
              options=(("1", "SETUP_CHECKVERSION_1", "Automatically check for software updates"),
                       ("0", "SETUP_CHECKVERSION_0", "Don't check for server software updates")),
              perl_source="Software.pm:24; software.html:6-8 (pref_checkVersion)"),
        Field("checkVersionInterval", "SETUP_CHECKVERSION", "Software Updates", "select",
              options=(("3600", "SETUP_CHECKVERSION_HOURLY", "Hourly"),
                       ("86400", "SETUP_CHECKVERSION_DAILY", "Daily"),
                       ("604800", "SETUP_CHECKVERSION_WEEKLY", "Weekly"),
                       ("2592000", "SETUP_CHECKVERSION_MONTHLY", "Monthly")),
              perl_source="Software.pm:24; software.html:15-18 (pref_checkVersionInterval)"),
        Field("autoDownloadUpdate", "SETUP_AUTO_DOWNLOAD", "Automatic Download", "select",
              options=(("1", "SETUP_AUTO_DOWNLOAD_1", "Automatically download updates when they're available."),
                       ("0", "SETUP_AUTO_DOWNLOAD_0", "Do not automatically download updates")),
              perl_source="Software.pm:26-28 (nur bei canAutoUpdate); software.html:28-29"),
    ),
)

_NETWORK = SettingsPage(
    route="/settings/server/networking.html",
    perl_class="Slim::Web::Settings::Server::Network",
    page_name="NETWORK_SETTINGS",                # Network.pm:20
    title_key="NETWORK_SETTINGS",
    title_default="Network",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Network.pm:19-41",
    fields=(
        Field("webproxy", "SETUP_WEBPROXY", "Web Proxy",
              perl_source="Network.pm:28; networking.html:3 (pref_webproxy)"),
        Field("httpport", "SETUP_HTTPPORT", "Web Server Port Number",
              validator=_is_port,
              perl_source="Network.pm:28,46-63; Prefs.pm:317 (intlimit 1..65535)"),
        Field("maxRedirects", "SETUP_MAX_REDIRECTS", "Maximum number of redirects",
              validator=_is_integer,
              perl_source="Network.pm:28; networking.html:11 (pref_maxRedirects)"),
        Field("bufferSecs", "SETUP_BUFFERSECS", "Radio Station Buffer Seconds",
              validator=_is_int_between(3, 30),
              perl_source="Network.pm:28; Prefs.pm:319 (intlimit 3..30)"),
        Field("remotestreamtimeout", "SETUP_REMOTESTREAMTIMEOUT", "Radio Station Timeout",
              validator=_is_number,
              perl_source="Network.pm:28; networking.html:20 (pref_remotestreamtimeout)"),
        Field("maxWMArate", "SETUP_MAXWMARATE", "Maximum WMA Stream Bitrate", "select",
              options=tuple((r, "NO_LIMIT" if r == "9999" else r,
                             "No Limit" if r == "9999" else r)
                            for r in _NETWORK_WMA_RATES),
              perl_source="Network.pm:28; networking.html:24-29 (pref_maxWMArate)"),
        Field("useEnhancedHTTP", "SETUP_ENHANCEDHTTP", "Streaming mode for HTTP(S)", "select",
              options=_NETWORK_HTTP_MODES,
              perl_source="Network.pm:28; networking.html:65-71 (pref_useEnhancedHTTP 0/1/2)"),
        # ``Network.pm:36-38`` — nur bei mehr als einem Client; die Vorlage
        # prüft ``prefs.exists('pref_syncStartDelay')`` (``networking.html:32``),
        # wir zeigen das Feld nur, wenn mehrere Player bekannt sind (siehe
        # ``_render_page``).
        Field("syncStartDelay", "SETUP_SYNCSTARTDELAY",
              "Synchronized Players Startup Delay (ms)", "conditional",
              perl_source="Network.pm:36-38 (nur bei clients() > 1); networking.html:33"),
    ),
)

_SECURITY = SettingsPage(
    route="/settings/server/security.html",
    perl_class="Slim::Web::Settings::Server::Security",
    page_name="SECURITY_SETTINGS",               # Security.pm:19
    title_key="SECURITY_SETTINGS",
    title_default="Security",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Security.pm:18-27",
    fields=(
        Field("authorize", "SETUP_AUTHORIZE", "Password Protection", "select",
              options=(("0", "SETUP_NO_AUTHORIZE", "No password protection"),
                       ("1", "SETUP_AUTHORIZE", "Password Protection")),
              perl_source="Security.pm:27,34-39; security.html:5-8 (pref_authorize)"),
        Field("username", "SETUP_USERNAME", "Username",
              perl_source="Security.pm:27,34-39; security.html:14 (pref_username)"),
        # ``pref_password``/``pref_password_repeat`` sind KEINE Einträge der
        # ``prefs()``-Liste — Perl setzt ``password`` selbst auf den
        # SHA1-base64-Wert (``Security.pm:55-59``) und die Vorlage füllt beide
        # Felder mit dem *gespeicherten* Hash (``security.html:18,22``
        # ``value="[% prefs.pref_password %]"``).  Wir zeigen beide Felder LEER
        # (den Hash geben wir nie heraus) und schreiben nur bei Eingabe —
        # siehe ``_save_server_security``.
        Field("password", "SETUP_PASSWORD", "Password",
              "password", never_show_value=True,
              perl_source="Security.pm:42-61 (sha1_base64); security.html:18 (pref_password)"),
        Field("password_repeat", "SETUP_PASSWORD_REPEAT", "Confirm Password",
              "password", never_show_value=True, not_stored=True,
              perl_source="Security.pm:46-51; security.html:22 (pref_password_repeat)"),
        Field("filterHosts", "SETUP_IPFILTER_HEAD", "Block Incoming Connections", "select",
              options=(("0", "SETUP_NO_IPFILTER", "Do not block"),
                       ("1", "SETUP_IPFILTER", "Block")),
              perl_source="Security.pm:27; security.html:28-31 (pref_filterHosts)"),
        Field("allowedHosts", "SETUP_FILTERRULE_HEAD", "Allowed IP Addresses",
              perl_source="Security.pm:27; security.html:37 (pref_allowedHosts)"),
        Field("csrfProtectionLevel", "SETUP_CSRFPROTECTIONLEVEL", "CSRF Protection Level", "select",
              options=(("0", "NONE", "None"),
                       ("1", "MEDIUM", "Medium"),
                       ("2", "HIGH", "High")),
              perl_source="Security.pm:27; security.html:41-45 (pref_csrfProtectionLevel)"),
        Field("corsAllowedHosts", "SETUP_CORS_ALLOWED_HOSTS", "CORS Allowed Hosts",
              perl_source="Security.pm:27; security.html:51 (pref_corsAllowedHosts)"),
        Field("insecureHTTPS", "SETUP_INSECURE_HTTPS", "Insecure HTTPS", "select",
              options=(("0", "NO", "No"), ("1", "YES", "Yes")),
              perl_source="Security.pm:27; security.html:55 (pref_insecureHTTPS)"),
    ),
)

_FILETYPES = SettingsPage(
    route="/settings/server/filetypes.html",
    perl_class="Slim::Web::Settings::Server::FileTypes",
    page_name="FORMATS_SETTINGS",                # FileTypes.pm:20
    title_key="FORMATS_SETTINGS",
    title_default="File Types",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/FileTypes.pm:19-29",
    fields=(
        Field("disabledextensionsaudio", "SETUP_DISABLEDEXTENSIONSAUDIO",
              "Disabled Audio File Extensions", no_pref_prefix=True, not_stored=True,
              perl_source="FileTypes.pm:37; filetypes.html:5 (disabledextensionsaudio)"),
        Field("disabledextensionsplaylist", "SETUP_DISABLEDEXTENSIONSPLAYLIST",
              "Disabled Playlist File Extensions", no_pref_prefix=True, not_stored=True,
              perl_source="FileTypes.pm:38; filetypes.html:9 (disabledextensionsplaylist)"),
        Field("prioritizeNative", "SETUP_GROUP_FORMATS_CONVERSION",
              "File Format Conversion Setup", "select",
              options=(("1", "YES", "Yes"), ("0", "NO", "No")),
              perl_source="FileTypes.pm:28,68; filetypes.html:14 (pref_prioritizeNative)"),
    ),
)

_DEBUGGING = SettingsPage(
    route="/settings/server/debugging.html",
    perl_class="Slim::Web::Settings::Server::Debugging",
    page_name="DEBUGGING_SETTINGS",              # Debugging.pm:17
    title_key="DEBUGGING_SETTINGS",
    title_default="Logging",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Debugging.pm:16-22",
    # Kein ``prefs()`` — die Seite hat eine eigene Oberfläche (Log-Sätze,
    # Kategorien, ``persist``); die Felder entstehen erst im Handler
    # (:30-76).  Siehe ``DEBUG_CATEGORIES``/``_render_debugging_page``.
)

_PERFORMANCE = SettingsPage(
    route="/settings/server/performance.html",
    perl_class="Slim::Web::Settings::Server::Performance",
    page_name="PERFORMANCE_SETTINGS",            # Performance.pm:18
    title_key="PERFORMANCE_SETTINGS",
    title_default="Performance",
    needs_client=False,
    perl_source="Slim/Web/Settings/Server/Performance.pm:17-30",
    fields=(
        Field("dbhighmem", "SETUP_DBHIGHMEM", "Database Memory Config", "select",
              options=(("0", "SETUP_DBHIGHMEM_NORMAL", "Normal"),
                       ("1", "SETUP_DBHIGHMEM_HIGH", "High (recommended for machines with 1+ GB RAM)"),
                       ("2", "SETUP_DBHIGHMEM_MAX", "Maximum (recommended for libraries with more than 50,000 tracks and machines with 2+ GB RAM)")),
              perl_source="Performance.pm:26; performance.html:6-8 (pref_dbhighmem)"),
        Field("dontTriggerScanOnPrefChange", "SETUP_SCAN_ON_PREF_CHANGE",
              "Trigger Scan on Preference Changes", "select",
              options=(("0", "SETUP_SCAN_ON_PREF_CHANGE_ON", "Trigger scan automatically"),
                       ("1", "SETUP_SCAN_ON_PREF_CHANGE_OFF", "Prompt to scan, but don't trigger it automatically")),
              perl_source="Performance.pm:26; performance.html:16-17 (pref_dontTriggerScanOnPrefChange)"),
        Field("useBalancedShuffle", "SETUP_SHUFFLE_METHOD", "Shuffle method", "select",
              options=(("0", "SETUP_USE_FASTER_SHUFFLE", "Use faster, but less balanced shuffle"),
                       ("1", "SETUP_USE_BALANCED_SHUFFLE", "Use more balanced, but slower shuffle")),
              perl_source="Performance.pm:26; performance.html:24-25 (pref_useBalancedShuffle)"),
        Field("disableStatistics", "SETUP_DISABLESTATISTICS", "Library Statistics", "select",
              options=(("0", "SETUP_ENABLE_STATISTICS", "Enable library statistics"),
                       ("1", "SETUP_DISABLE_STATISTICS", "Disable library statistics")),
              perl_source="Performance.pm:26; performance.html:32-33 (pref_disableStatistics)"),
        Field("precacheArtwork", "SETUP_PRECACHEARTWORK", "Artwork Pre-caching", "select",
              options=(("1", "SETUP_PRECACHEARTWORK_ENABLED", "Pre-cache album artwork"),
                       ("0", "SETUP_PRECACHEARTWORK_DISABLED", "Do not pre-cache artwork")),
              perl_source="Performance.pm:26; performance.html:41-42 (pref_precacheArtwork)"),
        Field("useLocalImageproxy", "SETUP_IMAGEPROXY", "Artwork resizing", "select",
              options=(("1", "SETUP_IMAGEPROXY_LOCAL", "Use Lyrion Music Server to resize artwork"),
                       ("2", "SETUP_IMAGEPROXY_HELPER", "Use Lyrion Music Server resizing helper to resize artwork")),
              perl_source="Performance.pm:26; performance.html:67-71 (pref_useLocalImageproxy)"),
        Field("serverPriority", "SETUP_SERVERPRIORITY", "Server Priority", "select",
              options=(("", "SETUP_PRIORITY_CURRENT", "Current Server Priority"), *_PRIORITY_OPTIONS),
              perl_source="Performance.pm:26; performance.html:78-86 (pref_serverPriority)"),
        Field("scannerPriority", "SETUP_SCANNERPRIORITY", "Scanner Priority", "select",
              options=(("", "SETUP_PRIORITY_CURRENT", "Current Server Priority"), *_PRIORITY_OPTIONS),
              perl_source="Performance.pm:26; performance.html:90-98 (pref_scannerPriority)"),
        Field("maxPlaylistLength", "SETUP_MAXPLAYLISTLENGTH", "Maximum Playlist Length",
              validator=_is_integer,
              perl_source="Performance.pm:26; Prefs.pm:332-336; performance.html:122"),
    ),
)


#: Perl-Route → Seite. Schlüssel ist der HTTP-Pfad (ohne Slash am Ende).
PAGES: dict[str, SettingsPage] = {
    _BASIC_SERVER.route: _BASIC_SERVER,
    _SOFTWARE.route: _SOFTWARE,
    _NETWORK.route: _NETWORK,
    _SECURITY.route: _SECURITY,
    _FILETYPES.route: _FILETYPES,
    _DEBUGGING.route: _DEBUGGING,
    _PERFORMANCE.route: _PERFORMANCE,
    _PLAYER_AUDIO.route: _PLAYER_AUDIO,
    _PLAYER_DISPLAY.route: _PLAYER_DISPLAY,
    _PLAYER_ALARM.route: _PLAYER_ALARM,
    _INFORMATION.route: _INFORMATION,
    _ART_ONLINE.route: _ART_ONLINE,
    _PLUGINS.route: _PLUGINS,
    # Aufgaben-Alias (kein Perl-Fund, siehe Modul-Docstring „UNKLAR").
    "/settings/information.html": _INFORMATION,
}

#: Zustand und Anstoss der wanted-Liste als JSON (Knopf/Anzeige der Seite).
#: Kein Perl-Vorbild — Perl hat keinen Hintergrund-Coverdienst, der Zustand
#: wäre also nirgends abzufragen (``Slim/Plugin/RadioArtwork/Plugin.pm``).
ART_WANTED_STATUS_URL = "/settings/artworkonline/status.json"
ART_WANTED_RUN_URL = "/settings/artworkonline/run"

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


# ── Online-Cover-Suche: Pref-Werte ↔ Laufzeit-Konfiguration ─────────────────
#
# Perl wertet die Prefs seiner Artwork-Kette an der Fundstelle aus
# (``Slim/Music/Artwork.pm:72`` ``$prefs->get('coverArt')``, ``:870``
# ``thumbSize``); genauso liest ``lyrion.media.art_online`` seine Prefs über
# diese Funktionen.  Fehlt eine Pref (frischer Server), gilt der Default aus
# ``art_online.PREF_DEFAULTS`` — die Suche läuft also ohne Einrichtung und ohne
# jeden API-Key (MusicBrainz + Cover Art Archive brauchen keinen).

def art_online_pref_values() -> dict[str, str]:
    """Alle ``artworkOnline*``-Prefs mit Vorbelegung (nie ``None``)."""
    prefs = get_prefs()
    values: dict[str, str] = {}
    for name, default in _ART_PREF_DEFAULTS.items():
        raw = prefs.get(name)
        values[name] = default if raw is None else str(raw)
    return values


def load_art_online_settings() -> ArtOnlineSettings:
    """Laufzeit-Einstellungen der Online-Suche aus den Prefs bauen."""
    return ArtOnlineSettings.from_mapping(art_online_pref_values())


def art_online_wanted_pref_values() -> dict[str, str]:
    """Die zwei Prefs der wanted-Liste mit Vorbelegung (nie ``None``).

    Eigener Satz, damit ``art_online.PREF_DEFAULTS`` (und damit das Formular auf
    ``/settings/server/basic.html``) unverändert bleibt; die Felder dieser Prefs
    stehen auf ``/settings/server/artworkonline.html``.
    """
    prefs = get_prefs()
    values: dict[str, str] = {}
    for name, default in _ART_WANTED_PREF_DEFAULTS.items():
        raw = prefs.get(name)
        values[name] = default if raw is None else str(raw)
    return values


# ── Bildproxy-Prefs (bewusste Abweichung, siehe Block oben) ─────────────────

def imageproxy_pref_values() -> dict[str, str]:
    """Alle ``imageProxy*``-Prefs dieses Ports mit Vorbelegung (nie ``None``)."""
    prefs = get_prefs()
    values: dict[str, str] = {}
    for name, default in IMAGEPROXY_PREF_DEFAULTS.items():
        raw = prefs.get(name)
        values[name] = default if raw is None else str(raw)
    return values


def imageproxy_follow_redirects() -> bool:
    """Verfolgt der Bildproxy Weiterleitungen serverseitig?  (Vorbelegung AN)

    Gelesen an der Fundstelle wie Perls ``$prefs->get(...)``
    (``Slim/Web/ImageProxy.pm:145``); fehlt die Pref (frischer Server), gilt
    ``IMAGEPROXY_PREF_DEFAULTS``.  Leer/``0``/``no``/``off``/``false`` = AUS —
    dann antwortet ``/imageproxy`` wieder mit Perls 301.
    """
    value = imageproxy_pref_values()[IMAGEPROXY_FOLLOW_REDIRECTS_PREF]
    return value.strip().lower() not in ("", "0", "no", "off", "false")


async def register_imageproxy_prefs() -> None:
    """``imageProxy*``-Prefs mit Default registrieren (idempotent).

    Wie :func:`register_art_online_prefs`: ohne Registrierung zeigte das
    Formular beim ersten Aufruf einen leeren Wert, obwohl der Bildproxy längst
    mit dem Default arbeitet.
    """
    prefs = get_prefs()
    for name, default in IMAGEPROXY_PREF_DEFAULTS.items():
        try:
            await prefs.init_preference(name, default=default,
                                        category="imageproxy")
        except Exception as exc:  # noqa: BLE001 - Settings-Seite darf nicht brechen
            logger.warning("settings: %s konnte nicht registriert werden (%s)",
                           name, exc)


async def register_art_online_prefs() -> None:
    """``artworkOnline*``-Prefs mit Default registrieren (idempotent).

    Wie Perls ``%defaults`` beim Start (``Slim/Utils/Prefs.pm:265-268``
    registriert ``coverArt``/``artfolder``/``thumbSize``): ohne Registrierung
    hätte die Settings-Seite beim ersten Aufruf leere Felder, obwohl die Suche
    mit Defaults arbeitet.
    """
    prefs = get_prefs()
    for name, default in _ART_PREF_DEFAULTS.items():
        try:
            await prefs.init_preference(name, default=default, category="artwork")
        except Exception as exc:  # noqa: BLE001 - Settings-Seite darf nicht brechen
            logger.warning("settings: %s konnte nicht registriert werden (%s)",
                           name, exc)
    # Dieselbe Registrierung für die Prefs der wanted-Liste (Dienst-Schalter,
    # Deckel je Durchlauf) — ohne sie zeigte die Seite leere Felder.
    for name, default in _ART_WANTED_PREF_DEFAULTS.items():
        try:
            await prefs.init_preference(name, default=default, category="artwork")
        except Exception as exc:  # noqa: BLE001 - Settings-Seite darf nicht brechen
            logger.warning("settings: %s konnte nicht registriert werden (%s)",
                           name, exc)


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
        if f.not_stored:
            # Reines Formularfeld (``password_repeat``): Perl liest es nur zum
            # Vergleich (``Security.pm:46``), es gibt keine Pref dieses Namens.
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


async def register_server_page_prefs() -> None:
    """``%defaults`` der neuen Server-Seiten registrieren (idempotent).

    Perl legt die Vorbelegung beim Start an (``Slim/Utils/Prefs.pm:129-286``
    ``init`` mit ``%defaults``, ausgewertet in ``Base.pm:178-234``); wer die
    Pref nie anfasst, liest sie trotzdem mit diesem Wert.  Unser Store kennt
    die Prefs sonst erst, wenn sie geschrieben wurden — das Formular zeigte
    dann leere Felder, obwohl der Server mit dem Default arbeitet.  Deshalb
    registriert jeder Seitenaufruf die Defaults der Seite, die er zeigt.
    """
    prefs = get_prefs()
    for name, (default, category) in _SERVER_PAGE_DEFAULTS.items():
        try:
            await prefs.init_preference(name, default=default, category=category)
        except Exception as exc:  # noqa: BLE001 - Settings-Seite darf nicht brechen
            logger.warning("settings: %s konnte nicht registriert werden (%s)",
                           name, exc)
    # Log-Stufen der Debugging-Seite (``log.level.<category>``), Vorbelegung
    # aus Perls ``logLevels()`` (``Slim/Utils/Log.pm:879-948``).
    for category, level in debug_categories():
        try:
            await prefs.init_preference(_DEBUG_PREF_PREFIX + category, default=level,
                                        category="logs")
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings: %s%s konnte nicht registriert werden (%s)",
                           _DEBUG_PREF_PREFIX, category, exc)


def debug_category_level(category: str) -> str:
    """Aktuelle Log-Stufe einer Kategorie (Perl ``allCategories``, ``Log.pm:496-514``).

    Perl liest die laufende Konfiguration (``%runningConfig``); unser Pendant
    ist die Pref ``log.level.<category>``, deren Vorbelegung Perls
    ``logLevels()``-Wert ist.
    """
    defaults = dict(debug_categories())
    value = get_prefs().get(_DEBUG_PREF_PREFIX + category)
    if value is None:
        return defaults.get(category, "ERROR")
    level = str(value).strip().upper()
    return level if level in DEBUG_LEVELS else defaults.get(category, "ERROR")


def parse_debug_logging_group(params: dict[str, str]) -> Optional[str]:
    """Perl ``logging_group`` (``Debugging.pm:32-36``) — ``""`` oder ``'DEFAULT'`` = keiner."""
    group = (params.get("logging_group") or "").strip().upper()
    if group in ("", "DEFAULT"):
        return None
    return group if group in {g for g, _k, _d in DEBUG_LOG_GROUPS} else None


def debug_group_levels(group: str) -> dict[str, str]:
    """Perl ``logLevels($group)`` (``Log.pm:876-957``) — Kategorien des Satzes.

    Die vier Sätze setzen je einen Teil der Kategorien auf ``DEBUG``
    (``Log.pm:66-102``); alle übrigen behalten ihren Grundwert.  Ein unbekannter
    Satz ändert nichts (Perl läuft in ``$logGroups->{$group}->{categories}``
    dann in einen Autovivifikations-Fehler; wir behandeln das als „kein Satz").
    """
    overrides: dict[str, str] = {}
    for name, _label_key, _label in DEBUG_LOG_GROUPS:
        if name != group:
            continue
        for category, level in debug_categories():
            overrides[category] = level
        if group == "SERVER":
            overrides.update({"server": "DEBUG", "server.plugins": "DEBUG"})
        elif group == "RADIO":
            overrides.update({"formats.audio": "DEBUG", "network.asyncdns": "DEBUG"})
        elif group == "TRANSCODING":
            overrides.update({"player.source": "DEBUG", "player.streaming": "DEBUG"})
        elif group == "SCANNER":
            overrides.update({"scan": "DEBUG", "scan.auto": "DEBUG",
                              "scan.scanner": "DEBUG", "scan.import": "DEBUG",
                              "artwork": "DEBUG", "database.info": "DEBUG",
                              "database.virtuallibraries": "DEBUG",
                              "formats.audio": "DEBUG", "formats.playlists": "DEBUG"})
    return overrides


async def _save_server_security(params: dict[str, str], page: SettingsPage,
                                player, invalid: Optional[list[tuple[str, str]]] = None
                                ) -> list[str]:
    """``Server/Security.pm:30-64`` — Passwort + Benutzername-Regel.

    Perl:

    * ``:34-39`` — ``authorize`` ohne ``username`` ist nicht erlaubt: Warnung
      ``SETUP_MISSING_USERNAME`` und ``authorize`` wird auf ``0`` gesetzt;
    * ``:42-51`` — ``password`` ≠ ``password_repeat``: Warnung
      ``SETUP_PASSWORD_MISMATCH`` und ebenfalls ``authorize = 0``;
    * ``:55-59`` — sonst wird ``password`` als ``sha1_base64`` gespeichert
      (nur wenn der Klartext abweicht).

    Die Basisklasse schreibt danach alle ``pref_*``-Werte (``Settings.pm:154-171``);
    ``password`` ist kein Formularfeld, deshalb hier von Hand.
    """
    prefs = get_prefs()
    warning = ""

    authorize = params.get("pref_authorize", "0")
    username = params.get("pref_username")
    if "pref_authorize" in params and authorize and authorize != "0":
        if username is not None and not username.strip():   # Security.pm:34-39
            warning += _string("SETUP_MISSING_USERNAME",
                               "You can't enable authorization without a password.") + " "
            params = dict(params, pref_authorize="0")

    plain = params.get("pref_password") or ""
    repeat = params.get("pref_password_repeat") or ""
    written_password: list[str] = []
    if plain:                                               # Security.pm:42-61
        if plain != repeat:
            warning += _string("SETUP_PASSWORD_MISMATCH",
                               "The passwords you entered do not match.") + " "
            params = dict(params, pref_authorize="0")
        else:
            stored = prefs.get("password") or ""
            hashed = perl_sha1_base64(plain)
            if stored != hashed:                            # Security.pm:57
                await prefs.set("password", hashed)
                written_password = ["password"]
            else:
                written_password = []

    # ``pref_password``/``pref_password_repeat`` dürfen NICHT über die generische
    # Schleife laufen: Perl speichert ``password`` nur als ``sha1_base64``
    # (``Security.pm:57-58``) und ``password_repeat`` überhaupt nicht — es ist
    # reines Vergleichsfeld (``:46``).  Ohne das Entfernen landete der
    # Klartext in der Pref ``password``.
    safe_params = {k: v for k, v in params.items()
                   if k not in ("pref_password", "pref_password_repeat")}
    written = await _save_simple_prefs(page, safe_params, player, invalid)
    written.extend(written_password)
    # Seitenrahmen; Perl hängt sie roh an ``$paramRef->{warning}``
    # (``Security.pm:36,48``), deshalb führt der Aufrufer sie zusammen.
    return written + (["!warning:" + warning] if warning else [])


async def _save_server_network(params: dict[str, str], page: SettingsPage,
                               player, invalid: Optional[list[tuple[str, str]]] = None
                               ) -> list[str]:
    """``Server/Network.pm:43-66`` — nur die Erfolgsmeldung zum ``httpport``.

    Perl prüft dort, ob der neue Port gültig ist (``$ok``, ``:48-50``) und hängt
    dann ``SETUP_HTTPPORT_OK`` mit dem Link auf die neue Server-URL an.  Die
    Prefs selbst schreibt die Basisklasse; wir schreiben sie über
    :func:`_save_simple_prefs` und melden den Port-Wechsel genauso.
    """
    old_port = _server_value("httpport")            # ``$prefs->get('httpport')`` (:46)
    written = await _save_simple_prefs(page, params, player, invalid)
    new_port = params.get("pref_httpport")
    if "pref_httpport" in params and new_port and new_port != old_port:
        written = written + ["!httpport_ok:" + new_port]
    return written


#: ``FileTypes.pm:44`` — die ``Conversions()``-Profile aus der Konversionstabelle.
#: Perl liest sie aus ``convert.conf``/``custom-convert.conf``
#: (``TranscodingHelper.pm:51-134``): je Zeile ``<input> <output> <clienttype>
#: <clientid>`` darunter das Kommando.  Dieser Port hat **dieselbe Datei**
#: (``lyrion/media/convert.conf``), also parsen wir sie mit derselben Regel —
#: keine erfundene Profilliste.
_FILETYPES_INTERNAL_PREFIXES = ("spdr", "test")     # FileTypes.pm:78


def _convert_conf_path():
    """``dirsFor('convert')/convert.conf`` (``TranscodingHelper.pm:58-64``)."""
    from pathlib import Path

    for candidate in (Path(__file__).resolve().parents[1] / "media" / "convert.conf",
                      Path(__file__).resolve().parents[2] / "media" / "convert.conf"):
        if candidate.is_file():
            return candidate
    return None


def conversion_table() -> dict[str, str]:
    """``Slim::Player::TranscodingHelper::Conversions()`` (``:39-41``).

    ``{profil: kommandzeile}``; die Profilbildung ist die von
    ``TranscodingHelper.pm:93-105`` (``"$input-$output-$clienttype-$clientid"``,
    Duplikate bekommen ``-<n>`` angehängt, ``clientid`` klein geschrieben).
    """
    from pathlib import Path

    path = _convert_conf_path()
    table: dict[str, str] = {}
    if path is None:
        return table
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return table

    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):        # :82-83
            continue
        stripped = stripped.split("#", 1)[0].strip()        # :86-88
        if not stripped:
            continue
        parts = stripped.split()                            # :93
        if len(parts) != 4:
            continue
        input_type, output_type, client_type, client_id = parts
        profile = f"{input_type}-{output_type}-{client_type}-{client_id.lower()}"
        # ``:102-105`` — doppeltes Profil bekommt ``-<n>``
        if profile in table:
            suffix = 1
            while f"{profile}-{suffix}" in table:
                suffix += 1
            profile = f"{profile}-{suffix}"
        if index >= len(lines):
            break
        command_line = lines[index]
        index += 1
        # ``:108-113`` — eine Capabilities-Zeile über dem Kommando wird
        # übersprungen (``_getCapabilities``); sonst ist die erste Zeile das
        # Kommando (Default-Capabilities I+F).
        if command_line.lstrip().startswith("#"):
            if index >= len(lines):
                break
            command_line = lines[index]
            index += 1
        command_line = command_line.strip()                 # :117-118
        if not command_line:                                # :127
            continue
        table[profile] = command_line
    return table


def _command_binaries(command: str) -> list[str]:
    """Perl ``checkBin``: die ``[...]``-Programme des Kommandos (``:284-304``)."""
    return [name for name in re.findall(r"\[([^]]+)\]", command or "") if name]


def _binary_available(name: str) -> bool:
    """Perl ``Slim::Utils::Misc::findbin`` (``:289``) — Programm im PATH?"""
    from shutil import which

    return bool(name) and which(name) is not None


def filetypes_profiles() -> list[tuple[str, str, str, list[str], int]]:
    """``(profile, input, output, binaries, enabled)`` der Konversionsprofile.

    Perl ``FileTypes.pm:75-126``: für jedes Profil aus ``Conversions()`` ohne
    ``transcode``-Anteil und ohne ``spdr``/``test``-Präfix entsteht eine Zeile
    mit ``input``/``output``, der Dekoder-Auswahl (``DISABLED`` plus die im
    Kommand gefundenen Programme, die im PATH liegen, ``:107-113``) und
    ``enabled = checkBin($profile)`` (``:84``).  ``disabledformats`` hält die
    abgewählten Profile (``:40,67``).

    Die Profilmenge kommt aus :func:`conversion_table` — unserer eigenen
    ``lyrion/media/convert.conf``.  Sie deckt sich mit der des Live-Perl bis auf
    drei ``lpcm``-Profile, die nur in unserer Datei stehen (Perls Live-Seite
    rendert 74, wir 77); die Abweichung kommt aus der Datei, nicht aus dem
    Parser.
    """
    disabled = {str(v) for v in _as_list(get_prefs().get("disabledformats"))}
    rows: list[tuple[str, str, str, list[str], int]] = []
    table = conversion_table()

    for profile in sorted(table):
        # ``:46`` ``grep {$_ !~ /transcode/}`` + ``:78`` ``spdr|test``
        if "transcode" in profile:
            continue
        parts = profile.split("-")                          # ``:80``
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        if source.startswith(_FILETYPES_INTERNAL_PREFIXES) or \
                target.startswith(_FILETYPES_INTERNAL_PREFIXES):
            continue
        command = table[profile]
        binaries = ["DISABLED"]
        if command == "-":                                  # ``:110-113`` native
            binaries.append("NATIVE")
        else:
            for name in _command_binaries(command):
                if _binary_available(name) and name not in binaries:
                    binaries.append(name)
        enabled = 1 if (profile not in disabled and len(binaries) > 1) else 0
        rows.append((profile, source, target, binaries, enabled))
    return rows


async def _save_debugging(params: dict[str, str]) -> list[str]:
    """``Server/Debugging.pm:28-52`` — Log-Satz oder Einzel-Kategorien.

    Perl: ist ``logging_group`` gesetzt, bestimmt ``setLogGroup`` alle
    Kategorien (``:32-36``); sonst setzt es je Kategorie den Formularwert
    (``:40-46``).  Danach ``persist`` (``:48``) und ``reInit`` (``:51``).
    Passwort/Login unberührt.

    ``persist`` schreibt Perl in die ``log4perl.conf`` (``Log.pm:205-244``);
    unser Gegenstück ist die Pref ``log.persist``, die die Log-Einstellungen
    über einen Neustart trägt.
    """
    prefs = get_prefs()
    written: list[str] = []

    group = parse_debug_logging_group(params)               # Debugging.pm:32
    if group:
        levels = debug_group_levels(group)
    else:
        levels = {}
        for category, _default in debug_categories():
            if category in params:
                level = str(params[category]).strip().upper()
                if level in DEBUG_LEVELS:                    # Log.pm:64,442
                    levels[category] = level

    for category, level in levels.items():
        await prefs.set(_DEBUG_PREF_PREFIX + category, level)  # :42-44
        written.append(_DEBUG_PREF_PREFIX + category)
        # Dieselbe Stufe an den laufenden Logger geben (Perl ruft
        # ``setLogLevelForCategory``; unser Pendant ist ``utils.log.set_level``).
        try:
            from lyrion.utils import log as log_module

            log_module.set_level(category, _python_level(level))
        except Exception as exc:  # noqa: BLE001 — Schreiben ist wichtiger
            logger.debug("settings: log level %s nicht gesetzt (%s)", category, exc)

    # ``persist`` (``:48``): Checkbox ⇒ gespeicherte Stufen beim nächsten
    # Start wieder anwenden; ohne ``persist`` schreibt Perl die Konfiguration
    # nur beim ersten Mal (``Log.pm:214-230``).
    persist = "1" if str(params.get("persist", "")).strip() else "0"
    await prefs.set("log.persist", persist)
    written.append("log.persist")

    if group:
        await prefs.set("log.group", group)
        written.append("log.group")
    return written


#: Perls ``validLevels`` → Python-``logging``-Stufen (``Log.pm:64`` gegen
#: ``utils/log.py:41-45``).
_DEBUG_LEVEL_TO_PYTHON: dict[str, int] = {
    "OFF": 100,          # über CRITICAL: nichts wird ausgegeben
    "FATAL": 50,
    "ERROR": 40,
    "WARN": 30,
    "INFO": 20,
    "DEBUG": 10,
}


def _python_level(perl_level: str) -> int:
    return _DEBUG_LEVEL_TO_PYTHON.get(perl_level.strip().upper(), 40)


async def _save_filetypes(params: dict[str, str], page: SettingsPage,
                          player, invalid: Optional[list[tuple[str, str]]] = None
                          ) -> list[str]:
    """``Server/FileTypes.pm:31-69`` — Handler-Schreibpfade.

    Perl liest hier die **unpräfixierten** Parameternamen (``:37-38``
    ``$paramRef->{'disabledextensionsaudio'}``), weil die Vorlage sie so
    ausgibt (``filetypes.html:5,9`` ``name="disabledextensionsaudio"`` ohne
    ``pref_``).  Die Profil-``select``s (``:37``) heissen wie das Profil selbst
    und liefern ``DISABLED`` oder den Dekodernamen; ``disabledformats`` ist die
    Liste der auf ``DISABLED`` gestellten Profile (``:40,51-67``).

    ``prioritizeNative`` läuft über ``pref_`` (``:28,68``).
    """
    prefs = get_prefs()
    written = await _save_simple_prefs(page, params, player, invalid)

    # ``:37-38`` — unpräfixierte Datei-Endungen.
    for name in ("disabledextensionsaudio", "disabledextensionsplaylist"):
        if name in params:
            await prefs.set(name, params[name])                  # FileTypes.pm:37-38
            written.append(name)
        elif "pref_" + name in params:                           # auch mit Präfix
            await prefs.set(name, params["pref_" + name])
            written.append(name)

    # ``:40-67`` — ``disabledformats`` aus den Profil-``select``s.
    disabled: list[str] = []
    for profile, _source, _target, _binaries, _enabled in filetypes_profiles():
        if profile in params:
            if params[profile] == "DISABLED":                    # :61-64
                disabled.append(profile)
            # ``checkBin($profile,'IgnorePrefs')`` → fehlt das Programm, bleibt
            # das Profil gesperrt (:51-59); unser ``enabled`` deckt das ab.
        elif profile in _as_list(prefs.get("disabledformats")):
            disabled.append(profile)
    await prefs.set("disabledformats", ",".join(sorted(set(disabled))))   # :67
    written.append("disabledformats")
    return written


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

    # ``Basic.pm:39-62`` — der Rescan-Knopf bzw. ein ``pref_rescan_mediadir<i>``
    # stösst einen Scan an.  Perl bildet ``getScanCommand($pref_rescantype)``
    # (``:41``) und schickt ihn an die Scan-Queue (``:60``); unser Weg ist
    # :func:`lyrion.control.rescan.request_scan`, dieselbe Kette wie CLI/JSON-RPC.
    scan_type = params.get("pref_rescantype") or "1rescan"
    single_dir = any(key.startswith("pref_rescan_mediadir") and params[key]
                     for key in params)
    if params.get("pref_rescan") or single_dir:
        try:
            from lyrion.control import rescan as scan

            target = None
            if single_dir:
                # ``Basic.pm:98-100`` — ``$singleDirScan`` der einen Ordner.
                for key in sorted(params):
                    if key.startswith("pref_rescan_mediadir") and params[key]:
                        index_attr = key[len("pref_rescan_mediadir"):]
                        target = params.get(f"pref_{_MEDIADIR_PREF}{index_attr}") or None
                        break
            if target:
                # ``Basic.pm:125`` — ``rescan full <ordner>``
                scan.request_scan("full", target)
            elif scan_type == "2wipedb":                        # Import.pm:94-96
                scan.request_scan(scan.DEFAULT_MODE)            # wipecache-Pfad
            elif scan_type == "3playlist":                      # Import.pm:97-100
                scan.request_scan("playlists")
            else:                                               # Import.pm:89-92
                scan.request_scan(scan.DEFAULT_MODE)
            written.append("pref_rescan")
        except Exception as exc:  # noqa: BLE001 — Speichern ist wichtiger
            logger.warning("settings: Rescan nicht angestossen (%s)", exc)
    return written


# ── HTML ────────────────────────────────────────────────────────────────────

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render_input(f: Field, value: str,
                  options: tuple[tuple[str, str, str], ...] = ()) -> str:
    fid = _field_id(f.pref)
    name = f.pref if f.no_pref_prefix else ("pref_" + f.pref)
    if f.kind == "select" and options:
        opts = []
        for val, key, default in options:
            sel = " selected" if str(value) == val else ""
            label = _string(key, default)
            opts.append(f'<option value="{_esc(val)}"{sel}>{_esc(label)}</option>')
        return (f'<select class="stdedit" name="{name}" id="{_esc(fid)}">'
                + "".join(opts) + "</select>")
    if f.kind == "password":
        # ``security.html:18,22`` ``<input type="password" …>``.
        return (f'<input type="password" class="stdedit" name="{name}" '
                f'id="{_esc(fid)}" value="{_esc(value)}" size="40">')
    return (f'<input type="text" class="stdedit" name="{name}" '
            f'id="{_esc(fid)}" value="{_esc(value)}" size="40">')


def _render_rescan_section() -> str:
    """``basic.html:84-94`` + ``Basic.pm:39-62,150-151`` — Rescan-Auswahl + Knopf.

    Perl zeigt ein ``select`` ``pref_rescantype`` mit den Scantypen
    (``Basic.pm:150-151`` setzt ``scanTypes`` aus ``Slim::Music::Import::
    getScanTypes``; die Tabelle ist ``Import.pm:88-101``) und den Knopf
    ``pref_rescan``.  Neben jedem Medienordner steht zudem ein eigener
    ``pref_rescan_mediadir<i>``-Knopf (``basic.html:69``, ``Basic.pm:98-100``).
    Die Ausführung läuft über :mod:`lyrion.control.rescan` — dieselbe Kette,
    die ``rescan``/``wipecache`` auch im CLI/JSON-RPC bedient (``Basic.pm:60``
    ``Slim::Control::Request::executeRequest(undef, $rescanType)``).
    """
    # ``Import.pm:88-101`` — die drei Kern-Typen; ``4onlinelibrary`` fügt das
    # OnlineLibrary-Plugin bei (auf dem Live-Server sichtbar, hier nicht
    # geladen) und fehlt daher.
    scan_types = (
        ("1rescan", "SETUP_STANDARDRESCAN", "Look for new and changed media files"),
        ("2wipedb", "SETUP_WIPEDB", "Clear library and rescan everything"),
        ("3playlist", "SETUP_PLAYLISTRESCAN", "Only rescan playlists"),
    )
    options = "".join(
        f'<option value="{_esc(key)}">{_esc(_string(label_key, default))}</option>'
        for key, label_key, default in scan_types)
    return (
        '<div class="settingSection">'
        '<div class="prefHead">'
        + _esc(_string("SETUP_RESCAN", "Rescan Media Library")) + "</div>"
        + '<div class="prefDesc">'
        + _esc(_string("SETUP_RESCAN_DESC", "Click Rescan to have Lyrion Music Server "
                       "scan through your media library and add new media or update "
                       "files that have changed.")) + "</div>"
        + f'<select class="stdedit" name="pref_rescantype" id="rescantype">{options}</select>'
        + f'<input name="pref_rescan" type="submit" class="stdclick" '
          f'value="{_esc(_string("SETUP_RESCAN_BUTTON", "Rescan"))}"><br/><br/>'
        + f'<a id="progressLink" href="/settings/server/status.html">'
          + _esc(_string("SETUP_VIEW_NOT_SCANNING", "View Previous Scan Details"))
        + "</a></div>")


def _render_mediadirs() -> str:
    """Medienordner-Zeilen wie ``basic.html:52-76`` (Parameter ``pref_mediadirs<i>``)."""
    prefs = get_prefs()
    paths = _as_list(prefs.get(_MEDIADIR_PREF))
    ignored = set(_as_list(prefs.get(_MEDIADIR_IGNORE_PREF)))
    rows = []
    for i, path in enumerate(paths):
        checked = "" if path in ignored else " checked"   # basic.html:66 (audio ⇒ aus)
        # ``basic.html:69`` — je Ordner ein eigener Rescan-Knopf
        # (``pref_rescan_mediadir<i>``); nur bei gesetztem Pfad.
        button = (f'<input name="pref_rescan_mediadir{i}" type="submit" '
                  f'class="stdclick" value="{_esc(_string("SETUP_RESCAN_BUTTON", "Rescan"))}">'
                  if path else "")
        rows.append(
            f'<tr><td><input type="text" class="stdedit" name="pref_{_MEDIADIR_PREF}{i}" '
            f'id="{_MEDIADIR_PREF}{i}" value="{_esc(path)}" size="40"></td>'
            f'<td><input type="checkbox" name="pref_{_MEDIADIR_IGNORE_PREF}{i}" '
            f'id="{_MEDIADIR_IGNORE_PREF}{i}" value="1"{checked}></td>'
            f"<td>{button}</td></tr>")
    # Leerzeile für einen weiteren Ordner (Basic.pm:146-148)
    i = len(paths)
    rows.append(
        f'<tr><td><input type="text" class="stdedit" name="pref_{_MEDIADIR_PREF}{i}" '
        f'id="{_MEDIADIR_PREF}{i}" value="" size="40"></td>'
        f'<td><input type="checkbox" name="pref_{_MEDIADIR_IGNORE_PREF}{i}" '
        f'id="{_MEDIADIR_IGNORE_PREF}{i}" value="1"></td><td></td></tr>')
    return ('<table border="0" cellspacing="0"><tr><th>'
            + _esc(get_string("FOLDER", default="Folder"))
            + '</th><th>' + _esc(get_string("MUSIC", default="Music"))
            + "</th><th></th></tr>" + "".join(rows) + "</table>")


def _render_filetypes_table() -> str:
    """``filetypes.html:19-49`` — die Konvertierungsprofile als Tabelle."""
    rows = filetypes_profiles()
    parts = ['<div class="settingSection"><table border="0" cellspacing="0">',
             "<tr><th>" + _esc(_string("FILE_FORMAT", "File Format")) + "</th>"
             "<th>" + _esc(_string("STREAM_FORMAT", "Stream Format")) + "&nbsp;&nbsp;</th>"
             "<th>" + _esc(_string("SETUP_INPUTTYPE", "From")) + "</th>"
             "<th>" + _esc(_string("DECODER", "Decoder")) + "</th></tr>"]
    for profile, source, target, binaries, enabled in rows:
        disabled_attr = ' disabled="disabled"' if len(binaries) <= 1 else ""
        opts = []
        for i, name in enumerate(binaries):
            sel = " selected" if i == enabled else ""
            label = _string(name, name)      # ``value | getstring`` (filetypes.html:40)
            opts.append(f'<option value="{_esc(name)}"{sel}>{_esc(label)}</option>')
        select = (f'<select class="stdedit" name="{_esc(profile)}" id="{_esc(profile)}"'
                  f'{disabled_attr}>' + "".join(opts) + "</select>")
        parts.append(f'<tr><td class="firstColumn">{_esc(_string(source, source))}</td>'
                     f'<td><label for="{_esc(profile)}" class="stdlabel">'
                     f'{_esc(_string(target, target))}</label></td>'
                     f"<td>({_esc('')})</td><td>{select}</td></tr>")
    parts.append("</table></div>")
    return "".join(parts)


def _debug_log_files() -> list[tuple[str, str, str]]:
    """``Debugging.pm:76`` ``getLogFiles()`` — ``(name, pfad, anzeigename)``.

    Perl liefert die Liste ``[{SERVER => serverLogFile}, {SCANNER => …},
    {PERFMON => …}]`` (``Log.pm:645-651``); die Scanner-Datei nur, wenn eine
    Bibliothek existiert (``Slim::Schema::hasLibrary()``), die Perfmon-Datei nur
    bei ``main::PERFMON``.  Pfade aus ``utils/log.py`` (``init_logging``:
    ``<log_dir>/lyrion.log``) bzw. ``config``.
    """
    entries: list[tuple[str, str, str]] = []
    from pathlib import Path

    log_dir = Path.home() / ".lyrion" / "Logs"
    try:
        from lyrion.utils import log as log_module

        for path, _handler in getattr(log_module, "_file_handlers", {}).items():
            name = Path(path).name
            key = name.split(".")[0].upper()          # lyrion.log → LYRION
            label = _string(f"SETUP_DEBUG_{key}_LOG", name)
            entries.append((name, str(path), label))
    except Exception as exc:  # noqa: BLE001 — die Seite darf daran nicht scheitern
        logger.debug("settings: Logdateien nicht lesbar (%s)", exc)
    if not entries:                                   # noch kein Log initialisiert
        entries.append(("lyrion.log", str(log_dir / "lyrion.log"),
                        _string("SETUP_DEBUG_SERVER_LOG", "Lyrion Music Server Log File")))
    return entries


def _render_debugging_page(warning: Optional[str]) -> bytes:
    """``settings/server/debugging.html`` (``Debugging.pm:54-78``).

    Aufbau wie die Vorlage: Protokollsatz-Auswahl, je Logdatei ein Block mit
    den Links (``.log``, ``?lines=100|500|1000``, ``?full=1``, ``?zip=1``),
    die ``persist``-Checkbox und darunter je Kategorie ein ``select`` mit den
    sechs Stufen (``validLevels``, ``Log.pm:64``) und dem Kategorienamen.
    """
    page = _DEBUGGING
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(_string(page.title_key, page.title_default))}</title>",
        "</head><body>",
        f'<div id="statusarea" class="statusarea">{warning or ""}</div>',
        f'<form name="settingsForm" id="settingsForm" method="post" action="{_esc(page.route)}">',
        '<input type="hidden" name="useAJAX" value="0">',
        f'<input type="hidden" name="page" value="{_esc(page.page_name)}">',
        '<div id="settingsRegion">',
        f"<h2>{_esc(_string('DEBUGGING_SETTINGS', 'Logging'))}</h2>",
    ]

    # Protokollsatz (``debugging.html:4-13``).
    group_opts = [f'<option value="">{_esc(_string("DEBUG_SELECT_SET", "Please select log set..."))}</option>',
                  f'<option value="DEFAULT">{_esc(_string("DEBUG_DEFAULT", "Reset logging preferences"))}</option>']
    for name, key, default in DEBUG_LOG_GROUPS:
        group_opts.append(f'<option value="{_esc(name)}">{_esc(_string(key, default))}</option>')
    parts.append('<div class="settingGroup">'
                 f'<label for="logging_group">{_esc(_string("DEBUGGING_SETTINGS", "Logging"))}</label>'
                 f'<select class="stdedit" name="logging_group" id="logging_group">'
                 + "".join(group_opts) + "</select></div>")

    # Logdateien mit den Links der Vorlage (``debugging.html:17-31``).
    for name, path, label in _debug_log_files():
        base = f"/{name}"
        parts.append(
            f'<div class="settingGroup"><label>{_esc(label)}</label>'
            + _esc(path)
            + f' <a href="{_esc(base)}" target="log">{_esc(name)}</a>'
            + f' <a href="{_esc(base)}?lines=100" target="log">100</a>, '
            + f'<a href="{_esc(base)}?lines=500" target="log">500</a>, '
            + f'<a href="{_esc(base)}?lines=1000" target="log">1000</a> '
            + _esc(_string("LINES", "lines"))
            + f', <a href="{_esc(base)}?full=1">{_esc(_string("EVERYTHING", "Everything").lower())}</a>'
            + f', <a href="{_esc(base)}?zip=1">{_esc(_string("SETUP_LOG_ZIPPED", "ZIP archive"))}</a>'
            + "</div>")

    # ``persist`` (``debugging.html:36-41``).
    persist = str(get_prefs().get("log.persist") or "0").strip() not in ("", "0")
    checked = ' checked="1"' if persist else ""
    parts.append(
        '<div class="settingGroup">'
        f'<input name="persist" id="persist" type="checkbox"{checked}> '
        f'{_esc(_string("PERSIST_DEBUG_SETTINGS", "Save logging settings for use at next application restart"))}'
        "</div>")

    # Kategorien (``debugging.html:43-59``).
    parts.append('<div class="settingSection">')
    for category, _default in debug_categories():
        current = debug_category_level(category)
        opts = []
        for level in DEBUG_LEVELS:
            sel = " selected" if level == current else ""
            key = f"SETUP_DEBUG_LEVEL_{level}"
            opts.append(f'<option value="{_esc(level)}"{sel}>'
                        f'{_esc(_string(key, level.title()))}</option>')
        label = _string(debug_description_token(category), category)
        parts.append(
            f"<p><select class=\"stdedit\" name=\"{_esc(category)}\" id=\"{_esc(category)}\">"
            + "".join(opts) + "</select>"
            + f' <label for="{_esc(category)}" class="stdlabel">({_esc(category)}) - '
            + f"{_esc(label)}</label></p>")
    parts.append("</div>")

    parts.append(
        '<div id="prefsSubmit">'
        f'<input name="saveSettings" id="saveSettings" type="submit" class="stdclick" '
        f'value="{_esc(_string("SAVE_SETTINGS", "Save Settings"))}">'
        '<input type="hidden" name="saveSettings" value="1"></div>')
    parts.append("</form></body></html>")
    return "\n".join(parts).encode("utf-8")


def _render_software_panel() -> str:
    """``software.html:35-39`` + ``Update.pm`` — Update-Status und Prüf-Knopf.

    Perl zeigt:
    * die laufende Version über ``$::VERSION``/``$::REVISION`` (``main::``);
    * die Warnung ``$::newVersion`` (``Software.pm:40-42``), die der
      Versions-Check setzt (``Update.pm:140``);
    * den Knopf ``checkForUpdateNow``, wenn NICHT aus dem Quellbaum gestartet
      (``software.html:35-38``).
    """
    from lyrion import __version__

    parts = ['<div class="settingSection">',
             '<div class="prefHead">'
             + _esc(_string("SETUP_CHECKVERSION", "Software Updates")) + "</div>",
             "<ul>",
             f"<li>Version: <strong>{_esc(__version__)}</strong></li>",
             "</ul>"]
    warning = get_prefs().get("serverUpdateAvailable") or ""
    if warning:
        parts.append(f'<div class="prefDesc" id="updateStatus">{_esc(str(warning))}</div>')
    if not _running_from_source():
        parts.append(
            '<div><input type="submit" name="checkForUpdateNow" id="checkForUpdateNow" '
            f'class="stdclick" value="{_esc(_string("SETUP_CHECK_NOW", "Check for updates now"))}">'
            ' <span id="updateMsg"></span></div>')
    parts.append("</div>")
    return "".join(parts)


async def _handle_software_check(send, params: dict[str, str]) -> None:
    """``Software.pm:44-60`` — ``checkForUpdateNow`` IST gesetzt: Perl prüft.

    Perl ruft ``Slim::Utils::Update::checkVersion(cb)`` und antwortet erst im
    Callback mit der Seite; die Meldung ist

    * ``SERVER_UPDATE_AVAILABLE_SHORT`` mit Link auf ``updateinfo.html``, wenn
      die Antwort-URL des Repos eine Versionsnummer enthält (``:49-51``),
    * sonst ``CONTROLPANEL_NO_UPDATE_AVAILABLE`` (``:52-54``),
    * bei ``curl``-Fehler ``CHECKVERSION_PROBLEM`` (``Update.pm:145``).

    Ausgeführt wird hier NUR der Vergleich (``servers.json`` lesen und
    ``compareVersions`` gegen die laufende Version, ``Update.pm:104-111``) —
    **kein Selbst-Update**: ``getUpdate`` (``Update.pm:165-...)`` lädt sonst
    herunter und ``$os->initUpdate()`` (``Update.pm:54``) startet den
    Installer.  Wir laden nichts herunter und starten nichts.
    """
    from lyrion.version import __version__

    prefs = get_prefs()
    # ``Update.pm:86`` + ``:87`` — die Prüfzeit wird auch bei uns festgehalten,
    # damit die Drossel (``checkVersionInterval``) greift.
    import time

    await prefs.set("checkVersionLastTime", str(time.time()))

    status: str
    error = ""
    try:
        payload = await _fetch_repository_json(UPDATE_REPOSITORY_URL)
        status = _evaluate_update_payload(payload, __version__)
    except Exception as exc:  # noqa: BLE001 — die Seite darf daran nicht scheitern
        logger.warning("settings: Versionsprüfung fehlgeschlagen (%s)", exc)
        error = _string("CHECKVERSION_PROBLEM",
                        "There was a problem while checking for updates to Lyrion Music Server. (Error code %s)") % "e"
        status = ""

    if not error and status:
        # ``Update.pm:138-141`` — eine Download-URL merkt sich Perl in
        # ``serverUpdateAvailable``; wir zeigen sie als HTML-Warnung.
        await prefs.set("serverUpdateAvailable", status)
        warning = status
    elif error:
        warning = error
    else:
        warning = _string("CONTROLPANEL_NO_UPDATE_AVAILABLE",
                          "There's no updated Lyrion Music Server version available.")
    logger.info("settings: Software-Prüfung → %s", status or warning)
    body = _render_page(_SOFTWARE, params, None, warning)
    await _send(send, 200, "text/html", body)


async def _fetch_repository_json(url: str) -> dict:
    """``servers.json`` lesen (``Update.pm:79-84``) — nur GET, read-only."""
    import urllib.request

    def _get() -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "lyrion"})
        with urllib.request.urlopen(req, timeout=15) as response:   # noqa: S310
            if not 200 <= int(response.status) < 300:               # Update.pm:98
                raise OSError(f"HTTP {response.status}")
            return json.loads(response.read().decode("utf-8", "replace"))

    return await asyncio.to_thread(_get)


def _evaluate_update_payload(payload: dict, running_version: str) -> str:
    """``Update.pm:102-141`` — gibt die Warnung (HTML) oder ``""`` zurück.

    Perl wählt ``$versions->{$::VERSION} || $versions->{latest}`` (``:105``),
    darin den Eintrag seines ``installerOS`` (``:104,109``) und vergleicht
    ``version``/``revision`` mit der laufenden Version (``:111``).  Ist die
    Repo-Version höher, entsteht ``SERVER_UPDATE_AVAILABLE`` mit Version und
    Download-Link (``:117``).  Ohne ``canAutoUpdate``/``autoDownloadUpdate``
    gibt es keinen Download (``:112-115``) — Perl zeigt nur den Link.
    """
    if not isinstance(payload, dict):
        return ""
    versions = payload.get(running_version) or payload.get("latest") or {}
    if not isinstance(versions, dict):
        return ""
    # ``$os->installerOS() || 'default'`` (``Update.pm:104``); eine
    # Plattformwahl steht uns nicht zur Verfügung, deshalb wie Perl der
    # ``default``-Zweig (``Slim/Utils/OS.pm`` ``installerOS`` der Basisklasse).
    update = versions.get("default") or {}
    if not isinstance(update, dict):
        return ""
    version = str(update.get("version") or "")
    if not version:
        return ""
    if not _version_greater(version, running_version):          # Update.pm:111
        return ""
    url = str(update.get("url") or "")                          # Update.pm:117
    return _string("SERVER_UPDATE_AVAILABLE",
                   'A new version of Lyrion Music Server is available (%s). '
                   '<a href="%s" target="update">Click here to download</a>.') % (
                       version, url)


def _server_hostname() -> str:
    """Hostname für Perls ``Slim::Utils::Network::serverURL()`` (``Network.pm:51``).

    Perl baut die URL aus der ersten nicht-lokalen Adresse
    (``Slim/Utils/Network.pm``); wir nehmen denselben Weg wie unser
    Netzwerkmodul und fallen auf ``localhost`` zurück.
    """
    try:
        from lyrion.utils import osdetect

        value = osdetect.hostname()          # ``utils/osdetect.py:211``
        if value:
            return str(value)
    except Exception as exc:  # noqa: BLE001 — die Warnung darf daran nicht scheitern
        logger.debug("settings: Hostname nicht lesbar (%s)", exc)
    return "localhost"


def _version_greater(candidate: str, running: str) -> bool:
    """Versionsvergleich wie ``Slim::Utils::Versions::compareVersions``.

    Punkt-getrennte Zahlengruppen, von links nach rechts; die erste
    unterschiedliche entscheidet (:0 der Perl-Routine liefert den Vergleich).
    """
    def _parts(text: str) -> list[int]:
        out: list[int] = []
        for chunk in str(text).split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            out.append(int(digits) if digits else 0)
        return out

    left, right = _parts(candidate), _parts(running)
    length = max(len(left), len(right))
    left += [0] * (length - len(left))
    right += [0] * (length - len(right))
    return left > right


def _render_filetypes_page(page: SettingsPage, params: dict[str, str],
                           warning: Optional[str]) -> bytes:
    """``settings/server/filetypes.html`` — Felder + Profiltabelle.

    Perl rendert dieselbe Vorlage für GET und POST (``FileTypes.pm:133``
    ``SUPER::handler``); der Seitenrahmen kommt aus :func:`_render_page`, die
    Profiltabelle aus :func:`_render_filetypes_table`.
    """
    body = _render_page(page, params, None, warning)
    table = _render_filetypes_table().encode("utf-8")
    # ``</div>`` vor ``#prefsSubmit`` schliesst ``#settingsRegion``; die
    # Tabelle steht in der Vorlage VOR dem Speichern-Knopf (``:19-49``).
    marker = b'<div id="prefsSubmit">'
    return body.replace(marker, table + marker, 1)


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


def _render_art_wanted_panel() -> str:
    """Zustand der wanted-Liste + Knopf für einen erneuten Durchlauf.

    Die Zahlen kommen beim Rendern **serverseitig** aus der Liste; danach hält
    ein kleines Skript sie per ``fetch`` aktuell (``ART_WANTED_STATUS_URL``) und
    stösst den Durchlauf über ``ART_WANTED_RUN_URL`` an.  Die Seite blockiert
    dabei nie: sie ist fertig gerendert, bevor das Skript läuft, und der Lauf
    selbst passiert im Server-Hintergrund (``media/art_online_wanted.py``).
    """
    try:
        from lyrion.media import art_online_wanted

        status = art_online_wanted.wanted_status()
        error = ""
    except Exception as exc:  # noqa: BLE001 - die Seite darf daran nicht scheitern
        logger.warning("settings: wanted-Status nicht lesbar (%s)", exc)
        status = {}
        error = str(exc)

    def _row(label_key: str, label: str, value: str, span_id: str) -> str:
        return (f"<tr><td>{_esc(get_string(label_key, default=label))}</td>"
                f'<td><span id="{_esc(span_id)}">{_esc(value)}</span></td></tr>')

    rows = [
        _row("SETUP_ART_WANTED_OPEN", "Open / offen",
             str(status.get("open", 0)), "artWantedOpen"),
        _row("SETUP_ART_WANTED_HIT", "Found / gefunden",
             str(status.get("hit", 0)), "artWantedHit"),
        _row("SETUP_ART_WANTED_MISS", "No match / kein Treffer",
             str(status.get("miss", 0)), "artWantedMiss"),
        _row("SETUP_ART_WANTED_LAST", "Last run / letzter Lauf",
             _format_stamp(status.get("last_run")), "artWantedLastRun"),
        _row("SETUP_ART_WANTED_PROGRESS", "Progress / Fortschritt",
             _format_progress(status), "artWantedProgress"),
        _row("SETUP_ART_WANTED_STATE", "State / Zustand",
             _format_state(status), "artWantedState"),
    ]
    button = get_string("SETUP_ART_WANTED_RUN",
                        default="Run wanted list again / Wanted-Liste erneut abarbeiten")
    return (
        '<div class="settingSection" id="artWantedSection">'
        '<div class="prefHead">'
        + _esc(get_string("SETUP_ART_WANTED_TITLE",
                          default="Artwork downloader: missing covers / fehlende Cover"))
        + '</div><table border="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
        + f'<div><input type="button" class="stdclick" id="artWantedRunBtn" '
          f'value="{_esc(button)}" data-status-url="{_esc(ART_WANTED_STATUS_URL)}" '
          f'data-run-url="{_esc(ART_WANTED_RUN_URL)}"> '
          f'<span id="artWantedMsg"></span></div>'
        + (f'<div id="artWantedError">{_esc(error)}</div>' if error else "")
        + "<script type=\"text/javascript\">" + _ART_WANTED_JS + "</script></div>"
    )


def _format_stamp(value: Any) -> str:
    """Zeitstempel als lokale Zeit (``-`` statt leerer Zelle)."""
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return "-"
    if not stamp:
        return "-"
    import datetime

    return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


def _format_progress(status: dict) -> str:
    """„geprüft/gesamt · aktuell <Album>“ — oder der letzte Lauf."""
    if status.get("running"):
        return "%s/%s · %s" % (status.get("checked", 0), status.get("pass_total", 0),
                               status.get("current") or "…")
    last = status.get("last_pass") or {}
    if not last:
        return "-"
    parts = []
    if last.get("hit"):
        parts.append("%s gefunden" % last["hit"])
    if last.get("miss"):
        parts.append("%s ohne Treffer" % last["miss"])
    if "checked" in last:
        parts.append("%s geprüft" % last["checked"])
    if last.get("seconds") is not None:
        parts.append("%ss" % last["seconds"])
    return ", ".join(parts) or "-"


def _format_state(status: dict) -> str:
    """Kurzer Zustandstext (Dienst, Konfigurationswechsel, letzter Grund)."""
    if status.get("running"):
        state = "läuft / running"
    elif not status.get("enabled"):
        state = "Suche aus / search off"
    elif status.get("service_started"):
        state = "wartet / idle"
    else:
        state = "Dienst aus / service off"
    if status.get("settings_changed"):
        state += " · Konfiguration geändert / config changed"
    reason = str(status.get("last_reason") or "")
    if reason:
        state += " · " + reason
    return state


#: Aktualisiert die Anzeige (2-s-Takt) und stösst den Durchlauf an.  Reines
#: ``fetch``/JSON: die Seite bleibt bedienbar, während der Durchlauf im
#: Hintergrund läuft.
_ART_WANTED_JS = """
(function () {
  var btn = document.getElementById('artWantedRunBtn');
  var msg = document.getElementById('artWantedMsg');
  if (!btn) { return; }
  var statusUrl = btn.getAttribute('data-status-url');
  var runUrl = btn.getAttribute('data-run-url');
  var fields = { open: 'artWantedOpen', hit: 'artWantedHit', miss: 'artWantedMiss' };
  function set(id, text) {
    var el = document.getElementById(id);
    if (el) { el.textContent = text; }
  }
  function stamp(value) {
    if (!value) { return '-'; }
    var d = new Date(value * 1000);
    return d.toLocaleString();
  }
  function render(s) {
    for (var key in fields) {
      if (s[key] !== undefined) { set(fields[key], String(s[key])); }
    }
    set('artWantedLastRun', stamp(s.last_run));
    if (s.running) {
      set('artWantedProgress', String(s.checked || 0) + '/' + String(s.pass_total || 0)
          + ' \\u00b7 ' + (s.current || '\\u2026'));
    } else if (s.last_pass && (s.last_pass.checked !== undefined)) {
      var p = s.last_pass;
      set('artWantedProgress', String(p.checked) + ' geprueft, '
          + String(p.hit || 0) + ' gefunden, ' + String(p.miss || 0) + ' ohne Treffer');
    }
    var state = s.running ? 'laeuft / running' : (s.enabled ? 'wartet / idle' : 'Suche aus / search off');
    if (s.settings_changed) { state += ' \\u00b7 Konfiguration geaendert / config changed'; }
    if (s.last_reason) { state += ' \\u00b7 ' + s.last_reason; }
    set('artWantedState', state);
  }
  function poll() {
    fetch(statusUrl, {cache: 'no-store'}).then(function (r) { return r.json(); })
      .then(render).catch(function () {});
  }
  if (btn.addEventListener) {
    btn.addEventListener('click', function () {
      btn.disabled = true;
      if (msg) { msg.textContent = 'angestossen / requested \\u2026'; }
      fetch(runUrl, {method: 'POST', cache: 'no-store'})
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (msg) {
            msg.textContent = s.started
              ? 'neuer Durchlauf gestartet / pass started'
              : 'laeuft bereits / already running';
          }
          render(s);
          btn.disabled = false;
          poll();
        })
        .catch(function (e) {
          if (msg) { msg.textContent = 'Fehler: ' + e; }
          btn.disabled = false;
        });
    });
  }
  poll();
  setInterval(poll, 2000);
})();
"""


async def _render_plugins_page(params: dict[str, str], warning: Optional[str]) -> bytes:
    """``settings/server/plugins.html`` — the plugin list.

    Perl builds this page in ``Plugins.pm:201-357`` (``_addInfo``) on top of the
    ``active``/``inactive`` lists of ``ExtensionsManager::getCurrentPlugins``;
    the port has no repo layer yet, so the list is the manifest register and the
    state pref (``PluginManager.pm:88-114,535-585``).  Per plugin the page shows
    the manifest fields of ``install.xml`` (name, version, creator, description)
    and the current state; a checkbox ``manual:<plugin>`` enables/disables
    (``Plugins.pm:91-94``).
    """
    from lyrion.plugins.manager import PluginManager

    manager = PluginManager()
    if not manager.registry.manifests:
        try:
            manager.discover_plugins(use_cache=False)
        except Exception as exc:  # noqa: BLE001 — the page must still render
            logger.warning("settings: plugin register unreadable (%s)", exc)

    # Perl seeds the state pref while parsing the manifest
    # (``PluginManager.pm:711-728``); a page call before the first ``load`` must
    # therefore still show ``enabled``/``disabled`` instead of an empty state.
    states: dict[str, str] = {}
    for name in manager.manifest_names():
        manifest = manager.registry.get(name)
        if manifest is None:
            continue
        if not manager.plugin_state(name):
            await manager.ensure_plugin_state(name, manifest.default_state)
        states[name] = manager.plugin_state(name)

    action = _PLUGINS.route
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(_string(_PLUGINS.title_key, _PLUGINS.title_default))}</title>",
        "</head><body>",
        f'<div id="statusarea" class="statusarea">{warning or ""}</div>',
        f'<form name="settingsForm" id="settingsForm" method="post" action="{_esc(action)}">',
        '<input type="hidden" name="useAJAX" value="0">',
        f'<input type="hidden" name="page" value="{_esc(_PLUGINS.page_name)}">',
        '<div id="settingsRegion">',
        f"<h2>{_esc(_string('SETUP_PLUGINS', 'Manage Plugins'))}</h2>",
        '<table id="pluginsList">',
        '<thead><tr>'
        f"<th>{_esc(_string('PLUGIN_NAME', 'Plugin'))}</th>"
        f"<th>{_esc(_string('ENABLED', 'Enabled'))}</th>"
        "</tr></thead><tbody>",
    ]

    for name in manager.manifest_names():
        manifest = manager.registry.get(name)
        if manifest is None:
            continue
        state = states.get(name) or manager.plugin_state(name)
        enabled = state in ("enabled", "needs-enable")
        # Perl shows the localized name (``$plugins->{$_}->{name}`` is a string
        # token, ``PluginManager.pm:578``); unknown tokens stay as they are.
        label = _string(manifest.name, manifest.name)
        desc = _string(manifest.description, manifest.description) \
            if manifest.description else ""
        checked = ' checked="checked"' if enabled else ""
        disabled_attr = ""
        if manifest.enforce:                      # PluginManager.pm:551-556
            disabled_attr = ' disabled="disabled"'
        parts.append(
            f'<tr class="plugin" data-plugin="{_esc(name)}" data-state="{_esc(state)}">'
            f'<td><strong>{_esc(label)}</strong>'
            f' <span class="version">{_esc(manifest.version)}</span>'
            f' <span class="creator">{_esc(manifest.creator)}</span>'
            f'<div class="description">{_esc(desc)}</div></td>'
            f'<td><input type="checkbox" name="manual:{_esc(name)}" value="1"'
            f'{checked}{disabled_attr}>'
            f'<span class="state">{_esc(state)}</span></td>'
            "</tr>")
    parts.append("</tbody></table>")
    parts.append("</div>")

    # Restart hint — Perl ``Plugins.pm:339-350``: needsRestart → the restart
    # message with a link, else empty.
    if manager.needs_restart():
        restart_url = _PLUGINS.route + "?restart=1"
        restart_msg = _string(
            "PLUGINS_CHANGED_NEED_RESTART",
            'Changes will take place at the next application restart. '
            '<a href="%s">Please click here to restart the server now.</a>')
        parts.append(f'<div id="restartWarning">{restart_msg % _esc(restart_url)}</div>')

    parts.append(
        '<div id="prefsSubmit">'
        f'<input name="saveSettings" id="saveSettings" type="submit" class="stdclick" '
        f'value="{_esc(_string("SAVE_SETTINGS", "Save Settings"))}">' 
        '<input type="hidden" name="saveSettings" value="1"></div>')
    parts.append("</form></body></html>")
    return "\n".join(parts).encode("utf-8")


async def _save_plugins_page(params: dict[str, str]) -> list[str]:
    """Write the ``manual:<plugin>`` toggles — ``Plugins.pm:91-94``.

    Checked → :meth:`PluginManager.enable_plugin` (→ ``needs-enable``), unchecked
    → :meth:`disable_plugin` (→ ``needs-disable``, refused for ``enforce``).
    """
    from lyrion.plugins.manager import PluginManager

    manager = PluginManager()
    if not manager.registry.manifests:
        manager.discover_plugins(use_cache=False)

    changed: list[str] = []
    for param, value in params.items():
        if not param.startswith("manual:"):
            continue
        name = param[len("manual:"):]
        if name not in manager.registry.manifests:
            continue                      # Perl only knows installed plugins
        if value and value != "0":
            if await manager.enable_plugin(name):
                changed.append(name)
        else:
            if await manager.disable_plugin(name):
                changed.append(name)
    return changed


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

    if page is _DEBUGGING:
        return _render_debugging_page(warning)

    parts.append(f'<form name="settingsForm" id="settingsForm" method="post" action="{_esc(action)}">')
    parts.append('<input type="hidden" name="useAJAX" value="0">')      # header.html:54
    parts.append(f'<input type="hidden" name="page" value="{_esc(page.page_name)}">')
    if page.needs_client and pid:
        parts.append(f'<input type="hidden" name="playerid" value="{_esc(pid)}">')
    parts.append('<div id="settingsRegion">')

    # Versionsanzeige + „auf Update prüfen" (``software.html:35-39``).
    if page is _SOFTWARE:
        parts.append(_render_software_panel())
    # ``autoDownloadUpdate`` steht in Perls ``prefs()`` nur, wenn das OS
    # selbst aktualisieren kann (``Software.pm:26-28``); auf Linux ist das
    # ``0`` (``Slim/Utils/OS.pm:451``) und die Vorlage rendert das Feld nicht
    # (``software.html:24-32``).  Wir rendern es gar nicht erst.
    suppress = set()
    if page is _SOFTWARE and not _can_auto_update():
        suppress.add("autoDownloadUpdate")
    if page is _PERFORMANCE and not _can_auto_rescan():
        # ``Performance.pm:28`` pusht die zwei Felder nur bei ``canAutoRescan``;
        # die Basisklasse antwortet ``0`` (``Slim/Utils/OS.pm:455``), die
        # Vorlage rendert sie dann nicht (``performance.html:102,115``).
        suppress.update(("autorescan", "autorescan_stat_interval"))
    if page is _NETWORK and not _multiple_clients():
        # ``Network.pm:36-38`` — ``syncStartDelay`` nur bei mehr als einem Client;
        # die Vorlage prüft ``prefs.exists('pref_syncStartDelay')`` (:32).
        suppress.add("syncStartDelay")

    for f in page.fields:
        if f.pref in suppress:
            continue
        value = (_client_value(page, f, player) if page.scope == "client"
                 else _server_value(f.pref))
        if f.never_show_value:
            value = ""
        options = _options_for(f, value, extra_options or {})
        parts.append(f'<div class="settingGroup"><label for="{_esc(_field_id(f.pref))}">'
                     f'{_esc(f.label())}</label>{_render_input(f, value, options)}</div>')

    if page is _BASIC_SERVER:
        parts.append(_render_mediadirs())
        parts.append(_render_rescan_section())

    # Zustand der wanted-Liste + Knopf für einen erneuten Durchlauf.  Steht
    # ausserhalb der Feldliste, weil es kein Pref-Formularfeld ist, sondern
    # eine Anzeige mit eigenem JSON-Endpunkt (``ART_WANTED_STATUS_URL``).
    if page is _ART_ONLINE:
        parts.append(_render_art_wanted_panel())

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


async def _handle_art_wanted_json(send, path: str) -> None:
    """``status.json``/``run`` der wanted-Liste beantworten (JSON, kein Blockieren).

    ``run`` stösst nur an: der Durchlauf läuft als Task des Serverprozesses
    weiter (``media/art_online_wanted.py`` ``request_pass``), die Antwort kommt
    sofort zurück — die Seite fragt danach den Fortschritt ab.
    """
    from lyrion.media import art_online_wanted

    payload: dict[str, Any]
    try:
        if path == ART_WANTED_RUN_URL:
            payload = art_online_wanted.trigger_pass(reason="gui")
        else:
            payload = art_online_wanted.wanted_status()
        payload["ok"] = True
    except Exception as exc:  # noqa: BLE001 - die Anzeige darf nie 500 werfen
        logger.warning("settings: wanted-Liste nicht bedienbar (%s)", exc)
        payload = {"ok": False, "error": str(exc)}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    await _send(send, 200, "application/json", body)


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

    # Zustand/Anstoss der wanted-Liste (Knopf und Anzeige der Artwork-Seite).
    # Kein Perl-Vorbild: Perl kennt keinen Hintergrund-Coverdienst; der Zustand
    # ist hier also eine Zutat dieses Ports.  Bewusst VOR der Seiten-Suche, weil
    # beide Pfade unter ``/settings/`` liegen.
    if path in (ART_WANTED_STATUS_URL, ART_WANTED_RUN_URL):
        await _handle_art_wanted_json(send, path)
        return

    page = PAGES.get(path)
    if page is None:
        await _send(send, 404, "text/html", b"<html><body>404 Not Found</body></html>")
        return

    player = _resolve_player(params) if page.needs_client else None

    # ``Software.pm:44-60`` — ``checkForUpdateNow`` ist gesetzt: Perl prüft auf
    # ein Update und antwortet erst mit dessen Ergebnis.  Bewusst VOR dem
    # normalen Speichern: der Knopf ist ein ``submit`` und würde sonst als
    # ``saveSettings``-Lauf mitgeschleppt.  Kein Selbst-Update (siehe
    # ``_handle_software_check``).
    if page is _SOFTWARE and "checkForUpdateNow" in params:
        await _handle_software_check(send, params)
        return

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

    # Dasselbe für die Online-Cover-Felder dieser Seite: Prefs mit Default
    # registrieren, damit das Formular die wirksamen Werte zeigt.
    if any(f.pref in _ART_PREF_DEFAULTS for f in page.fields):
        await register_art_online_prefs()
    if any(f.pref in _ART_WANTED_PREF_DEFAULTS for f in page.fields):
        await register_art_online_prefs()

    # Und für den Bildproxy-Schalter (bewusste Abweichung, Vorbelegung AN).
    if any(f.pref in IMAGEPROXY_PREF_DEFAULTS for f in page.fields):
        await register_imageproxy_prefs()

    # Und für die ``%defaults`` der neuen Server-Seiten (Software, Netzwerk,
    # Sicherheit, Dateitypen, Leistung) — siehe :func:`register_server_page_prefs`.
    if page in (_SOFTWARE, _NETWORK, _SECURITY, _FILETYPES, _PERFORMANCE):
        await register_server_page_prefs()
    if page is _DEBUGGING:
        await register_server_page_prefs()

    # Kennung der Online-Suche **vor** dem Speichern: ändert sich etwas
    # Suchrelevantes (Anbieter, Sprache, Land, API-Key), wird die wanted-Liste
    # danach erneut abgearbeitet (Aufgaben-Anforderung „neuer API-Key“).
    fingerprint_before = ""
    if any(f.pref in _ART_PREF_DEFAULTS for f in page.fields):
        fingerprint_before = load_art_online_settings().fingerprint()

    if method == "POST" and "saveSettings" in params:
        if page is _PLUGINS:
            # ``Plugins.pm:60-100`` — the page writes no ``pref_*`` values; it
            # toggles plugin state (``manual:<plugin>``) and shows that a restart
            # is required (``:341``).
            written = await _save_plugins_page(params)
            warning = _string(
                "SETUP_EXTENSIONS_RESTART_MSG",
                "Please restart Lyrion Music Server for the changes to take effect.")
            logger.info("settings: %s toggled %s", page.route, written)
            if str(params.get("useAJAX", "0")) == "1":
                await _send(send, 200, "text/plain", _render_ajax(warning, written))
                return
            await _send(send, 200, "text/html",
                        await _render_plugins_page(params, warning))
            return
        if page.needs_client and player is None:
            # Perl: Player/Display.pm:101-105 — ohne Client keine Einstellungen.
            warning = get_string("SETUP_NO_PREFS",
                                 default="There are no settings for this player on this page")
        else:
            invalid = []
            if page is _BASIC_SERVER:
                written = await _save_server_basic(params, page, player, invalid)
            elif page is _SECURITY:
                written = await _save_server_security(params, page, player, invalid)
            elif page is _NETWORK:
                written = await _save_server_network(params, page, player, invalid)
            elif page is _DEBUGGING:
                written = await _save_debugging(params)
            elif page is _FILETYPES:
                written = await _save_filetypes(params, page, player, invalid)
            else:
                written = await _save_simple_prefs(page, params, player, invalid)
            # ``!warning:``-Marker der Zusatz-Handler abtrennen (Perl hängt sie
            # roh an ``$paramRef->{warning}``, ``Security.pm:36,48``).
            extra_warning = "".join(
                part[len("!warning:"):] for part in written
                if isinstance(part, str) and part.startswith("!warning:"))
            written = [part for part in written
                       if not (isinstance(part, str) and part.startswith("!warning:"))]
            httpport_ok = [part[len("!httpport_ok:"):] for part in written
                           if isinstance(part, str) and part.startswith("!httpport_ok:")]
            written = [part for part in written
                       if not (isinstance(part, str) and part.startswith("!httpport_ok:"))]
            if extra_warning:
                warning = (warning or "") + extra_warning
            if httpport_ok:
                # ``Network.pm:53-60`` — SETUP_HTTPPORT_OK plus die Server-URL.
                port = httpport_ok[0]
                url = f"http://{_server_hostname()}:{_esc(port)}/"
                warning = (warning or "") + (
                    _string("SETUP_HTTPPORT_OK", "Now using port:")
                    + f'<blockquote><a target="_top" href="{url}">{url}</a></blockquote><br>')
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
            if fingerprint_before:
                # Nur eine suchrelevante Änderung löst den neuen Durchlauf aus
                # (Anbieter/Sprache/Land/Key); reines Speichern tut nichts.
                try:
                    from lyrion.media import art_online_wanted

                    art_online_wanted.note_settings_change(fingerprint_before)
                except Exception as exc:  # noqa: BLE001 - Speichern ist wichtiger
                    logger.warning("settings: wanted-Liste nicht neu angestossen (%s)",
                                   exc)
    elif page.needs_client and player is None:
        warning = get_string("SETUP_NO_PREFS",
                             default="There are no settings for this player on this page")

    if str(params.get("useAJAX", "0")) == "1":
        await _send(send, 200, "text/plain", _render_ajax(warning, written, invalid))
        return

    if page is _PLUGINS:
        # GET, or a POST that did not save (``Plugins.pm:48-145`` renders the
        # list in both cases; the restart hint comes from ``needsRestart``).
        await _send(send, 200, "text/html",
                    await _render_plugins_page(params, warning))
        return

    if page is _FILETYPES:
        # ``FileTypes.pm:133`` rendert dieselbe Vorlage für GET und POST;
        # zusätzlich die Profiltabelle (``filetypes.html:19-49``).
        await _send(send, 200, "text/html",
                    _render_filetypes_page(page, params, warning))
        return

    await _send(send, 200, "text/html",
                _render_page(page, params, player, warning, extra_options))
