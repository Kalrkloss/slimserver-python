"""PROT-19: IR/button opcodes -> actions, pinned against the Perl reference.

Perl sources (read-only pinned tree, every assertion carries a file:line):

* ``Slim/Networking/Slimproto.pm:52-72`` ``%message_handlers``; ``:521-546``
  ``_ir_handler`` (10-byte payload, ``unpack('NxxH8')`` :539); ``:1270-1280``
  ``_button_handler`` (``unpack('NH8')`` :1275 — the SAME ``IR::enqueue``);
  ``:1282-1318`` ``_knob_handler`` (``unpack('NNC')`` :1287, sign bit :1290).
* ``Slim/Hardware/IR.pm:43`` press styles; ``:85-140`` enqueue/idle;
  ``:435-455`` lookupCodeBytes (unknown -> 'unknownir' :644-651);
  ``:459-527`` lookup/lookupFunction (mode then 'common');
  ``:691-704`` front-panel ``.up``/``.down``; ``:798-877`` processFrontPanel;
  ``:1052-1114`` executeButton (unknown -> warn :1106-1112); ``:1116-1126``
  processCode -> ``['button', ...]``.
* ``Slim/Buttons/Common.pm:1338-1360`` getFunction (``X_Y`` -> sub ``X``);
  ``:266`` dead, ``:268-294`` fwd/rew, ``:296-337`` jump, ``:369-383`` pause,
  ``:385-408`` stop, ``:915-947`` repeat, ``:1008-1015`` muting,
  ``:1017-1033`` snooze, ``:1035-1113`` sleep, ``:1115-1130`` power,
  ``:1132-1166`` shuffle. ``Slim/Buttons/Power.pm:33-38`` play in the off
  mode. ``Slim/Buttons/Volume.pm:89-104`` volume mode (increment 1 at :99).
* ``IR/Default.map`` + ``IR/Slim_Devices_Remote.ir``/``Front_Panel.ir``/
  ``jvc_dvd.ir`` — the tables, loaded by ``IR.pm:359-394`` /
  ``:300-357``.
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.networking.protocol import (
    PERL_BUTTON_OPCODES,
    PERL_HANDLER_NAMES,
    SlimProtoClient,
)
from lyrion.player import buttons as B
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

# IR codes from IR/Slim_Devices_Remote.ir (button = code)
CODE_PLAY = "768910EF"
CODE_PAUSE = "768920DF"
CODE_REW = "7689C03F"
CODE_FWD = "7689A05F"
CODE_POWER = "768940BF"
CODE_VOLUP = "7689807F"
CODE_VOLDOWN = "768900FF"
CODE_SLEEP = "7689B847"
CODE_SHUFFLE = "7689D827"
CODE_REPEAT = "768938C7"
CODE_MUTE = "7689C43B"


class FakeManager:
    """Minimal stand-in for PlayerManager (records every action)."""

    def __init__(self, player: PlayerState):
        self.player = player
        self.calls: list[tuple] = []

    def get_player(self, mac):
        return self.player

    def set_power(self, mac, on):
        self.calls.append(("set_power", on))
        self.player.power = on

    async def set_volume(self, mac, volume):
        self.calls.append(("set_volume", volume))
        self.player.volume = volume
        return True

    async def pause_player(self, mac, pause):
        self.calls.append(("pause", pause))
        self.player.mode = "pause" if pause else "play"
        return True

    async def stop_player(self, mac):
        self.calls.append(("stop",))
        self.player.mode = "stop"
        return True

    async def playlist_next(self, mac):
        self.calls.append(("playlist_jump", "+1"))
        return True

    async def playlist_prev(self, mac):
        self.calls.append(("playlist_jump", "-1"))
        return True

    async def playlist_jump(self, mac, index):
        self.calls.append(("playlist_jump", str(index)))
        return True

    async def play_track(self, mac, track_id):
        self.calls.append(("play_track", track_id))
        return True

    async def play_url(self, mac, url, title=""):
        self.calls.append(("play_url", url))
        return True


def _player(**kw) -> PlayerState:
    p = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    p.playlist = [11, 22]
    p.current_track_id = 11
    p.power = True
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ---------------------------------------------------------------------------
# Wire formats (Slimproto.pm)
# ---------------------------------------------------------------------------

def test_ir_frame_is_10_bytes_time_then_hex_code():
    # :539 unpack('NxxH8') -> time = 4 BE bytes, 2 skipped, 4 bytes as hex.
    payload = bytes([0, 0, 0, 5, 0x00, 0x09]) + bytes.fromhex(CODE_PLAY.lower())
    assert B.parse_ir_frame(payload) == (5, CODE_PLAY)


def test_ir_frame_of_other_length_is_ignored():
    # :530-537 "bad length ... for IR. Ignoring"
    for n in (0, 8, 9, 11, 32):
        assert B.parse_ir_frame(b"\x00" * n) is None
    assert B.IR_FRAME_LEN == 10


def test_butn_frame_uses_the_same_hex_code_form():
    # :1275 unpack('NH8') — time(4) + code(4)
    payload = bytes([0, 0, 0, 7]) + bytes.fromhex(CODE_PAUSE.lower())
    assert B.parse_butn_frame(payload) == (7, CODE_PAUSE)


def test_knob_frame_position_is_signed():
    # :1287-1292 'NNC', negative when bit 31 is set. Perl negates the LOW 31
    # bits: -(x & 0x7fffffff), so 0x80000005 -> -5 and 0xffffffff ->
    # -0x7fffffff (not -1).
    assert B.parse_knob_frame(bytes([0, 0, 0, 1, 0, 0, 0, 5, 9])) == (1, 5, 9)
    neg5 = (0x80000005).to_bytes(4, "big")
    assert B.parse_knob_frame(bytes([0, 0, 0, 1]) + neg5 + b"\x02") == (1, -5, 2)
    all_f = (0xFFFFFFFF).to_bytes(4, "big")
    assert B.parse_knob_frame(bytes([0, 0, 0, 1]) + all_f + b"\x02") == (
        1, -0x7FFFFFFF, 2)


# ---------------------------------------------------------------------------
# Tables (IR/ files)
# ---------------------------------------------------------------------------

def test_code_sets_are_the_three_perl_ir_files():
    # IR.pm:165-199 finds every *.ir in the IR dir
    assert set(B.IR_CODE_SETS) == {
        "Slim_Devices_Remote.ir", "Front_Panel.ir", "jvc_dvd.ir",
    }
    remote = B.IR_CODE_SETS["Slim_Devices_Remote.ir"]
    # verbatim lines of IR/Slim_Devices_Remote.ir
    assert remote[CODE_PLAY] == "play"
    assert remote["768920DF"] == "pause"
    assert remote["768940BF"] == "power"
    assert remote["7689807F"] == "volup"
    assert remote["768900FF"] == "voldown"
    assert remote["7689C03F"] == "rew"
    assert remote["7689A05F"] == "fwd"
    assert remote["7689D827"] == "shuffle"
    assert remote["768938C7"] == "repeat"
    assert remote["7689B847"] == "sleep"
    assert remote["7689C43B"] == "muting"
    assert remote["7689B04F"] == "arrow_down"
    # IR/Front_Panel.ir keeps the .down suffix; IR/jvc_dvd.ir defines two
    # codes for 'play' (last one wins, IR.pm:392 hash assignment).
    assert B.IR_CODE_SETS["Front_Panel.ir"]["00010012"] == "play.down"
    assert B.IR_CODE_SETS["jvc_dvd.ir"]["0000F732"] == "play"
    assert B.IR_CODE_SETS["jvc_dvd.ir"]["0000F7D6"] == "play"


def test_default_map_common_section_is_verbatim():
    # IR/Default.map [common]
    common = B.BUTTON_FUNCTIONS["Default.map"]["common"]
    assert common["stop"] == "stop"
    assert common["pause.single"] == "pause"
    assert common["pause.hold"] == "stop"
    assert common["play.single"] == "play"
    assert common["rew.single"] == "jump_rew"
    assert common["fwd.single"] == "jump_fwd"
    assert common["power"] == "power_toggle"
    assert common["power_on"] == "power_on"
    assert common["power_off"] == "power_off"
    assert common["volup"] == "volume"
    assert common["voldown"] == "volume"
    assert common["sleep.single"] == "sleep"
    assert common["shuffle.single"] == "shuffle_toggle"
    assert common["repeat"] == "repeat_toggle"
    assert common["favorites.single"] == "favorites"
    assert common["0"] == "numberScroll_0"
    assert common["0.hold"] == "preset_0"
    assert common["brightness"] == "dead"


def test_button_star_expands_to_all_press_styles():
    # IR.pm:43 styles + IR.pm:343-355 expansion
    off = B.BUTTON_FUNCTIONS["Default.map"]["off"]
    for style in B.BUTTON_PRESS_STYLES:
        assert off["arrow_right" + style] == "dead"
    assert B.BUTTON_PRESS_STYLES == (
        "", ".single", ".double", ".repeat", ".hold", ".hold_release")


def test_lookup_button_respects_disabled_sets():
    # IR.pm:400-410 disabledirsets; Player.pm:40 default empty
    assert B.lookup_button(CODE_PLAY) == ("Slim_Devices_Remote.ir", "play")
    assert B.lookup_button(CODE_PLAY, disabled=("Slim_Devices_Remote.ir",)) == (
        None, None)
    assert B.lookup_button("DEADBEEF") == (None, None)


def test_lookup_function_walks_common_and_press_styles():
    # IR.pm:491-527 order (mode, 'common'); plain key then .single
    assert B.lookup_function("power") == "power_toggle"
    assert B.lookup_function("pause") == "pause"
    assert B.lookup_function("play") == "play"
    assert B.lookup_function("volup") == "volume"
    assert B.lookup_function("0") == "numberScroll_0"
    assert B.lookup_function("0", hold=True) == "preset_0"
    # the [off] section overrides [common] for a powered-off player
    assert B.lookup_function("volup", mode="off") == "dead"
    assert B.lookup_function("stop", mode="off") == "dead"
    assert B.lookup_function("nonsense") is None


def test_split_function_replicates_getFunction():
    # Common.pm:1338-1360: X_Y -> sub X, arg Y (first underscore wins)
    assert B.split_function("power_toggle") == ("power", "toggle")
    assert B.split_function("jump_fwd") == ("jump", "fwd")
    assert B.split_function("jump_rew") == ("jump", "rew")
    assert B.split_function("volume_front") == ("volume", "front")
    assert B.split_function("preset_3") == ("preset", "3")
    assert B.split_function("playPreset_3") == ("playPreset", "3")
    assert B.split_function("favorites_add2") == ("favorites", "add2")
    assert B.split_function("repeat_2") == ("repeat", "2")
    assert B.split_function("shuffle_on") == ("shuffle", "on")
    assert B.split_function("menu_home") == ("menu", "home")
    assert B.split_function("dead") == ("dead", None)
    # unknown sub names stay whole -> reported as not implemented
    assert B.split_function("song_scanner") == ("song_scanner", None)
    assert B.split_function("play_0") == ("play_0", None)


# ---------------------------------------------------------------------------
# handle_button -> actions
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_play_button_starts_playback_and_powers_on():
    # Power.pm:33-38 off-mode 'play' = power 1 + play
    player = _player(power=False, mode="stop")
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_PLAY, manager=pm))
    assert (res.button, res.function, res.sub, res.arg) == (
        "play", "play", "play", None)
    assert res.action == "executed"
    assert ("set_power", True) in pm.calls
    assert ("play_track", 11) in pm.calls


def test_pause_button_toggles_and_never_sends_a_toggle():
    # Common.pm:369-383 wants an explicit 0/1
    player = _player(mode="play")
    pm = FakeManager(player)
    _run(B.handle_button(MAC, CODE_PAUSE, manager=pm))
    assert pm.calls[-1] == ("pause", True)
    _run(B.handle_button(MAC, CODE_PAUSE, manager=pm))
    assert pm.calls[-1] == ("pause", False)


def test_fwd_and_rew_map_through_jump():
    # Default.map fwd.single = jump_fwd -> Common.pm:327-331 jump +1
    # rew.single = jump_rew -> Common.pm:310-325 jump -1 (songTime < 5)
    player = _player(mode="stop")
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_FWD, manager=pm))
    assert (res.function, res.sub, res.arg) == ("jump_fwd", "jump", "fwd")
    assert pm.calls[-1] == ("playlist_jump", "+1")
    res = _run(B.handle_button(MAC, CODE_REW, manager=pm))
    assert (res.function, res.sub, res.arg) == ("jump_rew", "jump", "rew")
    assert pm.calls[-1] == ("playlist_jump", "-1")


def test_jump_rew_restarts_a_running_track_after_five_seconds():
    # Common.pm:310-325: songTime >= 5 AND playing -> jump '+0' (restart)
    player = _player(mode="play", elapsed=42.0)
    pm = FakeManager(player)
    _run(B.handle_button(MAC, CODE_REW, manager=pm))
    assert pm.calls[-1] == ("playlist_jump", "+0")


def test_execute_named_button_runs_the_function_table():
    # Commands.pm:263-291 -> IR.pm:1061-1064 -> Common.pm:1338-1360
    player = _player(mode="play", playlist=[11, 22], playlist_position=0)
    pm = FakeManager(player)
    res = _run(B.execute_named_button("jump_fwd", MAC, manager=pm))
    assert (res.kind, res.function, res.sub, res.arg) == (
        "command", "jump_fwd", "jump", "fwd")
    assert res.action == "executed"
    assert pm.calls[-1] == ("playlist_jump", "+1")

    res = _run(B.execute_named_button("jump_rew", MAC, manager=pm))
    assert (res.function, res.sub, res.arg) == ("jump_rew", "jump", "rew")
    assert pm.calls[-1] == ("playlist_jump", "-1")


def test_button_fwd_name_is_looked_up_in_the_map_first():
    # 'fwd'/'rew' are BUTTON names: the map turns them into jump_fwd/jump_rew
    # (Default.map [common] fwd.single, IR.pm:491-527)
    player = _player(mode="play", elapsed=42.0)
    pm = FakeManager(player)
    res = _run(B.execute_named_button("fwd", MAC, manager=pm))
    assert (res.function, res.sub, res.arg) == ("jump_fwd", "jump", "fwd")
    assert pm.calls[-1] == ("playlist_jump", "+1")


def test_execute_named_button_unknown_function_is_logged_not_executed():
    # IR.pm:1106-1112 — Perl only warns; the socket stays open
    player = _player(mode="play")
    pm = FakeManager(player)
    res = _run(B.execute_named_button("next", MAC, manager=pm))
    assert res.action == "not-implemented"
    assert pm.calls == []


def test_power_button_uses_the_toggle_branch():
    # Common.pm:1115-1130: power_toggle -> else branch toggles
    player = _player(power=True)
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_POWER, manager=pm))
    assert res.function == "power_toggle"
    assert (res.sub, res.arg) == ("power", "toggle")
    assert pm.calls[-1] == ("set_power", False)


def test_volume_buttons_step_the_mixer():
    # Common.pm:969-982 + Volume.pm:89-104 (mixer volume, increment 1 at :99)
    player = _player(volume=50, use_volume_control=True)
    pm = FakeManager(player)
    assert _run(B.handle_button(MAC, CODE_VOLUP, manager=pm)).function == "volume"
    assert pm.calls[-1] == ("set_volume", 51)
    _run(B.handle_button(MAC, CODE_VOLDOWN, manager=pm))
    assert pm.calls[-1] == ("set_volume", 50)


def test_volume_buttons_are_dead_when_the_off_mode_applies():
    # Default.map [off] voldown.* = dead
    player = _player(power=False, volume=50)
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_VOLDOWN, manager=pm, mode="off"))
    assert (res.function, res.action) == ("dead", "executed")
    assert pm.calls == []


def test_volume_button_without_volume_control_does_nothing():
    # Common.pm:974 return if !$client->hasVolumeControl()
    player = _player(volume=50, use_volume_control=False)
    pm = FakeManager(player)
    _run(B.handle_button(MAC, CODE_VOLUP, manager=pm))
    assert pm.calls == []


def test_sleep_cycles_through_the_perl_choices():
    # Common.pm:1060 choices (0,15,30,45,60,90); :1095 sleep * 60
    player = _player(sleep_remaining=0)
    pm = FakeManager(player)
    _run(B.handle_button(MAC, CODE_SLEEP, manager=pm))
    assert player.sleep_remaining == 15 * 60
    _run(B.handle_button(MAC, CODE_SLEEP, manager=pm))
    assert player.sleep_remaining == 30 * 60
    player.sleep_remaining = 90 * 60
    _run(B.handle_button(MAC, CODE_SLEEP, manager=pm))
    assert player.sleep_remaining == 0          # wraps to "cancel sleep"


def test_shuffle_and_repeat_set_the_playlist_modes():
    # Common.pm:1132-1166 shuffle_toggle, :915-947 repeat_toggle
    player = _player(shuffle=0, repeat=0)
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_SHUFFLE, manager=pm))
    assert (res.function, res.sub, res.arg) == ("shuffle_toggle", "shuffle",
                                                "toggle")
    assert player.shuffle == 1
    res = _run(B.handle_button(MAC, CODE_REPEAT, manager=pm))
    assert (res.function, res.sub, res.arg) == ("repeat_toggle", "repeat",
                                                "toggle")
    assert player.repeat == 1
    res = _run(B.handle_button(MAC, CODE_REPEAT, manager=pm))
    assert player.repeat == 2


def test_mute_button_toggles_the_mute_flag():
    # Common.pm:1008-1015 mixer muting <!mute>
    player = _player(mute=False, volume=50)
    pm = FakeManager(player)
    _run(B.handle_button(MAC, CODE_MUTE, manager=pm))
    assert player.mute is True
    assert pm.calls[-1] == ("set_volume", 0)
    _run(B.handle_button(MAC, CODE_MUTE, manager=pm))
    assert player.mute is False


def test_unknown_code_only_reports_and_does_nothing():
    # IR.pm:644-651 unknown -> notify 'unknownir', return
    player = _player()
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, "DEADBEEF", manager=pm))
    assert res.action == "unknown-code"
    assert res.button is None
    assert pm.calls == []


def test_ui_function_is_reported_not_implemented():
    # IR.pm:1106-1112 "not implemented in mode" (warn, socket stays open):
    # 'home' resolves to Common.pm:1276 but has no server-side counterpart.
    player = _player()
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, "768922DD", manager=pm))  # home
    assert res.button == "home"
    assert res.function == "home"
    assert res.sub == "home"
    assert res.action == "not-implemented"
    assert pm.calls == []


def test_butn_uses_the_same_lookup_path_as_ir():
    # Slimproto.pm:1275-1277 -> the same IR::enqueue as :541
    player = _player()
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_POWER, kind="butn", manager=pm))
    assert res.kind == "butn"
    assert res.function == "power_toggle"
    assert pm.calls[-1] == ("set_power", False)


def test_front_panel_down_code_looks_up_the_base_button():
    # IR.pm:691-704 strips .down; :825 looks the BASE name up
    player = _player()
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, "00010012", kind="butn", manager=pm))
    assert (res.button, res.front_panel) == ("play.down", True)
    assert res.function == "play"
    assert ("play_track", 11) in pm.calls


def test_hold_selects_the_hold_style():
    # IR.pm:836-841 fireHold -> "<code>.hold"
    player = _player(mode="play")
    pm = FakeManager(player)
    res = _run(B.handle_button(MAC, CODE_PAUSE, hold=True, manager=pm))
    assert res.function == "stop"          # common pause.hold = stop
    assert pm.calls[-1] == ("stop",)


def test_handle_knob_is_unmapped_in_common_mode():
    # Slimproto.pm:1315 executeButton('knob'); no [common] 'knob' entry
    res = _run(B.handle_knob(MAC, 5, 1))
    assert res.button == "knob"
    assert res.action == "not-implemented"


def test_unklar_documents_the_open_questions():
    assert len(B.UNKLAR) >= 6
    joined = " ".join(B.UNKLAR)
    assert "per-player-class" in joined.lower() or "per-player" in joined
    assert "mode stack" in joined
    assert "knoa" in joined


# ---------------------------------------------------------------------------
# Opcode wiring (protocol.py)
# ---------------------------------------------------------------------------

def _client_with_player(player=None):
    player = player or _player()
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {}
    return client, player


def test_button_opcodes_are_declared():
    assert PERL_BUTTON_OPCODES == {"IR  ", "BUTN", "KNOB"}
    assert PERL_BUTTON_OPCODES <= set(PERL_HANDLER_NAMES)


def test_protocol_dispatch_parses_and_calls_handle_button(monkeypatch):
    calls = []

    async def fake_handle_button(mac, code, kind="ir", **kw):
        calls.append((mac, code, kind))
        return None

    async def fake_handle_knob(mac, position, sync=0, **kw):
        calls.append((mac, position, sync))
        return None

    monkeypatch.setattr(B, "handle_button", fake_handle_button)
    monkeypatch.setattr(B, "handle_knob", fake_handle_knob)
    client, _player = _client_with_player()

    ir = bytes([0, 0, 0, 1, 0, 9]) + bytes.fromhex(CODE_PLAY.lower())
    butn = bytes([0, 0, 0, 2]) + bytes.fromhex(CODE_PAUSE.lower())
    knob = bytes([0, 0, 0, 3, 0, 0, 0, 4, 0])

    async def scenario():
        await client._dispatch_button_frame(MAC, "IR  ", ir)
        await client._dispatch_button_frame(MAC, "BUTN", butn)
        await client._dispatch_button_frame(MAC, "KNOB", knob)

    _run(scenario())
    assert calls == [(MAC, CODE_PLAY, "ir"), (MAC, CODE_PAUSE, "butn"),
                     (MAC, 4, 0)]


def test_protocol_drops_a_malformed_ir_frame(monkeypatch, caplog):
    calls = []

    async def fake_handle_button(mac, code, kind="ir", **kw):
        calls.append(code)

    monkeypatch.setattr(B, "handle_button", fake_handle_button)
    client, _player = _client_with_player()
    with caplog.at_level("WARNING"):
        _run(client._dispatch_button_frame(MAC, "IR  ", b"\x00" * 9))
    assert calls == []
    assert "bad length" in caplog.text


def test_protocol_schedules_the_dispatch_without_blocking_the_loop():
    client, _player = _client_with_player()
    seen = []

    async def scenario():
        # IR frame -> _handle_player_event_frame must return immediately and
        # let the task run.
        client._handle_player_event_frame(
            MAC, "IR  ",
            bytes([0, 0, 0, 1, 0, 9]) + bytes.fromhex(CODE_PLAY.lower()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        seen.append("ran")

    _run(scenario())
    assert seen == ["ran"]


def test_ir_frame_reaches_a_real_action_through_the_protocol():
    """End-to-end: IR frame -> parsed -> button -> action on a real
    PlayerManager (the singleton the handler resolves without a manager)."""
    player = _player(power=True)
    client, player = _client_with_player(player)
    _run(client._dispatch_button_frame(
        MAC, "IR  ",
        bytes([0, 0, 0, 1, 0, 9]) + bytes.fromhex(CODE_POWER.lower())))
    # Common.pm:1115-1130 power_toggle toggled it off
    assert player.power is False
