"""Format detection + player capabilities, straight from the Perl LMS.

Everything asserted here has a Perl source:

* ``types.conf`` (loaded by Slim/Music/Info.pm:92) — suffix/MIME -> type.
* ``Slim/Player/Squeezebox.pm:546-770`` ``stream_s`` — type -> strm byte,
  plus the ``pcmsamplesize`` AAC container type ('5' mp4ff / '2' adts).
* ``Slim/Player/SqueezePlay.pm:170-200`` — a player declares its codecs as
  bare capability tokens; ``:59`` is the static fallback list.

Live finding 2026-09-12: a mixed-format battery found four tracks that never
started. Causes: the DB's content_type was used instead of the suffix
(``.opus`` stored as ``audio/ogg`` -> Vorbis decoder on Opus data;
``.mpc`` Musepack stored as ``chemical/x-mopac-input`` -> streamed as mp3;
``.mp4`` video -> mp3), the AAC container type was missing, and the player's
capabilities were never read (so Musepack/WMA were streamed raw).
"""

from __future__ import annotations

import pytest

from lyrion.formats import lms_types as lt
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import formats_from_capabilities
from lyrion.player.state import PlayerState

# The real capability string our SqueezePlay sends (captured 2026-09-12,
# 173 chars, from the HELO debug log line).
LIVE_SQUEEZEPLAY_CAPS = (
    "Model=squeezeplay,ModelName=SqueezePlay,Firmware=9.0.0-r1583,"
    "Balance=1,alc,aac,ogg,ogf,flc,aif,pcm,mp3,MaxSampleRate=384000,"
    "AccuratePlayPoints,ImmediateCrossfade,test,Rtmp=2"
)


@pytest.mark.parametrize("path,expected", [
    ("/music/foo.mp3", "mp3"),
    ("/music/foo.FLAC", "flc"),
    ("/music/foo.fla", "flc"),
    ("/music/foo.ogg", "ogg"),
    ("/music/foo.oga", "ogg"),
    ("/music/foo.opus", "ops"),
    ("/music/foo.m4a", "mp4"),
    ("/music/foo.mp4", "mp4"),
    ("/music/foo.aac", "aac"),
    ("/music/foo.wma", "wma"),
    ("/music/foo.mpc", "mpc"),
    ("/music/foo.wav", "wav"),
    ("/music/foo.aif", "aif"),
    ("/music/foo.dsf", "dsf"),
    ("/music/foo.dff", "dff"),
    ("file:///m/My%20Song.opus", "ops"),
    ("/music/noextension", None),
])
def test_type_from_suffix_matches_types_conf(path, expected):
    assert lt.type_from_suffix(path) == expected


def test_suffix_wins_over_the_db_mime_type():
    """The live DB stores .opus as audio/ogg and .mpc as
    chemical/x-mopac-input (both wrong). Perl decides by suffix first."""
    assert lt.describe_type("/m/Shadow Dance.opus", "audio/ogg") == "ops"
    assert lt.describe_type("/m/Heliopolis.mpc",
                            "chemical/x-mopac-input") == "mpc"
    assert lt.describe_type("/m/Monster.m4a", "audio/mp4") == "mp4"


@pytest.mark.parametrize("mime,expected", [
    ("audio/ogg;codecs=opus", "ops"),        # types.conf: ops
    ("audio/ogg;codecs=flac", "ogf"),        # types.conf: ogf
    ("audio/x-ms-wma", "wma"),
    ("audio/x-musepack", "mpc"),
    ("audio/x-m4a-lossless", "alc"),
    ("application/ogg", "ogg"),
])
def test_type_from_mime_incl_parameterised_entries(mime, expected):
    assert lt.type_from_mime(mime) == expected


@pytest.mark.parametrize("perl_type,byte", [
    ("mp3", "m"), ("flc", "f"), ("ogf", "f"), ("ogg", "o"), ("ops", "u"),
    ("alc", "l"), ("aac", "a"), ("mp4", "a"), ("wma", "w"), ("wav", "p"),
    ("aif", "p"), ("pcm", "p"), ("dsf", "d"), ("dff", "d"), ("test", "n"),
])
def test_format_byte_matches_stream_s(perl_type, byte):
    # Slim/Player/Squeezebox.pm:560-770
    assert lt.format_byte(perl_type) == byte


def test_unknown_format_falls_back_to_the_mp3_byte():
    # stream_s else-branch: "assume MP3" (Squeezebox.pm:762)
    assert lt.format_byte(None) == "m"
    assert lt.format_byte("musepack") == "m"


@pytest.mark.parametrize("perl_type,samplesize", [
    ("mp4", "5"),      # mp4ff  (Squeezebox.pm:715, wantFormat ne aac)
    ("aac", "2"),      # adts   (Squeezebox.pm:715, raw .aac stream)
    ("ogg", "?"),
    ("flc", "?"),
])
def test_pcm_samplesize_carries_the_aac_container_type(perl_type, samplesize):
    assert lt.pcm_samplesize_for(perl_type) == samplesize


def test_live_squeezeplay_capabilities_are_parsed_like_perl():
    formats = formats_from_capabilities(LIVE_SQUEEZEPLAY_CAPS, "squeezeplay")
    assert {"alac", "aac", "ogg", "flac", "aiff", "pcm", "mp3"} <= formats
    # SqueezePlay declares no wma / opus / dsd / musepack ...
    assert "wma" not in formats
    assert "opus" not in formats
    assert "musepack" not in formats


def test_musepack_is_transcoded_but_aac_is_not():
    """The two decisions the mixed-format battery exposed."""
    player = PlayerState(mac="1C:87:2C:47:FC:36", name="Taverne",
                         ip="127.0.0.1", port=1234)
    player.supported_formats = formats_from_capabilities(
        LIVE_SQUEEZEPLAY_CAPS, "squeezeplay")

    # declared formats stream directly
    assert SlimProtoClient._player_can_decode(player, "m", perl_type="mp3")
    assert SlimProtoClient._player_can_decode(player, "f", perl_type="flc")
    assert SlimProtoClient._player_can_decode(player, "o", perl_type="ogg")
    assert SlimProtoClient._player_can_decode(player, "a", perl_type="mp4")
    assert SlimProtoClient._player_can_decode(player, "l", perl_type="alc")
    # not declared -> ffmpeg transcode
    assert not SlimProtoClient._player_can_decode(player, "m", perl_type="mpc")
    assert not SlimProtoClient._player_can_decode(player, "w", perl_type="wma")
    assert not SlimProtoClient._player_can_decode(player, "u", perl_type="ops")


def test_capabilities_without_codec_tokens_use_the_model_list():
    """Perl SqueezePlay.pm:59 fallback: ogg flc aif pcm mp3 (no aac/wma)."""
    caps = "Model=squeezeplay,ModelName=SqueezePlay,Balance=1,MaxSampleRate=48000"
    formats = formats_from_capabilities(caps, "squeezeplay")
    assert formats == {"ogg", "flac", "aiff", "pcm", "mp3"}


def test_classic_hardware_lists_match_perl():
    # Squeezebox1.pm:282
    assert formats_from_capabilities(None, "squeezebox1") == {"aiff", "pcm", "mp3"}
    # Squeezebox2.pm:137 (also the base for SB2/SB3/Boom/Transporter)
    sb2 = formats_from_capabilities(None, "squeezebox2")
    assert sb2 == {"wma", "ogg", "flac", "aiff", "pcm", "mp3"}
    # SLIMP3.pm:285-287
    assert formats_from_capabilities(None, "slimp3") == {"mp3"}
