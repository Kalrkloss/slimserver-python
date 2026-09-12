"""strm `outputThreshold` per source format (Perl stream_s).

Perl fills this byte per format in Slim/Player/Squeezebox.pm `stream_s`
(:579-769): pcm/wav/aif 0 (:602, :622), flac 0 but 20 for samplerate >= 88200
(:645-648) and 20 for Ogg-FLAC (:655), wma 10 / wma-lossless 50 (:682-686),
ogg 20 (:696), ops 20 (:705), alac 0 (:714), mp4/aac 0 (:731), dsf/dff 0
(:740), SqueezePlayDirect 1 (:750), test 0 (:759), mp3 and the DEFAULT branch
1 (:769).

We used to send `1 if codec == "m" else 0`, which gave ogg/opus 0 instead of
20 and wma 0 instead of 10.
"""

import struct

import pytest

from lyrion.networking.protocol import (
    FLAC_HIRES_SAMPLERATE,
    OUTPUT_THRESHOLD_BY_FORMAT,
    SlimProtoClient,
    output_threshold,
)


@pytest.mark.parametrize("codec,expected", sorted(OUTPUT_THRESHOLD_BY_FORMAT.items()))
def test_table_matches_perl_stream_s(codec, expected):
    assert output_threshold(codec) == expected


def test_flac_hires_and_ogf_variants_get_20():
    assert output_threshold("f", samplerate=44100) == 0
    assert output_threshold("f", samplerate=88200) == 20      # :645-648
    assert output_threshold("f", samplerate=192000) == 20
    assert FLAC_HIRES_SAMPLERATE == 88200


def test_unknown_format_falls_back_to_the_mp3_default_branch():
    # Perl: the final `else` assumes MP3 → formatbyte 'm', threshold 1 (:761-769)
    assert output_threshold("") == 1
    assert output_threshold(None) == 1
    assert output_threshold("z") == 1


def test_frame_carries_the_perl_threshold_for_ogg_and_mp3():
    """Wiring check: the byte really lands in the strm body (offset 12)."""
    request = b"GET /stream.mp3?player=021122334455 HTTP/1.0\r\n\r\n"

    ogg = SlimProtoClient._build_stream_frame(
        request=request, codec="o", autostart=1, server_port=9000)
    leaf = ogg[2:][4:28]
    assert leaf[12] == 20

    mp3 = SlimProtoClient._build_stream_frame(
        request=request, codec="m", autostart=1, server_port=9000)
    assert mp3[2:][4:28][12] == 1

    flac_hires = SlimProtoClient._build_stream_frame(
        request=request, codec="f", autostart=1, server_port=9000,
        samplerate=96000)
    assert flac_hires[2:][4:28][12] == 20

    # the frame stays a well-formed 24-byte strm body + request
    assert struct.unpack(">H", mp3[:2])[0] == 4 + 24 + len(request)
