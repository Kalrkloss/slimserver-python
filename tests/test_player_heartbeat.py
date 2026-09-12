"""Player heartbeat: `strm 't'` every 5 s (Perl requestStatus).

Perl keeps players alive with Slim/Networking/Slimproto.pm:199-241
(`check_all_clients`, interval `$check_all_clients_time = 5` at :40): every
interval each client gets `requestStatus()`, which is literally
`stream('t')` (Squeezebox2.pm:383-385), and a client that has not answered
for 3 intervals is disconnected.

We used to write a `setd` frame with id=1 every 10 s. That is not a
heartbeat: firmwareid 1 is the `digitalOutputEncoding` setting
(Squeezebox2.pm:916-919), so the "keepalive" was pushing value 0 into a real
player setting.

The expected bytes below are the output of the Perl pack itself:

    perl -e 'print unpack("H*", pack "aaaaaaaCCCaCCCNnN",
             "t",0,"m","?","?","?","?",0,0,0,"0",0,0,0,0,0,0)'
    -> 74306d3f3f3f3f0000003000000000000000000000000000  (24 bytes)
"""

from lyrion.networking.protocol import KEEPALIVE_SECONDS, SlimProtoClient

PERL_STRM_T_BODY = bytes.fromhex(
    "74306d3f3f3f3f0000003000000000000000000000000000"
)


def test_keepalive_interval_matches_perl():
    assert KEEPALIVE_SECONDS == 5.0          # Slimproto.pm:40


def test_status_request_frame_is_the_perl_stream_t_body():
    frame = SlimProtoClient._build_strm_control_frame("t")
    length = int.from_bytes(frame[:2], "big")
    assert frame[2:6] == b"strm"
    body = frame[6:]
    assert len(body) == 24
    assert length == 4 + 24
    assert body == PERL_STRM_T_BODY
    assert body[0:1] == b"t"
    assert body[14:18] == bytes(4)           # replayGain 0 for 't'
    assert body[18:20] == bytes(2)           # server_port 0
    assert body[20:24] == bytes(4)           # server_ip 0


def test_keepalive_frame_is_not_a_setd_write():
    """Guard against regressing to the old setd id=1 write, which the Perl
    table maps to the digitalOutputEncoding setting (Squeezebox2.pm:916-919).
    """
    frame = SlimProtoClient._build_strm_control_frame("t")
    assert b"setd" not in frame
    assert frame[2:6] == b"strm"
