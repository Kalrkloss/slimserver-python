"""Display-Verdrahtung (PROT-18): die Frames, die Perl bei Zustandswechseln sendet.

Wer sendet in Perl was — und an wen?
-------------------------------------
* Die Display-Klasse kommt aus der HELO-Device-ID: ``Slimproto.pm:1038-1121``
  (squeezebox2/softsqueeze -> ``Slim::Display::Squeezebox2`` :1038-1041/:1083-1086,
  boom/softboom -> ``Boom`` :1048-1051/:1093-1096, transporter/softsqueeze3 ->
  ``Transporter`` :1053-1056/:1088-1091, squeezebox -> ``SqueezeboxG`` (bitmapped,
  :1063-1065) sonst ``Text`` (:1067-1069), squeezeslave -> ``Text`` :1098-1101,
  receiver -> ``NoDisplay`` :1043-1046, squeezeplay/controller -> ``NoDisplay``
  :1103-1106).
* Ein Screen-Update ist immer ``$client->update()`` und landet in
  ``Display.pm:141-218``; Zustandswechsel rufen es direkt — Track-Start /
  Ende des Pufferns ``Player.pm:1115-1116`` und ``:1250-1252``.
* Grafik (Squeezebox2-Familie): ``Squeezebox2.pm:218-227`` (``updateScreen``) ->
  ``visualizer()`` (Frame ``visu``, :259-291; Modustabelle :72-129) **und**
  ``drawFrameBuf`` (Frame ``grfe``, :230-250, Header offset/transition/param).
  ``Squeezebox2::drawFrameBuf`` ruft ``visualizer()`` (:241) VOR dem
  Framebuffer, also kommt ``visu`` zuerst. ``Transporter.pm:200-212`` schickt
  Screen 2 zusätzlich auf offset 640.
* Grafik SB1 (SqueezeboxG): ``SqueezeboxG.pm:143-167`` -> Frame ``grfd`` mit
  Header ``pack('n', 560)`` (:34 ``1*280*2``, :158).
* Text (squeezeslave, SB1 ohne Bitmap): ``Text.pm:437-441`` ->
  ``TextVFD::vfdUpdate`` -> ``Player/Squeezebox.pm:495-502`` -> Frame ``vfdc``
  (Payload = roher TextVFD-Stream, ``Display/Lib/TextVFD.pm:312-357``).
* Helligkeit (nur Grafik): ``Graphics.pm:496-509`` -> ``grfb`` mit
  ``brightnessMap[level]``; gerufen aus ``Player.pm:119`` (Connect), ``:257``
  (Power-Off), ``:281-287`` (Power-On), ``Squeezebox.pm:127`` (Reconnect) und
  ``Buttons/ScreenSaver.pm:116-125`` (Idle-Dim). ``Text.pm:575-580`` sendet bei
  Helligkeitsänderung KEINEN Frame, sondern zeichnet den Screen neu.
* ``NoDisplay`` stubb't ``update``/``brightness``/``curLines``/``scroll*``
  (``NoDisplay.pm:26-56``) und liefert ``vfdmodel() = 'none'`` (:59); die
  CLI/Jive-Kopie ``EmulatedSqueezebox2`` stubb't ``updateScreen``/
  ``drawFrameBuf``/``visualizer``/``visualizerParams`` (``EmulatedSqueezebox2.pm:25-29``).
  Ein Client ohne Display bekommt deshalb **keinen** dieser Frames.

Was diese Verdrahtung rendert — und was nicht
---------------------------------------------
Die Nutzlasten entstehen in :mod:`lyrion.player.fonts`, einem Port von
``Slim/Display/Lib/Fonts.pm`` (Bitmap-Schriften aus ``graphics/*.font.bmp``,
``string`` :292-522) und ``Slim/Display/Lib/TextVFD.pm`` (``vfdUpdate``
:147-363). ``grfe``/``grfd`` bekommen die gerenderten Screen-Bits,
``vfdc`` den VFD-Strom — aber **nur**, wenn der Aufrufer ``text=`` übergibt.
Wer welchen Text auf welche Zeile schreibt, entscheidet in Perl das
Button-Modul ``Slim/Buttons/Playlist.pm:398-483`` (``lines()`` →
``currentSongLines``) mit ``Slim/Player/Player.pm:488-706``; im Port liefert
ihn ``lyrion.player.manager`` (``_now_playing_lines``: Statuszeile + Titel).
Ohne ``text`` geht weiterhin nur der Visualizer-/Helligkeitsframe raus statt
einer erfundenen Bitmap. ``screen2`` (Album/Interpret, nur Transporter —
``Display.pm:859`` ``hasScreen2``, ``Transporter.pm:319-325``) ist nicht
verdrahtet; Perl zeigt es in einem eigenen Screen (``Player.pm:638-665``).
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional, Sequence, Union

from . import fonts

logger = logging.getLogger(__name__)

# Perl-Klassen (Slim/Display/*.pm) — Namen wie im Perl-Paket.
SQUEEZEBOX2 = "Squeezebox2"
BOOM = "Boom"
TRANSPORTER = "Transporter"
SQUEEZEBOXG = "SqueezeboxG"
TEXT = "Text"
NODISPLAY = "NoDisplay"

# HELO-Device-ID -> Display-Klasse (Slimproto.pm:1038-1121).
DEVICE_ID_DISPLAY_CLASS: dict[str, str] = {
    "squeezebox2": SQUEEZEBOX2,      # :1038-1041
    "softsqueeze": SQUEEZEBOX2,      # :1083-1086
    "boom": BOOM,                    # :1048-1051
    "softboom": BOOM,                # :1093-1096
    "transporter": TRANSPORTER,      # :1053-1056
    "softsqueeze3": TRANSPORTER,     # :1088-1091
    "squeezeslave": TEXT,            # :1098-1101
    "receiver": NODISPLAY,           # :1043-1046
    "squeezeplay": NODISPLAY,        # :1103-1106
    "controller": NODISPLAY,         # :1103-1106
    # Port-eigene/weitere Modellnamen, die Perl auf dieselben Klassen abbildet:
    "squeezebox3": SQUEEZEBOX2,      # SB3 meldet sich als deviceid squeezebox2
    "squeezelite": NODISPLAY,        # meldet sich als squeezeplay (live 2026-09-12)
    "baby": NODISPLAY,               # Squeezebox Radio: jive-Display (NoDisplay)
    "http": NODISPLAY,               # HTTP.pm: kein Display
    "web": NODISPLAY,                # Web Client
    "disconnected": NODISPLAY,       # Disconnected.pm: kein Display
}
# SB1 ist zweigeteilt (Slimproto.pm:1058-1070): bitmapped -> SqueezeboxG,
# sonst Text. Der HELO-Capability-Wert steuert das (``$bitmapped``).
SB1_DEVICE_IDS = frozenset({"squeezebox", "squeezebox1", "squeezeboxclassic"})

# Klassen, die von ``Slim::Display::Squeezebox2`` erben und deshalb den
# Visualizer (``visu``) kennen. ``SqueezeboxG.pm:25`` erbt dagegen direkt von
# ``Graphics.pm`` — dort gibt es ``sub visualizer`` nicht, also kein ``visu``.
VISU_CLASSES = frozenset({SQUEEZEBOX2, BOOM, TRANSPORTER})
# Klassen mit ``Graphics.pm:496-509`` ``brightness`` -> ``grfb``.
GRFB_CLASSES = frozenset({SQUEEZEBOX2, BOOM, TRANSPORTER, SQUEEZEBOXG})
# Klassen mit einem Bitmap-Framebuffer (``grfe`` / ``grfd``).
FRAMEBUF_CLASSES = frozenset({SQUEEZEBOX2, BOOM, TRANSPORTER, SQUEEZEBOXG})

# Die Visualizer-Tabelle aus Squeezebox2.pm:72-129, Index = Modusnummer.
# ``params`` ist die Zahlenfolge, die ``visualizer()`` (:278-289) als
# ``pack("CC", which, count)`` + ``pack("N", param)`` verschickt.
VISUALIZER_MODES: tuple[tuple[int, ...], ...] = (
    (0,),                                                     # 0  BLANK :74-76
    (0,),                                                     # 1  ELAPSED :78-80
    (0,),                                                     # 2  REMAINING :82-84
    (1, 0, 0, 280, 18, 302, 18),                              # 3  VUMETER_SMALL :86-88
    (1, 0, 0, 280, 18, 302, 18),                              # 4  + ELAPSED :90-92
    (1, 0, 0, 280, 18, 302, 18),                              # 5  + REMAINING :94-96
    (2, 1, 1, 0x10000, 280, 40, 0, 4, 1, 0, 1, 3),            # 6  SPECTRUM_SMALL :98-100
    (2, 1, 1, 0x10000, 280, 40, 0, 4, 1, 0, 1, 3),            # 7  + ELAPSED :102-104
    (2, 1, 1, 0x10000, 280, 40, 0, 4, 1, 0, 1, 3),            # 8  + REMAINING :106-108
    (2, 0, 0, 0x10000, 0, 160, 0, 4, 1, 1, 1, 1,
     160, 160, 1, 4, 1, 1, 1, 1),                             # 9  SPECTRUM :110-112
    (2, 0, 0, 0x10000, 0, 160, 0, 4, 1, 1, 1, 1,
     160, 160, 1, 4, 1, 1, 1, 1),                             # 10 + ELAPSED :114-116
    (2, 0, 0, 0x10000, 0, 160, 0, 4, 1, 1, 1, 1,
     160, 160, 1, 4, 1, 1, 1, 1),                             # 11 + REMAINING :118-120
    (0,),                                                     # 12 BUFFERFULLNESS :122-124
    (0,),                                                     # 13 CLOCK :126-128
)
# Transporter hat eine eigene Visualizer-Tabelle (Transporter.pm:101-127) und
# eigene Prefs visualMode/visualModes (Transporter.pm:38-39, :297-306).
TRANSPORTER_VISUALIZERS: tuple[tuple[int, ...], ...] = (
    (0,),                                                     # 0 BLANK :102-104
    (0,),                                                     # 1 EXTENDED_TEXT :105-108
    (1, 0, 1, 320, 160, 480, 160),                            # 2 ANALOG_VUMETER :109-111
    (1, 0, 0, 340, 130, 490, 130),                            # 3 DIGITAL_VUMETER :112-114
    (2, 0, 0, 0x10000, 320, 160, 0, 4, 1, 1, 1, 3,
     480, 160, 1, 4, 1, 1, 1, 3),                             # 4 SPECTRUM :115-117
    (2, 0, 0, 0x10000, 320, 160, 0, 4, 1, 1, 1, 1,
     480, 160, 1, 4, 1, 1, 1, 1),                             # 5 SPECTRUM+TEXT :118-120
)

# Default-Prefs je Klasse (Perl ``$defaultPrefs``):
#   Squeezebox2.pm:131-134  playingDisplayMode 5, playingDisplayModes [0..11]
#   Boom.pm:115-121         playingDisplayMode 1, playingDisplayModes [0..10]
#   Transporter.pm:35-40    visualMode 2, visualModes [0..5]
#   SqueezeboxG.pm:36-42    playingDisplayMode 0, playingDisplayModes [0..5]
DEFAULT_PLAYING_DISPLAY_MODE: dict[str, int] = {SQUEEZEBOX2: 5, BOOM: 1}
DEFAULT_PLAYING_DISPLAY_MODES: dict[str, tuple[int, ...]] = {
    SQUEEZEBOX2: tuple(range(12)),
    BOOM: tuple(range(11)),
}
DEFAULT_TRANSPORTER_VISUAL_MODE = 2
DEFAULT_TRANSPORTER_VISUAL_MODES: tuple[int, ...] = tuple(range(6))

# Helligkeits-Defaults (Pref-Namen wie in Perl):
#   Graphics.pm:39-43 (Squeezebox2/Transporter/SqueezeboxG): idle 2, off 1, on 4
#   Boom.pm:117-121: idle 6, off 6, on 6
#   Text.pm:40-46: off 1, idle 2, on 4
BRIGHTNESS_DEFAULTS: dict[str, dict[str, int]] = {
    SQUEEZEBOX2: {"idleBrightness": 2, "powerOffBrightness": 1, "powerOnBrightness": 4},
    TRANSPORTER: {"idleBrightness": 2, "powerOffBrightness": 1, "powerOnBrightness": 4},
    SQUEEZEBOXG: {"idleBrightness": 2, "powerOffBrightness": 1, "powerOnBrightness": 4},
    BOOM: {"idleBrightness": 6, "powerOffBrightness": 6, "powerOnBrightness": 6},
    TEXT: {"idleBrightness": 2, "powerOffBrightness": 1, "powerOnBrightness": 4},
}

# Framebuffer-Größe = bytesPerColumn * displayWidth (Graphics.pm:99-103):
# Squeezebox2.pm:180-182 (4 B/Spalte) * :188-190 (320) = 1280;
# SqueezeboxG.pm:34 ``1 * 280 * 2`` = 560 (Header-Wert im ``grfd``).
FRAMEBUF_BYTES: dict[str, int] = {SQUEEZEBOXG: 560}
# SqueezeboxG.pm:34 — der ``grfd``-Header ist ``pack('n', 560)``.
GRAPHICS_FRAMEBUF_LIVE = 560


def display_class_for(model: str, *, bitmapped: bool = False) -> str:
    """Perl-Display-Klasse für einen HELO-Device-/Modellnamen.

    ``bitmapped`` entscheidet nur bei SB1 (Slimproto.pm:1058-1070): bitmapped
    -> ``SqueezeboxG``, sonst ``Text``. Unbekannte Namen -> ``NoDisplay``, denn
    Perl wählt die Klasse ausschließlich über die Device-ID (:1029-1121) und
    schließt unbekannte IDs sogar (:1031-1036) — wir senden dort lieber nichts
    als an eine geratene Klasse.
    """
    m = (model or "").lower()
    if m in SB1_DEVICE_IDS:
        return SQUEEZEBOXG if bitmapped else TEXT
    if m in DEVICE_ID_DISPLAY_CLASS:
        return DEVICE_ID_DISPLAY_CLASS[m]
    return NODISPLAY


def _pref(player: Any, key: str, default: Any) -> Any:
    """``$prefs->client($client)->get($key)`` (mit Perl-Default)."""
    prefs = getattr(player, "playerprefs", None)
    if isinstance(prefs, dict):
        value = prefs.get(key, default)
        if value is not None:
            return value
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


class DisplayWiring:
    """Sendet die Perl-Displayframes bei Zustandswechseln.

    ``handler`` ist der ``SlimProtoClient`` (``send_visu``/``send_grfb``/
    ``send_display_framebuffer``/``send_grfd_framebuffer``/``send_vfdc``).
    Fehlt ein Sender (Test-Doubles, ältere Handler), passiert schlicht nichts.
    """

    def __init__(self, handler: Any = None) -> None:
        self._handler = handler
        # Squeezebox2.pm:274-276 — ``lastVisMode`` pro Display. Perl vergleicht
        # die Referenz; wir vergleichen den Wert, weil ``visualizerParams()``
        # immer dieselbe Tabellen-Referenz liefert (Squeezebox2.pm:311).
        self._last_visu: dict[str, tuple[int, ...]] = {}
        # Display.pm:378-398 ``currBrightness`` pro Display.
        self._brightness: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Klasse / Fähigkeiten
    # ------------------------------------------------------------------

    def display_class(self, player: Any) -> str:
        """Display-Klasse des Spielers (HELO-Modell + SB1-Bitmap-Capability)."""
        model = getattr(player, "model", "") or ""
        bitmapped = bool(getattr(player, "bitmapped", False))
        return display_class_for(model, bitmapped=bitmapped)

    def brightness_map(self, display_class: str, player: Any = None) -> tuple[int, ...]:
        """Perl ``brightnessMap`` — die u16-Codes, die ``grfb`` verschickt.

        Squeezebox2/Boom-Erbe/Transporter: ``(65535, 0, 1, 3, 4)``
        (Squeezebox2.pm:209-211). SqueezeboxG: ``(0, 1, 4, 16, 30)``
        (SqueezeboxG.pm:135-137). Boom überschreibt die Map sensorabhängig
        (Boom.pm:180-196) — ``maxBrightness`` ist dort 6 (``$#map``).
        Text: ``(0 .. $MAXBRIGHTNESS)`` = ``(0, 1, 2, 3, 4)``
        (``Text.pm:585-591`` mit ``TextVFD.pm:30``); dort geht **kein** ``grfb``
        raus, sondern nur ein Neuzeichnen (``Text.pm:575-580``), die Tabelle
        liefert also bloß die Klemmgrenze 0..4.
        """
        if display_class == SQUEEZEBOXG:
            return (0, 1, 4, 16, 30)
        if display_class == TEXT:
            return tuple(range(fonts.VFD_MAX_BRIGHTNESS + 1))
        if display_class == BOOM:
            # Boom.pm:180-196: (0,1,2,3,4,5, divisor*256 + offset) mit
            # sensAutoBrightness (1..20) und minAutoBrightness (1..7).
            sens = _as_int(_pref(player, "sensAutoBrightness", 1), 1)
            sens = max(1, min(20, sens))
            offset = _as_int(_pref(player, "minAutoBrightness", 1), 1)
            offset = max(1, min(7, offset))
            return (0, 1, 2, 3, 4, 5, (21 - sens) * 256 + offset)
        if display_class in (SQUEEZEBOX2, TRANSPORTER):
            return (65535, 0, 1, 3, 4)
        return ()

    def _show_visualizer(self, player: Any, display_class: str) -> bool:
        """Perl ``showVisualizer``.

        Squeezebox2/Boom: ``playmode eq 'play' || showingNowPlaying``
        (Squeezebox2.pm:252-257). Die zweite Hälfte hängt an den Button-Modi
        (``Buttons/Playlist.pm``, PROT-19) — im Port gibt es die nicht, also
        zählt nur ``playmode``. Transporter: ``$client->power()``
        (Transporter.pm:308-317) plus Digital-Input-Ausnahme (:313-315).
        """
        if display_class == TRANSPORTER:
            return bool(getattr(player, "power", False))
        return getattr(player, "mode", "stop") == "play"

    def visualizer_params(self, player: Any) -> Optional[tuple[int, ...]]:
        """Perl ``visualizerParams`` — ``None``, wenn die Klasse keinen hat.

        Squeezebox2/Boom: Squeezebox2.pm:293-312 (Prefs ``playingDisplayMode``/
        ``playingDisplayModes``, versteckt = ``[0]`` :301-305).
        Transporter: Transporter.pm:297-306 mit ``visualMode``/``visualModes``.
        SqueezeboxG/Text/NoDisplay: kein ``sub visualizer`` -> ``None``.
        """
        display_class = self.display_class(player)
        if display_class in (SQUEEZEBOX2, BOOM):
            if not self._show_visualizer(player, display_class):
                return (0,)                     # :301-305 "hide all visualisers"
            modes = _pref(player, "playingDisplayModes",
                          DEFAULT_PLAYING_DISPLAY_MODES[display_class])
            index = _as_int(_pref(player, "playingDisplayMode",
                                  DEFAULT_PLAYING_DISPLAY_MODE[display_class]), -1)
            if not isinstance(modes, (list, tuple)) or not 0 <= index < len(modes):
                return VISUALIZER_MODES[0]      # :303-305 "not defined/<0 -> 0"
            mode = _as_int(modes[index], 0)
            if not 0 <= mode < len(VISUALIZER_MODES):
                return VISUALIZER_MODES[0]      # :307-309 "> nmodes -> nmodes"
            return VISUALIZER_MODES[mode]
        if display_class == TRANSPORTER:
            if not self._show_visualizer(player, display_class):
                return (0,)                     # :303
            modes = _pref(player, "visualModes", DEFAULT_TRANSPORTER_VISUAL_MODES)
            index = _as_int(_pref(player, "visualMode",
                                  DEFAULT_TRANSPORTER_VISUAL_MODE), 0)
            if not isinstance(modes, (list, tuple)) or not 0 <= index < len(modes):
                return TRANSPORTER_VISUALIZERS[0]
            mode = _as_int(modes[index], 0)
            if not 0 <= mode < len(TRANSPORTER_VISUALIZERS):
                return TRANSPORTER_VISUALIZERS[0]
            return TRANSPORTER_VISUALIZERS[mode]
        return None

    # ------------------------------------------------------------------
    # Nutzlasten (Font-Renderer + TextVFD-Encoder)
    # ------------------------------------------------------------------

    def display_mode(self, player: Any, display_class: Optional[str] = None) -> int:
        """Modusnummer für ``displayWidth`` — ``Squeezebox2.pm:195-203``.

        ``if ($display->showVisualizer() && !defined($client->modeParam('visu')))
        { $mode = $cprefs->get('playingDisplayModes')->[$cprefs->get(
        'playingDisplayMode')]; }``. ``showVisualizer`` wird hier wie in
        :meth:`_show_visualizer` allein aus dem Wiedergabemodus abgeleitet
        (der Button-Modus-Teil ist PROT-19), ``modeParam('visu')`` setzt der
        Port nicht. Transporter/SqueezeboxG ignorieren den Modus
        (``Transporter.pm:192-193`` = 320, ``SqueezeboxG.pm:127-129`` = 280).
        """
        display_class = display_class or self.display_class(player)
        if display_class not in (SQUEEZEBOX2, BOOM):
            return 0
        if not self._show_visualizer(player, display_class):
            return 0
        modes = _pref(player, "playingDisplayModes",
                      DEFAULT_PLAYING_DISPLAY_MODES.get(display_class, (0,)))
        index = _as_int(_pref(player, "playingDisplayMode",
                              DEFAULT_PLAYING_DISPLAY_MODE.get(display_class, 0)), -1)
        if not isinstance(modes, (list, tuple)) or not 0 <= index < len(modes):
            return 0
        return _as_int(modes[index], 0)

    def screen_width(self, player: Any, display_class: Optional[str] = None) -> int:
        """``displayWidth`` in Pixelspalten (``Squeezebox2.pm:188-203``)."""
        display_class = display_class or self.display_class(player)
        return fonts.screen_width(display_class, self.display_mode(player, display_class))

    def vfd_model(self, player: Any) -> str:
        """``$client->vfdmodel`` für Text-Displays — ``Text.pm:85-107``.

        ``SqueezeSlave`` -> ``squeezeslave`` (:100-101); sonst (SB1, das in
        ``Slimproto.pm:1058-1067`` ``Slim::Player::Squeezebox1`` ist, also nicht
        die SLIMP3-Klasse) -> ``noritake-european`` (:105-106). Die
        SLIMP3-MAC-Tabelle (:90-98, futaba/noritake je MAC) bleibt unportiert,
        weil der Port keine SLIMP3-Device-ID kennt
        (``display_class_for`` -> ``NoDisplay``).
        """
        model = (getattr(player, "model", "") or "").lower()
        if model in ("squeezeslave", "softsqueeze"):
            return "squeezeslave" if model == "squeezeslave" else "noritake-european"
        return "noritake-european"

    def _payloads(
        self,
        player: Any,
        display_class: str,
        *,
        bits: Optional[bytes],
        text: Union[str, Sequence[str], None],
        vfd: Optional[bytes],
    ) -> tuple[Optional[bytes], Optional[bytes]]:
        """Rendert fehlende Nutzlasten aus ``text`` (nichts wird erfunden).

        Grafik: ``fonts.render_display_text`` (Font-Renderer, ``Fonts.pm``).
        Text: ``fonts.vfd_update`` (``TextVFD.pm:147-363``) mit dem
        VFD-Modell (:meth:`vfd_model`), der Zeichenbreite
        (``Text.pm:80-83``) und der zuletzt gesetzten Helligkeit
        (``$client->brightness()``, ``Display.pm:378-398``).
        """
        if text is None:
            return bits, vfd
        try:
            if bits is None and display_class in FRAMEBUF_CLASSES:
                bits = fonts.render_display_text(
                    display_class, text, mode=self.display_mode(player, display_class))
            if vfd is None and display_class == TEXT:
                lines = [text] if isinstance(text, str) else list(text)
                mac = str(getattr(player, "mac", ""))
                brightness = self._brightness.get(mac)
                if brightness is None:
                    brightness = self.brightness_for_power(
                        player, bool(getattr(player, "power", False)))
                vfd = fonts.vfd_update(
                    lines[0] if lines else None,
                    lines[1] if len(lines) > 1 else None,
                    model=self.vfd_model(player),
                    width=fonts.TEXT_DISPLAY_WIDTH,
                    brightness=brightness,
                )
        except Exception as exc:  # noqa: BLE001 — kein Displayfehler nach außen
            logger.debug("Display-Nutzlast für %s nicht renderbar: %s",
                         getattr(player, "mac", ""), exc)
            return bits, vfd
        return bits, vfd

    # ------------------------------------------------------------------
    # Zustandswechsel -> Frames
    # ------------------------------------------------------------------

    async def update(
        self,
        player: Any,
        *,
        bits: Optional[bytes] = None,
        text: Union[str, Sequence[str], None] = None,
        vfd: Optional[bytes] = None,
        force_visu: bool = False,
    ) -> list[str]:
        """Perl ``$client->update()`` (``Player.pm:152`` -> ``Display.pm:141``).

        Reihenfolge wie ``Squeezebox2::drawFrameBuf`` (:241 vor :248):
        erst der Visualizer (``visu``), dann der Framebuffer (``grfe`` bzw.
        ``grfd``); Text-Displays bekommen ``vfdc``. ``NoDisplay`` bekommt
        nichts (``NoDisplay.pm:32 sub update {}``).

        ``text`` sind die Displayzeilen (Perl ``line[0..]``); daraus rendert
        :meth:`_payloads` die fehlenden Nutzlasten — ``grfe``/``grfd`` über den
        Font-Renderer (``Fonts.pm:292-522``, ``Graphics.pm:398-414``),
        ``vfdc`` über ``TextVFD.pm:147-363``. ``bits``/``vfd`` überstimmen das
        (fertige Bytes); fehlen beide und ``text``, geht nur der
        Visualizer-Frame raus (wir erfinden keine Bitmap).
        """
        display_class = self.display_class(player)
        mac = str(getattr(player, "mac", ""))
        if display_class == NODISPLAY:
            logger.debug("Display: %s ist NoDisplay — kein Frame (NoDisplay.pm:32)", mac)
            return []

        bits, vfd = self._payloads(player, display_class, bits=bits, text=text, vfd=vfd)

        sent: list[str] = []

        if display_class in VISU_CLASSES:
            params = self.visualizer_params(player)
            if params is not None and (force_visu or self._last_visu.get(mac) != params):
                if await self._call("send_visu", mac, list(params)):
                    sent.append("visu")
                    self._last_visu[mac] = params

        if bits is not None and display_class in FRAMEBUF_CLASSES:
            if display_class == SQUEEZEBOXG:
                # SqueezeboxG.pm:149-167 — eigener 'grfd'-Frame.
                ok = await self._call("send_grfd_framebuffer", mac, bytes(bits))
            else:
                # Squeezebox2.pm:230-250 — offset 0, transition 'c', param 0.
                ok = await self._call("send_display_framebuffer", mac, bytes(bits))
            if ok:
                sent.append("grfd" if display_class == SQUEEZEBOXG else "grfe")

        if vfd is not None and display_class == TEXT:
            # Text.pm:437-441 -> Player/Squeezebox.pm:495-502.
            if await self._call("send_vfdc", mac, bytes(vfd)):
                sent.append("vfdc")

        return sent

    async def power(
        self, player: Any, on: bool,
        *, bits: Optional[bytes] = None, text: Union[str, Sequence[str], None] = None,
        vfd: Optional[bytes] = None,
    ) -> list[str]:
        """Perl ``Player::power`` — ``Player.pm:255-290``.

        Power-Off (:255-259): killAnimation, ``brightness(powerOffBrightness)``,
        Modus ``off``. Power-On (:265-290): Audio-Ausgänge, ``update``,
        ``brightness(powerOnBrightness)`` (min. 1, :283-286), Welcome-Screen.
        Die Helligkeit entfällt bei ``NoDisplay`` ausdrücklich (:283-284).
        """
        display_class = self.display_class(player)
        if display_class == NODISPLAY:
            return []
        sent: list[str] = []
        if on:
            await self.set_brightness(player, self.brightness_for_power(player, True))
            sent += await self.update(player, bits=bits, text=text, vfd=vfd)
        else:
            sent += await self.update(player, bits=bits, text=text, vfd=vfd)
            await self.set_brightness(player, self.brightness_for_power(player, False))
        return sent

    async def on_connect(
        self, player: Any, *, bits: Optional[bytes] = None,
        text: Union[str, Sequence[str], None] = None,
        vfd: Optional[bytes] = None,
    ) -> list[str]:
        """Perl-Connect — ``Player.pm:114-124`` + ``Squeezebox.pm:124-134``.

        ``return if $client->display->isa('Slim::Display::NoDisplay')``
        (Player.pm:114) kommt VOR Helligkeit/Visualizer; danach
        ``brightness(powerOn/OffBrightness)`` (:119) und
        ``$client->display->visualizer(1)`` (Squeezebox.pm:134 — der einzige
        Aufruf mit ``$forceSend``).
        """
        display_class = self.display_class(player)
        if display_class == NODISPLAY:
            return []
        sent: list[str] = []
        await self.set_brightness(player, self.brightness_for_power(
            player, bool(getattr(player, "power", False))))
        sent += await self.update(player, bits=bits, text=text, vfd=vfd, force_visu=True)
        return sent

    async def show_briefly(
        self,
        player: Any,
        *,
        bits: Optional[bytes] = None,
        text: Union[str, Sequence[str], None] = None,
        previous_bits: Optional[bytes] = None,
        previous_text: Union[str, Sequence[str], None] = None,
        duration: Optional[int] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> list[str]:
        """Perl ``Display::showBriefly`` — ``Display.pm:221-327``.

        Zeichnet den Screen (:298) und stellt nach ``duration`` Sekunden den
        alten wieder her (``endShowBriefly`` via Timer, :325). ``duration``
        ist Perls Default 1 s (``Display.pm:258``), identisch mit dem Pref
        ``displaytexttimeout`` = 1 (``Slim/Utils/Prefs.pm:169``).
        Ein Client ohne Display bekommt auch hier nichts (NoDisplay.pm:24-38
        notifiziert nur CLI/Jive).
        """
        from lyrion.networking.protocol import DISPLAY_DURATION_DEFAULT

        if self.display_class(player) == NODISPLAY:
            return []
        delay = DISPLAY_DURATION_DEFAULT if not duration else duration
        sent = await self.update(player, bits=bits, text=text)
        # Perl's restore runs from a timer (Display.pm:325) — only await it when
        # the caller hands in a sleeper (tests); a request path must not block.
        if sleep is not None:
            await sleep(delay)
            if previous_bits is not None or previous_text is not None:
                sent += await self.update(player, bits=previous_bits,
                                          text=previous_text)
        return sent

    def brightness_for_power(self, player: Any, on: bool) -> int:
        """``powerOnBrightness`` / ``powerOffBrightness`` (mit Klassendefault)."""
        display_class = self.display_class(player)
        key = "powerOnBrightness" if on else "powerOffBrightness"
        default = BRIGHTNESS_DEFAULTS.get(display_class, {}).get(key, 0)
        level = _as_int(_pref(player, key, default), default)
        # Player.pm:283-286 — powerOnBrightness < 1 wird auf 1 gehoben.
        if on:
            level = max(1, level)
        return level

    async def set_brightness(self, player: Any, brightness: int) -> Optional[int]:
        """Perl ``$client->brightness($level)``.

        Grafik: ``Graphics.pm:496-509`` -> ``grfb`` mit
        ``brightnessMap[level]``, geklemmt auf 0..``maxBrightness``
        (Display.pm:389-391). Text: ``Text.pm:575-580`` sendet KEINEN Frame,
        sondern zeichnet den Screen neu (das braucht den TextVFD-Encoder).
        ``NoDisplay``: ``NoDisplay.pm:37 sub brightness {}`` -> nichts.
        Gibt den gesendeten u16-Code zurück (oder ``None``).
        """
        display_class = self.display_class(player)
        mac = str(getattr(player, "mac", ""))
        if display_class == NODISPLAY:
            return None
        table = self.brightness_map(display_class, player)
        if not table:
            return None
        level = max(0, min(len(table) - 1, _as_int(brightness, 0)))
        self._brightness[mac] = level
        if display_class not in GRFB_CLASSES:
            # Text.pm:575-580 — kein 'grfb' für Text-Displays.
            return None
        code = table[level]
        if await self._call("send_grfb", mac, code):
            return code
        return None

    # ------------------------------------------------------------------
    # Sender
    # ------------------------------------------------------------------

    async def _call(self, name: str, *args: Any) -> bool:
        """Rufe einen Sender des Handlers auf — fehlt er, ist es ein No-Op.

        Der ``type()``-Check verhindert, dass Test-Doubles (MagicMock) einen
        zufälligen Sender liefern, den man nicht awaiten kann.
        """
        if self._handler is None:
            return False
        sender = getattr(type(self._handler), name, None)
        if sender is None:
            return False
        try:
            result = getattr(self._handler, name)(*args)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception as exc:  # noqa: BLE001 — ein Displayfehler darf nie stören
            logger.debug("Display-Frame %s(%s) fehlgeschlagen: %s",
                         name, ", ".join(str(a) for a in args[1:]), exc)
            return False


__all__ = [
    "BOOM", "DISPLAY_CLASSES", "DisplayWiring", "FRAMEBUF_BYTES",
    "GRAPHICS_FRAMEBUF_LIVE", "NODISPLAY", "SQUEEZEBOX2", "SQUEEZEBOXG",
    "TEXT", "TRANSPORTER", "TRANSPORTER_VISUALIZERS", "VISUALIZER_MODES",
    "display_class_for",
]

# Kleiner Alias für Introspektion/Tests: Modell -> Klasse (ohne SB1-Split).
DISPLAY_CLASSES = DEVICE_ID_DISPLAY_CLASS
