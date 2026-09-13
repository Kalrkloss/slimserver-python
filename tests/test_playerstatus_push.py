"""Pushed ``playerstatus`` payload — the fields SqueezePlay checks before it
re-sends volume/power (regression guard for the LIVE-19 / LIVE-25 request
storm, ~55k ``mixer volume`` + ~26k ``status`` in one session).

Why this file exists
--------------------
SqueezePlay keeps ``power`` and ``mixer volume`` locally (``LocalPlayer``) and
ships each change with a ``seq_no:<N>`` param.  On every pushed status it
compares the server's ``seq_no`` against its own counter:

* ``slim/Player.lua:1225-1230`` — ``if self:isLocal() and playerInfo.seq_no
  then ... if not self:isSequenceNumberInSync(tonumber(playerInfo.seq_no))``
  (``playerInfo.seq_no = event.data.seq_no``, ``Player.lua:1220``).
* ``audio/Playback.lua:1094-1112`` — ``isSequenceNumberInSync``: the local
  player branch is ``elseif sequenceNumber ~= self.sequenceNumber then ...
  return false`` (``:1108-1111``, debug "server sequence # out of sync").
* ``slim/Player.lua:1331-1333`` — out of sync ⇒
  ``self:refreshLocallyMaintainedParameters()``.
* ``slim/LocalPlayer.lua:153-164`` — that re-sends ``mixer volume``
  (``_volumeNoIncrement`` → ``Player.lua:1762``, NO seq) and ``power``
  (``LocalPlayer.lua:428`` increments → ``Player.lua:1746``, WITH seq).

The pushed pair is therefore exactly ``['mixer','volume',47]`` +
``['power','1',false,'seq_no:N']`` with a monotonically **rising** N — the
live signature of the storm.  It only stops when the pushed ``seq_no`` equals
the number the client last sent, so the pushed payload MUST carry it.

Perl side (read-only ``/tmp/lms-ref``):
* ``Slim/Player/Client.pm:208-210`` — stores ``sequenceNumber`` per client
  (``mixer`` → ``Commands.pm:559-562``, ``power`` → ``:2586-2589``).
* ``Slim/Control/Queries.pm:4196`` — ``statusQuery`` emits it as ``seq_no``.
* ``Slim/Web/Cometd.pm:390-470`` — a ``/slim/subscribe`` stores the request
  and re-executes it on every player change; the result is the pushed data.

These run in-process against ``CometdManager`` + the REAL ``JSONRPCAPI``
payload builder: no server start/stop, no live write commands, no DB write.
"""

import asyncio

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI
from lyrion.web.cometd import CometdManager

MAC = "1c:87:2c:47:fc:36"            # jive registers the LOWER-case spelling
MAC_CLEAN = "1C872C47FC36"          # PlayerState.mac is upper case

# Exactly what SqueezePlay's Now-Playing / status view subscribes with
# (Player.lua:993, verbatim in the live log).
JIVE_REQUEST = [MAC, ["status", "-", 10, "menu:menu", "useContextMenu:1",
                      "subscribe:600"]]

# The live value the storming client last sent (server echoed it 1:1).
CLIENT_SEQ_NO = 783495
CLIENT_VOLUME = 47


def _install_player(seq_no=CLIENT_SEQ_NO, power=True, volume=CLIENT_VOLUME,
                    mode="stop") -> PlayerState:
    """Put a real PlayerState behind the PlayerManager singleton.

    Mirrors tests/test_seq_no.py: the API takes no ``pm`` argument on the
    Cometd path, so the handler resolves ``PlayerManager()`` itself.
    """
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=43856)
    player.power = power
    player.volume = volume
    player.mode = mode
    player.seq_no = seq_no
    player.playlist = []
    player.playlist_position = 0

    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


async def _pushed_playerstatus():
    """Subscribe like jive, trigger a change, return the pushed payload."""
    mgr = CometdManager(JSONRPCAPI())
    hs = await mgr.handle_messages([{"channel": "/meta/handshake", "id": 1}])
    cid = hs[0]["clientId"]
    channel = f"/{cid}/slim/playerstatus/{MAC}"

    await mgr.handle_messages([{
        "channel": "/slim/subscribe", "id": 2,
        "data": {"request": JIVE_REQUEST, "response": channel},
    }])
    await mgr.wait_for_events(cid, timeout=0)   # drain the seed event

    await mgr.notify_player_status(MAC)         # what a STAT change triggers
    return channel, await mgr.wait_for_events(cid, timeout=0)


def _run(coro):
    return asyncio.run(coro)


def _client_seq_in_sync(server_seq, local_seq) -> bool:
    """Playback.lua:1094-1112 — local-player branch (``:1108-1111``).

    ``sequenceController`` is nil for the local player, so the comparison is
    a plain ``server == local``; a mismatch is the "out of sync" that makes
    jive call ``refreshLocallyMaintainedParameters`` (Player.lua:1331-1332).
    """
    if server_seq is None:
        return True     # playerInfo.seq_no falsy ⇒ useSequenceNumber stays false
    return int(server_seq) == int(local_seq)


# ---------------------------------------------------------------------------
# The push carries the fields the client's sync check reads
# ---------------------------------------------------------------------------
def test_pushed_playerstatus_carries_the_clients_seq_no():
    """The seq_no the client sent must come back — otherwise it loops.

    Player.lua:1227 compares ``event.data.seq_no`` with the client counter.
    We echo the client's last ``power``/``mixer`` seq (api.py:4124,
    Perl Queries.pm:4196).
    """
    _install_player()
    channel, events = _run(_pushed_playerstatus())

    assert len(events) == 1, f"expected exactly one push, got {events}"
    assert events[0]["channel"] == channel
    data = events[0]["data"]

    assert data["seq_no"] == CLIENT_SEQ_NO, data
    # the client's own check would pass with the value it last sent
    assert _client_seq_in_sync(data["seq_no"], CLIENT_SEQ_NO)


def test_pushed_playerstatus_has_the_fields_player_lua_reads():
    """Player.lua:1212-1220 unpacks these from ``event.data``.

    ``_process_status`` stores the whole payload as ``self.state``
    (Player.lua:1186), so a missing/renamed field silently changes behaviour.
    """
    _install_player()
    _channel, events = _run(_pushed_playerstatus())
    data = events[0]["data"]

    for field in ("power", "mixer volume", "mode", "player_connected",
                  "digital_volume_control", "use_volume_control", "seq_no"):
        assert field in data, f"{field!r} missing from the pushed payload: {data}"

    assert data["power"] == 1
    assert data["mixer volume"] == CLIENT_VOLUME
    assert data["mode"] == "stop"
    assert data["player_connected"] == 1


def test_pushed_playerstatus_has_ip_and_playlist_timestamp():
    """``player_ip`` (Queries.pm:4055) and ``playlist_timestamp``
    (Queries.pm:4211) are part of the status the client pages through."""
    _install_player()
    _channel, events = _run(_pushed_playerstatus())
    data = events[0]["data"]

    assert "player_ip" in data
    assert "playlist_timestamp" in data


# ---------------------------------------------------------------------------
# The loop condition itself — documented so a payload regression is caught
# ---------------------------------------------------------------------------
def test_out_of_sync_seq_no_is_what_drives_the_loop():
    """A stale/zero ``seq_no`` is the loop (LIVE-19: hardcoded ``seq_no: 0``).

    With the old hardcoded 0 the comparison at Playback.lua:1108 failed on
    every push, so jive always called ``refreshLocallyMaintainedParameters``
    (Player.lua:1332) and re-sent ``mixer volume`` + ``power`` forever.
    """
    local = CLIENT_SEQ_NO
    assert _client_seq_in_sync(CLIENT_SEQ_NO, local)      # echoed 1:1 → stops
    assert not _client_seq_in_sync(0, local)             # pre-fix → loops
    assert not _client_seq_in_sync(CLIENT_SEQ_NO - 1, local)


def test_pushed_seq_no_tracks_the_clients_last_command():
    """After the client sends ``power ... seq_no:N`` the push must say N.

    ``power``/``mixer`` store the param on the player (api.py:4848-4853) and
    the seed/refresh push reads it back — the single increment that ends the
    storm (LocalPlayer.lua:152 comment "only update seq number on last call").
    """
    player = _install_player(seq_no=0)
    player.seq_no = 4211                       # what the client last sent
    _channel, events = _run(_pushed_playerstatus())

    assert events[0]["data"]["seq_no"] == 4211
