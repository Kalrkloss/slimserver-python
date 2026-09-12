"""PROT-18-Verdrahtung: welche Displayframes bei Zustandswechseln rausgehen.

Alle Erwartungen sind aus dem Perl-Referenzbaum (``/tmp/lms-ref``) gelesen;
jede Zeile trägt ``Datei:Zeile``. Die Byte-Muster folgen dem Vorbild aus
``tests/test_display_frames.py`` (Golden-Hex, Rahmenlänge inkl. Opcode).

Perl-Fakten, auf denen die Verdrahtung steht:

* Display-Klasse aus der HELO-Device-ID —
  ``Slim/Networking/Slimproto.pm:1038-1121``.
* Screen-Update ``$client->update()`` — ``Slim/Player/Player.pm:152`` ->
  ``Slim/Display/Display.pm:141-218``; Zustandswechsel rufen es direkt
  (``Player.pm:1115-1116``, ``:1250-1252``).
* ``visu`` — ``Slim/Display/Squeezebox2.pm:259-291`` mit der Modustabelle
  ``:72-129``; ``showVisualizer`` ``:252-257``; ``visualizerParams``
  ``:293-312``; Defaults ``:131-134``. Empfänger sind nur die Squeezebox2-Erben
  (``SqueezeboxG.pm:25`` erbt direkt von ``Graphics.pm`` — dort gibt es
  ``sub visualizer`` nicht).
* ``grfe`` — ``Squeezebox2.pm:230-250`` (offset/transition/param + Bits),
  von ``drawFrameBuf`` VOR dem Frame gerufener ``visualizer()`` (:241).
* ``grfd`` — ``SqueezeboxG.pm:149-167``, Header ``pack('n', 560)`` (:34).
* ``vfdc`` — ``Text.pm:437-441`` -> ``Player/Squeezebox.pm:495-502``.
* ``grfb`` (Helligkeit) — ``Graphics.pm:496-509``; gerufen aus
  ``Player.pm:119/:257/:281-287``; NoDisplay wird ausdrücklich ausgenommen
  (:283-284); Text sendet keinen Frame (``Text.pm:575-580``).
* ``NoDisplay.pm:26-56`` und ``EmulatedSqueezebox2.pm:25-29`` stubb'en alles aus.
* Dauer 1 s — ``Display.pm:258`` + ``Utils/Prefs.pm:169`` (``displaytexttimeout``).
"""

from __future__ import annotations

import asyncio
import struct

from lyrion.networking.protocol import (
    DISPLAY_DURATION_DEFAULT,
    GRAPHICS_FRAMEBUF_LIVE,
    SlimProtoClient,
)
from lyrion.player.display import (
    BOOM,
    DisplayWiring,
    NODISPLAY,
    SQUEEZEBOX2,
    SQUEEZEBOXG,
    TEXT,
    TRANSPORTER,
    VISUALIZER_MODES,
    display_class_for,
)
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

# Erwartete Bytes, mit Perl ``pack`` erzeugt (vgl. tests/test_display_frames.py):
# Squeezebox2.pm:131-134 playingDisplayMode 5 -> playingDisplayModes[5] = 5 ->
# modes[5]{params} = [1,0,0,280,18,302,18] (:72-129) = wie Modus 3 (:86-88).
VISU_MODE5_HEX = (
    "001e"                       # Länge 30 = 4 Opcode + 2 + 6*4
    "76697375"                   # 'visu'
    "0106"                       # pack("CC", which=1, count=6) — :283
    "00000000" "00000000"        # 0, 0
    "00000118" "00000012"        # 280, 18
    "0000012e" "00000012"        # 302, 18
)
VISU_HIDDEN_HEX = "0006" "76697375" "0000"   # [0] -> pack("CC",0,0) :266-268


class _FakeWriter:
    def __init__(self, closing: bool = False) -> None:
        self.frames: list[bytes] = []
        self.closing = closing

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing


def _client_with_writer(closing: bool = False) -> tuple[SlimProtoClient, _FakeWriter]:
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter(closing=closing)
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer


def _player(model: str = "squeezebox2", **kwargs) -> PlayerState:
    return PlayerState(
        mac=MAC, name="Taverne", ip="127.0.0.1", port=3483, model=model, **kwargs
    )


def _payload(frame: bytes) -> bytes:
    (length,) = struct.unpack(">H", frame[:2])
    payload = frame[2:]
    assert length == len(payload), "Rahmenlänge inkl. 4 Opcode-Bytes (Squeezebox.pm:1159)"
    return payload


def _opcodes(writer: _FakeWriter) -> list[str]:
    return [frame[2:6].decode("ascii") for frame in writer.frames]


# ── Modell -> Display-Klasse (Slimproto.pm:1038-1121) ──────────────────────


def test_display_class_table_follows_perl_slimproto():
    assert display_class_for("squeezebox2") == SQUEEZEBOX2          # :1038-1041
    assert display_class_for("softsqueeze") == SQUEEZEBOX2          # :1083-1086
    assert display_class_for("Squeezebox2".upper()) == SQUEEZEBOX2  # case-insensitiv
    assert display_class_for("boom") == BOOM                        # :1048-1051
    assert display_class_for("softboom") == BOOM                    # :1093-1096
    assert display_class_for("transporter") == TRANSPORTER          # :1053-1056
    assert display_class_for("softsqueeze3") == TRANSPORTER         # :1088-1091
    assert display_class_for("squeezeslave") == TEXT                # :1098-1101
    # SB1 ist zweigeteilt: bitmapped -> SqueezeboxG, sonst Text (:1063-1070)
    assert display_class_for("squeezebox", bitmapped=True) == SQUEEZEBOXG
    assert display_class_for("squeezebox", bitmapped=False) == TEXT
    # NoDisplay-Fälle
    assert display_class_for("receiver") == NODISPLAY               # :1043-1046
    assert display_class_for("squeezeplay") == NODISPLAY            # :1103-1106
    assert display_class_for("controller") == NODISPLAY             # :1103-1106
    assert display_class_for("squeezelite") == NODISPLAY  # meldet sich als squeezeplay
    assert display_class_for("baby") == NODISPLAY         # Squeezebox Radio (jive)
    assert display_class_for("http") == NODISPLAY         # HTTP.pm: kein Display
    assert display_class_for("web") == NODISPLAY
    assert display_class_for("quatsch") == NODISPLAY      # unbekannt: nichts raten
    assert display_class_for("") == NODISPLAY


# ── Track-Wechsel: visu + grfe, byte-genau ────────────────────────────────


def test_track_change_sends_visu_then_framebuffer_exact_perl_bytes():
    """Zustandswechsel ``stop`` -> ``play`` mit gerendertem Screen.

    ``Squeezebox2::drawFrameBuf`` ruft ``visualizer()`` (:241) und schickt
    danach ``grfe`` (:243-248) — in dieser Reihenfolge.
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        bits = bytes.fromhex("aabbccdd")

        sent = await wiring.update(player, bits=bits)
        assert sent == ["visu", "grfe"]
        assert writer.frames[0].hex() == VISU_MODE5_HEX
        # grfe: Länge 12 = 4 Opcode + offset(2) + transition(1) + param(1) + 4 Bits
        assert writer.frames[1].hex() == "000c" "67726665" "00006300" "aabbccdd"
        assert _payload(writer.frames[1])[4:8] == bytes.fromhex("00006300")

    asyncio.run(run())


def test_second_track_change_while_playing_sends_only_the_framebuffer():
    """``lastVisMode``-Dedup: ``return`` bei unveränderten Parametern (:274-276)."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        assert await wiring.update(player, bits=b"\x01\x02") == ["visu", "grfe"]
        assert await wiring.update(player, bits=b"\x03\x04") == ["grfe"]
        assert _opcodes(writer) == ["visu", "grfe", "grfe"]

    asyncio.run(run())


def test_pause_hides_the_visualizer_and_stop_keeps_it_hidden():
    """``showVisualizer`` :252-257 -> ``visualizerParams`` :301-305 = ``[0]``."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        await wiring.update(player, bits=b"\x00" * 4)      # visu + grfe
        player.mode = "pause"
        assert await wiring.update(player) == ["visu"]
        assert writer.frames[2].hex() == VISU_HIDDEN_HEX   # [0] -> "0000"
        player.mode = "stop"
        assert await wiring.update(player) == []          # bleibt versteckt

    asyncio.run(run())


def test_transporter_uses_its_own_visualizer_table_and_power_gate():
    """Transporter.pm:297-306 (visualMode/visualModes) + :308-317 (``power()``)."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        # visualMode 2 -> visualModes[2] = 2 -> visualizers[2] ANALOG_VUMETER
        player = _player("transporter", mode="play", power=True)
        assert await wiring.update(player) == ["visu"]
        # params [1,0,1,320,160,480,160] -> "0106" + 6 u32
        assert writer.frames[0].hex() == (
            "001e" "76697375" "0106"
            "00000000" "00000001" "00000140" "000000a0" "000001e0" "000000a0"
        )
        # Transporter.pm:308-317: showVisualizer = $client->power()
        player.power = False
        assert await wiring.update(player) == ["visu"]
        assert writer.frames[1].hex() == VISU_HIDDEN_HEX

    asyncio.run(run())


def test_bitmapped_sb1_gets_grfd_not_grfe_and_no_visualizer():
    """SqueezeboxG: ``grfd`` (:139-167), Header ``pack('n',560)`` (:34/:158).

    Ein zu kurzer Bitmap-Puffer wird mit NULs auf ``screenBytes()`` = 560
    aufgefüllt (:160-166 ``substr($framebuf . chr(0) x screenBytes, …)``).
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox", bitmapped=True, mode="play")

        sent = await wiring.update(player, bits=bytes.fromhex("aabb"))
        assert sent == ["grfd"]                       # kein 'visu' (Graphics.pm)
        frame = writer.frames[0]
        assert frame.hex() == "0236" "67726664" "0230" + "aabb" + "00" * 558
        # Länge 566 = 4 Opcode + 2 Header + 560 Bitmap (Squeezebox.pm:1159)
        assert struct.unpack(">H", frame[0:2])[0] == 4 + 2 + 560
        assert _payload(frame)[4:6] == struct.pack(">H", GRAPHICS_FRAMEBUF_LIVE)
        assert len(_payload(frame)) - 6 == 560        # genau screenBytes()

    asyncio.run(run())


def test_text_display_gets_vfdc_only():
    """Text.pm:437-441 -> TextVFD -> ``Squeezebox.pm:495-502`` (``vfdc``)."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezeslave", mode="play")
        vfd = bytes.fromhex("0233020002300300")
        assert await wiring.update(player, vfd=vfd) == ["vfdc"]
        assert writer.frames[0].hex() == "000c" "76666463" + vfd.hex()
        # Text.pm:575-580 — Helligkeit zeichnet nur neu, kein 'grfb'.
        assert await wiring.set_brightness(player, 4) is None
        assert _opcodes(writer) == ["vfdc"]

    asyncio.run(run())


# ── Clients ohne Display bekommen NICHTS ──────────────────────────────────


def test_no_display_models_receive_nothing_at_all():
    """NoDisplay.pm:26-56 / EmulatedSqueezebox2.pm:25-29 — alles ein No-Op.

    Auch mit gerenderten Bits und bei jedem Zustandswechsel bleibt der
    Socket leer; Perl bricht sogar vorher ab (Player.pm:114, :283-284).
    """
    async def run():
        for model in ("squeezeplay", "controller", "receiver", "squeezelite",
                      "baby", "http", "web", "quatsch"):
            client, writer = _client_with_writer()
            wiring = DisplayWiring(client)
            player = _player(model, mode="play", power=True)
            assert DisplayWiring(client).display_class(player) == NODISPLAY
            assert await wiring.update(player, bits=b"\xff" * 8,
                                       vfd=b"\x01\x02") == []
            assert await wiring.power(player, True) == []
            assert await wiring.power(player, False) == []
            assert await wiring.on_connect(player) == []
            assert await wiring.set_brightness(player, 4) is None
            assert await wiring.show_briefly(player, bits=b"\x01") == []
            assert writer.frames == [], f"{model} darf keinen Frame bekommen"

    asyncio.run(run())


def test_missing_writer_or_closed_socket_sends_nothing():
    """Perl ``$client->opened()`` (Squeezebox.pm:504-514 / Squeezebox2.pm:239)."""
    async def run():
        client = SlimProtoClient.__new__(SlimProtoClient)
        client._player_writers = {}
        wiring = DisplayWiring(client)
        assert await wiring.update(_player(mode="play"), bits=b"\x01\x02") == []
        client, writer = _client_with_writer(closing=True)
        wiring = DisplayWiring(client)
        assert await wiring.update(_player(mode="play"), bits=b"\x01\x02") == []
        assert writer.frames == []

    asyncio.run(run())


# ── Helligkeit / Power (grfb) ─────────────────────────────────────────────


def test_power_sets_brightness_per_perl_defaults():
    """``Player.pm:257`` (off) / ``:281-287`` (on) + ``Graphics.pm:496-509``.

    Defaults: Graphics.pm:39-43 on 4 / off 1 (Squeezebox2, Transporter,
    SqueezeboxG), Boom.pm:117-121 alle 6, Text.pm:40-46 on 4 / off 1.
    """
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="stop")
        await wiring.power(player, False)
        grfb = [f for f in writer.frames if f[2:6] == b"grfb"]
        assert [f.hex() for f in grfb] == ["0006" "67726662" "0000"]   # map[1] = 0
        await wiring.power(player, True)
        grfb = [f for f in writer.frames if f[2:6] == b"grfb"]
        # Player.pm:281-287 — powerOnBrightness 4 -> brightnessMap[4] = 4
        assert [f.hex() for f in grfb] == [
            "0006" "67726662" "0000", "0006" "67726662" "0004"]

        # SqueezeboxG-Map (0,1,4,16,30) — SqueezeboxG.pm:135-137
        client_g, writer_g = _client_with_writer()
        wiring_g = DisplayWiring(client_g)
        await wiring_g.set_brightness(
            _player("squeezebox", bitmapped=True, mode="play"), 4)
        assert writer_g.frames[0].hex() == "0006" "67726662" "001e"  # 30

        # Boom-Map sensorabhängig — Boom.pm:180-196; Defaults 1/1 -> 20*256+1
        client_b, writer_b = _client_with_writer()
        wiring_b = DisplayWiring(client_b)
        player_b = _player("boom", mode="play")
        assert await wiring_b.set_brightness(player_b, 6) == 5121
        assert writer_b.frames[0].hex() == "0006" "67726662" "1401"

    asyncio.run(run())


def test_power_on_brightness_is_raised_to_one_like_perl():
    """``Player.pm:283-286``: ``powerOnBrightness < 1`` wird auf 1 gehoben."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        player.playerprefs["powerOnBrightness"] = 0
        await wiring.power(player, True)
        assert writer.frames[0].hex() == "0006" "67726662" "0000"   # map[1] = 0
        assert wiring.brightness_for_power(player, True) == 1

    asyncio.run(run())


def test_brightness_is_clamped_to_max_brightness():
    """``Display.pm:389-391`` — unter 0 / über ``maxBrightness`` klemmen."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        assert await wiring.set_brightness(player, 99) == 4          # $#map = 4
        assert writer.frames[0].hex() == "0006" "67726662" "0004"
        assert await wiring.set_brightness(player, -3) == 65535      # map[0]
        assert writer.frames[1].hex() == "0006" "67726662" "ffff"

    asyncio.run(run())


# ── Dauer / Intervall (Display.pm:258 + Prefs.pm:169) ─────────────────────


def test_duration_default_is_perls_one_second():
    """``Display.pm:258`` ``$duration = $args->{'duration'} || 1`` und
    ``Utils/Prefs.pm:169`` ``'displaytexttimeout' => 1``."""
    assert DISPLAY_DURATION_DEFAULT == 1


def test_show_briefly_waits_the_perl_duration_and_restores_the_old_screen():
    """``Display.pm:298`` (zeichnen) + ``:325`` (Timer ``duration``) + ``:329``."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        waits: list[float] = []

        async def fake_sleep(delay: float) -> None:
            waits.append(delay)

        await wiring.update(player)            # Visualizer einmal (:274-276)
        writer.frames.clear()
        sent = await wiring.show_briefly(
            player, bits=b"\xaa\xbb", previous_bits=b"\xcc\xdd",
            sleep=fake_sleep,
        )
        assert waits == [1], "Perls Default = 1 s (Display.pm:258/Prefs.pm:169)"
        assert sent == ["grfe", "grfe"]        # hin und zurück (:298, :349-365)
        assert _payload(writer.frames[0])[8:] == b"\xaa\xbb"
        assert _payload(writer.frames[1])[8:] == b"\xcc\xdd"
        # explizite Dauer wird unverändert weitergereicht
        await wiring.show_briefly(player, bits=b"\x01", duration=5, sleep=fake_sleep)
        assert waits == [1, 5]

    asyncio.run(run())


def test_show_briefly_uses_the_wiring_without_blocking():
    """Ohne ``sleep`` darf der Request-Pfad nicht warten (CLI ``display``)."""
    async def run():
        client, writer = _client_with_writer()
        wiring = DisplayWiring(client)
        player = _player("squeezebox2", mode="play")
        assert await wiring.show_briefly(player, bits=b"\x01\x02") == ["visu", "grfe"]

    asyncio.run(run())


# ── Verdrahtung im Manager ────────────────────────────────────────────────


def test_manager_display_on_connect_sends_brightness_and_visualizer():
    """HELO-Pfad: ``Player.pm:114-124`` + ``Squeezebox.pm:124-134``.

    ``Squeezebox.pm:127`` ``brightness(powerOnBrightness)`` und :134
    ``visualizer(1)`` (der einzige ``$forceSend``-Aufruf).
    """
    async def run():
        client, writer = _client_with_writer()
        player = _player("squeezebox2", mode="stop", power=True)
        pm = object.__new__(PlayerManager)
        pm._initialized = True
        pm.players = {MAC_CLEAN: player}
        pm._protocol_handler = client
        pm._display_wiring = None
        PlayerManager._instance = pm

        sent = await pm.display_on_connect(MAC)
        assert sent == ["visu"], "nur der Visualizer geht ohne Renderer raus"
        assert _opcodes(writer) == ["grfb", "visu"]
        assert writer.frames[0].hex() == "0006" "67726662" "0004"  # on: map[4]

        # NoDisplay bricht ab (Player.pm:114) — nichts, nicht einmal grfb.
        client_sp, writer_sp = _client_with_writer()
        pm.players = {MAC_CLEAN: _player("squeezeplay", mode="stop", power=True)}
        pm._protocol_handler = client_sp
        pm._display_wiring = None
        assert await pm.display_on_connect(MAC) == []
        assert writer_sp.frames == []

    asyncio.run(run())


def test_manager_play_track_triggers_the_display_update():
    """``Player.pm:1115-1116``/:1250-1252 — Track-Start aktualisiert den Screen."""
    class _Handler:
        def __init__(self) -> None:
            self.mac = MAC
            self.frames: list[str] = []
            self.writer = _FakeWriter()
            self._player_writers = {MAC_CLEAN: self.writer}

        async def send_strm_to_player(self, mac, track_id):
            self.frames.append("strm")
            return True

        # Die beiden Display-Sender des echten Clients (send_visu liest
        # ``self._player_writers``, deshalb setzt der Fake sie oben).
        send_visu = SlimProtoClient.send_visu
        send_grfb = SlimProtoClient.send_grfb

    async def run():
        handler = _Handler()
        player = _player("squeezebox2", mode="stop")
        pm = object.__new__(PlayerManager)
        pm._initialized = True
        pm.players = {MAC_CLEAN: player}
        pm._protocol_handler = handler
        pm._display_wiring = None
        PlayerManager._instance = pm

        assert await pm.play_track(MAC, 4242) is True
        assert player.mode == "play"
        # Player.pm:1115-1116/:1250-1252 — der Track-Start aktualisiert den
        # Screen; ohne Renderer bleibt davon der Visualizer (und beim
        # Power-On zusätzlich die Helligkeit, Player.pm:281-287).
        assert "visu" in _opcodes(handler.writer)
        assert "grfb" in _opcodes(handler.writer)
        assert "strm" in handler.frames

    asyncio.run(run())


def test_visualizer_modes_table_matches_perl_lines():
    """Die Modustabelle Squeezebox2.pm:72-129 in Kurzform."""
    assert VISUALIZER_MODES[0] == (0,)                       # BLANK :74-76
    assert VISUALIZER_MODES[3] == (1, 0, 0, 280, 18, 302, 18)  # VUMETER_SMALL :86-88
    assert VISUALIZER_MODES[6] == (2, 1, 1, 0x10000, 280, 40, 0, 4, 1, 0, 1, 3)  # :98-100
    assert VISUALIZER_MODES[9][:4] == (2, 0, 0, 0x10000)     # SPECTRUM :110-112
    assert len(VISUALIZER_MODES) == 14                       # :72-129
