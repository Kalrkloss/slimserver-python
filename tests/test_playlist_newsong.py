"""``playlist newsong`` — the two Perl triggers, the sync rule, the CLI delivery.

Perl fires the notification from exactly two places:

* ``Slim/Player/StreamingController.pm:381-393`` (``_Playing``) — a song
  really started playing in the stream: **4 elements**

      if ( $last_song ) {
          Slim::Control::Request::notifyFromArray($self->master(),
              [ 'playlist', 'newsong',
                Slim::Music::Info::standardTitle(
                    $self->master(), $last_song->currentTrack()),
                $last_song->index() ] );
      }

* ``Slim/Music/Info.pm:534`` (``setCurrentTitle`` with a client, i.e. the
  in-stream ICY metadata path ``Protocols/HTTP.pm:333-355``) — **3 elements**,
  plus ``Info.pm:536-546`` for every other *playing master* on the same URL::

      Slim::Control::Request::notifyFromArray( $client,
          [ 'playlist', 'newsong', $title ] );

  The 2-argument ``setCurrentTitle`` (``Info.pm:476`` from ``getMetaData`` /
  ``readTags``, ``Commands.pm:1366``) has no client and therefore fires no
  newsong — only the ``currentTitleCallbacks``.

``newsong`` declares no parameters (``Request.pm:645``:
``[1,0,0,undef]``), so the surplus tokens become the positional ``_p2``
(title) and ``_p3`` (index) — ``AudioScrobbler`` uses exactly that to tell the
two forms apart (``Slim/Plugin/AudioScrobbler/Plugin.pm:322-327``).

The line a listening CLI client sees is
``Slim/Control/Stdio.pm:123-138`` (``array_to_string``: client id in front,
every element escaped) of ``renderAsArray`` (``Request.pm:2226-2296``).
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.control import notifications
from lyrion.control.cli_commands import cmd_listen
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.networking import protocol
from lyrion.player import streaming
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "24:0A:C4:29:77:90"
MAC2 = "00:04:20:2B:88:C8"
MAC3 = "1C:87:2C:47:FC:36"
MAC4 = "AA:BB:CC:DD:EE:FF"
CLIENT = "24%3A0A%3AC4%3A29%3A77%3A90"
TRACK = 47111
STREAM_URL = "http://stream.example/radio.mp3"


@pytest.fixture(autouse=True)
def _clean():
    """No controller, no queued notification and no CLI listener may survive."""
    streaming.reset()
    notifications.reset()
    pm = PlayerManager()
    saved = dict(pm.players)
    pm.players.clear()
    yield
    streaming.reset()
    notifications.reset()
    pm.players.clear()
    pm.players.update(saved)


def _player(mac: str, *, mode: str = "play", url: str = "", title: str = "",
            sync_master: str | None = None) -> PlayerState:
    player = PlayerState(mac=mac, name=mac, ip="127.0.0.1", port=0,
                         connected=True, mode=mode)
    player.current_url = url or None
    player.current_title = title
    player.sync_master = sync_master
    PlayerManager().players[mac] = player
    return player


def _capture() -> list[notifications.Notification]:
    seen: list[notifications.Notification] = []
    notifications.subscribe(seen.append)
    return seen


# ---------------------------------------------------------------------------
# The notification itself (Positionalität _p2/_p3, Rendering)
# ---------------------------------------------------------------------------

def test_four_element_form_renders_title_and_index():
    """``StreamingController.pm:381-393`` — verbs + title + index."""
    notification = notifications.Notification.from_array(
        MAC, ["playlist", "newsong", "Money", 3])

    assert notification.verbs == ["playlist", "newsong"]
    assert notification.request_string == "playlist newsong"
    assert notification.request_str == "playlist,newsong"
    # ``Request.pm:1049-1061`` — no declared parameters for 'newsong', so the
    # surplus tokens land as _p2/_p3, exactly what AudioScrobbler tests for.
    assert notification.params == [("_p2", "Money"), ("_p3", 3)]
    assert notification.get_param("_p3") == 3
    assert notification.render_as_array() == ["playlist", "newsong", "Money", 3]
    assert notification.render_line() == f"{CLIENT} playlist newsong Money 3"


def test_three_element_form_has_no_index():
    """``Info.pm:534`` — ``['playlist','newsong',$title]``, no ``_p3``."""
    notification = notifications.Notification.from_array(
        MAC, ["playlist", "newsong", "Bayern 3"])

    assert notification.params == [("_p2", "Bayern 3")]
    assert notification.get_param("_p3") is None
    assert notification.render_line() == f"{CLIENT} playlist newsong Bayern%203"


def test_request_without_client_is_not_prefixed():
    """``Stdio.pm:131`` — the client id is prefixed only ``if defined $clientid``."""
    notification = notifications.Notification.from_array(
        None, ["playlist", "newsong", "x", 1])
    assert notification.render_line() == "playlist newsong x 1"


def test_is_command_matches_verb_groups():
    """``Request.pm:2381-2395`` (``__matchingRequest``)."""
    notification = notifications.Notification.from_array(
        MAC, ["playlist", "newsong", "x", 1])
    assert notification.is_command([["playlist"], ["newsong", "stop"]])
    assert not notification.is_command([["playlist"], ["stop"]])
    assert not notification.is_command([["client"], ["new"]])


# ---------------------------------------------------------------------------
# Trigger 1 — StreamingController::_Playing (4 elements)
# ---------------------------------------------------------------------------

def test_playing_fires_four_element_newsong():
    """``_Playing`` :381-393 over the *default* hook of a live controller."""
    player = _player(MAC, mode="play", title="Money")
    player.playlist_position = 3

    control = streaming.get_controller(MAC)          # no explicit hooks
    assert control.hooks.get("playback_started") is not None
    control.songqueue = [TRACK]                      # $last_song
    control._set_playing_state(streaming.PlayingState.BUFFERING)
    control._set_streaming_state(streaming.StreamingState.STREAMING)

    seen = _capture()

    async def _run():
        # Perl's ``playerTrackStarted`` → controller event ``Started``
        # (StreamingController.pm:2250-2266) → action ``_Playing``.
        streaming.apply_event(MAC, "Started")
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert len(seen) == 1
    line = seen[0].render_line()
    assert line == f"{CLIENT} playlist newsong Money 3", line
    assert [k for k, _ in seen[0].params] == ["_p2", "_p3"]


def test_playing_uses_the_song_objects_own_index():
    """``$last_song->index()`` (``Song.pm:81-121``) wins over the player state."""
    player = _player(MAC, mode="play", title="Album Track")
    player.playlist_position = 99                    # must not be used

    seen = _capture()

    async def _run():
        await streaming.send_newsong_playing(MAC, {"index": 7})

    asyncio.run(_run())

    assert seen[0].render_line() == f"{CLIENT} playlist newsong Album%20Track 7"


def test_playing_without_a_song_sends_nothing():
    """``if ( $last_song )`` — an empty queue fires no notification (:381)."""
    _player(MAC, mode="play", title="x")
    control = streaming.get_controller(MAC)
    seen = _capture()

    async def _run():
        control.resolve("Started")            # songqueue is empty
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert seen == []


def test_act_playing_passes_the_queue_head():
    """``self.playing_song()`` == ``$songqueue->[-1]`` (:1805-1807)."""
    recorded: list[dict] = []
    hooks = streaming.StreamingHooks(
        playback_started=lambda **kw: recorded.append(kw))
    control = streaming.get_controller(MAC, hooks)
    control.songqueue = [TRACK, TRACK + 1]
    # ``Started`` needs the state the strm send prepared (:1350-1352).
    control._set_playing_state(streaming.PlayingState.BUFFERING)
    control._set_streaming_state(streaming.StreamingState.STREAMING)

    async def _run():
        control.resolve("Started")

    asyncio.run(_run())

    assert recorded == [{"master": "240AC4297790", "song": TRACK + 1}]


# ---------------------------------------------------------------------------
# Trigger 2 — Info::setCurrentTitle (3 elements) + sync rule
# ---------------------------------------------------------------------------

def test_metadata_change_fires_three_element_newsong():
    """``Info.pm:526-534`` — client form → 3-element notification."""
    _player(MAC, mode="play", url=STREAM_URL)
    seen = _capture()

    asyncio.run(protocol._notify_metadata_display(MAC, STREAM_URL, "New Song"))

    assert [n.render_line() for n in seen] == [
        f"{CLIENT} playlist newsong New%20Song"]
    assert [k for k, _ in seen[0].params] == ["_p2"]


def test_metadata_reaches_other_playing_master_on_the_same_url():
    """``Info.pm:536-546`` (Bug 17174) — other playing masters of that station."""
    _player(MAC, mode="play", url=STREAM_URL)
    _player(MAC2, mode="play", url=STREAM_URL)          # other master, same URL
    _player(MAC3, mode="play", url="http://other/x")    # different station
    _player(MAC4, mode="play", url=STREAM_URL,
            sync_master=MAC)                            # slave → excluded
    seen = _capture()

    asyncio.run(protocol._notify_metadata_display(MAC, STREAM_URL, "Titel"))

    assert [n.client_id for n in seen] == [MAC, MAC2]


def test_metadata_skips_players_that_are_not_playing():
    """``next unless $_ && $_->controller() && $_->isPlaying();`` (:540)."""
    _player(MAC, mode="play", url=STREAM_URL)
    stopped = _player(MAC2, mode="stop", url=STREAM_URL)
    assert stopped.mode == "stop"
    seen = _capture()

    asyncio.run(protocol._notify_metadata_display(MAC, STREAM_URL, "Titel"))

    assert [n.client_id for n in seen] == [MAC]


def test_stream_handler_fires_the_newsong_on_a_title_change(monkeypatch):
    """The STMu path calls ``setCurrentTitle($url,$title,$client)`` on change."""
    player = _player(MAC, mode="play", url=STREAM_URL, title="Radio")
    seen = _capture()
    client = object.__new__(protocol.SlimProtoClient)

    async def _run():
        client._handle_stmu_frame(MAC, b"StreamTitle='Artist - Neu';")
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert player.current_title == "Artist - Neu"
    assert [n.render_line() for n in seen] == [
        f"{CLIENT} playlist newsong Artist%20-%20Neu"]

    # ``Info.pm:516`` — an unchanged title does not enter the branch at all.
    seen.clear()

    async def _again():
        client._handle_stmu_frame(MAC, b"StreamTitle='Artist - Neu';")
        await asyncio.sleep(0.05)

    asyncio.run(_again())
    assert seen == []


def test_standard_title_defaults_to_the_current_song_title():
    """``standardTitle`` uses ``titleFormat`` 'TITLE' (``Info.pm:701-739``)."""
    player = _player(MAC, mode="play", title="Bayern 3")
    assert asyncio.run(streaming.standard_title(player)) == "Bayern 3"
    assert asyncio.run(streaming.standard_title(None)) == ""


# ---------------------------------------------------------------------------
# CLI delivery (Plugin/CLI/Plugin.pm:969-1017, Stdio.pm:123-138)
# ---------------------------------------------------------------------------

def test_listener_receives_the_rendered_line_and_unsubscribes():
    written: list[str] = []
    notifications.register_cli_connection("cli-1", written.append)

    notifications.notify_from_array(MAC, ["playlist", "newsong", "A", 1])
    assert written == []                     # not listening yet (:982)

    notifications.cli_set_listen("cli-1", "*")           # listen 1
    notifications.notify_from_array(MAC, ["playlist", "newsong", "A", 1])
    assert written == [f"{CLIENT} playlist newsong A 1"]

    notifications.cli_set_listen("cli-1", None)          # listen 0
    notifications.notify_from_array(MAC, ["playlist", "newsong", "B", 2])
    assert written == [f"{CLIENT} playlist newsong A 1"]


def test_a_term_filtered_listener_only_gets_its_terms():
    """``cli_subscribe_terms`` (``Plugin.pm:921-930`` + :1001-1006)."""
    written: list[str] = []
    notifications.register_cli_connection("cli-1", written.append)
    notifications.cli_set_listen("cli-1", [["playlist"], ["stop"]])

    notifications.notify_from_array(MAC, ["playlist", "newsong", "A", 1])
    assert written == []

    notifications.notify_from_array(MAC, ["playlist", "stop"])
    assert written == [f"{CLIENT} playlist stop"]


def test_a_cli_sourced_notification_is_not_echoed_to_its_sender():
    """``Plugin.pm:994-995`` — don't echo twice to the sender."""
    written: list[str] = []
    notifications.register_cli_connection("cli-1", written.append)
    notifications.register_cli_connection("cli-2", written.append)
    notifications.cli_set_listen("cli-1", "*")
    notifications.cli_set_listen("cli-2", "*")

    notifications.notify_from_array(MAC, ["playlist", "newsong", "A", 1],
                                    source="CLI", connection_id="cli-1")

    assert written == [f"{CLIENT} playlist newsong A 1"]   # only cli-2


def test_cli_listener_is_subscribed_only_while_someone_listens():
    """``cli_subscribe_manage`` (``Plugin.pm:934-966``)."""
    assert notifications._listeners == {}
    notifications.register_cli_connection("cli-1", lambda line: None)
    assert notifications._listeners == {}

    notifications.cli_set_listen("cli-1", "*")
    assert list(notifications._listeners) == [id(notifications._cli_subscribe_notification)]

    notifications.unregister_cli_connection("cli-1")
    assert notifications._listeners == {}


def test_cli_wire_listen_client_sees_the_newsong():
    """End to end: ``listen 1`` on the real CLI server socket."""
    async def _run():
        from lyrion.control.cli_server import handle_client

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"listen 1\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 2) == b"listen 1\n"

            notifications.notify_from_array(
                MAC, ["playlist", "newsong", "Money", 3])
            line = await asyncio.wait_for(reader.readline(), 2)
            assert line == f"{CLIENT} playlist newsong Money 3\n".encode()

            writer.write(b"listen ?\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 2) == b"listen 1\n"

            writer.write(b"listen 0\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 2) == b"listen 0\n"

            notifications.notify_from_array(
                MAC, ["playlist", "newsong", "Zwei", 4])
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), 0.2)
        finally:
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()

    asyncio.run(_run())


def test_listen_command_and_query_follow_perl():
    """``Plugin/CLI/Plugin.pm:799-845`` — echo, the toggle and ``_listen``."""
    handler = CLIHandler()
    ctx = CLIContext(client_id="cli-1")
    notifications.register_cli_connection(ctx.client_id, lambda line: None)

    async def _status():
        # Before any listen: Perl answers 0 (``defined(...)||0``).
        assert await cmd_listen(handler, ctx, ["?"]) == ["listen 0"]
        assert await cmd_listen(handler, ctx, []) == ["listen "]
        # ``listen`` without a parameter toggles (no subscribe hash yet) …
        assert notifications.cli_get_listen(ctx.client_id) == 1
        assert await cmd_listen(handler, ctx, ["1"]) == ["listen 1"]
        assert await cmd_listen(handler, ctx, ["?"]) == ["listen 1"]
        # ``listen 0`` cancels; 'on'/'off' numify to 0 in Perl (:826-831).
        assert await cmd_listen(handler, ctx, ["0"]) == ["listen 0"]
        assert await cmd_listen(handler, ctx, ["?"]) == ["listen 0"]
        assert await cmd_listen(handler, ctx, ["off"]) == ["listen off"]
        assert notifications.cli_get_listen(ctx.client_id) == 0

    handler._ctx = ctx                      # a handler without a socket
    asyncio.run(_status())


def test_status_subscription_is_reexecuted_after_the_perl_delay(monkeypatch):
    """``Queries.pm:3925-3990`` + ``Request.pm:2055-2102`` (``relevant - 1``)."""
    calls: list[str] = []
    from lyrion.control.cli import CLIHandler

    monkeypatch.setattr(CLIHandler, "notify_subscribers",
                        staticmethod(lambda player_id: calls.append(player_id)))

    notification = notifications.Notification.from_array(
        MAC, ["playlist", "newsong", "Money", 3])
    assert notifications.status_query_filter(notification, MAC) == 1.3
    assert notifications.status_query_filter(notification, MAC2) == 0

    # Bug 10064 (``Queries.pm:3972-3976``): ``isCommand([['playlist',
    # 'newmetadata']])`` is ONE verb group, so every ``playlist …``
    # notification is routed to the sync group of the notification's client.
    master = _player(MAC, mode="play", title="Money")
    master.sync_slaves = [MAC2]
    slave = _player(MAC2, mode="play", title="Money", sync_master=MAC)
    assert slave.sync_master == MAC
    assert notifications.status_query_filter(notification, MAC2) == 1.3
    assert notifications.status_query_filter(notification, MAC4) == 0

    async def _run():
        notifications._autoexecute(notification)
        await asyncio.sleep(0.05)
        assert calls == []                  # 1.3s - 1 = 0.3s timer
        await asyncio.sleep(0.4)

    asyncio.run(_run())
    assert calls == [MAC]


# ---------------------------------------------------------------------------
# The CLI request itself (Perl: dispatchable but functionless → status 104)
# ---------------------------------------------------------------------------

def test_a_cli_client_sending_playlist_newsong_gets_the_echo():
    """``Request.pm:645`` ``[1,0,0,undef]`` — no handler → echoed verbatim."""
    from lyrion.control.cli import CLIHandler

    handler = CLIHandler()
    ctx = CLIContext(client_id="cli-1")

    async def _run():
        return await handler.dispatch(ctx, ("playlist", ["newsong", "x"]))

    assert asyncio.run(_run()) == ["playlist newsong x"]
