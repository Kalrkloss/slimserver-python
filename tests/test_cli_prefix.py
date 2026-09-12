"""CTRL-02 rest: the client-id prefix rule, the exit/quit echo, the echo path.

Perl is the sole reference.  The reply line is ``<clientid> <terms...>`` and
the client id is prepended **iff the request resolved to a client**:

* ``Slim/Control/Stdio.pm:123-138`` ``array_to_string`` —
  ``unshift @elements, $clientid if defined $clientid;`` (:131).  The id
  already carries the escaping, so it is the *first* element of the line.
* ``Slim/Control/Stdio.pm:96-116`` ``string_to_array`` — the id comes from the
  FIRST token when it resolves to a player
  (``Slim::Player::Client::getClient($elements[0])``, :108) and is then removed
  from the request array (:112).
* ``Slim/Plugin/CLI/Plugin.pm:578-599`` — for a request that matched a dispatch
  entry with the ``requires Client`` flag (the ``C`` column,
  ``Slim/Control/Request.pm:474-637``) and that carries no client, one is
  allocated from ``Slim::Player::Client::clientRandom()`` and stored on the
  request (:592-599) *before* it is dispatched.
* ``Slim/Control/Request.pm:1021-1041`` — which of the two leaf variants is
  used: the *command* variant (:1026) when the last request token is not ``?``,
  the *query* variant (:1036) when it is.  If the chosen variant does not exist
  the request never becomes dispatchable.
* ``Slim/Control/Request.pm:1063-1101`` + ``Slim/Plugin/CLI/Plugin.pm:657-663``
  — a request that matches no dispatch leaf gets ``_status = 104``, every token
  becomes the positional parameter ``_p<i>`` (:1095-1100) and the request is
  **echoed verbatim** ("will echo as is...", :659).  It never gets a client
  allocated, so its echo is **bare**.  Perl has no "unknown command" answer at
  all (``cli_request_write``, :665/:692-698).

The rule this file pins, therefore:

    prefix  ⟺  the request resolved to a client
              = a leading player MAC token in the request line
                (Stdio.pm:96-116), or
                a dispatch entry with requiresClient=1 **whose variant for
                this request exists** (Request.pm:1021-1041 +
                Plugin/CLI/Plugin.pm:592-599)

Every fixture below is a REAL Perl CLI answer, captured read-only on
2026-09-12 against the Perl LMS 9.1.1 at 192.168.1.90:9090 with::

    timeout 7 bash -c 'exec 3<>/dev/tcp/192.168.1.90/9090; \
        printf "<command>\n" >&3; timeout 5 cat <&3'

Only ``?``-queries and read-only commands were sent (no power/play/pause/stop/
sync/alarm/pref write, no ``playlist …`` mutation — those would change the
production LMS).  The captured line is given per test as ``LIVE``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
# The MAC the live Perl LMS answered for.  Perl echoes the request's own
# spelling; our renderer quotes uppercase hex, hence CLIENT.
LIVE_MAC = "24:0A:C4:29:77:90"
CLIENT = "24%3A0A%3AC4%3A29%3A77%3A90"


def _player(**kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(
        mac=LIVE_MAC,
        name="Schlafzimmer",
        ip="192.168.1.154",
        port=52091,
        model="squeezeesp32",
        model_name="SqueezeESP32",
        firmware="v1.0-1672-16",
        connected=True,
        power=True,
        mode="stop",
        volume=23,
        seq_no=0,
        signal_strength=44,
        remote=1,
        current_title="Substan - Chasing The Light",
        playlist_total=1,
        playlist_position=0,
        repeat=0,
        shuffle=0,
    )
    base.update(kw)
    return PlayerState(**base)


def _pm_with(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in players}
    return pm


def _dispatch(cmd: str, args: list[str],
              player_id: Optional[str] = LIVE_MAC) -> list[str]:
    handler = CLIHandler()
    ctx = CLIContext(player_id=player_id)
    ctx.command = cmd
    return asyncio.run(handler.dispatch(ctx, (cmd, args)))


# ---------------------------------------------------------------------------
# 1. a non-dispatchable request is echoed BARE (no client id)
#    Request.pm:1063-1101 (status 104) + Plugin/CLI/Plugin.pm:657-663
#    + Stdio.pm:131 (no id defined → nothing prepended)
# ---------------------------------------------------------------------------
def test_playlist_question_is_echoed_bare_like_perl():
    # Request.pm:548-591 — 'playlist' is only a *prefix* in the verb tree (its
    # leaves are 'playlist <verb> …'); the token '?' reaches no leaf, so the
    # request is not dispatchable and is echoed without a client id.
    # LIVE: `playlist ?` → `playlist %3F`
    _pm_with(_player())
    assert _dispatch("playlist", ["?"]) == ["playlist %3F"]


def test_randomplay_question_is_echoed_bare_like_perl():
    # RandomPlay/Plugin.pm:178 registers `['randomplay', '_mode']` — a command
    # entry only (the trailing '_mode' is not '?', so `::[1]` stays unset).
    # Request.pm:1036 therefore selects a query variant that does not exist →
    # status 104 → echo.  LIVE: `randomplay ?` → `randomplay %3F`
    _pm_with(_player())
    assert _dispatch("randomplay", ["?"]) == ["randomplay %3F"]


def test_alarms_question_is_echoed_bare_like_perl():
    # Request.pm:477 registers `['alarms','_index','_quantity']` — a command
    # entry only; `?` selects the (missing) query variant → status 104.
    # LIVE: `alarms ?` → `alarms %3F`
    assert _dispatch("alarms", ["?"]) == ["alarms %3F"]


def test_mode_without_a_variant_is_echoed_bare_like_perl():
    # `['mode','?']` is the query (Request.pm:525); the command variants are the
    # *literal* children 'pause'/'play'/'stop' (:664-666), so the bare word
    # 'mode' has no command variant at its own leaf → status 104 → bare echo.
    # LIVE: `mode` → `mode`
    assert _dispatch("mode", []) == ["mode"]


def test_signalstrength_and_version_bare_are_echoed_bare_like_perl():
    # `['signalstrength','?']` (:613) and `['version','?']` (:630) have a query
    # variant only → the bare word is not dispatchable.  LIVE: `signalstrength`
    # → `signalstrength`, `version` → `version`.
    assert _dispatch("signalstrength", []) == ["signalstrength"]
    assert _dispatch("version", []) == ["version"]


def test_a_question_request_of_a_bare_prefix_is_echoed_bare_like_perl():
    # 'playlist'/'search'/'mode' as bare prefixes.  LIVE: `playlist next` →
    # `playlist next` (no Perl leaf at all — the verb is an unknown request and
    # is echoed token-by-token), `search ?` → `search %3F`.
    _pm_with(_player())
    assert _dispatch("playlist", ["next"]) == ["playlist next"]


# ---------------------------------------------------------------------------
# 2. an id from the request line IS kept (Stdio.pm:96-116 + :131)
# ---------------------------------------------------------------------------
def test_a_leading_player_mac_is_echoed_back_with_the_echo():
    # string_to_array() shifts the leading player token out of the request and
    # keeps it as the client id, so the echo of a non-dispatchable request is
    # prefixed — even though the words alone would not be.
    # LIVE: `<mac> playlist ?`      → `<mac> playlist %3F`
    #       `<mac> randomplay ?`    → `<mac> randomplay %3F`
    #       `<mac> bogus zzz`       → `<mac> bogus zzz`
    _pm_with(_player())
    assert _dispatch(LIVE_MAC, ["playlist", "?"]) == [f"{CLIENT} playlist %3F"]
    assert _dispatch(LIVE_MAC, ["randomplay", "?"]) == [f"{CLIENT} randomplay %3F"]


# ---------------------------------------------------------------------------
# 3. a requiresClient dispatch keeps the session client id
#    (Request.pm 474-637 'C' column + Plugin/CLI/Plugin.pm:592-599)
# ---------------------------------------------------------------------------
def test_requires_client_queries_are_prefixed():
    # LIVE: `mode ?` → `<mac> mode stop`, `name ?` → `<mac> name Schlafzimmer`,
    #       `signalstrength ?` → `<mac> signalstrength 45`
    _pm_with(_player())
    assert _dispatch("mode", ["?"]) == [f"{CLIENT} mode stop"]
    assert _dispatch("name", ["?"]) == [f"{CLIENT} name Schlafzimmer"]
    line = _dispatch("signalstrength", ["?"])[0]
    assert line.startswith(f"{CLIENT} signalstrength ")


def test_the_literal_mode_commands_are_prefixed():
    # Request.pm:664-666 — 'mode stop' hits the literal child 'stop' (a command
    # entry, requiresClient=1), so the echo carries the client id.
    # LIVE (read-only equivalent): `mode ?` → `<mac> mode stop`
    _pm_with(_player())
    for word in ("pause", "play", "stop"):
        assert _dispatch("mode", [word]) == [f"{CLIENT} mode {word}"]


def test_requires_no_client_queries_are_not_prefixed():
    # Request.pm:630 'version ?' and :536 'player count ?' have requiresClient=0
    # → nothing is allocated → no prefix.  LIVE: `version ?` → `version 9.1.1`,
    # `player count ?` → `player count 4`.
    _pm_with(_player())
    line = _dispatch("version", ["?"])[0]
    assert not line.startswith(CLIENT) and line.startswith("version "), line
    line = _dispatch("player", ["count", "?"])[0]
    assert not line.startswith(CLIENT) and line.startswith("player count "), line


# ---------------------------------------------------------------------------
# 4. randomplay dispatches when no '?' is sent (RandomPlay/Plugin.pm:178)
# ---------------------------------------------------------------------------
def test_randomplay_without_a_question_is_dispatched_with_the_mode_slot():
    # `addDispatch(['randomplay','_mode'], …)` — a command entry with
    # requiresClient=1, so the echo is prefixed and the unset '_mode' still
    # occupies its slot (Request.pm:1026-1028).  LIVE: `randomplay` →
    # `<mac> randomplay ` (trailing space from the unset slot).
    _pm_with(_player())
    assert _dispatch("randomplay", []) == [f"{CLIENT} randomplay "]
    assert _dispatch("randomplay", ["1"]) == [f"{CLIENT} randomplay 1"]


# ---------------------------------------------------------------------------
# 5. there is no "unknown command" — the request is echoed
#    Plugin/CLI/Plugin.pm:657-663 + Request.pm:1093-1100
# ---------------------------------------------------------------------------
def test_connect_question_is_echoed_not_invented():
    # Request.pm:496 `['connect','_where']` — a command entry only, so `connect
    # ?` selects a missing query variant → status 104 → echo.  LIVE:
    # `connect ?` → `connect %3F`
    assert _dispatch("connect", ["?"]) == ["connect %3F"]


def test_unknown_requests_are_echoed_verbatim():
    # LIVE: `bogus cmd x` → `bogus cmd x`, `<mac> bogus zzz` → `<mac> bogus zzz`
    _pm_with(_player())
    assert _dispatch("bogus", ["cmd", "x"]) == ["bogus cmd x"]
    assert _dispatch(LIVE_MAC, ["bogus", "zzz"]) == [f"{CLIENT} bogus zzz"]


def test_no_answer_ever_says_unknown_command():
    # The whole point: Perl answers the request, never an invented error.
    _pm_with(_player())
    for cmd, args in (
        ("connect", ["?"]), ("bogus", []), ("bogus", ["cmd", "x"]),
        ("playercount", []), ("logout", []), ("nosuchverb", ["a", "b"]),
    ):
        line = _dispatch(cmd, args)[0]
        assert "unknown command" not in line, line


# ---------------------------------------------------------------------------
# 6. exit/quit are echoed, THEN the connection closes
#    Plugin/CLI/Plugin.pm:618-619 ($exit = 1) + :665 (:692-698 write)
#    Plugin/CLI/Plugin.pm:318-333 client_socket_close closes the socket, with
#    no extra bytes — LIVE `exit` is exactly b"exit\n", `quit` b"quit\n"
# ---------------------------------------------------------------------------
class _FakeWriter:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def _wire(payload: bytes, monkeypatch) -> bytes:
    """Drive control.cli_server.handle_client over an in-memory connection."""
    from lyrion.control import cli_server
    import lyrion.control.request as request_mod

    class _StubDispatcher:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def start(self) -> None:
            return None

        def get_default_player(self) -> None:
            return None

        async def player_command(self, *a: Any, **k: Any) -> list[str]:
            return []

    class _Cfg:
        def get(self, key: str, default: Any = None) -> Any:
            return ""

    monkeypatch.setattr(request_mod, "RequestDispatcher", _StubDispatcher)
    monkeypatch.setattr("lyrion.config.get_config", lambda: _Cfg())

    async def run() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        writer = _FakeWriter()
        await cli_server.handle_client(reader, writer)
        return writer.data

    return asyncio.run(run())


def test_exit_is_written_before_the_socket_closes(monkeypatch):
    assert _wire(b"exit\n", monkeypatch) == b"exit\n"


def test_quit_is_written_before_the_socket_closes(monkeypatch):
    assert _wire(b"quit\n", monkeypatch) == b"quit\n"


def test_the_close_sends_no_extra_terminator(monkeypatch):
    # Perl writes exactly one terminator per answer (Plugin/CLI/Plugin.pm:698)
    # and closes without a trailing blank line; LIVE `exit` is b"exit\n".
    assert _wire(b"unsubscribe\nexit\n", monkeypatch) == b"unsubscribe\nexit\n"


def test_a_plain_command_then_eof_sends_only_the_answer(monkeypatch):
    # LIVE: `unsubscribe` → `unsubscribe\n` and then the peer closes.
    assert _wire(b"unsubscribe\n", monkeypatch) == b"unsubscribe\n"
