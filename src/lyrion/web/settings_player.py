"""Player-Settings-Seiten, die ``web/settings.py`` noch nicht fÃ¼hrt (Perl ``Slim/Web/Settings/Player/*``).

Diese Datei ist die **ErgÃ¤nzung** zu ``lyrion/web/settings.py``: sie benutzt
dessen Bausteine (``Field``, ``SettingsPage``, ``PAGES``-Form, Token-Helfer)
unverÃ¤ndert weiter und bringt die Seiten mit, die in der Registry von
``settings.py`` fehlen.  ``settings.py`` selbst wird hier NICHT verÃ¤ndert â€” der
exakte Registry-Patch steht am Ende dieses Docstrings.

Perl-Referenz (``/tmp/lms-ref``, read-only)
===========================================

* ``/settings/player/menu.html``
  ``Slim/Web/Settings/Player/Menu.pm``: ``name`` :16-18 (``MENU_SETTINGS``),
  ``page`` :20-22, ``needsClient`` :24-26, ``validFor`` :28-33
  (``!$client->display->isa('Slim::Display::NoDisplay')``), ``handler`` :35-109.
  Die Pref ist ``menuItem`` **je Client** (``$prefs->client($client)->get('menuItem')``
  :41/:93; ``preferences('server')`` :40).  Aktionen/Parameter:
  ``Action<i>`` = ``Up``|``Down``|``Remove`` (:45-60), ``removeItems`` +
  ``menuItemRemove<i>`` (:63-66), ``addItems`` + ``nonMenuItemAdd<i>`` (:69-77)
  bzw. ``pluginItemAdd<i>`` (:79-85); ``nonMenuItems`` ist die ZÃ¤hlgrenze
  (:33 Vorlage, :71).  Leere Liste â†’ ``NOW_PLAYING`` (:88-91).
  Danach ``Slim::Buttons::Home::updateMenu($client)`` (:95) und die
  Anzeige-Listen ``menuItems``/``menuItemNames``/``nonMenuItems`` (:97-99).
  Titel/Text: ``SETUP_GROUP_MENUITEMS`` (Template
  ``HTML/EN/settings/player/menu.html:4``), Buttons ``MOVEUP``/``MOVEDOWN``/
  ``DELETE`` (:9-11), ``ADD`` (:32).
  Die Liste der verfÃ¼gbaren MenÃ¼punkte ist Perls ``%home`` (``Home.pm:98``,
  ``NOW_PLAYING``) **plus** jeder von Modulen registrierte Punkt
  (``Slim::Buttons::Home::addMenuOption``, z. B. ``Slim/Buttons/Alarm.pm:309``
  ``ALARM``, ``Slim/Buttons/Settings.pm:797-798`` ``SETTINGS``/``MUSICSOURCE``,
  ``Slim/Buttons/GlobalSearch.pm:40`` ``GLOBAL_SEARCH``, ``Slim/Buttons/Home.pm:825``
  Plugin-Punkte) minus der bereits belegten (``unusedMenuOptions``,
  ``Home.pm:684-710``).
* ``/settings/player/remote.html``
  ``Slim/Web/Settings/Player/Remote.pm``: ``name`` :18-20 (``REMOTE_SETTINGS``),
  ``page`` :22-24, ``needsClient`` :26-28, ``validFor`` :30-35 (``$client->hasIR``),
  ``prefs`` :37-45 (``irmap`` nur wenn mehr als eine IR-Map-Datei existiert,
  :42), ``handler`` :47-85.  Geschrieben wird ``disabledirsets`` aus den
  **abgewÃ¤hlten** IR-Sets (:55-70; die Vorlage liefert je Set zwei
  ``pref_irsetlist<i>``-Felder, deshalb der ``!ref``-Test :62-65), die
  Vorlage punktet die Auswahl in ``pref_disabledirsets`` (:73).  Liste:
  ``irmapOptions``/``irsetlist`` (:75-76).  Titel/Text:
  ``SETUP_GROUP_IRSETS`` (``HTML/EN/settings/player/remote.html:3``),
  ``SETUP_IRMAP`` (:13).
* ``/settings/player/synchronization.html``
  ``Slim/Web/Settings/Player/Synchronization.pm``: ``name`` :18-20
  (``SETUP_SYNCHRONIZE``), ``page`` :22-24, ``needsClient`` :26-28,
  ``validFor`` :30-35 (``$client->isSynced() || scalar(Slim::Player::Sync::canSyncWith($client)) > 0``),
  ``prefs`` :37-42 (``syncVolume syncPower startDelay maintainSync playDelay
  packetLatency minSyncAdjust``), ``handler`` :44-73.  ``synchronize`` ist
  **keine** Pref: es ist die Ziel-Player-ID, die ``$otherClient->execute(['sync',
  $client->id])`` auslÃ¶st (:50-58); die Vorlage setzt ``-1`` = "keine
  Synchronisation" (:66, ``SETUP_NO_SYNCHRONIZATION`` :92).  ``syncGroups``
  (:77-95) sind die synchronisierbaren Player + der eigene (wenn Master) +
  ``-1``.  Titel/Text: ``SETUP_SYNCHRONIZE`` + ``SETUP_SYNCVOLUME`` (ON/OFF),
  ``SETUP_SYNCPOWER`` (ON/OFF), ``SETUP_MAINTAINSYNC`` (ON/OFF),
  ``SETUP_STARTDELAY``, ``SETUP_PLAYDELAY``, ``SETUP_MINSYNCADJUST``,
  ``SETUP_PACKETLATENCY`` (``HTML/EN/settings/player/synchronization.html:5-60``).

NachzÃ¼gler (Abgleich der bestehenden Seiten gegen Perl, ErgÃ¤nzungsvorschlag)
=============================================================================

* ``Player/Audio.pm:30-120`` listet die Prefs bedingt nach GerÃ¤tefÃ¤higkeit:
  unbedingt sind ``powerOnResume lameQuality maxBitrate fadeInDuration`` (:33).
  ``web/settings.py`` fÃ¼hrt genau diese vier.  FÃ¼r einen Squeezebox2-artigen
  Client (unser ``squeezeplay``/``squeezelite``) liefert Perl zusÃ¤tzlich
  ``mp3StreamingMethod`` (:107-109, ``isa('Slim::Player::Squeezebox2')``) und
  fÃ¼r GerÃ¤te mit Digital-Ausgang ``digitalVolumeControl mp3SilencePrelude``
  (:47-49).  Die Live-Ge genprobe auf 192.168.1.90 (Squeezebox Radio
  00:04:20:2b:88:c8) zeigt genau diese zusÃ¤tzlichen Felder: ``pref_digitalVolumeControl``,
  ``pref_mp3SilencePrelude``, ``pref_mp3StreamingMethod``, ``pref_balance``,
  ``pref_outputChannels``, ``pref_polarityInversion``, ``pref_replayGainMode``,
  ``pref_remoteReplayGain``, ``pref_transitionDuration``,
  ``pref_transitionSampleRestriction``, ``pref_transitionSmart``,
  ``pref_transitionType``.  Sie stehen hier als ``AUDIO_EXTRA_FIELDS`` und sind
  NICHT im Registry-Standard â€” der Patch unten hÃ¤ngt sie an ``_PLAYER_AUDIO``.
* ``Player/Display.pm:37-64``: unbedingt sind die 9 Grafik-unabhÃ¤ngigen Prefs
  (``powerOnBrightness powerOffBrightness idleBrightness scrollMode scrollPause
  scrollPauseDouble scrollRate scrollRateDouble alwaysShowCount``, :45-47).
  ``web/settings.py`` fÃ¼hrt sie.  Danach kommen je nach Display-Klasse
  ``activeFont_curr idleFont_curr scrollPixels scrollPixelsDouble``
  (``Slim::Display::Graphics``, :49-51) ODER ``doublesize offDisplaySize
  largeTextFont`` (:55) bzw. fÃ¼r Boom ``minAutoBrightness sensAutoBrightness``
  (:58-61).  Diese sind gerÃ¤tespezifisch und in ``DISPLAY_EXTRA_FIELDS``.
* ``Player/Alarm.pm:41-53``: ``prefs`` sind ``alarmfadeseconds alarmsEnabled``
  (:44) und â€” nur wenn Digital-LautstÃ¤rkeregelung nicht vorhanden (:46-50) â€”
  ``alarmDefaultVolume`` (:49).  Dazu die Minutenâ†’Sekunden-Felder
  ``pref_alarmSnoozeMinutes``/``pref_alarmTimeoutMinutes`` (:76-77), die
  ``web/settings.py`` bereits fÃ¼hrt.  Alarm-Anlegen/-Entfernen (:60-108,
  ``Slim::Utils::Alarm``) bleibt wie in ``settings.py`` auskommentiert
  (Docstring dort, Abschnitt "UNKLAR"); wir ergÃ¤nzen nur
  ``alarmDefaultVolume`` in der Registry (bedingte Pref, s. Patch).

Registry-Patch fÃ¼r ``src/lyrion/web/settings.py`` (exakt)
==========================================================

Die Zeilennummern sind der Stand beim Bau dieser Datei; die Ankerangabe in
Klammern bleibt gÃ¼ltig, wenn sich die Nummern verschieben.

1. **Import** â€” nach ``from lyrion.utils.strings import get_string``
   (Stand: ``settings.py:143``) einfÃ¼gen:

       from lyrion.web.settings_player import (
           PLAYER_PAGES,
           handle_player_page_request,
           AUDIO_EXTRA_FIELDS,
           DISPLAY_EXTRA_FIELDS,
           ALARM_EXTRA_FIELDS,
       )

2. **Registry** â€” in der ``PAGES``-Zuweisung (Stand: ``settings.py:1073-1089``)
   direkt nach ``_PLAYER_ALARM.route: _PLAYER_ALARM,`` (Stand: ``:1083``):

       # Player-Seiten aus web/settings_player.py (Perl
       # Slim/Web/Settings/Player/{Menu,Remote,Synchronization}.pm).
       **PLAYER_PAGES,

3. **Dispatch** â€” in ``handle_settings_request`` eine Zeile VOR
   ``page = PAGES.get(path)`` (Stand: ``settings.py:2574``):

       # Seiten mit eigenem Handler (menu/remote/synchronization).
       if path in PLAYER_PAGES:
           await handle_player_page_request(scope, receive, send, PLAYER_PAGES[path])
           return

4. **NachzÃ¼gler-Felder an die bestehenden Seiten hÃ¤ngen** (additiv, damit die
   Reihenfolge der Perl-``prefs()``-Liste erhalten bleibt).  Im
   ``_PLAYER_AUDIO``-Block nach ``Field("fadeInDuration", ...)``
   (Stand: ``settings.py:420``):

       *AUDIO_EXTRA_FIELDS,

   im ``_PLAYER_DISPLAY``-Block nach ``Field("alwaysShowCount", ...)``
   (Stand: ``settings.py:470``):

       *DISPLAY_EXTRA_FIELDS,

   im ``_PLAYER_ALARM``-Block nach ``Field("alarmTimeoutMinutes", ...)``
   (Stand: ``settings.py:499``):

       *ALARM_EXTRA_FIELDS,

**Warum eine eigene Datei?**  Der Auftrag verbietet Ã„nderungen an
``settings.py`` (parallel arbeitender Agent).  ``PLAYER_PAGES`` ist ein
``dict[str, SettingsPage]`` mit exakt den SchlÃ¼sseln, die ``PAGES.get(path)``
sucht; die Seiten Ã¼bernimmt :func:`handle_player_page_request` â€” der
Referenz-Pfad in ``settings.py`` bleibt unberÃ¼hrt (die drei Schritte oben sind
die einzige Ã„nderung).

UNKLAR / bewusst nicht nachgebildet
-----------------------------------

* **Player-Auswahl**: Perl holt den Client in ``Slim/Web/HTTP.pm:1164-1169``
  Ã¼ber ``playerid``; ``web/settings.py`` ``_resolve_player`` tut dasselbe und
  wird hier **weiterverwendet** (kein zweiter Weg).  Ohne ``playerid`` bleibt
  ``player`` ``None`` und die Seiten laufen wie Perl in den
  ``SETUP_NO_PREFS``-Zweig (``Menu.pm:102-106``, ``Remote.pm:78-82``).
* **IR-Sets**: Perls ``Slim::Hardware::IR::irfiles/irfileName/mapfiles``
  (``Slim/Hardware/IR.pm:165-243``) lesen echte ``.ir``-Dateien aus dem
  Installationsbaum.  Unser Port hat keinen IR-Dateibaum; ``irsetlist`` ist
  deshalb immer leer (die Seite rendert dann nur den ``SETUP_IRMAP``-Block,
  wenn Ã¼berhaupt Maps gelistet sind) â€” es werden KEINE Sets erfunden.  Dasselbe
  gilt fÃ¼r ``irmapOptions`` (``IR.pm:223``): ohne Map-Dateien bleibt
  ``pref_irmap`` unsichtbar (Perl: ``scalar(keys ...) > 1``, ``Remote.pm:42``).
* **MenÃ¼punkte**: Die verfÃ¼gbaren Punkte der Perl-Installation sind das
  ``%home``-Register (``Home.pm:98``) plus die ``addMenuOption``-Aufrufe der
  Module/Plugins.  Unser Port registriert bislang keine MenÃ¼punkte; die
  ``nonMenuItems``-Liste stammt deshalb aus ``menuItem`` selbst (bekannte
  Punkte, die der Spieler nicht mehr fÃ¼hrt) und bleibt bei leerem
  ``menuItem`` auf Perls ``NOW_PLAYING``-Fallback (:88-91).  Die
  ``pluginItemAdd``-Zeile ist in Perl selbst auskommentiert (:100) und entfÃ¤llt.
* **Alarm-Editor**: ``Alarm.pm:60-108,139-177`` legt Alarme an/entfernt sie
  Ã¼ber ``Slim::Utils::Alarm``; unser Port fÃ¼hrt Alarme in ``lyrion.player``
  (siehe ``tests/test_alarms.py``) â€” die Seite schreibt wie ``settings.py``
  nur die Alarm-Prefs.  Anlegen/LÃ¶schen bleibt dem Alarm-Modul des Ports
  vorbehalten.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Optional
from urllib.parse import quote

from lyrion.player.manager import PlayerManager
from lyrion.web.settings import (  # noqa: F401 — Bausteine werden weiterbenutzt
    Field,
    SettingsPage,
    _as_list,
    _esc,
    _render_input,
    _save_simple_prefs,
    _string,
)

logger = logging.getLogger(__name__)

#: Kennzeichen einer Seite mit eigenem Handler (kein reines ``pref_*``-Formular).
HANDLER_MENU = "menu"
HANDLER_REMOTE = "remote"
HANDLER_SYNC = "synchronization"

#: Perl ``Menu.pm:41``/``:93`` — Pref je Client, die die HauptmenÃ¼-Reihenfolge hÃ¤lt.
MENU_ITEM_PREF = "menuItem"

#: Perl ``Home.pm:88-91`` (Menu.pm) — Fallback, wenn die Liste leer ist.
DEFAULT_MENU_ITEMS: tuple[str, ...] = ("NOW_PLAYING",)

#: Perl ``Remote.pm:70`` — Liste der abgewÃ¤hlten IR-Sets (Client-Pref).
DISABLED_IRSETS_PREF = "disabledirsets"

#: Perl ``Remote.pm:42`` — Pref fÃ¼r die Tastenbelegung (nur bei >1 Map-Datei).
IRMAP_PREF = "irmap"

#: Perl ``Synchronization.pm:39`` — die Pref-Liste der Seite, in Perls Reihenfolge.
SYNC_PREFS: tuple[str, ...] = (
    "syncVolume", "syncPower", "startDelay", "maintainSync",
    "playDelay", "packetLatency", "minSyncAdjust",
)

#: Perl ``Synchronization.pm:66``/``:92`` — "keine Synchronisierung".
_NO_SYNC = "-1"

#: Kleiner Perl-Wortlaut fÃ¼r die Zwei-Wert-Auswahlen (ON/OFF-Paare).
_ON_OFF = {
    "SETUP_SYNCVOLUME": ("SETUP_SYNCVOLUME_ON", "SETUP_SYNCVOLUME_OFF"),
    "SETUP_SYNCPOWER": ("SETUP_SYNCPOWER_ON", "SETUP_SYNCPOWER_OFF"),
    "SETUP_MAINTAINSYNC": ("SETUP_MAINTAINSYNC_ON", "SETUP_MAINTAINSYNC_OFF"),
}


# ── Fähigkeits-Regeln (Perl ``Client.pm``/``Display.pm``) ────────────────────

def _display_class(player) -> str:
    """Perl-Display-Klasse des Clients (``Player/Display.pm:34`` ``$client->display->isa``).

    Unsere Entsprechung ist ``lyrion.player.display.display_class_for``
    (``UUID``-/``HELO``-Tabelle).  ``menu``/``display`` sind nur fÃ¼r Clients mit
    Display gÃ¼ltig (``Menu.pm:32``, ``Display.pm:34``); ``validFor`` ist damit
    dieselbe Bedingung wie Perls ``!$client->display->isa('Slim::Display::NoDisplay')``.
    """
    from lyrion.player.display import display_class_for

    try:
        return display_class_for(getattr(player, "model", ""),
                                 bitmapped=bool(getattr(player, "bitmapped", False)))
    except Exception as exc:  # noqa: BLE001 — Fähigkeitsfrage darf nicht brechen
        logger.debug("settings_player: display class unknown (%s)", exc)
        return "NoDisplay"


def _has_display(player) -> bool:
    """Menu.pm:32 / Display.pm:34 — hat der Client ein echtes Display?"""
    from lyrion.player.display import NODISPLAY

    return _display_class(player) != NODISPLAY


def _has_ir(player) -> bool:
    """Remote.pm:34 — ``$client->hasIR``.

    Unser Port fÃ¼hrt kein IR-Flag in ``PlayerState``; Perls ``hasIR`` ist True
    fÃ¼r Hardware-Clients (Squeezebox/SB2/Boom/Transporter/Receiver).  Wir
    leiten es aus der Modellfamilie ab, die ``display_class_for`` ohnehin kennt
    (``receiver``/``squeezeplay``/``squeezelite`` haben in Perl kein IR,
    ``Slim/Player/*.pm`` ``hasIR``).
    """
    model = (getattr(player, "model", "") or "").lower()
    return model in ("squeezebox", "squeezebox2", "squeezeboxg", "boom",
                     "transporter", "softsqueeze")


def _is_player(player) -> bool:
    """Menu.pm:38 / Remote.pm:50 / Alarm.pm:35 — ``$client->isPlayer``."""
    return bool(getattr(player, "is_player", True))


def _can_sync_with(player) -> list:
    """Synchronization.pm:34,82 — ``Slim::Player::Sync::canSyncWith($client)``.

    ``Slim/Player/Sync.pm:98-121``: jeder andere verbundene Client mit
    ``can_sync``.  Der Client selbst ist nicht dabei.
    """
    manager = PlayerManager()
    out = []
    for other in manager.get_connected_players():
        if other.mac == getattr(player, "mac", None):
            continue
        if not getattr(other, "can_sync", True):
            continue
        out.append(other)
    return out


def _is_synced(player) -> bool:
    """Synchronization.pm:34 — ``$client->isSynced()`` (``Client.pm``)."""
    return getattr(player, "sync_master", None) is not None \
        or bool(getattr(player, "sync_slaves", None))


def _can_offer_sync(player) -> bool:
    """Synchronization.pm:34 — ``isSynced() || canSyncWith() > 0``."""
    return _is_synced(player) or bool(_can_sync_with(player))


# ── Seiten + Felder ──────────────────────────────────────────────────────────

#: Zusatzfelder von ``Player/Audio.pm`` (gerÃ¤teabhÃ¤ngig; Perl :35-117).
#: Reihenfolge wie Perls ``push``-Reihenfolge der Capability-Zweige.
AUDIO_EXTRA_FIELDS: tuple[Field, ...] = (
    Field("powerOffDac", "SETUP_POWEROFFDAC", "Power Off DAC",
          perl_source="Audio.pm:35-37 (hasPowerControl)"),
    Field("disableDac", "SETUP_DISABLEDAC", "Disable DAC",
          perl_source="Audio.pm:39-41 (hasDisableDac)"),
    Field("transitionType", "SETUP_TRANSITIONTYPE", "Transition Type", "select",
          options=(
              ("0", "TRANSITION_NONE", "None"),
              ("1", "TRANSITION_CROSSFADE", "Crossfade"),
              ("2", "TRANSITION_FADE_IN", "Fade in"),
              ("3", "TRANSITION_FADE_OUT", "Fade out"),
              ("4", "TRANSITION_FADE_IN_OUT", "Fade in and out"),
          ),
          perl_source="Audio.pm:43-45 (maxTransitionDuration); audio.html (pref_transitionType)"),
    Field("transitionDuration", "SETUP_TRANSITIONDURATION", "Transition Duration",
          perl_source="Audio.pm:44 (transitionDuration)"),
    Field("transitionSmart", "SETUP_TRANSSMART", "Smart Transition", "select",
          options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Audio.pm:44 (transitionSmart)"),
    Field("transitionSampleRestriction", "SETUP_TRANSRESTRICTION",
          "Transition Sample Restriction",
          perl_source="Audio.pm:44 (transitionSampleRestriction)"),
    Field("digitalVolumeControl", "SETUP_DIGITALVOLUMECONTROL",
          "Digital Volume Control", "select",
          options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Audio.pm:47-49 (hasDigitalOut)"),
    Field("mp3SilencePrelude", "SETUP_MP3SILENCEPRELUDE", "MP3 Silence Prelude",
          perl_source="Audio.pm:48 (mp3SilencePrelude)"),
    Field("preampVolumeControl", "SETUP_PREAMP", "Preamp Volume Control",
          perl_source="Audio.pm:51-53 (hasPreAmp)"),
    Field("digitalOutputEncoding", "SETUP_DIGITALOUTPUTENCODING",
          "Digital Output Encoding",
          perl_source="Audio.pm:55-57 (hasAesbeu)"),
    Field("clockSource", "SETUP_CLOCKSOURCE", "Clock Source",
          perl_source="Audio.pm:59-61 (hasExternalClock)"),
    Field("fxloopSource", "SETUP_FXLOOPSOURCE", "Effects Loop Source",
          perl_source="Audio.pm:63-65 (hasEffectsLoop)"),
    Field("fxloopClock", "SETUP_FXLOOPCLOCK", "Effects Loop Clock",
          perl_source="Audio.pm:67-69 (hasEffectsLoop)"),
    Field("polarityInversion", "SETUP_POLARITY", "Polarity Inversion", "select",
          options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Audio.pm:71-73 (hasPolarityInversion)"),
    Field("wordClockOutput", "SETUP_WORDCLOCK", "Word Clock Output",
          perl_source="Audio.pm:75-77 (hasDigitalIn)"),
    Field("rolloffSlow", "SETUP_ROLLOFFSLOW", "Slow Rolloff", "select",
          options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Audio.pm:79-81 (hasRolloff)"),
    Field("replayGainMode", "SETUP_REPLAYGAINMODE", "Replay Gain Mode", "select",
          options=(
              ("0", "REPLAYGAIN_DISABLED", "No Volume Adjustment"),
              ("1", "REPLAYGAIN_TRACK_GAIN", "Track Gain"),
              ("2", "REPLAYGAIN_ALBUM_GAIN", "Album Gain"),
              ("3", "REPLAYGAIN_SMART_GAIN", "Smart Gain"),
          ),
          perl_source="Audio.pm:83-85 (canDoReplayGain); audio.html (pref_replayGainMode)"),
    Field("remoteReplayGain", "SETUP_REMOTEREPLAYGAIN", "Remote Replay Gain",
          perl_source="Audio.pm:84 (remoteReplayGain)"),
    Field("localReplayGain", "SETUP_LOCALREPLAYGAIN", "Local Replay Gain",
          perl_source="Audio.pm:84 (localReplayGain)"),
    Field("analogOutMode", "SETUP_ANALOGOUTMODE", "Analog Output Mode", "select",
          options=(
              ("0", "ANALOGOUTMODE_HEADPHONE", "Headphones"),
              ("1", "ANALOGOUTMODE_SUBOUT", "Subwoofer"),
              ("2", "ANALOGOUTMODE_ALWAYS_ON", "Always On"),
              ("3", "ANALOGOUTMODE_ALWAYS_OFF", "Always Off"),
          ),
          perl_source="Audio.pm:87-89 (hasHeadSubOut)"),
    Field("bass", "SETUP_BASS", "Bass",
          perl_source="Audio.pm:91-93 (maxBass-minBass>0)"),
    Field("treble", "SETUP_TREBLE", "Treble",
          perl_source="Audio.pm:95-97 (maxTreble-minTreble>0)"),
    Field("stereoxl", "SETUP_STEREOXL", "Stereo XL", "select",
          options=(("0", "CHOICE_OFF", "Off"), ("1", "ON", "On")),
          perl_source="Audio.pm:99-101 (maxXL-minXL)"),
    Field("lineInLevel", "SETUP_LINEIN_LEVEL", "Line In Level",
          perl_source="Audio.pm:103-105 (hasLineIn + LineIn-Plugin)"),
    Field("lineInAlwaysOn", "SETUP_LINEIN_ALWAYSON", "Line In Always On",
          "select", options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Audio.pm:104 (lineInAlwaysOn)"),
    Field("mp3StreamingMethod", "SETUP_MP3STREAMINGMETHOD",
          "MP3 Streaming Method", "select",
          options=(
              ("-1", "SETUP_MP3STREAMINGMETHOD_MASK", "Mask"),
              ("0", "SETUP_MP3STREAMINGMETHOD_NONE", "None"),
              ("1", "SETUP_MP3STREAMINGMETHOD_XING", "Xing Header"),
          ),
          perl_source="Audio.pm:107-109 (isa Squeezebox2); live audio.html (pref_mp3StreamingMethod)"),
    Field("outputChannels", "SETUP_OUTPUTCHANNELS", "Output Channels", "select",
          options=(
              ("0", "SETUP_OUTPUTCHANNELS_BOTH", "Both"),
              ("1", "SETUP_OUTPUTCHANNELS_LEFT", "Left"),
              ("2", "SETUP_OUTPUTCHANNELS_RIGHT", "Right"),
          ),
          perl_source="Audio.pm:111-113 (hasOutputChannels)"),
    Field("balance", "SETUP_BALANCE", "Balance",
          perl_source="Audio.pm:115-117 (hasBalance)"),
)

#: Zusatzfelder von ``Player/Display.pm`` (Display-Klasse/Boom; Perl :49-61).
DISPLAY_EXTRA_FIELDS: tuple[Field, ...] = (
    # Graphics-Displays (Slim::Display::Graphics), Display.pm:49-51
    Field("activeFont_curr", "SETUP_ACTIVEFONT", "Active Font",
          perl_source="Display.pm:51 (Graphics: activeFont_curr)"),
    Field("idleFont_curr", "SETUP_IDLEFONT", "Idle Font",
          perl_source="Display.pm:51 (Graphics: idleFont_curr)"),
    Field("scrollPixels", "SETUP_SCROLLPIXELS", "Scroll Pixels",
          perl_source="Display.pm:51 (Graphics: scrollPixels)"),
    Field("scrollPixelsDouble", "SETUP_SCROLLPIXELSDOUBLE",
          "Scroll Pixels (Double Size)",
          perl_source="Display.pm:51 (Graphics: scrollPixelsDouble)"),
    # Text-Displays (SB1/SB2/Boom/Transporter/SqueezeboxG), Display.pm:55
    Field("doublesize", "SETUP_DOUBLESIZE", "Double Size", "select",
          options=(("1", "YES", "Yes"), ("0", "NO", "No")),
          perl_source="Display.pm:55 (Non-Graphics: doublesize)"),
    Field("offDisplaySize", "SETUP_OFFDISPLAYSIZE", "Off Display Size", "select",
          options=(("0", "SETUP_DISPLAY_SIZE_SMALL", "Small"),
                   ("1", "SETUP_DISPLAY_SIZE_MEDIUM", "Medium"),
                   ("2", "SETUP_DISPLAY_SIZE_LARGE", "Large")),
          perl_source="Display.pm:55 (offDisplaySize)"),
    Field("largeTextFont", "SETUP_LARGETEXTFONT", "Large Text Font",
          perl_source="Display.pm:55 (largeTextFont)"),
    # Boom (Display.pm:58-61)
    Field("minAutoBrightness", "SETUP_MINAUTOBRIGHTNESS",
          "Minimal Brightness (Automatic)",
          perl_source="Display.pm:59 (isa Slim::Player::Boom)"),
    Field("sensAutoBrightness", "SETUP_SENSAUTOBRIGHTNESS",
          "Brightness Sensitivity (Automatic)",
          perl_source="Display.pm:60 (sensAutoBrightness)"),
)

#: Zusatzfeld von ``Player/Alarm.pm`` (bedingte Pref; Perl :46-50).
ALARM_EXTRA_FIELDS: tuple[Field, ...] = (
    Field("alarmDefaultVolume", "ALARM_VOLUME", "Alarm Volume",
          perl_source="Alarm.pm:49 (nur wenn digitalVolumeControl aus; :46-50)"),
)

_PLAYER_MENU = SettingsPage(
    route="/settings/player/menu.html",
    perl_class="Slim::Web::Settings::Player::Menu",
    page_name="MENU_SETTINGS",               # Menu.pm:17
    title_key="MENU_SETTINGS",
    title_default="Menus",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Menu.pm:16-109",
    fields=(
        # ``menuItem`` ist eine Listen-Pref und wird vom Handler geschrieben;
        # das ``Field`` trÃ¤gt das Schreiben/Anzeigen im Namensschema mit
        # (Perl-Parameter sind aber ``menuItemRemove<i>``, s. Handler).
        Field(MENU_ITEM_PREF, "SETUP_GROUP_MENUITEMS", "Home Menu Customization",
              perl_source="Menu.pm:41,93 (menuItem) + menu.html:12 (menuItemRemove<i>)"),
    ),
)

_PLAYER_REMOTE = SettingsPage(
    route="/settings/player/remote.html",
    perl_class="Slim::Web::Settings::Player::Remote",
    page_name="REMOTE_SETTINGS",             # Remote.pm:19
    title_key="REMOTE_SETTINGS",
    title_default="Remote",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Remote.pm:18-85",
    fields=(
        Field(DISABLED_IRSETS_PREF, "SETUP_GROUP_IRSETS", "Remote Controls",
              perl_source="Remote.pm:70 (disabledirsets) + remote.html:5-6 (pref_irsetlist<i>)"),
        Field(IRMAP_PREF, "SETUP_IRMAP", "Remote Button Functions", "select",
              perl_source="Remote.pm:42 (irmap, nur bei >1 Map-Datei)"),
    ),
)

_PLAYER_SYNC = SettingsPage(
    route="/settings/player/synchronization.html",
    perl_class="Slim::Web::Settings::Player::Synchronization",
    page_name="SETUP_SYNCHRONIZE",           # Synchronization.pm:19
    title_key="SETUP_SYNCHRONIZE",
    title_default="Synchronize",
    needs_client=True,
    scope="client",
    perl_source="Slim/Web/Settings/Player/Synchronization.pm:18-95",
    fields=(
        Field("syncVolume", "SETUP_SYNCVOLUME", "Synchronize Volume", "select",
              options=(("1", "SETUP_SYNCVOLUME_ON", "Sync player's volume"),
                       ("0", "SETUP_SYNCVOLUME_OFF", "Don't sync player's volume")),
              perl_source="Synchronization.pm:39; synchronization.html:14-19"),
        Field("syncPower", "SETUP_SYNCPOWER", "Synchronize Power", "select",
              options=(("1", "SETUP_SYNCPOWER_ON", "Power off/on with group"),
                       ("0", "SETUP_SYNCPOWER_OFF", "Power off/on separately")),
              perl_source="Synchronization.pm:39; synchronization.html:25-30"),
        Field("maintainSync", "SETUP_MAINTAINSYNC", "Maintain Synchronization",
              "select",
              options=(("1", "SETUP_MAINTAINSYNC_ON", "Maintain synchronization while playing"),
                       ("0", "SETUP_MAINTAINSYNC_OFF", "Don't maintain synchronization")),
              perl_source="Synchronization.pm:39; synchronization.html:36-41"),
        Field("startDelay", "SETUP_STARTDELAY", "Player Start Delay (ms)",
              perl_source="Synchronization.pm:39; synchronization.html:47"),
        Field("playDelay", "SETUP_PLAYDELAY", "Player Audio Delay (ms)",
              perl_source="Synchronization.pm:39; synchronization.html:51"),
        Field("minSyncAdjust", "SETUP_MINSYNCADJUST",
              "Minimum Synchronization Adjustment (ms)",
              perl_source="Synchronization.pm:39; synchronization.html:55"),
        Field("packetLatency", "SETUP_PACKETLATENCY",
              "Network Packet Latency (ms)",
              perl_source="Synchronization.pm:39; synchronization.html:59"),
    ),
)

#: Perl-Route â†’ Seite.  Die SchlÃ¼ssel sind exakt die, die ``PAGES`` erwartet.
PLAYER_PAGES: dict[str, SettingsPage] = {
    _PLAYER_MENU.route: _PLAYER_MENU,
    _PLAYER_REMOTE.route: _PLAYER_REMOTE,
    _PLAYER_SYNC.route: _PLAYER_SYNC,
}

#: Seiten mit eigenem Handler (Perl ``handler()`` statt reiner ``prefs()``-Liste).
PAGES_WITH_HANDLER: dict[str, str] = {
    _PLAYER_MENU.route: HANDLER_MENU,
    _PLAYER_REMOTE.route: HANDLER_REMOTE,
    _PLAYER_SYNC.route: HANDLER_SYNC,
}


def page_handler(page: SettingsPage) -> Optional[str]:
    """Handler-Kennzeichen einer Seite (``None`` = reine ``prefs()``-Seite)."""
    return PAGES_WITH_HANDLER.get(page.route)


def valid_for(page: SettingsPage, player) -> bool:
    """Perl ``validFor`` (Menu.pm:28-33, Remote.pm:30-35, Synchronization.pm:30-35).

    Ohne Client (``player is None``) â†’ ``False`` wie Perls ``validFor`` mit
    ``undef`` angelehnt; die Seite zeigt dann ``SETUP_NO_PREFS``.
    """
    if player is None:
        return False
    if page is _PLAYER_MENU:
        return _has_display(player)
    if page is _PLAYER_REMOTE:
        return _has_ir(player)
    if page is _PLAYER_SYNC:
        return _can_offer_sync(player)
    return True


# ── Hilfsdaten ───────────────────────────────────────────────────────────────

def _client_prefs(player) -> dict:
    """``$prefs->client($client)`` — unser ``player.playerprefs``-Dict."""
    prefs = getattr(player, "playerprefs", None)
    if prefs is None:
        return {}
    return prefs


def _menu_items(player) -> list[str]:
    """Perl ``Menu.pm:41`` — ``menuItem`` als Liste (Skalar â†’ 1-elementig)."""
    raw = _client_prefs(player).get(MENU_ITEM_PREF)
    return _as_list(raw)


def _menu_item_name(player, value: str) -> str:
    """Perl ``Menu.pm:111-133`` — Anzeigename eines MenÃ¼punkts.

    Perl: existiert ein String-Token â†’ Ã¼bersetzt; sonst ein installiertes
    Plugin â†’ dessen Anzeigename; sonst der rohe Wert.  Unser Port hat kein
    Plugin-MenÃ¼-Register, deshalb nur die String-Tabelle und der Rohwert.
    """
    from lyrion.i18n import get_string

    name = get_string(value, default=value) if value else value
    return name or value


def _sync_groups(player) -> list[tuple[str, str]]:
    """Perl ``Synchronization.pm:77-95`` — ``(wert, label)``-Zeilen des ``select``.

    Reihenfolge: synchronisierbare Player, dann der eigene (wenn Master), dann
    ``-1`` = ``SETUP_NO_SYNCHRONIZATION``.
    """
    from lyrion.i18n import get_string

    rows: list[tuple[str, str]] = []
    for other in _can_sync_with(player):
        rows.append((other.mac, _sync_name(other, player)))
    if not getattr(player, "sync_master", None) and getattr(player, "sync_slaves", None):
        rows.append((player.mac, _sync_name(player, player)))
    rows.append((_NO_SYNC, get_string("SETUP_NO_SYNCHRONIZATION",
                                      default="No Synchronization")))
    return rows


def _sync_name(one, other) -> str:
    """Perl ``Slim/Player/Sync.pm:27-96`` ``syncname`` — Name des Sync-Partners."""
    name = getattr(one, "name", "") or getattr(one, "mac", "")
    return name or getattr(one, "mac", "")


def _sync_selected(player) -> str:
    """Perl ``Synchronization.pm:66-70`` — Master-ID oder ``-1``."""
    master = getattr(player, "sync_master", None)
    return master if master else _NO_SYNC


# ── Handler: MenÃ¼ ────────────────────────────────────────────────────────────

def _apply_menu_actions(player, params: dict[str, str]) -> list[str]:
    """Perl ``Menu.pm:43-99``: ``Action<i>``/``menuItemRemove<i>``/``nonMenuItemAdd<i>``."""
    items = _menu_items(player)
    if not items:
        items = list(DEFAULT_MENU_ITEMS)

    # Perl lÃ¤uft abwÃ¤rts Ã¼ber die Liste (:43) und splict dabei.
    for i in range(len(items) - 1, -1, -1):
        action = params.get(f"Action{i}")
        if action == "Remove":
            del items[i]
        elif action == "Up" and i > 0:
            items.insert(i - 1, items.pop(i))
        elif action == "Down" and i < len(items) - 1:
            items.insert(i + 1, items.pop(i))

        if params.get("removeItems") and params.get(f"menuItemRemove{i}"):
            del items[i]

    if params.get("addItems"):
        # Perls ``nonMenuItems`` (Vorlage :33) ist der hÃ¶chste Index; die
        # von uns gerenderten Zeilen tragen ``nonMenuItemAdd<i>``.
        limit = params.get("nonMenuItems")
        if limit is not None and str(limit).lstrip("-").isdigit():
            count = int(limit) + 1
        else:
            count = _max_index(params, "nonMenuItemAdd") + 1
        for i in range(count):
            value = params.get(f"nonMenuItemAdd{i}")
            if value:
                items.append(value)

    if not items:
        items = list(DEFAULT_MENU_ITEMS)   # Menu.pm:88-91

    _client_prefs(player)[MENU_ITEM_PREF] = items   # Menu.pm:93
    return items


def _max_index(params: dict[str, str], prefix: str) -> int:
    """HÃ¶chster ``<prefix><n>``-Index in den Parametern (âˆ’1 wenn keiner)."""
    highest = -1
    for key in params:
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            highest = max(highest, int(key[len(prefix):]))
    return highest


# ── Handler: Fernbedienung ───────────────────────────────────────────────────

def _apply_remote_save(player, params: dict[str, str]) -> list[str]:
    """Perl ``Remote.pm:53-71``: ``disabledirsets`` aus den ``pref_irsetlist<i>``.

    Unser Port hat keinen IR-Dateibaum (``Slim/Hardware/IR.pm:165-243``), also
    auch keine Set-Liste; der Handler sammelt trotzdem jede gemeldete
    ``pref_irsetlist<i>``-Markierung (die Vorlage liefert je Set zwei Felder,
    deshalb der ``!ref``-Test :62-65).
    """
    disabled: list[str] = []
    index = 0
    while True:
        key = f"pref_irsetlist{index}"
        if key not in params:
            break
        disabled.append(params[key])
        index += 1
    _client_prefs(player)[DISABLED_IRSETS_PREF] = disabled
    return [DISABLED_IRSETS_PREF] if index else []


def irsetlist() -> dict[str, str]:
    """Perl ``Remote.pm:76`` — ``irsetlist`` (Wert â†’ Anzeigename).

    Ohne IR-Dateibaum leer; Perl erfÃ¤nde hier die Sets aus
    ``Slim::Hardware::IR::irfiles($client)`` (``IR.pm:165``).
    """
    return {}


# ── Handler: Synchronisation ─────────────────────────────────────────────────

def _apply_sync(player, params: dict[str, str]) -> list[str]:
    """Perl ``Synchronization.pm:48-58``: ``synchronize`` lÃ¶st ``sync`` aus.

    ``synchronize`` ist KEINE Pref; die eigentlichen Prefs schreibt
    ``_save_simple_prefs`` (Schleife in ``Settings.pm:154-171``).
    """
    written: list[str] = []
    target = params.get("synchronize")
    if params.get("saveSettings") and target:
        manager = PlayerManager()
        if target == _NO_SYNC:
            # Perl :55 — ``$client->execute(['sync','-'])`` = sich lÃ¶sen.
            manager.unsync_player(player.mac)
            written.append("synchronize")
        else:
            other = manager.get_player(target)
            if other is not None and other.mac != player.mac:
                # Perl :53 — ``$otherClient->execute(['sync', $client->id])``.
                manager.sync_players(other.mac, [player.mac])
                written.append("synchronize")
            else:
                manager.unsync_player(player.mac)
                written.append("synchronize")
    return written


# ── Auslieferung: Felder + Speichern ─────────────────────────────────────────

async def save_player_page(page: SettingsPage, params: dict[str, str], player,
                           invalid: Optional[list[tuple[str, str]]] = None) -> list[str]:
    """Speichert die Felder einer Player-Seite (Perl ``Settings.pm:154-171``).

    ``synchronize`` ist keine Pref und wird von beiden Pfaden ignoriert
    (Perl nimmt es in ``Synchronization.pm:50-58`` getrennt).  Die MenÃ¼-Seite
    schreibt ihre ``menuItem``-Liste im Handler.
    """
    handler = page_handler(page)
    written = await _save_simple_prefs(page, params, player, invalid)

    if handler == HANDLER_MENU and player is not None and "saveSettings" in params:
        _apply_menu_actions(player, params)
        written.append(MENU_ITEM_PREF)
    elif handler == HANDLER_REMOTE and player is not None and "saveSettings" in params:
        written.extend(_apply_remote_save(player, params))
    elif handler == HANDLER_SYNC and player is not None:
        written.extend(_apply_sync(player, params))
    return written


def _render_menu_page(page: SettingsPage, player, params: dict[str, str]) -> str:
    """MenÃ¼-Reihenfolge + verfÃ¼gbare (nicht gefÃ¼hrte) Punkte (Perl ``menu.html``)."""
    from lyrion.i18n import get_string

    items = _menu_items(player) or list(DEFAULT_MENU_ITEMS)
    pid = quote(str(getattr(player, "mac", "")), safe="")
    route = page.route

    rows = []
    for i, value in enumerate(items):
        label = _menu_item_name(player, value)
        base = f"{route}?playerid={pid}"
        rows.append(
            "<tr>"
            f"<td>{_esc(label)}&nbsp;&nbsp;</td>"
            f'<td><a href="{_esc(base)}&amp;Action{i}=Up">'
            f'{_esc(get_string("MOVEUP", default="Move Up"))}</a>&nbsp;</td>'
            f'<td><a href="{_esc(base)}&amp;Action{i}=Down">'
            f'{_esc(get_string("MOVEDOWN", default="Move Down"))}</a>&nbsp;</td>'
            f'<td><a href="{_esc(base)}&amp;Action{i}=Remove">'
            f'{_esc(get_string("DELETE", default="Delete"))}</a>&nbsp;</td>'
            f'<td><input type=checkbox class="stdedit" name="menuItemRemove{i}" '
            f'id="menuItemRemove{i}" value="1" /></td>'
            "</tr>"
        )

    # VerfÃ¼gbar = Punkte, die unser Port kennt und die nicht gefÃ¼hrt sind.
    used = set(items)
    available = [v for v in DEFAULT_MENU_ITEMS if v not in used]
    non_rows = []
    for i, value in enumerate(available):
        label = _menu_item_name(player, value)
        non_rows.append(
            f'<td><input type=checkbox class="stdedit" name="nonMenuItemAdd{i}" '
            f'id="nonMenuItemAdd{i}" value="{_esc(value)}" /> {_esc(label)}'
            "&nbsp;&nbsp;&nbsp;&nbsp;</td>")

    out = ['<div class="settingSection">']
    out.append(f'<div class="prefHead">'
               f'{_esc(get_string("SETUP_GROUP_MENUITEMS", default="Home Menu Customization"))}'
               "</div>")
    out.append(f'<div class="prefDesc">{_esc(get_string("SETUP_GROUP_MENUITEMS_DESC", default=""))}</div>')
    out.append("<table>" + "".join(rows) + "</table>")
    out.append(f'<p><input name="removeItems" type="submit" class="stdclick" '
               f'value="{_esc(get_string("DELETE", default="Delete"))}"></p>')
    out.append("</div>")

    out.append('<div class="settingSection">')
    out.append(f'<div class="prefHead">'
               f'{_esc(get_string("SETUP_GROUP_NONMENUITEMS_INTRO", default="Inactive Menu Items:"))}'
               "</div>")
    out.append("<table><tr>" + "".join(non_rows) + "</tr></table>")
    out.append(f'<p><input name="addItems" type="submit" class="stdclick" '
               f'value="{_esc(get_string("ADD", default="Add"))}">'
               f'<input type=hidden name="nonMenuItems" value="{len(available) - 1}" /></p>')
    out.append("</div>")
    return "".join(out)


def _render_remote_page(page: SettingsPage, player, params: dict[str, str]) -> str:
    """IR-Sets + Tastenbelegung (Perl ``remote.html``)."""
    from lyrion.i18n import get_string

    prefs = _client_prefs(player)
    disabled = set(_as_list(prefs.get(DISABLED_IRSETS_PREF)))
    sets = irsetlist()

    out = []
    if sets:
        out.append('<div class="settingSection">')
        out.append(f'<div class="prefHead">'
                   f'{_esc(get_string("SETUP_GROUP_IRSETS", default="Remote Controls"))}</div>')
        out.append(f'<div class="prefDesc">'
                   f'{_esc(get_string("SETUP_GROUP_IRSETS_DESC", default=""))}</div>')
        rows = []
        for i, (key, label) in enumerate(sets.items()):
            checked = "" if key in disabled else " checked"
            rows.append(
                f'<input type=hidden name="pref_irsetlist{i}" value="{_esc(key)}" />'
                f'<input type=checkbox{checked} class="stdedit" '
                f'name="pref_irsetlist{i}" id="irsetlist{i}" value="0" />'
                f'<label for="{_esc(key)}" class="stdlabel">{_esc(label)}</label><br>')
        out.append("".join(rows))
        out.append("</div>")
    return "".join(out)


def _render_sync_page(page: SettingsPage, player, params: dict[str, str], extra_options) -> str:
    """Felder der Seite + ``synchronize``-Auswahl (Perl ``synchronization.html``)."""
    from lyrion.i18n import get_string

    out = []
    out.append('<div class="settingSection">')
    # ``synchronize``: eigene Zeile, keine Pref.
    out.append(f'<div class="settingGroup"><label for="synchronize">'
               f'{_esc(get_string("SETUP_SYNCHRONIZE", default="Synchronize"))}'
               "</label>"
               f'<select class="stdedit" name="synchronize" id="synchronize">')
    selected = _sync_selected(player)
    for value, label in _sync_groups(player):
        sel = " selected" if str(selected) == str(value) else ""
        out.append(f'<option{sel} value="{_esc(value)}">{_esc(label)}</option>')
    out.append("</select></div>")

    for f in page.fields:
        value = str(_client_prefs(player).get(f.pref, "") if
                    _client_prefs(player).get(f.pref) is not None else "")
        if f.pref in _ON_OFF and not value:
            value = "1"
        out.append(f'<div class="settingGroup"><label for="{_esc(f.pref)}">'
                   f'{_esc(f.label())}</label>{_render_input(f, value, f.options)}</div>')
    out.append("</div>")
    return "".join(out)


def render_player_page(page: SettingsPage, player, params: dict[str, str],
                       extra_options=None) -> str:
    """HTML-Fragment einer Player-Seite mit eigenem Handler (ohne Formularrahmen).

    Der Formularrahmen (``form``/``saveSettings``/``playerid``) kommt wie in
    ``settings.py`` von :func:`lyrion.web.settings._render_page`; hier entsteht
    nur der seiten-spezifische ``settingsRegion``-Inhalt.
    """
    handler = page_handler(page)
    if handler == HANDLER_MENU:
        return _render_menu_page(page, player, params)
    if handler == HANDLER_REMOTE:
        return _render_remote_page(page, player, params)
    if handler == HANDLER_SYNC:
        return _render_sync_page(page, player, params, extra_options)
    return ""


# ── ASGI: vollstÃ¤ndige Auslieferung der Player-Seiten ────────────────────────
#
# Damit der Eintrag in ``settings.py`` minimal bleibt (Import + ``**PLAYER_PAGES``
# + eine Dispatch-Zeile), bringt dieses Modul den kompletten Anfrageweg fÃ¼r
# seine Seiten mit.  Es benutzt die Bausteine von ``settings.py`` weiter
# (Param-Parsing, Client-AuflÃ¶sung, Antwort-Sender, AJAX-Fragment), damit beide
# Seitenfamilien identisch antworten (Perl ``Slim/Web/Settings.pm:135-287``).

def _render_full_page(page: SettingsPage, params: dict[str, str], player,
                      warning: Optional[str]) -> bytes:
    """VollstÃ¤ndige HTML-Seite (Rahmen wie ``settings.py`` ``_render_page``).

    Der Rahmen entspricht ``HTML/EN/settings/header.html`` + ``footer.html``:
    ``form`` mit ``saveSettings``/``useAJAX``/``page``/``playerid``, davor der
    ``statusarea``-Block mit der Warnung (``Settings.pm:193-200``).
    """
    from lyrion.i18n import get_string as _gs

    action = page.route
    pid = params.get("playerid", "")
    if pid:
        action += "?playerid=" + quote(pid, safe="")

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(_gs(page.title_key, default=page.title_default))}</title>",
        "</head><body>",
        # Wie in ``settings.py``: ``warning`` wird roh eingesetzt (header.html:42-43).
        f'<div id="statusarea" class="statusarea">{warning or ""}</div>',
        f'<form name="settingsForm" id="settingsForm" method="post" action="{_esc(action)}">',
        '<input type="hidden" name="useAJAX" value="0">',
        f'<input type="hidden" name="page" value="{_esc(page.page_name)}">',
    ]
    if page.needs_client and pid:
        parts.append(f'<input type="hidden" name="playerid" value="{_esc(pid)}">')
    parts.append('<div id="settingsRegion">')
    parts.append(render_player_page(page, player, params))
    parts.append("</div>")
    parts.append('<div id="prefsSubmit">'
                 + f'<input name="saveSettings" id="saveSettings" type="submit" '
                   f'class="stdclick" value="{_esc(_gs("SAVE_SETTINGS", default="Save Settings"))}">'
                 + '<input type="hidden" name="saveSettings" value="1"></div>')
    parts.append("</form></body></html>")
    return "\n".join(parts).encode("utf-8")


async def handle_player_page_request(scope: dict, receive, send,
                                     page: Optional[SettingsPage] = None) -> None:
    """Bedient die drei Player-Seiten (Perl ``Settings.pm:135-287``).

    Wird aus ``settings.py`` genau dann gerufen, wenn die angefragte Seite in
    ``PLAYER_PAGES`` liegt (siehe Registry-Patch im Modul-Docstring).  Der
    Referenz-Pfad innerhalb von ``settings.py`` bleibt unberÃ¼hrt.
    """
    from lyrion.web import settings as S

    method = scope.get("method", "GET")
    path = scope.get("path", "")
    body = await S._read_body(receive)
    params = S._parse_params(scope, body)

    if page is None:
        page = PLAYER_PAGES.get(path)
    if page is None:
        await S._send(send, 404, "text/html",
                      b"<html><body>404 Not Found</body></html>")
        return

    player = S._resolve_player(params) if page.needs_client else None

    warning: Optional[str] = None
    written: list[str] = []
    invalid: list[tuple[str, str]] = []

    if method == "POST" and "saveSettings" in params:
        if player is None:
            # Perl ``Menu.pm:102-106``/``Remote.pm:78-82``: SETUP_NO_PREFS.
            warning = S._string(
                "SETUP_NO_PREFS",
                "There are no settings for this player on this page")
        else:
            invalid = []
            written = await save_player_page(page, params, player, invalid)
            for pref, value in invalid:
                warning = (warning or "") + (
                    S._string("SETTINGS_INVALIDVALUE",
                              'Invalid value "%s" for %s') % (S._esc(value), pref)) + "<br/>"
            if not warning:
                warning = S._string("SETUP_CHANGES_SAVED",
                                    "Changes have been saved.")
            logger.info("settings_player: %s saved %s", page.route, written)
    elif page.needs_client and player is None:
        warning = S._string("SETUP_NO_PREFS",
                            "There are no settings for this player on this page")

    if str(params.get("useAJAX", "0")) == "1":
        await S._send(send, 200, "text/plain",
                      S._render_ajax(warning, written, invalid))
        return

    await S._send(send, 200, "text/html",
                  _render_full_page(page, params, player, warning))


__all__ = [
    "ALARM_EXTRA_FIELDS",
    "AUDIO_EXTRA_FIELDS",
    "DEFAULT_MENU_ITEMS",
    "DISABLED_IRSETS_PREF",
    "DISPLAY_EXTRA_FIELDS",
    "HANDLER_MENU",
    "HANDLER_REMOTE",
    "HANDLER_SYNC",
    "IRMAP_PREF",
    "MENU_ITEM_PREF",
    "PAGES_WITH_HANDLER",
    "PLAYER_PAGES",
    "SYNC_PREFS",
    "handle_player_page_request",
    "irsetlist",
    "page_handler",
    "render_player_page",
    "save_player_page",
    "valid_for",
]
