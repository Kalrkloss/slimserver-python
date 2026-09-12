"""Slimproto-Opcode-Dispatch nach Perl (Audit B1/B2/B5, PROT-01/02).

Perl ``Slim/Networking/Slimproto.pm``:

* ``:52-72``  ``%message_handlers`` — the opcode is the RAW 4-byte ASCII name
  (``'IR  '`` is space padded) and the lookup is a case-sensitive hash lookup.
* ``:921-937`` ``_bye_handler`` — ``chr(1)`` asks for a firmware upgrade;
  Perl NEVER closes the control connection here.
* ``:599-679`` ``_disco_handler`` — ``DSCO`` reports a data-channel disconnect
  (reason byte 0..4), resets ``connecting``/``readyToStream`` and keeps the
  control connection open.
* ``:939-942`` ``_shut_handler`` — the only handler that closes the socket.
* ``:432``     unknown opcode -> ``Unknown slimproto op`` warning, socket stays.

Live finding 2026-09-12 (audit B1): our dispatch lower-cased the opcode and
compared it against ``"bye"``, so a real ``BYE!`` frame fell through to the
debug branch; and even on a match our semantics (close the socket) were wrong.
"""

from __future__ import annotations


import pytest

from lyrion.networking.protocol import (
    DISCO_REASONS,
    PERL_CLOSE_OPCODES,
    PERL_HANDLER_NAMES,
    PERL_KEEP_OPEN_OPCODES,
    PERL_MESSAGE_HANDLERS,
    SlimProtoClient,
    classify_opcode,
)
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"


def test_handler_table_matches_perl():
    # Slimproto.pm:52-72, verbatim (note the space padded IR opcode)
    assert PERL_MESSAGE_HANDLERS == {
        "ANIC", "BODY", "BUTN", "BYE!", "DBUG", "DSCO", "IR  ", "KNOB",
        "META", "RAWI", "RESP", "SETD", "STAT", "UREQ", "ALSS", "SHUT",
    }
    assert "IR  " in PERL_MESSAGE_HANDLERS
    assert "IR" not in PERL_MESSAGE_HANDLERS       # 4 bytes, space padded


@pytest.mark.parametrize("op_raw,kind", [
    ("SHUT", "close"),          # :939-942
    ("BYE!", "bye"),            # :921-937
    ("DSCO", "dsco"),           # :599-679
    ("STAT", "handler"),
    ("RESP", "handler"),
    ("SETD", "handler"),
    ("META", "handler"),
    ("IR  ", "handler"),
    ("BUTN", "handler"),
    ("KNOB", "handler"),
    ("RAWI", "handler"),
    ("ANIC", "handler"),
    ("ALSS", "handler"),
    ("UREQ", "handler"),
    ("DBUG", "handler"),
    ("BODY", "handler"),
    ("HELO", "helo"),
    ("ZZZZ", "unknown"),
])
def test_classify_opcode(op_raw, kind):
    assert classify_opcode(op_raw) == kind


def test_opcode_lookup_is_case_sensitive_like_perl():
    """The B1 bug: 'BYE!' must match, 'bye!' must not."""
    assert classify_opcode("BYE!") == "bye"
    assert classify_opcode("bye!") == "unknown"
    assert classify_opcode("Bye!") == "unknown"


def test_only_shut_closes_the_connection():
    assert PERL_CLOSE_OPCODES == {"SHUT"}
    assert PERL_KEEP_OPEN_OPCODES == {"BYE!", "DSCO"}
    # ... and every keep-open opcode is also a known handler
    assert PERL_KEEP_OPEN_OPCODES <= PERL_MESSAGE_HANDLERS


def test_dsco_reasons_match_perl_strings():
    # Slimproto.pm:603-609
    assert DISCO_REASONS == {
        0: "Connection closed normally",
        1: "Connection reset by local host",
        2: "Connection reset by remote host",
        3: "Connection is no longer able to work",
        4: "Connection timed out",
    }


def test_event_handlers_are_named_like_the_perl_subs():
    assert PERL_HANDLER_NAMES["IR  "] == "_ir_handler"
    assert PERL_HANDLER_NAMES["BUTN"] == "_button_handler"
    assert PERL_HANDLER_NAMES["KNOB"] == "_knob_handler"
    assert PERL_HANDLER_NAMES["RAWI"] == "_raw_ir_handler"
    assert PERL_HANDLER_NAMES["UREQ"] == "_update_request_handler"


def _client_with_player(strm_sent_track=None):
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.strm_sent_track = strm_sent_track
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {}
    return client, player


def test_bye_frame_keeps_the_player_and_does_not_raise():
    client, _player = _client_with_player()
    # chr(1) = firmware upgrade request (Perl _bye_handler :924-933)
    client._handle_bye_frame(MAC, b"\x01")
    client._handle_bye_frame(MAC, b"")
    client._handle_bye_frame(MAC, b"\x00")


def test_dsco_resets_the_strm_guard_like_ready_to_stream():
    """Perl: connecting(0) + readyToStream(1) — our equivalent is clearing the
    strm idempotency guard so a replay of the same track is streamed again."""
    client, player = _client_with_player(strm_sent_track=4242)
    client._handle_dsco_frame(MAC, b"\x04")       # "Connection timed out"
    assert player.strm_sent_track is None
    # an empty payload means reason 0 (Perl unpack('C', '') -> 0)
    client._handle_dsco_frame(MAC, b"")


def test_player_event_frames_are_recognised_and_keep_the_socket():
    client, _player = _client_with_player()
    for op in ("IR  ", "BUTN", "KNOB", "RAWI", "ANIC", "ALSS", "UREQ",
               "DBUG", "BODY"):
        client._handle_player_event_frame(MAC, op, b"\x01\x02")
        assert op not in PERL_CLOSE_OPCODES


def test_unknown_opcode_is_logged_not_closed(caplog):
    """Perl logs 'Unknown slimproto op' and keeps the socket (Slimproto.pm:432)."""
    client, _player = _client_with_player()
    assert classify_opcode("QUIT") == "unknown"
    # the classifier never reports a close for an unknown opcode
    for op in ("QUIT", "quit", "XXXX", ""):
        assert classify_opcode(op) != "close"


def test_dsco_does_not_touch_other_players():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.strm_sent_track = 4242
    other = PlayerState(mac="AA:BB:CC:DD:EE:02", name="Other", ip="127.0.0.1",
                        port=1235)
    other.strm_sent_track = 99
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player, "AABBCCDDEE02": other}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {}

    client._handle_dsco_frame(MAC, b"\x01")
    assert player.strm_sent_track is None
    assert other.strm_sent_track == 99, "DSCO must only affect that player"
