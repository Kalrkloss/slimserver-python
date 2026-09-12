"""audg-Gain je Playerklasse — jive vergleicht gegen seine eigene Tabelle.

Der Client rechnet den empfangenen Gain gegen die Tabelle seines eigenen
Player-Modells und erklärt bei Abweichung „server sequence # out of sync"
(`share/jive/jive/audio/Playback.lua`, `_serverVolumeToGain`), revertiert die
Lautstärke und schickt sie erneut — im Fehlerfall eine Endlosschleife
(live 2026-09-12: ~300k `mixer volume`/`power`-Requests in Minuten).

Perl:
* ``Squeezebox2.pm:229-239`` getVolumeParameters = -50 dB, stepPoint -1,
  step Fraction 1 (klassische Squeezebox2/3).
* ``SqueezePlay.pm:219-226`` und ``Boom.pm:131-139`` = -74 dB, stepPoint 25,
  stepFraction 0.5 (jive nutzt die Boom-Kurve, im Lua steht die SB2-Kurve
  auskommentiert direkt darunter).

Die Tabelle unten ist das Original aus der installierten jive-Quelle
(`share/jive/jive/audio/Playback.lua`, `_defaultVolumeToGain`) — sie ist die
Ground Truth, gegen die der Client prüft.
"""

from __future__ import annotations

from lyrion.networking.protocol import (
    VOLUME_PARAMS_BOOM,
    VOLUME_PARAMS_SQUEEZEBOX2,
    audg_gain,
    volume_params_for,
)

# _defaultVolumeToGain (Boom-Kurve) aus Playback.lua, Index = Volume 1..100
JIVE_TABLE = [
    16, 18, 22, 26, 31, 36, 43, 51, 61, 72,
    85, 101, 120, 142, 168, 200, 237, 281, 333, 395,
    468, 555, 658, 781, 926, 980, 1037, 1098, 1162, 1230,
    1302, 1378, 1458, 1543, 1634, 1729, 1830, 1937, 2050, 2048,
    2304, 2304, 2560, 2816, 2816, 3072, 3328, 3328, 3584, 3840,
    4096, 4352, 4608, 4864, 5120, 5376, 5632, 6144, 6400, 6656,
    7168, 7680, 7936, 8448, 8960, 9472, 9984, 10752, 11264, 12032,
    12544, 13312, 14080, 14848, 15872, 16640, 17664, 18688, 19968, 20992,
    22272, 23552, 24832, 26368, 27904, 29696, 31232, 33024, 35072, 37120,
    39424, 41728, 44032, 46592, 49408, 52224, 55296, 58624, 61952, 65536,
]


def test_squeezeplay_curve_matches_the_jive_table_exactly():
    for volume, expected in enumerate(JIVE_TABLE, start=1):
        assert audg_gain(volume, "squeezeplay") == expected, f"volume {volume}"


def test_boom_and_controller_use_the_same_curve_as_squeezeplay():
    for model in ("boom", "softboom", "controller"):
        for volume in (1, 25, 26, 47, 50, 79, 100):
            assert audg_gain(volume, model) == audg_gain(volume, "squeezeplay")


def test_squeezebox2_curve_is_a_different_curve():
    # Genau diese Verwechslung war der Live-Bug: bei 47 liefern die Kurven
    # verschiedene Gains, bei 50/100 zufällig dieselben.
    assert audg_gain(47, "squeezebox2") == 3072
    assert audg_gain(47, "squeezeplay") == 3328
    assert audg_gain(50, "squeezebox2") == audg_gain(50, "squeezeplay") == 3840
    assert audg_gain(100, "squeezebox2") == audg_gain(100, "squeezeplay") == 65536


def test_curve_selection_is_per_model():
    assert volume_params_for("squeezeplay") == VOLUME_PARAMS_BOOM
    assert volume_params_for("controller") == VOLUME_PARAMS_BOOM
    assert volume_params_for("boom") == VOLUME_PARAMS_BOOM
    assert volume_params_for("squeezebox2") == VOLUME_PARAMS_SQUEEZEBOX2
    assert volume_params_for("squeezelite") == VOLUME_PARAMS_SQUEEZEBOX2
    assert volume_params_for("") == VOLUME_PARAMS_SQUEEZEBOX2


def test_mute_stays_zero_on_every_curve():
    for model in ("squeezeplay", "squeezebox2", "boom"):
        assert audg_gain(0, model) == 0
