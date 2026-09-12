"""`seq_no` echo — the fix for SqueezePlay's volume/power request storm.

SqueezePlay maintains volume and power locally and sends each change with a
`seq_no:<N>` param. Perl stores it per client
(Slim/Player/Client.pm:208-210; mixer → Commands.pm:559-562, power →
:2586-2589), emits it in the status (Queries.pm:4196) and in the audg frame
(Squeezebox2.pm:312-317). jive's Player.lua:1223-1333 compares the value with
its own counter; on a mismatch it reverts the volume and re-sends — with the
old hardcoded `seq_no: 0` the live client looped (`mixer volume 100` 55k times,
`status` 26k times in one session), which made the whole UI sluggish.
"""

import asyncio
import struct

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI, _seq_no_from_args

MAC = "1c:87:2c:47:fc:36"
MAC_CLEAN = "1C872C47FC36"


# ----------------------------------------------------------------------
# (a) parsing the client's param
# ----------------------------------------------------------------------
def test_seq_no_is_parsed_from_jive_style_args():
    # jive sends: ['power', '1', False, 'seq_no:54255'] (a boolean rides along)
    assert _seq_no_from_args(["1", False, "seq_no:54255"]) == 54255
    assert _seq_no_from_args(["volume", "100", "seq_no:7"]) == 7


def test_no_seq_no_means_none():
    assert _seq_no_from_args(["volume", "100"]) is None
    assert _seq_no_from_args([None, False, 5]) is None
    assert _seq_no_from_args([]) is None


# ----------------------------------------------------------------------
# (b) the recorded value reaches the playerstatus payload
# ----------------------------------------------------------------------
class _PM:
    def __init__(self, player):
        self._player = player

    def get_player(self, mac):
        return self._player

    def get_all_players(self):
        return [self._player]


def _status(player):
    api = JSONRPCAPI()
    return asyncio.run(
        api._json_player_status(_PM(player), player.mac, ["-", "10", "menu:menu"])
    )


def test_status_echoes_the_clients_sequence_number():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    player.power = True
    player.seq_no = 54255

    assert _status(player)["seq_no"] == 54255


def test_status_seq_no_defaults_to_zero():
    player = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    assert _status(player)["seq_no"] == 0


# ----------------------------------------------------------------------
# (c) audg carries the sequence number (Perl always sends the 7-field form)
# ----------------------------------------------------------------------
class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass

    def is_closing(self):
        return False


def _client_with_writer():
    player = PlayerState(mac=MAC, name="T", ip="127.0.0.1", port=1234)
    player.volume = 50
    player.seq_no = 54255
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC_CLEAN: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm

    client = SlimProtoClient.__new__(SlimProtoClient)
    writer = _FakeWriter()
    client._player_writers = {MAC_CLEAN: writer}
    return client, writer


def test_audg_frame_has_the_perl_seven_field_layout():
    client, writer = _client_with_writer()

    assert asyncio.run(client.send_volume_to_player(MAC, 100)) is True

    frame = writer.frames[-1]
    length = struct.unpack(">H", frame[:2])[0]
    payload = frame[2:]
    assert length == len(payload)
    assert payload[:4] == b"audg"
    body = payload[4:]
    # oldGainL, oldGainR, dvc, preamp, gainL, gainR, sequenceNumber
    # (Perl Squeezebox2.pm:313 pack('NNCCNNN'))
    assert len(body) == 4 + 4 + 1 + 1 + 4 + 4 + 4
    assert struct.unpack(">I", body[-4:])[0] == 54255
    assert struct.unpack(">I", body[14:18])[0] == int(100 * 655.36)
