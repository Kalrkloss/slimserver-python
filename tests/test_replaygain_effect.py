"""ReplayGain-Wirkung: fetchGainMode, preventClipping, strm-Feld, Status.

Perl: ``Slim/Player/ReplayGain.pm:22-79`` (Modi), ``:267-278``
(preventClipping), ``Squeezebox2.pm:888-898`` (canDoReplayGain → dBToFixed),
``Squeezebox.pm:914`` (Feld im strm-Frame), ``Queries.pm:4109-4112``
(Statusfeld nur wenn definiert). Defaults ``Squeezebox2.pm:47-49``:
replayGainMode 0, remoteReplayGain -5, localReplayGain 0.

Erwartungswerte per ``perl -e`` gerechnet (nicht abgeleitet):
    pcl(-6, 0.9888) = -6            pcl(+2, 0.9888) = 0.0978308451051875
    pcl(-6, undef)  = -6            pcl(-6, 0)      = -6
    dbf(-6.6) = 30720   dbf(-5) = 36864   dbf(0) = 65536
    (dBToFixed nutzt für -30..0 dB die 8-Bit-Zweige: int(fm*256+0.5)*256)
"""

from __future__ import annotations

from lyrion.player.replaygain import (
    DEFAULT_LOCAL_REPLAY_GAIN,
    DEFAULT_REMOTE_REPLAY_GAIN,
    fetch_gain_mode,
    prevent_clipping,
)


# ── preventClipping (ReplayGain.pm:267-278) ────────────────────────────────


def test_prevent_clipping_matches_perl():
    assert prevent_clipping(-6, 0.9888) == -6            # -6 < noclip → bleibt
    assert abs(prevent_clipping(2, 0.9888) - 0.0978308451051875) < 1e-12
    assert prevent_clipping(-6, None) == -6
    assert prevent_clipping(-6, 0) == -6                 # peak 0 → kein Limit
    assert prevent_clipping(None, 0.5) is None


# ── fetchGainMode (ReplayGain.pm:22-79) ────────────────────────────────────


def test_mode_zero_means_off():
    # Default eines SqueezePlay (Squeezebox2.pm:47)
    assert fetch_gain_mode({}, track_gain=-6.6, track_peak=0.99) is None
    assert fetch_gain_mode({"replayGainMode": 0}, track_gain=-6.6) is None
    assert fetch_gain_mode(None, track_gain=-6.6) is None


def test_mode_one_uses_track_gain():
    prefs = {"replayGainMode": 1}
    assert fetch_gain_mode(prefs, track_gain=-6.6, track_peak=0.98) == -6.6
    # ohne Track-Gain greift localReplayGain (Default 0)
    assert fetch_gain_mode(prefs) == DEFAULT_LOCAL_REPLAY_GAIN
    # Tag in Perls Schreibweise als String
    assert fetch_gain_mode(prefs, track_gain=None) == 0.0


def test_mode_two_uses_album_gain():
    prefs = {"replayGainMode": 2}
    assert fetch_gain_mode(prefs, track_gain=-6.6, album_gain=-3.0) == -3.0
    assert fetch_gain_mode(prefs) is None            # Album ohne Gain → None


def test_remote_streams_use_remote_gain_default():
    prefs = {"replayGainMode": 1}
    # Radio ohne Track-Gain → remoteReplayGain (Default -5)
    assert fetch_gain_mode(prefs, remote=True) == DEFAULT_REMOTE_REPLAY_GAIN
    # mit Track-Gain gewinnt der Track
    assert fetch_gain_mode(prefs, track_gain=-2.5, remote=True) == -2.5
    # eigener Pref-Wert schlägt den Default
    assert fetch_gain_mode({"replayGainMode": 1, "remoteReplayGain": -7},
                           remote=True) == -7


def test_smart_mode_prefers_album_when_neighbours_match():
    prefs = {"replayGainMode": 3}
    assert fetch_gain_mode(prefs, track_gain=-6.6, album_gain=-3.0,
                           neighbours_same_album=True) == -3.0
    # Nachbar aus anderem Album → Track-Gain
    assert fetch_gain_mode(prefs, track_gain=-6.6, album_gain=-3.0,
                           neighbours_same_album=False) == -6.6
    # Album ohne Gain → Track-Gain
    assert fetch_gain_mode(prefs, track_gain=-6.6,
                           neighbours_same_album=True) == -6.6


def test_clipping_limit_applies_to_the_chosen_gain():
    prefs = {"replayGainMode": 1}
    # Perl begrenzt nur, wenn -20*log10(peak) KLEINER als der Gain ist.
    assert fetch_gain_mode(prefs, track_gain=5, track_peak=0.5) == 5.0
    assert abs(fetch_gain_mode(prefs, track_gain=10, track_peak=0.5)
               - 6.020599913279624) < 1e-12


# ── strm-Frame-Feld (Squeezebox.pm:914) ────────────────────────────────────


def test_strm_frame_carries_the_fixed_gain():
    import struct

    from lyrion.networking.protocol import SlimProtoClient, db_to_fixed

    frame = SlimProtoClient._build_stream_frame(
        request=b"GET /stream.mp3 HTTP/1.0\r\n\r\n",
        codec="m", replay_gain=db_to_fixed(-6.6),
    )
    payload = frame[2:]
    assert payload[:4] == b"strm"
    # Aufbau (Perl Squeezebox.pm:1030-1050): strm(4) cmd(1) autostart(1)
    # format(1) pcm(4) threshold(1) spdif(1) transitionDuration(1)
    # transitionType(1) flags(1) outputThreshold(1) slaves(1) replayGain(4)
    # port(2) ip(4) request…
    head = 4 + 1 + 1 + 1 + 4 + 1 + 1 + 1 + 1 + 1 + 1 + 1
    assert struct.unpack(">I", payload[head:head + 4])[0] == 30720   # dbf(-6.6)
    assert struct.unpack(">H", payload[head + 4:head + 6])[0] == 9000


def test_strm_frame_default_is_zero():
    import struct

    from lyrion.networking.protocol import SlimProtoClient

    frame = SlimProtoClient._build_stream_frame(request=b"x")
    payload = frame[2:]
    head = 4 + 1 + 1 + 1 + 4 + 1 + 1 + 1 + 1 + 1 + 1 + 1
    assert struct.unpack(">I", payload[head:head + 4])[0] == 0


def test_replay_gain_fixed_is_zero_in_mode_zero(monkeypatch):
    # Modus 0 (Default) → kein Gain, auch wenn der Track Werte hat
    from lyrion.networking import protocol as proto
    from lyrion.player.manager import PlayerManager
    from lyrion.player.state import PlayerState

    mac = "1C:87:2C:47:FC:36"
    pm = PlayerManager()
    pm.players = {mac: PlayerState(mac=mac, name="T", ip="127.0.0.1", port=0,
                                   playerprefs={"replayGainMode": 0})}
    client = proto.SlimProtoClient.__new__(proto.SlimProtoClient)
    assert client._replay_gain_fixed(mac, 1) == 0
