"""playerpref: Wert ablegen UND anwenden (Perl setChange-Callbacks).

Perl ``prefCommand`` (``Slim/Control/Commands.pm:2631-2677``) schreibt den Wert
nur in den Pref-Store::

    preferences($namespace)->client($client)->set($prefName, $newValue);

Die Wirkung kommt über die registrierten ``setChange``-Callbacks, z. B.
``Slim/Player/Player.pm:79`` (``bass``) — und die Frames lesen die Prefs
später wieder aus (``Squeezebox2.pm:283-303``: dvc-Byte, preamp).

Die neuen Jive-Settings-Menüs senden genau solche ``playerpref``-Kommandos
(``Jive.pm:1242`` digitalVolumeControl, ``:1179`` stereoxl, ``:1207``
analogOutMode, ``Jive.pm:1809`` Brightness, ``:2311`` transitionType, ``:1865``
font ``_curr``). Vorher war der JSON-RPC-Pfad dafür ein No-Op.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lyrion.networking.protocol import SlimProtoClient, audg_preamp
from lyrion.player.manager import PlayerManager
from lyrion.player.playerprefs import apply_player_pref
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"


def _player(**kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(mac=MAC, name="T", ip="127.0.0.1", port=0, model="squeezeplay",
                digital_volume_control=True, preamp_volume_control=0, power=True)
    base.update(kw)
    return PlayerState(**base)


def _pm(player: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {player.mac: player}
    return pm


def _req(command: list[str], player: PlayerState | None = None):
    if player is not None:
        _pm(player)
    return asyncio.run(JSONRPCAPI()._slim_request(MAC, command))


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


# ── Ablegen + Wirkung ──────────────────────────────────────────────────────


def test_digital_volume_control_is_applied():
    p = _player()
    assert apply_player_pref(p, "digitalVolumeControl", 0) == "applied"
    assert p.digital_volume_control is False
    assert p.playerprefs["digitalVolumeControl"] == 0
    assert apply_player_pref(p, "digitalVolumeControl", 1) == "applied"
    assert p.digital_volume_control is True


def test_preamp_volume_control_is_applied_and_lands_in_the_frame():
    p = _player()
    assert apply_player_pref(p, "preampVolumeControl", 10) == "applied"
    assert p.preamp_volume_control == 10
    # Perl Squeezebox2.pm:302 — preamp = 255 - int(2*value)
    assert audg_preamp(p.preamp_volume_control) == 235

    # … und im echten audg-Frame sichtbar (Byte nach dem dvc-Byte)
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC.replace(":", ""): writer}
    _pm(p)
    asyncio.run(client.send_volume_to_player(MAC, 50))
    frame = writer.frames[0]
    payload = frame[2:]
    # Layout: audg | oldL(4) | oldR(4) | dvc(1) | preamp(1) | gainL(4) | gainR(4) | seq
    assert payload[:4] == b"audg"
    assert payload[12] == 1         # dvc: digitalVolumeControl == 1
    assert payload[13] == 235       # preamp aus dem playerpref


def test_digital_volume_control_zero_changes_the_audg_byte():
    p = _player()
    apply_player_pref(p, "digitalVolumeControl", 0)
    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC.replace(":", ""): writer}
    _pm(p)
    asyncio.run(client.send_volume_to_player(MAC, 50))
    assert writer.frames[0][2:][12] == 0


def test_mixer_prefs_are_applied_and_clamped():
    p = _player()
    assert apply_player_pref(p, "bass", 10) == "applied"
    assert p.bass == 10
    assert apply_player_pref(p, "bass", 150) == "clamped"     # Player.pm:366-367
    assert p.bass == 100
    assert apply_player_pref(p, "pitch", 50) == "clamped"      # Client.pm:682-683
    assert p.pitch == 100


def test_unknown_pref_is_only_stored():
    p = _player()
    assert apply_player_pref(p, "analogOutMode", 2) == "stored"
    assert p.playerprefs["analogOutMode"] == 2


# ── JSON-RPC-Pfad (die Jive-Action) ────────────────────────────────────────


def test_jsonrpc_playerpref_applies_the_value():
    p = _player()
    _req(["playerpref", "digitalVolumeControl", "0"], p)
    assert p.digital_volume_control is False


def test_jsonrpc_playerpref_accepts_the_valtag_value_prefix():
    # Jive-Action: cmd ['playerpref','preampVolumeControl'], valtag 'value'
    p = _player()
    _req(["playerpref", "preampVolumeControl", "value:20"], p)
    assert p.preamp_volume_control == 20


def test_jsonrpc_playerpref_without_player_is_a_noop():
    pm = PlayerManager()
    pm.players = {}
    assert asyncio.run(
        JSONRPCAPI()._slim_request(MAC, ["playerpref", "bass", "5"])
    ) == {}


# ── CLI-Pfad ───────────────────────────────────────────────────────────────


def test_cli_playerpref_applies_too():
    from lyrion.control.cli import CLIContext, CLIHandler

    p = _player()
    _pm(p)
    handler = CLIHandler()
    ctx = CLIContext(client_id="t", player_id=MAC)
    asyncio.run(handler.dispatch(ctx, ("playerpref", ["bass", "7"])))
    assert p.bass == 7
    assert p.playerprefs["bass"] == "7"
