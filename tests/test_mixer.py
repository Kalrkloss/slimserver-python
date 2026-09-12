"""mixer-Befehl/-Query wie Perl (CTRL-01: bass/treble/pitch/muting).

Perl-Quellen:
* Query ``mixerQuery`` — ``Slim/Control/Queries.pm:2118-2141``: gültige
  Entities ``volume, muting, treble, bass, pitch``; Antwort ``_<entity>`` mit
  dem Pref-/Getter-Wert.
* Command ``mixerCommand`` — ``Slim/Control/Commands.pm:545-665``: führendes
  ``+``/``-`` ist RELATIV (``:601-606``), Werte werden auf den Bereich des
  Players geklemmt, ``muting`` toggelt und ist eine TEMPORÄRE Verstärkung 0
  (``fade_volume(±0.3125)``, ``tempVolume``).
* Bereiche: bass/treble 0..100 (``Player.pm:366-367``), pitch ist für Player
  ohne echte Pitch-Steuerung auf 100 festgenagelt
  (``Client.pm:682-683`` maxPitch == minPitch == 100), volume 0..100
  (``Player.pm:360-361``).

Ground Truth (read-only Probe Perl-LMS 2026-09-12):
``mixer volume ?`` → ``{"_volume": "23"}``, ``mixer bass ?`` → ``{"_bass": "0"}``,
``mixer treble ?`` → ``{"_treble": "0"}``, ``mixer pitch ?`` → ``{"_pitch": "100"}``;
``mixer ?`` (unbekannte Entity) → gar keine Antwort.
"""

from __future__ import annotations

import asyncio

from lyrion.web.api import JSONRPCAPI, _mixer_new_value, _mixer_range

MAC = "1C:87:2C:47:FC:36"


class _Player:
    mac = MAC

    def __init__(self) -> None:
        self.volume = 23
        self.bass = 0
        self.treble = 0
        self.pitch = 100
        self.mute = False
        self.power = True


class _FakePM:
    def __init__(self, player: _Player) -> None:
        self.player = player
        self.volumes: list[int] = []

    def get_player(self, mac):
        return self.player if mac == MAC else None

    async def set_volume(self, mac, volume):
        self.volumes.append(volume)
        return True

    def send_command(self, mac, cmd):
        raise AssertionError(f"unexpected raw send: {cmd}")


def _run(coro):
    return asyncio.run(coro)


# ── Helfer (Perl-Semantik) ─────────────────────────────────────────────────


def test_relative_and_absolute_values():
    # Commands.pm:601-606
    assert _mixer_new_value(50, "+3") == 53
    assert _mixer_new_value(50, "-3") == 47
    assert _mixer_new_value(50, "30") == 30
    assert _mixer_new_value(50, "abc") is None


def test_ranges_match_perl():
    assert _mixer_range("bass") == (0, 100)      # Player.pm:366-367
    assert _mixer_range("treble") == (0, 100)
    assert _mixer_range("pitch") == (100, 100)   # Client.pm:682-683
    assert _mixer_range("volume") == (0, 100)


# ── Query-Zweig (Queries.pm:2118-2141) ─────────────────────────────────────


def test_mixer_query_returns_perl_shape():
    from lyrion.web.api import _mixer_value

    p = _Player()
    assert _mixer_value(p, "volume") == 23
    assert _mixer_value(p, "muting") == 0
    assert _mixer_value(p, "bass") == 0
    assert _mixer_value(p, "treble") == 0
    assert _mixer_value(p, "pitch") == 100
    # unbekannte Entity: Perl bad dispatch
    assert _mixer_value(p, "stereoxl") is None
    assert _mixer_value(p, "?") is None


def test_mixer_query_values_are_strings_end_to_end(monkeypatch):
    p = _Player()
    monkeypatch.setattr(
        "lyrion.web.api._mixer_value", lambda player, entity: 23 if entity == "volume" else None
    )
    res = _run(JSONRPCAPI()._slim_request(MAC, ["mixer", "volume", "?"]))
    assert res == {"_volume": "23"}      # String, wie Perl


def test_mixer_unknown_entity_returns_empty_result():
    res = _run(JSONRPCAPI()._slim_request(MAC, ["mixer", "?", "?"]))
    assert res == {}


# ── Command-Zweig (Commands.pm:545-665) ────────────────────────────────────


def test_mixer_bass_absolute_and_relative():
    p = _Player()
    pm = _FakePM(p)
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "mixer", ["bass", "30"]))
    assert p.bass == 30
    _run(api._json_control(pm, MAC, "mixer", ["bass", "+5"]))
    assert p.bass == 35
    _run(api._json_control(pm, MAC, "mixer", ["bass", "-10"]))
    assert p.bass == 25


def test_mixer_bass_is_clamped_to_perls_range():
    p = _Player()
    pm = _FakePM(p)
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "mixer", ["bass", "150"]))
    assert p.bass == 100
    _run(api._json_control(pm, MAC, "mixer", ["bass", "-500"]))
    assert p.bass == 0


def test_mixer_pitch_stays_at_100_for_players_without_pitch_control():
    p = _Player()
    pm = _FakePM(p)
    _run(JSONRPCAPI()._json_control(pm, MAC, "mixer", ["pitch", "50"]))
    assert p.pitch == 100


def test_mixer_volume_absolute_and_relative_sends_audg():
    p = _Player()
    pm = _FakePM(p)
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "mixer", ["volume", "40"]))
    _run(api._json_control(pm, MAC, "mixer", ["volume", "+5"]))
    assert p.volume == 45
    assert pm.volumes == [40, 45]


def test_mixer_muting_toggles_gain_but_keeps_the_volume_pref():
    p = _Player()
    pm = _FakePM(p)
    api = JSONRPCAPI()
    # mute: gain 0, Volume-Pref bleibt (Perl tempVolume)
    _run(api._json_control(pm, MAC, "mixer", ["muting", "1"]))
    assert p.mute is True
    assert p.volume == 23
    assert pm.volumes == [0]
    # unmute: der unveränderte Volume-Pref wird wieder gesendet
    _run(api._json_control(pm, MAC, "mixer", ["muting", "0"]))
    assert p.mute is False
    assert pm.volumes == [0, 23]
    # toggle in beide Richtungen
    _run(api._json_control(pm, MAC, "mixer", ["muting", "toggle"]))
    assert p.mute is True
    _run(api._json_control(pm, MAC, "mixer", ["muting", "toggle"]))
    assert p.mute is False
