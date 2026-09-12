"""Transcoding-Regelwerk im Streaming-Pfad (Perl ``Slim/Player/Song.pm``).

Perl wendet die ``convert.conf``-Regeln beim **Öffnen des Streams** an, nicht
im Player:

    Song.pm:402-424    ``if (main::TRANSCODING)`` → pro Format
                       ``getConvertCommand2($song, $type, \\@streamFormats, [], \\@wantOptions)``
    Song.pm:426-428    kein Transcoder → ``logError`` + ``PROBLEM_CONVERT_FILE``
    Song.pm:463-468    ``TRANSCODING`` aus → ``{command => '-', streamformat => $format}``
    Song.pm:476-480    ``command eq '-'`` + ``canDirectStream`` → Quellstream
    Song.pm:576-586    echter Befehl → ``tokenizeConvertCommand2`` (externer Prozess)
    Song.pm:630-631    ``$self->_streamFormat($transcoder->{'streamformat'})``
    Song.pm:687-688    ``$self->_streamFormat(...)`` + ``$client->streamformat(...)``
    Song.pm:763        ``sub streamformat`` (Fallback: ``contentType``)
    StreamingController.pm:1316  ``'format' => $song->streamformat()``
    Squeezebox.pm:186/549        ``stream_s($params)`` liest ``$params->{format}``
    Squeezebox.pm:574-781        ``$format`` → ``$formatbyte`` (pcm/wav :597 'p',
                                 flc/ogf :637 'f', Default mp3 :764 'm')
    CapabilitiesHelper.pm:38-59  ``supportedFormats($client)`` (HELO-Caps)
    TranscodingHelper.pm:355-366 @supportedformats + ``prioritizeNative``
    Prefs.pm:205                 ``prioritizeNative => 1``

``Slim/Player/Source.pm`` (Referenz ``d1d0a683``) ruft TranscodingHelper
**nicht** auf — dort liegt nur die Chunk-Pumpe (:107-410).

Unser Port startet KEINEN externen Konverter (``[flac]``/``[sox]``/``[lame]``
aus ``tokenizeConvertCommand2``, TranscodingHelper.pm:519-673). Deshalb gilt:
die Auswahl wird getroffen, protokolliert und im Frame nur dann als anderer Typ
ausgedrückt, wenn wir diesen Typ auch wirklich liefern. Ohne Konverter bleibt
der direkt spielbare Typ stehen — ein Frame für einen Typ, den der Player nie
bekommt, wäre ein toter Stream.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from lyrion.media import transcoding
from lyrion.media.transcoding import (
    format_token_to_perl_type,
    select_stream_rule,
)
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"
TRACK_ID = 47111

# Alle in convert.conf referenzierten Binaries "vorhanden" (checkBin,
# TranscodingHelper.pm:263-307) — damit die Auswahl unabhängig davon ist, was
# auf der Maschine installiert ist.
_ALL_BINS = lambda name: "/usr/bin/" + name       # noqa: E731


# ===========================================================================
# Regelauswahl (reine Funktion, Perl Song.pm:419)
# ===========================================================================

def test_native_type_selects_the_passthrough_profile():
    """Kann der Player den Quelltyp, wählt Perl das ``<type>-<type>-*-*``
    Profil mit Kommando ``-`` (convert.conf:295-296 ``flc flc * *`` →
    ``[flac]`` wird gar nicht gestartet, Song.pm:476-480)."""
    rule = select_stream_rule("flc", ["flac", "mp3", "pcm"], find_bin=_ALL_BINS)
    assert rule.native is True
    assert rule.needs_conversion is False
    assert rule.profile == "flc-flc-*-*"
    assert rule.command == "-"
    assert rule.streamformat == "flc"
    assert rule.reason == "passthrough"
    assert "flc-flc-*-*" in rule.describe()


def test_source_type_is_tried_first_like_prioritize_native():
    """``prioritizeNative => 1`` (Prefs.pm:205, TranscodingHelper.pm:359-366)
    zieht den Quelltyp nach vorn — auch wenn die Caps ihn zuletzt nennen."""
    rule = select_stream_rule("ogg", ["mp3", "ogg"], find_bin=_ALL_BINS)
    assert rule.profile == "ogg-ogg-*-*"      # convert.conf:203-204
    assert rule.needs_conversion is False


def test_client_without_the_type_selects_the_conversion_rule():
    """Kann der Player den Typ NICHT und existiert eine Konvertierung, wird
    sie gewählt und benannt (das ist die Regel, die Perl anwendet)."""
    rule = select_stream_rule("flc", ["pcm"], find_bin=_ALL_BINS)
    assert rule.native is False
    assert rule.needs_conversion is True
    assert rule.profile == "flc-pcm-*-*"        # convert.conf:185-187
    assert rule.streamformat == "pcm"
    assert (rule.command or "").startswith("[flac]")
    assert rule.reason == "convert"
    assert "flc -> pcm" in rule.describe()


def test_missing_binary_rejects_the_rule():
    """Ohne Binary ist das Profil unbrauchbar (``checkBin``,
    TranscodingHelper.pm:263-307 → ``next PROFILE`` :396-397): Perl
    transkodiert dann nicht, und wir dürfen keinen Frame dafür bauen."""
    rule = select_stream_rule("flc", ["pcm"], find_bin=lambda name: None)
    assert rule.needs_conversion is False
    assert rule.profile is None
    assert rule.reason == "no-rule"
    assert "PROBLEM_CONVERT_FILE" in rule.describe()


def test_no_rule_and_no_capabilities_never_convert():
    """(c) unbekannter Typ bzw. Player ohne Caps → keine Konvertierung."""
    unknown = select_stream_rule(None, ["mp3"], find_bin=_ALL_BINS)
    assert unknown.reason == "unknown-source-type"
    assert unknown.needs_conversion is False

    silent = select_stream_rule("mp3", [], find_bin=_ALL_BINS)
    assert silent.reason == "client-declares-no-formats"
    assert silent.needs_conversion is False

    # Typ ohne passende Regel für die angebotenen Formate: Perl würde die
    # Wiedergabe mit PROBLEM_CONVERT_FILE abbrechen (Song.pm:426-428).
    none_matched = select_stream_rule("wvp", ["aac"], find_bin=_ALL_BINS)
    assert none_matched.reason == "no-rule"
    assert none_matched.needs_conversion is False


@pytest.mark.parametrize("token,perl_type", [
    ("flac", "flc"), ("flc", "flc"),        # SqueezePlay.pm:59 vs. types.conf
    ("aiff", "aif"), ("opus", "ops"), ("alac", "alc"),
    ("mp3", "mp3"), ("pcm", "pcm"), ("ogg", "ogg"), ("aac", "aac"),
    ("wma", "wma"), ("dsd", "dsf"), ("musepack", "mpc"),
])
def test_capability_tokens_map_to_the_convert_conf_vocabulary(token, perl_type):
    """convert.conf-Profile werden aus ``$type``/``$checkFormat`` gebaut
    (TranscodingHelper.pm:371-386); Perl-Caps sind dort bereits Perl-Typen."""
    assert format_token_to_perl_type(token) == perl_type


def test_unknown_token_is_not_silently_rewritten():
    assert format_token_to_perl_type("tone") == "tone"


# ===========================================================================
# Streaming-Pfad (strm-Frame)
# ===========================================================================

class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, value):
        self._value = value

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._value)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_track(monkeypatch, url, content_type):
    """Track-Zeile der DB-Abfrage in ``_stream_track_to_player`` ersetzen."""
    from types import SimpleNamespace

    import lyrion.database.sqlite_helper as helper

    track = SimpleNamespace(id=TRACK_ID, url=url, content_type=content_type)
    monkeypatch.setattr(helper, "db_session", lambda: _FakeSession(track))


def _new_player(formats) -> PlayerState:
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.supported_formats = set(formats)
    player.mode = "stop"          # kein Switch → kein 'q'-Frame
    return player


def _make_client(player: PlayerState):
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    client._player_connections = {MAC_CLEAN: 1}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client, writer


def _stream_frames(writer: FakeWriter):
    return [f for f in writer.frames if f[2:7] == b"strms"]


def _codec_byte(frame: bytes) -> str:
    """Formatbyte des strm-Frames (Payload-Offset 6, nach dem 2-Byte-Length)."""
    return frame[8:9].decode()


def _pcm_fields(frame: bytes) -> tuple[str, str, str, str]:
    """pcmsamplesize/-rate/-channels/-endian des Frames (Squeezebox.pm:1024)."""
    size, rate, chan, endian = (frame[9 + i: 10 + i].decode() for i in range(4))
    return size, rate, chan, endian


def _send(client, mac=MAC, track_id=TRACK_ID):
    return asyncio.run(client.send_strm_to_player(mac, track_id))


def _audio_file(tmp_path, name: str):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return path


# ── (a) Player kann den Quelltyp → keine Konvertierung ────────────────────

def test_client_decodes_source_keeps_the_source_frame_byte(
        monkeypatch, tmp_path, caplog):
    """(a) FLAC-fähiger Player: Frame-Byte wie vor der Verdrahtung
    (``format_byte('flc')`` = 'f', Squeezebox.pm:635-637), kein Transcoder."""
    path = _audio_file(tmp_path, "song.flac")
    _patch_track(monkeypatch, "file://" + str(path), "audio/flac")
    player = _new_player({"flac", "mp3", "pcm"})
    client, writer = _make_client(player)
    caplog.set_level(logging.INFO, logger="lyrion.networking.protocol")

    assert _send(client) is True

    frames = _stream_frames(writer)
    assert len(frames) == 1, "genau ein strm 's'-Frame"
    assert _codec_byte(frames[0]) == "f"
    assert b"transcode=1" not in frames[0]
    assert _pcm_fields(frames[0]) == ("?", "?", "?", "?")
    assert "flc-flc-*-*" in caplog.text, "die Regel muss sichtbar sein"


# ── (b) Player kann nicht, Regel vorhanden ────────────────────────────────

def test_rule_found_with_converter_delivers_raw_pcm(monkeypatch, tmp_path,
                                                    caplog):
    """(b1) Player ohne FLAC: Perl wählt ``flc-pcm-*-*`` und schickt das
    Kommando an ``[flac]``; unser Ersatz ist der ffmpeg-PCM-Pfad. Der Frame
    trägt 'p' **plus** die PCM-Parameter — also genau das, was /stream.mp3
    liefert (kein kaputter Frame)."""
    path = _audio_file(tmp_path, "song.flac")
    _patch_track(monkeypatch, "file://" + str(path), "audio/flac")
    player = _new_player({"mp3", "pcm"})          # kein 'flac'
    client, writer = _make_client(player)

    import lyrion.web.stream as stream_mod

    monkeypatch.setattr(stream_mod, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(stream_mod, "ffprobe_audio_info", lambda p: {
        "codec": "flac", "bits": 16, "rate": 44100, "channels": 2,
        "bigendian": False,
    })
    caplog.set_level(logging.INFO, logger="lyrion.networking.protocol")

    assert _send(client) is True

    frames = _stream_frames(writer)
    assert len(frames) == 1
    assert _codec_byte(frames[0]) == "p"          # Squeezebox.pm:595-597
    assert b"transcode=1" in frames[0]
    assert _pcm_fields(frames[0]) == ("1", "3", "2", "1")
    assert "flc-pcm-*-*" in caplog.text, "gewählte Regel + Begründung sichtbar"


def test_rule_found_without_converter_serves_the_source(monkeypatch, tmp_path,
                                                       caplog):
    """(b2) Regel vorhanden, aber kein Konverter: kein Frame für einen Typ,
    den der Player nie bekommt — der Quelltyp bleibt stehen (Perl-Fundstelle
    für den Alternativzweig: Song.pm:476-480 ``command eq '-'``)."""
    path = _audio_file(tmp_path, "song.flac")
    _patch_track(monkeypatch, "file://" + str(path), "audio/flac")
    player = _new_player({"mp3", "pcm"})
    client, writer = _make_client(player)

    import lyrion.web.stream as stream_mod

    monkeypatch.setattr(stream_mod, "ffmpeg_available", lambda: False)
    caplog.set_level(logging.INFO, logger="lyrion.networking.protocol")

    assert _send(client) is True

    frames = _stream_frames(writer)
    assert len(frames) == 1, "der Stream bleibt bestehen (kein toter Stream)"
    assert _codec_byte(frames[0]) == "f"
    assert b"transcode=1" not in frames[0]
    assert "flc-pcm-*-*" in caplog.text


# ── (c) unbekannter Typ → bisheriges Verhalten ────────────────────────────

def test_unknown_type_keeps_the_previous_behaviour(monkeypatch, tmp_path,
                                                   caplog):
    """(c) Kein Perl-Typ → Default-Byte 'm' (Squeezebox.pm:761-764) wie
    bisher, keine Konvertierung."""
    path = _audio_file(tmp_path, "song.bin")
    _patch_track(monkeypatch, "file://" + str(path), None)
    player = _new_player({"mp3", "pcm"})
    client, writer = _make_client(player)
    caplog.set_level(logging.INFO, logger="lyrion.networking.protocol")

    assert _send(client) is True

    frames = _stream_frames(writer)
    assert len(frames) == 1
    assert _codec_byte(frames[0]) == "m"
    assert b"transcode=1" not in frames[0]
    assert "no Perl type" in caplog.text


def test_rule_lookup_is_logged_for_every_decision(monkeypatch, tmp_path,
                                                 caplog):
    """Die Auswahl ist ohne Frame-Inspektion nachvollziehbar (Regeldatei
    convert.conf ist die einzige Quelle: ``Conversions()``,
    TranscodingHelper.pm:39-41)."""
    path = _audio_file(tmp_path, "song.mpc")
    _patch_track(monkeypatch, "file://" + str(path), "audio/x-musepack")
    player = _new_player({"mp3", "pcm"})
    client, _writer = _make_client(player)
    caplog.set_level(logging.INFO, logger="lyrion.networking.protocol")

    assert _send(client) is True

    rule = client._stream_rule_for_track(MAC_CLEAN, "mpc", player)
    assert rule is not None
    assert rule.source_type == "mpc"
    assert "client formats" in rule.describe() or rule.reason == "no-rule"
    assert "transcode rule" in caplog.text


def test_convert_conf_table_is_not_mutated_by_the_lookup():
    """Die Auswahl darf die geladene Tabelle nicht verändern (sie ist der
    Singleton ``get_conversion_tables``)."""
    before = dict(transcoding.get_conversion_tables().command_table)
    select_stream_rule("flc", ["pcm"], find_bin=_ALL_BINS)
    assert transcoding.get_conversion_tables().command_table == before
