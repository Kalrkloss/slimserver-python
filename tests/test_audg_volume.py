"""audg volume → gain curve (Perl parity).

Perl does NOT map the 0..100 volume linearly to the player's 16.16 gain: it
converts through a dB curve, so volume 50 is -24.75 dB (gain 3840/65536 =
0.059), while a linear mapping would send 32768 (0.5) — about 18 dB too loud
and with the whole usable range squeezed into the top third of the knob.

Sources:
* Slim/Player/Squeezebox2.pm:200-211 — @volume_map (old-style gain)
* Squeezebox2.pm:229-239 — getVolumeParameters (totalVolumeRange -50,
  stepPoint -1, stepFraction 1)
* Squeezebox2.pm:241-275 — getVolume (line equation)
* Squeezebox2.pm:213-228 — dBToFixed (16.16 fixed point, 8 extra bits for
  -30..0 dB)
* Squeezebox2.pm:283-303 — volume(): newGain/oldGain/dvc/preamp
* Slim/Player/Player.pm:38-39 — defaults digitalVolumeControl 1,
  preampVolumeControl 0

The expected values below were produced by running the Perl subs themselves
(`perl -e` with @volume_map, getVolume and dBToFixed copied verbatim) — they
are not computed from a paraphrase of the code.
"""

import pytest

from lyrion.networking.protocol import (
    STATIC_GAIN,
    VOLUME_MAP,
    audg_gain,
    audg_old_gain,
    audg_preamp,
    db_to_fixed,
    get_volume_db,
)

# volume → Perl dBToFixed(getVolume(volume)) from the reference run
PERL_GAINS = {
    0: 0,
    1: 232,
    10: 388,
    25: 912,
    50: 3840,
    75: 15872,
    90: 37120,
    100: 65536,
}


@pytest.mark.parametrize("volume,gain", sorted(PERL_GAINS.items()))
def test_gain_matches_the_perl_dB_curve(volume, gain):
    assert audg_gain(volume) == gain


def test_linear_mapping_would_be_wrong():
    # Guard against someone "simplifying" this back to int(volume * 655.36):
    # at volume 50 Perl is ~18 dB below the linear value.
    assert audg_gain(50) != int(50 * 655.36)
    assert audg_gain(50) / STATIC_GAIN == pytest.approx(0.0586, abs=0.001)
    assert audg_gain(100) == STATIC_GAIN          # endpoints do agree
    assert audg_gain(0) == 0                      # mute


def test_volume_map_has_101_entries_and_perl_values():
    assert len(VOLUME_MAP) == 101
    assert VOLUME_MAP[0] == 0
    assert VOLUME_MAP[50] == 46
    assert VOLUME_MAP[100] == 128


def test_old_gain_uses_the_map_not_the_curve():
    assert audg_old_gain(50) == 46
    assert audg_old_gain(0) == 0
    assert audg_old_gain(130) == VOLUME_MAP[100]   # clamped


def test_preamp_default_is_255_and_follows_the_pref():
    # Player.pm:39 preampVolumeControl default 0 → 255
    assert audg_preamp() == 255
    assert audg_preamp(0) == 255
    assert audg_preamp(25) == 205


def test_get_volume_db_endpoints_and_monotonicity():
    assert get_volume_db(100) == pytest.approx(0.0)
    assert get_volume_db(0) == pytest.approx(-50 * 100 / 101, abs=1e-9)
    dbs = [get_volume_db(v) for v in range(101)]
    assert dbs == sorted(dbs)                      # strictly rising
    assert get_volume_db(50) == pytest.approx(-24.7525, abs=0.001)


def test_db_to_fixed_matches_perl_rounding():
    # > -30 dB path keeps 8 extra bits; rounding is int(x + 0.5)
    assert db_to_fixed(0) == 65536
    assert db_to_fixed(-6.0206) == 32768           # half amplitude
    assert db_to_fixed(-40) == int(10 ** (-2) * 65536 + 0.5)
