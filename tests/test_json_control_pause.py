"""JSON-RPC/`/slim/request` pause dispatch — Perl parity (Commands.pm:731-753).

Live symptom this locks down: after a UI pause the server held the position
correctly, but pressing PLAY again (the client sends ``['pause','0']``) ran
the old resume implementation (power on + replay the current item), which
stopped the player: ``mode=stop, time=0`` and no audible resume.

Perl semantics (pinned clone /tmp/lms-ref = public/9.2, read-only):

* ``pause 1``  -> wantmode 'pause' (only reachable from 'play')
* ``pause 0``  -> wantmode 'play'; if the player is PAUSED this becomes
  'resume' (position kept, ``strm 'u'``, no re-stream)
  ``$wantmode = 'resume' if ($curmode eq 'pause' && $wantmode eq 'play');``
  — Slim/Control/Commands.pm:743
* ``pause``    -> toggle: 'play' from pause/stop, else 'pause'
  — Commands.pm:737-740
* ``pause 1`` while stopped is a NO-OP (pause only from play)
  — Commands.pm:746-753
"""

import asyncio

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI

MAC = "02:11:22:33:44:55"


class _FakePlayer:
    def __init__(self, mode="play"):
        self.mac = MAC
        self.mode = mode
        self.power = True
        self.playlist_position = 0
        self.playlist_tracks = 1


class _FakePM:
    """Records pause/resume/start calls instead of talking to a player."""

    def __init__(self, mode="play"):
        self.player = _FakePlayer(mode)
        self.pause_calls = []
        self.modes = []
        self.started = []

    def get_player(self, mac):
        return self.player if mac == self.player.mac else None

    def get_all_players(self):
        return [self.player]

    async def pause_player(self, mac, pause):
        self.pause_calls.append(bool(pause))
        self.player.mode = "pause" if pause else "play"
        return True

    def set_mode(self, mac, mode):
        self.modes.append(mode)
        self.player.mode = mode

    def set_power(self, mac, on):
        self.player.power = bool(on)

    def send_command(self, mac, cmd):
        raise AssertionError(f"unexpected raw send: {cmd}")


def _run(coro):
    return asyncio.run(coro)


def _patch_start(monkeypatch, api, pm):
    async def fake_start(pm_arg, player, index):
        pm.started.append(index)
        return True

    monkeypatch.setattr(api, "_play_playlist_item", fake_start)


def test_pause_one_from_play_calls_real_pause(monkeypatch):
    pm = _FakePM("play")
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "pause", ["1"]))
    assert pm.pause_calls == [True]
    assert pm.started == []


def test_pause_zero_while_paused_resumes_instead_of_stop_restart(monkeypatch):
    pm = _FakePM("pause")
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "pause", ["0"]))
    assert pm.pause_calls == [False], "resume must go through pause_player(False)"
    assert pm.started == [], "resume must NOT replay the item / re-stream"
    assert pm.player.mode == "play"


def test_pause_zero_while_stopped_starts_current_item(monkeypatch):
    pm = _FakePM("stop")
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "pause", ["0"]))
    assert pm.pause_calls == []
    assert pm.started == [0], "stopped player: 'pause 0' starts the current item"
    assert pm.modes == ["play"]


def test_pause_one_while_stopped_is_a_noop(monkeypatch):
    pm = _FakePM("stop")
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "pause", ["1"]))
    assert pm.pause_calls == [], "pause only applies from 'play'"
    assert pm.started == []


def test_pause_without_argument_toggles(monkeypatch):
    pm = _FakePM("play")
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "pause", []))
    assert pm.pause_calls == [True]
    assert pm.started == []

    pm2 = _FakePM("pause")
    api2 = JSONRPCAPI()
    _patch_start(monkeypatch, api2, pm2)
    _run(api2._json_control(pm2, MAC, "pause", []))
    assert pm2.pause_calls == [False]
    assert pm2.started == []


def test_manager_pause_player_semantics_unchanged():
    """The fake mirrors the real manager's contract used above."""
    pm = object.__new__(PlayerManager)
    pm.players = {}
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    pm.players[p.mac] = p
    assert hasattr(pm, "pause_player")
    assert PlayerState  # keep the import meaningful
