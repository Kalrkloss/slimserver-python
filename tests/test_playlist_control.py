"""``playlistcontrol … cmd:load`` / ``play`` dispatch — Perl parity.

Live symptom this suite locks down: SqueezePlay sends
``playlistcontrol useContextMenu:1 cmd:load menu:1 track_id:<id>`` and
afterwards ``play``; the server answered ``successful: true`` and the UI
showed the track's metadata, but the player never started. Two independent
reasons existed in the dispatch:

* the queue was only ever replaced for a SINGLE track id — the comma
  separated list Perl accepts fell through and ``play`` then started
  whatever happened to be at the old index (or nothing at all);
* ``play`` re-streamed the item unconditionally, so ``play`` on an already
  playing player restarted it and ``play`` on a paused one never resumed;
  a stale ``playlist_position = -1`` made ``play`` a silent no-op.

Perl semantics (reference tree /tmp/lms-ref = LMS public/9.2, read-only):

* ``play control`` — ``playcontrolCommand`` (Slim/Control/Commands.pm:697-781):
  - ``$wantmode = 'resume' if ($curmode eq 'pause' && $wantmode eq 'play');``
    (:743) → ``Slim::Player::Source::playmode`` resume branch
    (Slim/Player/Source.pm:83-85) → ``$controller->resume`` → ``strm 'u'``,
    the file is NOT streamed again and the position survives.
  - from stop: ``$client->execute(['playlist','jump',
    Slim::Player::Source::playingSongIndex($client), $fadeIn])`` (:758-763);
    ``playlistJumpCommand`` powers the player on (:942-944) and starts that
    index via ``$client->controller()->play($newIndex, …)`` (:1020).
    ``playingSongIndex`` is ``return $song ? $song->index() : 0``
    (Source.pm:229-233) — never negative.
  - while already playing nothing happens: the whole action sits behind
    ``if ($curmode ne $wantmode)`` (:756).
* ``playlistcontrol cmd:load`` — (Commands.pm:1864-2168):
  - ``Slim::Player::Playlist::stopAndClear($client)`` first (:1941) →
    the queue is REPLACED, not appended.
  - ``track_id`` is split on commas and the sent order is kept
    (:2027-2049, ``%track_ids_order``).
  - dispatch to ``playlist loadtracks listRef`` (:2158-2162) and inside
    ``playlistXtracksCommand``: split again (:1688-1696), ``addTracks``
    (:1752-1753), then ``$client->execute(['playlist','jump',
    $jumpToIndex, $fadeIn])`` (:1796) with an undefined index → index 0 →
    the FIRST loaded track starts.
* items are used VERBATIM (only whitespace trimmed):
  ``my $url = blessed($item) ? $item->url : $item;``
  — playlistXitemCommand, Commands.pm:1354-1359.
"""

import asyncio

from lyrion.web.api import JSONRPCAPI

MAC = "02:11:22:33:44:55"


class _FakePlayer:
    def __init__(self, mode="stop", position=0, playlist=None):
        self.mac = MAC
        self.name = "TestPlayer"
        self.mode = mode
        self.power = False
        self.playlist = list(playlist or [])
        self.playlist_position = position
        self.playlist_total = len(self.playlist)
        self.playlist_modified = 0
        self.shuffle = 0
        self.repeat = 0
        self.current_track_id = None
        self.current_title = ""
        self.current_url = None
        self.remote = 0
        self.remote_meta = {}
        self.elapsed = 0.0
        self.pause_time = 0.0
        self.last_activity = 0.0
        self.playerprefs = {}


class _FakeHandler:
    """Records the strm/remote-stream frames the manager would emit."""

    def __init__(self):
        self.strm = []
        self.remote = []

    async def send_strm_to_player(self, mac, track_id):
        self.strm.append(track_id)
        return True

    async def send_remote_stream(self, mac, url, codec="m"):
        self.remote.append((url, codec))
        return True

    async def send_audio_outputs(self, mac, enabled):
        return True


class _FakePM:
    def __init__(self, player):
        self.player = player
        self.pause_calls = []
        self.modes = []
        self.started = []
        self.play_urls = []
        self.power_calls = []
        self._protocol_handler = _FakeHandler()

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

    async def power_on_for_playback(self, player):
        self.power_calls.append(player.mac)
        player.power = True

    async def play_url(self, pid, url, title=""):
        self.play_urls.append((url, title))
        player = self.player
        player.playlist = [url]
        player.playlist_position = 0
        player.playlist_total = 1
        player.mode = "play"
        player.current_url = url
        return True

    def send_command(self, mac, cmd):
        raise AssertionError(f"unexpected raw send: {cmd}")


def _run(coro):
    return asyncio.run(coro)


def _patch_start(monkeypatch, api, pm):
    """Patch the item start so the tests can assert the index it used."""

    async def fake_start(pm_arg, player, index):
        pm.started.append(index)
        return True

    monkeypatch.setattr(api, "_play_playlist_item", fake_start)


def _load_args(track_id):
    return ["useContextMenu:1", "cmd:load", "menu:1", f"track_id:{track_id}"]


# ── playlistcontrol cmd:load ────────────────────────────────────────────────


def test_load_single_track_replaces_queue_and_starts_it(monkeypatch):
    pm = _FakePM(_FakePlayer("stop", playlist=[111, 222]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "playlistcontrol", _load_args(80264)))
    assert pm.player.playlist == [80264], "cmd:load replaces the queue"
    assert pm.player.playlist_total == 1
    assert pm.started == [0], "load must start the first loaded item"


def test_load_comma_track_id_list_keeps_sent_order(monkeypatch):
    """Perl splits ``track_id`` on commas and keeps the sent order."""
    pm = _FakePM(_FakePlayer("stop", playlist=[999]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "playlistcontrol",
                           _load_args("80264,111,222")))
    assert pm.player.playlist == [80264, 111, 222]
    assert pm.started == [0], "the FIRST loaded track starts"


def test_load_with_real_start_sends_strm_and_sets_mode_play():
    """End-to-end through the real ``_play_playlist_item``."""
    pm = _FakePM(_FakePlayer("stop", playlist=[999]))
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "playlistcontrol", _load_args(4242)))
    assert pm._protocol_handler.strm == [4242]
    assert pm.player.mode == "play"
    assert pm.player.current_track_id == 4242
    assert "play" in pm.modes
    assert pm.power_calls == [MAC], "playing powers the player on (Commands.pm:942-944)"


def test_load_url_token_plays_the_bare_url():
    """``cmd:load url:<x>`` must not hand 'url:<x>' to the player."""
    pm = _FakePM(_FakePlayer("stop"))
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "playlistcontrol",
                           ["cmd:load", "url:http://stream/Hit.MP3", "title:Radio"]))
    assert pm.play_urls == [("http://stream/Hit.MP3", "")]


# ── playlistcontrol cmd:add / cmd:insert ────────────────────────────────────


def test_add_only_appends_and_never_starts(monkeypatch):
    pm = _FakePM(_FakePlayer("play", position=0, playlist=[1]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "playlistcontrol",
                           ["cmd:add", "track_id:42"]))
    assert pm.player.playlist == [1, 42]
    assert pm.started == [], "cmd:add must not start playback"
    assert pm.player.mode == "play", "cmd:add keeps the playmode"


def test_add_keeps_the_url_case():
    """URLs are case sensitive — Perl stores the item verbatim."""
    pm = _FakePM(_FakePlayer("stop"))
    api = JSONRPCAPI()
    _run(api._json_control(pm, MAC, "playlistcontrol",
                           ["cmd:add", "url:http://Host/Stream.MP3",
                            "title:Radio"]))
    assert pm.player.playlist == ["http://Host/Stream.MP3"]


def test_insert_goes_after_the_current_track_and_does_not_start(monkeypatch):
    pm = _FakePM(_FakePlayer("play", position=0, playlist=[1, 2, 3]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "playlistcontrol",
                           ["cmd:insert", "track_id:42"]))
    assert pm.player.playlist == [1, 42, 2, 3]
    assert pm.started == [], "cmd:insert (play next) does not start"


# ── play ────────────────────────────────────────────────────────────────────


def test_play_while_paused_resumes_without_restreaming(monkeypatch):
    pm = _FakePM(_FakePlayer("pause", position=0, playlist=[7]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "play", [""]))
    assert pm.pause_calls == [False], "resume goes through pause_player(False)"
    assert pm.started == [], "resume must NOT re-stream the file (Commands.pm:743)"
    assert pm.player.mode == "play"


def test_play_while_playing_is_a_noop(monkeypatch):
    pm = _FakePM(_FakePlayer("play", position=2, playlist=[1, 2, 3]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "play", [""]))
    assert pm.started == [], "already playing: Perl does nothing (Commands.pm:756)"
    assert pm.pause_calls == []


def test_play_from_stop_starts_the_current_index(monkeypatch):
    pm = _FakePM(_FakePlayer("stop", position=1, playlist=[1, 2, 3]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "play", [""]))
    assert pm.started == [1], "from stop: playlist jump playingSongIndex"


def test_play_with_stale_negative_index_starts_the_first_item(monkeypatch):
    """A failed strm send leaves position=-1; play must not become a no-op."""
    pm = _FakePM(_FakePlayer("stop", position=-1, playlist=[5, 6]))
    api = JSONRPCAPI()
    _patch_start(monkeypatch, api, pm)
    _run(api._json_control(pm, MAC, "play", [""]))
    assert pm.started == [0]
