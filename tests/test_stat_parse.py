"""STAT parsing (PROT-15) and strm frame parity vs. Perl (LIVE-02).

Ground truth
------------
Perl LMS reads the STAT frame with a single unpack (READ-ONLY reference
``/tmp/lms-ref`` = public/9.2 @ d1d0a683d):

    Slim/Networking/Slimproto.pm:768
        unpack ('a4CCCNNNNnNNNNnNNn', $$data_ref)
        -> event, num_crlf, mas_initialized, mas_mode,
           rptr/decodeSize, wptr/decodeFull,
           bytes_received_H, bytes_received_L, signal_strength, jiffies,
           output_buffer_size, output_buffer_fullness, elapsed_seconds,
           voltage, elapsed_milliseconds, server_timestamp, error_code
    Slimproto.pm:770-777  len 51 -> error_code forced to 0
                                  53 = current firmware, 57 = legacy +4 junk

SqueezePlay packs its own STAT exactly like that (it only omits the
trailing error_code -> 51 bytes):

    ~/opt/squeezeplay/share/jive/jive/net/SlimProto.lua:167-195

The hex payloads below are REAL frames lifted from the live server log
``~/.lyrion/Lyrion/Logs/lyrion.log`` (debug build of this repo).
Everything expected here is derived from the Perl/SqueezePlay layout,
never from the implementation under test.

The strm expectations were produced by running the *Perl* pack template
itself:

    perl -e 'printf "%s", unpack "H*",
        pack "aaaaaaaCCCaCCCNnN",
             ($cmd, $autostart, "m","?","?","?","?",
              $bufferThreshold, 0, 0, 0, 0, $outputThreshold, 0,
              $replayGain, 0, 0)'
    s 255 1 9000 -> 73316d3f3f3f3fff00003000010000000000232800000000
    p -> 70306d3f3f3f3f0000003000000000000000000000000000
    f -> 66306d3f3f3f3f0000003000000000000000000000000000
    q -> 71306d3f3f3f3f0000003000000000000000000000000000
    u -> 75306d3f3f3f3f0000003000000000000000000000000000
    a -> 61306d3f3f3f3f0000003000000000000000000000000000
"""

from __future__ import annotations

import struct

import pytest

import lyrion.player.manager as manager_mod
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.state import PlayerState

# ──────────────────────────────────────────────────────────────────────
# Real frames from ~/.lyrion/Lyrion/Logs/lyrion.log
# ──────────────────────────────────────────────────────────────────────

# SqueezePlay (jive) — 51 bytes, no error_code.
SP_STMT = "53544d740000000030000000000bbf0000000000003e38ffff0075f37f0035d54000034f780000000000000000000000000000"
SP_STMO = "53544d6f00000000300000000000000000000000000000ffff008392540035d540000000000000006e00000001af2500000000"
SP_STMF = "53544d6600000000300000000000000000000000000000ffff0075f2970035d540000000000000000000000000000000000000"
SP_STMD = "53544d6400000000300000000000000000000000081330ffff003b85810035d540001026600000000000000000000000000000"
SP_STMS = "53544d7300000000300000000029600000000000014054ffff0075f5c90035d5400016a6800000000000000000005700000000"
# Ack frame: SqueezePlay acks every frame it has no subscription for with
# sendStatus(opcode) (SlimProto.lua:555-558) -> event == the opcode.
SP_CONT_ACK = "636f6e7400000000300000000000000000000000000000ffff0075f3170035d540000000000000000000000000000000000000"

# The LIVE-02 frame: SqueezePlay stuck at 50 s (elapsed_ms 50154 constant,
# output buffer ~3.36 MB full) while receiving repeated strm/flush frames.
SP_LIVE02_STUCK = (
    "53544d7400000000300000002fffff000000000034566cffff0042b3b0"
    "0035d5400035cb780000003200000000c3ea00000000"
)

# squeezelite — 53 bytes incl. trailing error_code.
SL_STMT = "53544d74000000002000000001c46900000000000ee925ffff00b1ae400035d5400032c6d00000002c00000000ad21000000000000"
SL_STMS = "53544d73000000002000000000065e0000000000010000ffff00b285c60035d54000154f7800000000000000000000000000000000"
SL_STMF = "53544d66000000002000000001bc3e00000000001c17d6ffff00b281c80035d5400032a64000000000000000000000000000000000"

# Expected values, hand-derived from the Perl unpack order above.
# name: (frame, {field: value})
EXPECTED = {
    "SP_STMT": (SP_STMT, dict(
        event="STMt", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=3007,
        bytes_received_h=0, bytes_received_l=15928, bytes_received=15928,
        signal_strength=65535, jiffies=7730047,
        output_buffer_size=3528000, output_buffer_fullness=216952,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=0.0,
    )),
    "SP_STMO": (SP_STMO, dict(
        event="STMo", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=0,
        bytes_received_h=0, bytes_received_l=0, bytes_received=0,
        signal_strength=65535, jiffies=8622676,
        output_buffer_size=3528000, output_buffer_fullness=0,
        elapsed_seconds=110, voltage=0, elapsed_milliseconds=110373,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=110.373,
    )),
    "SP_STMF": (SP_STMF, dict(
        event="STMf", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=0,
        bytes_received_h=0, bytes_received_l=0, bytes_received=0,
        signal_strength=65535, jiffies=7729815,
        output_buffer_size=3528000, output_buffer_fullness=0,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=0.0,
    )),
    "SP_STMD": (SP_STMD, dict(
        event="STMd", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=0,
        bytes_received_h=0, bytes_received_l=529200, bytes_received=529200,
        signal_strength=65535, jiffies=3900801,
        output_buffer_size=3528000, output_buffer_fullness=1058400,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=0.0,
    )),
    "SP_STMS": (SP_STMS, dict(
        event="STMs", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=10592,
        bytes_received_h=0, bytes_received_l=82004, bytes_received=82004,
        signal_strength=65535, jiffies=7730633,
        output_buffer_size=3528000, output_buffer_fullness=1484416,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=87,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=0.087,
    )),
    "SP_CONT_ACK": (SP_CONT_ACK, dict(
        event="cont", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=3145728, buffer_fullness=0,
        bytes_received_h=0, bytes_received_l=0, bytes_received=0,
        signal_strength=65535, jiffies=7729943,
        output_buffer_size=3528000, output_buffer_fullness=0,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=51,
        elapsed_seconds_precise=0.0,
    )),
    "SL_STMT": (SL_STMT, dict(
        event="STMt", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=2097152, buffer_fullness=115817,
        bytes_received_h=0, bytes_received_l=977189, bytes_received=977189,
        signal_strength=65535, jiffies=11644480,
        output_buffer_size=3528000, output_buffer_fullness=3327696,
        elapsed_seconds=44, voltage=0, elapsed_milliseconds=44321,
        server_timestamp=0, error_code=0, length=53,
        elapsed_seconds_precise=44.321,
    )),
    "SL_STMS": (SL_STMS, dict(
        event="STMs", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=2097152, buffer_fullness=1630,
        bytes_received_h=0, bytes_received_l=65536, bytes_received=65536,
        signal_strength=65535, jiffies=11699654,
        output_buffer_size=3528000, output_buffer_fullness=1396600,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=53,
        elapsed_seconds_precise=0.0,
    )),
    "SL_STMF": (SL_STMF, dict(
        event="STMf", num_crlf=0, mas_initialized=0, mas_mode=0,
        buffer_size=2097152, buffer_fullness=113726,
        bytes_received_h=0, bytes_received_l=1841110, bytes_received=1841110,
        signal_strength=65535, jiffies=11698632,
        output_buffer_size=3528000, output_buffer_fullness=3319360,
        elapsed_seconds=0, voltage=0, elapsed_milliseconds=0,
        server_timestamp=0, error_code=0, length=53,
        elapsed_seconds_precise=0.0,
    )),
}

# Perl ``unpack('a4CCCNNNNnNNNNnNNn')`` as an struct format — an
# independent second implementation used to cross-check the parser.
_PERL_STRUCT = ">4sBBBIIIIHIIIIHIIH"
_PERL_NAMES = (
    "event", "num_crlf", "mas_initialized", "mas_mode", "buffer_size",
    "buffer_fullness", "bytes_received_h", "bytes_received_l",
    "signal_strength", "jiffies", "output_buffer_size",
    "output_buffer_fullness", "elapsed_seconds", "voltage",
    "elapsed_milliseconds", "server_timestamp", "error_code",
)


def _perl_unpack_53(payload: bytes) -> dict:
    vals = struct.unpack(_PERL_STRUCT, payload[:53])
    out = dict(zip(_PERL_NAMES, vals))
    out["event"] = out["event"].decode("ascii")
    return out


# ──────────────────────────────────────────────────────────────────────
# 1. Parser
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_stat_frame_fields(name):
    hexstr, expected = EXPECTED[name]
    payload = bytes.fromhex(hexstr)
    assert len(payload) == expected["length"]
    got = SlimProtoClient.parse_stat_payload(payload)
    for field, want in expected.items():
        assert got[field] == want, f"{name}.{field}: {got[field]!r} != {want!r}"


@pytest.mark.parametrize("name", ["SL_STMT", "SL_STMS", "SL_STMF"])
def test_stat_parser_agrees_with_perl_unpack_struct(name):
    """53-byte frames: our parser == an independent struct mirror of Perl."""
    payload = bytes.fromhex(EXPECTED[name][0])
    got = SlimProtoClient.parse_stat_payload(payload)
    ref = _perl_unpack_53(payload)
    for field in ("num_crlf", "mas_initialized", "mas_mode", "buffer_size",
                  "buffer_fullness", "bytes_received_h", "bytes_received_l",
                  "signal_strength", "jiffies", "output_buffer_size",
                  "output_buffer_fullness", "elapsed_seconds", "voltage",
                  "elapsed_milliseconds", "server_timestamp", "error_code"):
        assert got[field] == ref[field], field
    assert got["event"] == ref["event"]


def test_stat_length_variants_and_error_code():
    """51-byte SqueezePlay frames carry no error_code (Perl forces 0)."""
    short = bytes.fromhex(SP_STMT)
    assert len(short) == 51
    assert SlimProtoClient.parse_stat_payload(short)["error_code"] == 0

    # A 53-byte frame keeps the real error_code.
    long = bytearray(short[:51])
    long += b"\x00\x07"
    got = SlimProtoClient.parse_stat_payload(bytes(long))
    assert got["length"] == 53
    assert got["error_code"] == 7

    # 57-byte legacy frame: same 53 fields + 4 trailing junk bytes.
    legacy = bytes(long) + b"\xde\xad\xbe\xef"
    got = SlimProtoClient.parse_stat_payload(legacy)
    assert got["length"] == 57
    assert got["error_code"] == 7
    assert got["jiffies"] == EXPECTED["SP_STMT"][1]["jiffies"]


def test_stat_truncated_payload_never_raises():
    for n in (0, 1, 3, 4, 7, 20, 25, 36, 50):
        got = SlimProtoClient.parse_stat_payload(bytes(range(n)))
        assert got["length"] == n
        if n < 37:
            assert got["output_buffer_fullness"] == 0


def test_live02_stuck_frame_is_a_real_player_state_not_a_parse_artifact():
    """LIVE-02: elapsed is frozen at 50.154 s while jiffies keeps counting.

    Decoded with the Perl/SqueezePlay layout this frame is unambiguously
    *internally consistent*: elapsed_seconds=50 matches
    elapsed_milliseconds=50154, decodeFull (3145727) ~= buffer_size, and
    the output buffer holds 3525496 of 3528000 bytes. So the "elapsed
    constant at 50 s / out_fullness 0" symptom in
    .hermes/gap-analysis/06 is genuine player state after repeated
    flush/restart, NOT a wrong field offset.
    """
    got = SlimProtoClient.parse_stat_payload(bytes.fromhex(SP_LIVE02_STUCK))
    assert got["elapsed_seconds"] == 50
    assert got["elapsed_milliseconds"] == 50154
    assert got["elapsed_seconds_precise"] == pytest.approx(50.154)
    assert got["buffer_size"] == 3145728
    assert got["buffer_fullness"] == 3145727
    assert got["output_buffer_size"] == 3528000
    assert got["output_buffer_fullness"] == 3525496
    # elapsed_ms is what makes the frozen output clock visible at all.
    assert got["elapsed_seconds"] * 1000 != got["elapsed_milliseconds"]


# ──────────────────────────────────────────────────────────────────────
# 2. Event dispatch is unchanged
# ──────────────────────────────────────────────────────────────────────

class _FakeManager:
    def __init__(self, player):
        self._player = player

    def get_player(self, mac):
        return self._player


def _client_and_player(monkeypatch, mode="stop", remote=0):
    player = PlayerState(mac="02:11:22:33:44:55", name="T", ip="127.0.0.1", port=0)
    player.mode = mode
    player.remote = remote
    fake = _FakeManager(player)
    monkeypatch.setattr(manager_mod, "PlayerManager", lambda: fake)
    # Keep the dispatch hermetic: without a Cometd manager the handler's
    # notify branch is skipped instead of building an un-awaited coroutine
    # (the tests here have no running event loop / no cometd clients).
    try:
        import lyrion.web.cometd as cometd_mod
        monkeypatch.setattr(cometd_mod, "get_manager", lambda: None)
    except Exception:  # pragma: no cover
        pass
    client = object.__new__(SlimProtoClient)
    return client, player


@pytest.mark.parametrize("event,start_mode,expected", [
    ("STMs", "stop", "play"),
    ("STMp", "play", "pause"),
    ("STMf", "play", "stop"),
    ("STMn", "play", "stop"),
    ("play", "pause", "play"),
    ("load", "stop", "loading"),
])
def test_stat_event_dispatch_unchanged(monkeypatch, event, start_mode, expected):
    client, player = _client_and_player(monkeypatch, mode=start_mode)
    payload = event.encode("ascii") + bytes(47)  # 51-byte frame, zeros
    client._handle_stat_frame(player.mac, payload)
    assert player.mode == expected


def test_stat_stmf_keeps_pause_mode(monkeypatch):
    client, player = _client_and_player(monkeypatch, mode="pause")
    payload = b"STMf" + bytes(47)
    client._handle_stat_frame(player.mac, payload)
    assert player.mode == "pause"


def test_stat_persists_elapsed_and_signal_strength(monkeypatch):
    client, player = _client_and_player(monkeypatch)
    client._handle_stat_frame(player.mac, bytes.fromhex(SP_LIVE02_STUCK))
    assert player.elapsed == pytest.approx(50.154)
    # 0xffff means "unknown" and must not clobber the stored value.
    assert player._stat["jiffies"] == 4371376


# ──────────────────────────────────────────────────────────────────────
# 3. strm frames vs the Perl pack template
# ──────────────────────────────────────────────────────────────────────

# Perl-generated 24-byte bodies (see module docstring).
PERL_STRM_BODY = {
    "p": "70306d3f3f3f3f0000003000000000000000000000000000",
    "f": "66306d3f3f3f3f0000003000000000000000000000000000",
    "q": "71306d3f3f3f3f0000003000000000000000000000000000",
    "u": "75306d3f3f3f3f0000003000000000000000000000000000",
    "a": "61306d3f3f3f3f0000003000000000000000000000000000",
}


@pytest.mark.parametrize("command", sorted(PERL_STRM_BODY))
def test_strm_control_frame_is_byte_identical_to_perl(command):
    frame = SlimProtoClient._build_strm_control_frame(command)
    assert frame[:2] == struct.pack(">H", 28)
    body = frame[2:]
    assert body[:4] == b"strm"
    assert body[4:].hex() == PERL_STRM_BODY[command]


def test_strm_control_frame_replay_gain_is_ms():
    # Perl stream('a', {interval}) -> replayGain = interval * 1000
    frame = SlimProtoClient._build_strm_control_frame("a", 3000)
    payload = frame[2:]           # b"strm" + 24-byte leaf
    assert payload[:4] == b"strm"
    assert payload[4:][14:18] == struct.pack(">I", 3000)


def test_strm_start_frame_matches_perl_except_known_threshold_delta():
    """Perl stream('s') local file: autostart 1, format 'm', pcm '?',
    transitionType '0', outputThreshold 1 for mp3, port 9000, ip 0.

    perl (threshold 255):
        73316d3f3f3f3fff00003000010000000000232800000000
    ours (threshold 50 -> PROT-07/P2, still open):
        73316d3f3f3f3f3200003000010000000000232800000000
    """
    request = b"GET /stream.mp3?player=021122334455 HTTP/1.0\r\n\r\n"
    frame = SlimProtoClient._build_stream_frame(
        request=request, codec="m", autostart=1, server_port=9000,
    )
    assert frame[:2] == struct.pack(">H", 4 + 24 + len(request))
    payload = frame[2:]
    assert payload[:4] == b"strm"
    leaf = payload[4:28]                     # the 24-byte Perl body
    assert leaf[0:1] == b"s"
    assert leaf[1:2] == b"1"                 # autostart
    assert leaf[2:3] == b"m"                 # format byte
    assert leaf[3:7] == b"????"              # pcm size/rate/chan/endian
    assert leaf[7] == 50                     # bufferThreshold (Perl: 255)
    assert leaf[8] == 0                      # spdif
    assert leaf[9] == 0                      # transitionDuration
    assert leaf[10:11] == b"0"               # transitionType NONE
    assert leaf[11] == 0                     # flags
    assert leaf[12] == 1                     # outputThreshold (mp3)
    assert leaf[13] == 0                     # slaves
    assert leaf[14:18] == bytes(4)           # replayGain
    assert leaf[18:20] == struct.pack(">H", 9000)
    assert leaf[20:24] == bytes(4)           # serverIp = 0 -> peer
    assert payload[28:] == request


# ──────────────────────────────────────────────────────────────────────
# 4. LIVE-02 idempotency guard: repeated cmd:load must not flush-restart
# ──────────────────────────────────────────────────────────────────────

class _FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written += data

    async def drain(self):
        return None

    def is_closing(self):
        return False


def _strm_client(monkeypatch, player):
    monkeypatch.setattr(manager_mod, "PlayerManager", lambda: _FakeManager(player))
    client = object.__new__(SlimProtoClient)
    writer = _FakeWriter()
    mac = player.mac.replace(":", "").upper()
    client._player_writers = {mac: writer}
    client.web_port = 9000
    return client, writer, mac


@pytest.mark.asyncio
async def test_repeat_load_of_same_playing_track_does_not_restart(monkeypatch):
    """LIVE-02: SqueezePlay repeats `playlistcontrol cmd:load <same track>`
    every ~2-6 s. The old code answered each repeat with `strm 'f'` +
    `strm 's'`, tearing down buffers before audio could start.
    """
    player = PlayerState(mac="1C:87:2C:47:FC:36", name="T", ip="127.0.0.1", port=0)
    player.mode = "play"
    player.current_track_id = 51994
    # The guard keys off the track we ACTUALLY streamed (not the optimistic
    # current_track_id the caller sets before sending) — see
    # tests/test_strm_idempotency.py for the first-play regression.
    player.strm_sent_track = 51994
    client, writer, mac = _strm_client(monkeypatch, player)

    ok = await client.send_strm_to_player(mac, 51994)
    assert ok is True
    assert writer.written == b""  # no flush, no strm


@pytest.mark.asyncio
async def test_strm_to_disconnected_player_returns_false(monkeypatch):
    player = PlayerState(mac="1C:87:2C:47:FC:36", name="T", ip="127.0.0.1", port=0)
    client = object.__new__(SlimProtoClient)
    client._player_writers = {}
    assert await client.send_strm_to_player("1C872C47FC36", 1) is False


def test_control_frame_rejects_multichar_command():
    with pytest.raises(ValueError):
        SlimProtoClient._build_strm_control_frame("pp")
