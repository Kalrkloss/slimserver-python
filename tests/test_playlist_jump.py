"""Naechster-/Vorheriger-Titel bei gefuellter Playlist (Regression).

Symptom: bei gefuellter Playlist taten die Naechster-/Vorheriger-Tasten der
Controller nichts. Was die Apps dabei senden, steht im Log des laufenden
Servers (``/tmp/lyrion-live.log``, 2026-09-18 19:57):

    Cometd PAYLOAD /slim/request: {'request':
        ['1c:87:2c:47:fc:36', ['button', 'jump_fwd']], 'response': ...}
    Cometd PAYLOAD /slim/request: {'request':
        ['1c:87:2c:47:fc:36', ['button', 'jump_rew']], 'response': ...}

also NICHT ``button next``/``prev``, sondern die FUNKTIONSNAMEN ``jump_fwd``/
``jump_rew`` — SqueezeJS' Transportbuttons (``HTML/EN/html/SqueezeJS/UI.js:297``
fuer fwd, ``:260`` fuer rew; HTTP-Clients schicken stattdessen
``['playlist','index','+1']``, ``UI.js:306``).

Perl-Weg (jede Zeile zitiert):

* ``Commands.pm:263-291`` ``buttonCommand`` → ``Slim::Hardware::IR::
  executeButton($client, $button, $time, undef, 1)`` (``:288``; ``_orFunction``
  ist per Dispatch-Default 1).
* ``IR.pm:1059-1104``: ``lookupFunction`` (Map-Treffer) sonst — ``:1061-1064`` —
  der Name selbst als Funktion; ``getFunction`` (``Common.pm:1338-1360``)
  zerlegt ``jump_fwd`` in Sub ``jump`` + Argument ``fwd``.
* ``Common.pm:296-337`` ``%functions{'jump'}``: ``fwd`` → ``playlist jump +1``
  (``:327-331``), ``rew`` → ``-1`` wenn ``songTime < 5`` oder gestoppt, sonst
  ``+0`` (``:310-325``), sonst ``+0``.
* ``Commands.pm:921-1036`` ``playlistJumpCommand`` (bedient ``jump`` UND
  ``index``, ``:925``): relative Offsets ``+n``/``-n`` (``:970-990``),
  Sonderfaelle ``+0``/Einzeltitel-``-1`` (Neustart, ``:974-979``), ``+1`` =
  ``skip()`` (``:980-986``), Wrap-around (``:1009-1014``), am Ende
  ``play(#newIndex)`` (``:1016-1021``).
"""

from __future__ import annotations

import asyncio

from lyrion.player.manager import PlayerManager, jump_target
from lyrion.player.state import PlayerState

from lyrion.web.api import JSONRPCAPI

MAC = "02:11:22:33:44:55"
STREAMS = ["http://a.invalid/1.mp3", "http://b.invalid/2.mp3",
           "http://c.invalid/3.mp3"]


class _FakeHandler:
    """Records the strm/remote-stream frames a jump sends."""

    def __init__(self) -> None:
        self.strm: list = []
        self.remote: list = []

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
        self.strm.append((mac, track_id))
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        self.remote.append((mac, url))
        return True


def _pm(playlist, position=0, mode="play", repeat=0,
        ) -> tuple[PlayerManager, PlayerState, _FakeHandler]:
    pm = object.__new__(PlayerManager)
    pm.players = {}
    handler = _FakeHandler()
    pm._protocol_handler = handler
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    p.playlist = list(playlist)
    for field, value in (("playlist_position", position),
                         ("playlist_total", len(playlist)),
                         ("mode", mode), ("power", True), ("repeat", repeat)):
        setattr(p, field, value)
    pm.players[p.mac] = p
    return pm, p, handler


# ---------------------------------------------------------------------------
# jump_target — die Perl-Arithmetik (Commands.pm:937-1014)
# ---------------------------------------------------------------------------

def test_relative_jump_wraps_like_perl():
    _, p, _h = _pm(STREAMS, position=0)
    assert jump_target(p, "+1") == 1
    p.playlist_position = 2
    assert jump_target(p, "+1") == 0            # :1010-1013 %$songcount
    p.playlist_position = 0
    assert jump_target(p, "-1") == 2            # :1013 ($newIndex + $songcount)
    assert jump_target(p, "+2") == 2            # :990 playingSongIndex + $index


def test_absolute_index_is_wrapped_like_perl():
    _, p, _h = _pm(STREAMS, position=0)
    assert jump_target(p, "2") == 2
    assert jump_target(p, "3") == 0             # :1010 wraps >= songcount
    _, single, _h2 = _pm(["http://only.invalid/x.mp3"])
    assert jump_target(single, "0") == 0


def test_restart_forms_keep_the_index():
    # :974-979 — '+0' und (Einzeltitel) '-1' starten den laufenden Titel neu
    _, p, _h = _pm(STREAMS, position=1)
    assert jump_target(p, "+0") == 1
    _, single, _h2 = _pm(["http://only.invalid/x.mp3"])
    assert jump_target(single, "-1") == 0
    assert jump_target(single, "+1") == 0       # Einzeltitel: kein Wechsel


def test_repeat_song_replays_the_current_track_on_skip():
    # StreamingController.pm:876-878: nextsong gibt bei repeat == 1 $currsong
    _, p, _h = _pm(STREAMS, position=1, repeat=1)
    assert jump_target(p, "+1") == 1
    p.repeat = 0
    assert jump_target(p, "+1") == 2


def test_stopped_player_uses_the_plain_offset():
    # Alle Sonderfaelle liegen in Perl hinter `if (!$isStopped)` (:972)
    _, p, _h = _pm(STREAMS, position=0, mode="stop")
    assert jump_target(p, "+1") == 1
    assert jump_target(p, "-1") == 2


def test_empty_playlist_and_garbage_return_nothing():
    _, p, _h = _pm([], position=0)
    assert jump_target(p, "+1") is None         # :937 (|| return)
    _, p2, _h2 = _pm(STREAMS, position=0)
    assert jump_target(p2, "abc") is None
    assert jump_target(p2, "?") is None


# ---------------------------------------------------------------------------
# PlayerManager.playlist_jump (Commands.pm:1016-1021 spielt das Ziel)
# ---------------------------------------------------------------------------

def test_playlist_jump_plays_the_next_stream_entry():
    pm, p, h = _pm(STREAMS, position=0)
    assert asyncio.run(pm.playlist_jump(MAC, "+1")) is True
    assert p.playlist_position == 1
    assert p.mode == "play"
    assert p.remote == 1
    assert p.current_url == STREAMS[1]
    assert h.remote[-1] == (MAC, STREAMS[1])
    assert len(p.playlist) == 3                 # Queue bleibt erhalten


def test_playlist_jump_back_wraps_to_the_last_entry():
    pm, p, _h = _pm(STREAMS, position=0)
    assert asyncio.run(pm.playlist_jump(MAC, "-1")) is True
    assert p.playlist_position == 2
    assert p.current_url == STREAMS[2]


def test_playlist_jump_on_a_single_entry_keeps_index_zero():
    pm, p, _h = _pm([STREAMS[0]], position=0)
    assert asyncio.run(pm.playlist_jump(MAC, "+1")) is True
    assert p.playlist_position == 0             # Perl: kein Wechsel
    assert p.current_url == STREAMS[0]          # ... aber Neustart


def test_playlist_next_and_prev_share_the_jump_arithmetic():
    pm, p, _h = _pm(STREAMS, position=1)
    assert asyncio.run(pm.playlist_next(MAC)) is True
    assert p.playlist_position == 2
    assert asyncio.run(pm.playlist_prev(MAC)) is True
    assert p.playlist_position == 1


def test_playlist_jump_plays_a_track_id_entry():
    pm, p, h = _pm([11, 22, 33], position=0)
    assert asyncio.run(pm.playlist_jump(MAC, "+1")) is True
    assert p.playlist_position == 1
    assert h.strm[-1] == (MAC, 22)


# ---------------------------------------------------------------------------
# JSON/cometd: 'button jump_fwd' und 'playlist jump/index'
# ---------------------------------------------------------------------------

def test_json_button_jump_fwd_moves_the_playlist_index():
    pm, p, _h = _pm(STREAMS, position=0)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "button", ["jump_fwd"])

    asyncio.run(run())
    assert p.playlist_position == 1
    assert p.current_url == STREAMS[1]


def test_json_button_jump_rew_steps_back():
    pm, p, _h = _pm(STREAMS, position=2)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "button", ["jump_rew"])

    asyncio.run(run())
    assert p.playlist_position == 1


def test_json_button_jump_rew_restarts_a_running_track():
    # Common.pm:310-325 — songTime >= 5 → '+0' (Neustart des laufenden Titels)
    pm, p, h = _pm(STREAMS, position=1)
    p.elapsed = 30.0

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "button", ["jump_rew"])

    asyncio.run(run())
    assert p.playlist_position == 1
    assert h.remote[-1] == (MAC, STREAMS[1])


def test_json_button_fwd_looks_the_button_up_in_the_map():
    # 'fwd' ist ein BUTTON-Name: Default.map [common] fwd.single = jump_fwd
    pm, p, _h = _pm(STREAMS, position=0)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "button", ["fwd"])

    asyncio.run(run())
    assert p.playlist_position == 1


def test_json_playlist_jump_and_index_take_relative_offsets():
    for verb in ("jump", "index"):
        pm, p, _h = _pm(STREAMS, position=0)

        async def run(verb=verb):
            await JSONRPCAPI()._json_control(pm, MAC, "playlist",
                                             [verb, "+1"])

        asyncio.run(run())
        assert p.playlist_position == 1, verb


def test_json_playlist_jump_on_a_single_entry_keeps_index_zero():
    pm, p, _h = _pm([STREAMS[0]], position=0)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "playlist", ["jump", "+1"])

    asyncio.run(run())
    assert p.playlist_position == 0


def test_json_playlist_index_absolute_still_works():
    pm, p, _h = _pm(STREAMS, position=0)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "playlist", ["index", "2"])

    asyncio.run(run())
    assert p.playlist_position == 2


def test_json_button_without_a_playlist_changes_nothing():
    pm, p, h = _pm([], position=0)

    async def run():
        await JSONRPCAPI()._json_control(pm, MAC, "button", ["jump_fwd"])

    asyncio.run(run())
    assert p.playlist_position == 0
    assert h.remote == []


def test_slim_request_route_reaches_the_button_table(monkeypatch):
    """Der Weg der Apps: slim.request -> _json_control -> button."""
    pm, p, _h = _pm(STREAMS, position=0)
    monkeypatch.setattr("lyrion.player.manager.PlayerManager", lambda: pm)
    res = asyncio.run(JSONRPCAPI()._slim_request(MAC, ["button", "jump_fwd"]))
    assert res == {}                          # buttonCommand liefert kein Result
    assert p.playlist_position == 1


# ---------------------------------------------------------------------------
# CLI: derselbe buttonCommand-Weg (Commands.pm:263-291 fuer alle Quellen)
# ---------------------------------------------------------------------------

def _cli(cmd, args, pid=MAC) -> list[str]:
    from lyrion.control.cli import CLIContext, CLIHandler

    ctx = CLIContext(player_id=pid)
    ctx.command = cmd
    return asyncio.run(CLIHandler().dispatch(ctx, (cmd, args)))


def test_cli_button_jump_fwd_moves_the_playlist_index(monkeypatch):
    pm, p, _h = _pm(STREAMS, position=0)
    monkeypatch.setattr("lyrion.player.PlayerManager", lambda: pm)
    lines = _cli("button", ["jump_fwd"])
    assert p.playlist_position == 1
    assert lines and "button jump_fwd" in lines[0]


def test_cli_playlist_jump_takes_a_relative_offset(monkeypatch):
    pm, p, _h = _pm(STREAMS, position=0)
    monkeypatch.setattr("lyrion.player.PlayerManager", lambda: pm)
    _cli("playlist", ["jump", "+1"])
    assert p.playlist_position == 1
    _cli("playlist", ["index", "-1"])
    assert p.playlist_position == 0
