"""CLI text format (CTRL-02): Perl's ONE escaped line per answer.

Perl is the reference (``/tmp/lms-ref``, read-only):

* ``Slim/Plugin/CLI/Plugin.pm:692-698`` — the answer is
  ``array_to_string($request->clientid(), $request->renderAsArray())`` plus
  the connection's terminator (LF) ⇒ **one line per answer**.
* ``Slim/Control/Request.pm:2226-2296`` — ``renderAsArray`` conventions:
  request verbs first (:2242); params/results: ``__*`` suppressed
  (:2248/:2261), ``_*`` → bare value (:2288-2290), otherwise ``key:value``
  *without* a space (:2253/:2291); a key ending in ``_loop`` is unrolled
  inline, one ``key:value`` run per item, no loop name (:2264-2281); a plain
  ARRAY value is joined with ',' (:2284-2286).
* ``Slim/Control/Stdio.pm:123-138`` — client id in front when it is defined
  (:131), every element ``uri_escape_utf8``-escaped (:134), joined with one
  space (:137).
* ``Slim/Control/Request.pm:1021-1061`` — ``?`` handling: the query runs only
  when the *last* token is ``?``; the ``?`` occupies the declared parameter
  slot and is consumed (:1036), a ``_``-named slot echoes its token bare
  (:1027), surplus tokens are echoed positionally (:1059) or as ``key:value``
  for tag-capable requests (:1054).

All fixtures below are REAL Perl CLI answers, captured read-only on
2026-09-12 against the Perl LMS 9.1.1 at 192.168.1.90:9090::

    timeout 6 bash -c 'exec 3<>/dev/tcp/192.168.1.90/9090; \\
        printf "<command>\\n" >&3; timeout 4 cat <&3'

The round-trip tests rebuild the answer from the structured data and compare
byte for byte with the captured line.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lyrion import __version__
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.control.queries import escape, query_params, render_line
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

# ---------------------------------------------------------------------------
# Live Perl fixtures (192.168.1.90:9090, read-only, 2026-09-12)
# ---------------------------------------------------------------------------

PERL_PING = "ping"
PERL_VERSION = "version 9.1.1"
PERL_VER_ECHO = "ver 0 %3F"
PERL_PLAYERS_QUESTION = "players %3F"
PERL_PLAYER_COUNT = "player count 4"
PERL_MIXER_VOLUME = "24%3A0a%3Ac4%3A29%3A77%3A90 mixer volume 23"
PERL_MIXER_BASS = "24%3A0a%3Ac4%3A29%3A77%3A90 mixer bass 0"

# players 0 1 → count first (Queries.pm:2602), then the loop unrolled inline.
PERL_PLAYERS_0_1 = (
    "players 0 1 count%3A4 playerindex%3A0 playerid%3A24%3A0a%3Ac4%3A29%3A77%3A90 "
    "uuid%3A ip%3A192.168.1.154%3A52091 name%3ASchlafzimmer seq_no%3A0 "
    "model%3Asqueezeesp32 modelname%3ASqueezeESP32 power%3A1 isplaying%3A0 "
    "displaytype%3Anone isplayer%3A1 canpoweroff%3A1 connected%3A1 "
    "firmware%3Av1.0-1672-16 playerindex%3A1 playerid%3Aca%3Ac8%3Ac7%3A26%3A6d%3A38 "
    "uuid%3A7b62791c2b9745c7bb392644e774ceb2 ip%3A192.168.1.225%3A48512 "
    "name%3AK%EF%BF%BDche seq_no%3A0 model%3Asqueezeplay modelname%3ASB%20Player "
    "power%3A1 isplaying%3A0 displaytype%3Anone isplayer%3A1 canpoweroff%3A1 "
    "connected%3A1 firmware%3A0 playerindex%3A2 playerid%3A00%3A00%3A00%3A00%3A00%3A00 "
    "uuid%3A ip%3A192.168.1.130%3A45614 name%3ATaverne seq_no%3A0 "
    "model%3Asqueezelite modelname%3ASqueezeLite power%3A1 isplaying%3A0 "
    "displaytype%3Anone isplayer%3A1 canpoweroff%3A1 connected%3A1 "
    "firmware%3Av1.9.9-1449 playerindex%3A3 playerid%3A00%3A04%3A20%3A2b%3A88%3Ac8 "
    "uuid%3A7bc9731ee5024d382b6b24b107b5293a ip%3A192.168.1.127%3A47982 "
    "name%3ASqueezebox%20Radio seq_no%3A23 model%3Ababy modelname%3ASqueezebox%20Radio "
    "power%3A0 isplaying%3A0 displaytype%3Anone isplayer%3A1 canpoweroff%3A1 "
    "connected%3A1 firmware%3A7.7.3-r16676"
)

# The same four players as structured data (values exactly as Perl sent them).
PERL_PLAYERS = [
    {
        "playerindex": 0, "playerid": "24:0a:c4:29:77:90", "uuid": "",
        "ip": "192.168.1.154:52091", "name": "Schlafzimmer", "seq_no": 0,
        "model": "squeezeesp32", "modelname": "SqueezeESP32", "power": 1,
        "isplaying": 0, "displaytype": "none", "isplayer": 1,
        "canpoweroff": 1, "connected": 1, "firmware": "v1.0-1672-16",
    },
    {
        "playerindex": 1, "playerid": "ca:c8:c7:26:6d:38",
        "uuid": "7b62791c2b9745c7bb392644e774ceb2",
        # Perl's own answer carried U+FFFD here (its Latin-1 path mangled
        # 'Küche'); the escaping is what we pin down, not the mojibake.
        "ip": "192.168.1.225:48512", "name": "K\ufffdche", "seq_no": 0,
        "model": "squeezeplay", "modelname": "SB Player", "power": 1,
        "isplaying": 0, "displaytype": "none", "isplayer": 1,
        "canpoweroff": 1, "connected": 1, "firmware": "0",
    },
    {
        "playerindex": 2, "playerid": "00:00:00:00:00:00", "uuid": "",
        "ip": "192.168.1.130:45614", "name": "Taverne", "seq_no": 0,
        "model": "squeezelite", "modelname": "SqueezeLite", "power": 1,
        "isplaying": 0, "displaytype": "none", "isplayer": 1,
        "canpoweroff": 1, "connected": 1, "firmware": "v1.9.9-1449",
    },
    {
        "playerindex": 3, "playerid": "00:04:20:2b:88:c8",
        "uuid": "7bc9731ee5024d382b6b24b107b5293a",
        "ip": "192.168.1.127:47982", "name": "Squeezebox Radio", "seq_no": 23,
        "model": "baby", "modelname": "Squeezebox Radio", "power": 0,
        "isplaying": 0, "displaytype": "none", "isplayer": 1,
        "canpoweroff": 1, "connected": 1, "firmware": "7.7.3-r16676",
    },
]

# artists 0 1 → loop first, count last (Queries.pm:989); note the empty
# favorites_url placeholder and the double-escaped '%3F' inside a value.
PERL_ARTISTS_0_1 = (
    "artists 0 1 id%3A5620 artist%3A%3F "
    "favorites_url%3Adb%3Acontributor.name%3D%253F count%3A11151"
)

# albums 0 1 → an empty value ('performance%3A') and a value with its own
# escaping ('Kein%2520Album' = 'Kein%20Album').
PERL_ALBUMS_0_1 = (
    "albums 0 1 id%3A45 performance%3A "
    "favorites_url%3Adb%3Aalbum.title%3DKein%2520Album%26contributor.name%3DDiverse%2520Interpreten "
    "favorites_title%3AKein%20Album album%3AKein%20Album count%3A7181"
)

# status without a resolved client-index/quantity: Perl echoes two empty
# parameter tokens → a double space after the verb (Request.pm:1027 assigns
# undef, :2253 concatenates it as '').
PERL_STATUS_EMPTY_PARAMS = "status   player_name%3ASchlafzimmer"

MAC = "24:0A:C4:29:77:90"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _player(**kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(
        mac=MAC,
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
        signal_strength=49,
        remote=1,
        current_title="Substan - Chasing The Light",
        playlist_total=1,
        playlist_position=0,
        playlist_timestamp=1789068014.15917,
    )
    base.update(kw)
    return PlayerState(**base)


def _pm_with(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in players}
    return pm


def _dispatch(cmd: str, args: list[str], player_id: str = MAC) -> list[str]:
    handler = CLIHandler()
    ctx = CLIContext(player_id=player_id)
    ctx.command = cmd
    return asyncio.run(handler.dispatch(ctx, (cmd, args)))


# ---------------------------------------------------------------------------
# escaping — Stdio.pm:134 (uri_escape_utf8)
# ---------------------------------------------------------------------------
def test_escape_keeps_only_unreserved_characters():
    # The live Perl answers contain all of these.
    assert escape(":") == "%3A"
    assert escape("SB Player") == "SB%20Player"          # modelname%3ASB%20Player
    assert escape("K\ufffdche") == "K%EF%BF%BDche"       # name%3AK%EF%BF%BDche
    assert escape("192.168.1.154:52091") == "192.168.1.154%3A52091"  # '.' kept
    assert escape("v1.0-1672-16") == "v1.0-1672-16"      # '-' kept
    assert escape("") == ""
    assert escape(None) == ""                            # Perl undef → ''
    assert escape(True) == "1"
    assert escape(23) == "23"


def test_escape_percent_escapes_an_already_escaped_value_once():
    # favorites_url%3Adb%3Aalbum.title%3DKein%2520Album%26… (live albums line)
    value = "db:album.title=Kein%20Album&contributor.name=Diverse%20Interpreten"
    assert escape(value) == (
        "db%3Aalbum.title%3DKein%2520Album%26contributor.name%3DDiverse%2520Interpreten"
    )


# ---------------------------------------------------------------------------
# render_line — Request.pm:2226-2296 + Stdio.pm:123-138
# ---------------------------------------------------------------------------
def test_players_answer_round_trips_the_live_perl_line():
    line = render_line(
        terms=["players"],
        params=query_params(["0", "1"], ["_index", "_quantity"]),
        results=[("count", 4), ("players_loop", PERL_PLAYERS)],
    )
    assert line == PERL_PLAYERS_0_1


def test_artists_answer_round_trips_the_live_perl_line():
    line = render_line(
        terms=["artists"],
        params=query_params(["0", "1"], ["_index", "_quantity"]),
        results=[
            ("artists_loop", [{
                "id": 5620,
                "artist": "?",
                "favorites_url": "db:contributor.name=%3F",
            }]),
            ("count", 11151),
        ],
    )
    assert line == PERL_ARTISTS_0_1


def test_albums_answer_round_trips_the_live_perl_line():
    line = render_line(
        terms=["albums"],
        params=query_params(["0", "1"], ["_index", "_quantity"]),
        results=[
            ("albums_loop", [{
                "id": 45,
                "performance": "",
                "favorites_url": "db:album.title=Kein%20Album&contributor.name=Diverse%20Interpreten",
                "favorites_title": "Kein Album",
                "album": "Kein Album",
            }]),
            ("count", 7181),
        ],
    )
    assert line == PERL_ALBUMS_0_1


def test_empty_value_renders_as_key_without_value():
    # 'uuid%3A' in the live players line
    assert render_line(results=[("uuid", "")]) == "uuid%3A"
    assert render_line(results=[("performance", None)]) == "performance%3A"


def test_underscore_keys_render_bare_double_underscore_is_silent():
    # Request.pm:2288-2290 / :2261
    assert render_line(results=[("_version", "9.2.0")]) == "9.2.0"
    assert render_line(results=[("_count", 4)]) == "4"
    assert render_line(results=[("__private", "x"), ("count", 1)]) == "count%3A1"


def test_array_result_is_comma_joined():
    # Request.pm:2284-2286 (sync_slaves, genre lists, …)
    assert render_line(results=[("sync_slaves", ["aa:bb", "cc:dd"])]) == (
        "sync_slaves%3Aaa%3Abb%2Ccc%3Add"
    )


def test_client_id_is_prefixed_only_when_resolved():
    # Stdio.pm:131
    assert render_line(clientid="24:0a:c4:29:77:90", terms=["mixer", "volume"],
                       results=[("_volume", 23)]) == PERL_MIXER_VOLUME
    assert render_line(terms=["players"]) == "players"


def test_loop_is_unrolled_inline_without_loop_name():
    # Request.pm:2264-2281 — no 'item_loop'/'players_loop' token, no newline
    line = render_line(results=[("item_loop", [{"id": 1, "text": "a b"},
                                               {"id": 2, "text": ""}])])
    assert line == "id%3A1 text%3Aa%20b id%3A2 text%3A"
    assert "\n" not in line


# ---------------------------------------------------------------------------
# query_params — Request.pm:1021-1061 (the '?' echo rules)
# ---------------------------------------------------------------------------
def test_declared_underscore_slots_echo_their_token_bare():
    # status <mac> - 1 → '_index'/'_quantity' are declared → '<mac> - 1'
    assert query_params(["24:0a:c4:29:77:90", "-", "1"],
                        ["_index", "_quantity"]) == [
        ("_index", "24:0a:c4:29:77:90"), ("_quantity", "-"), ("_p2", "1"),
    ]


def test_query_marker_slot_is_consumed_and_not_echoed():
    # mixer volume ? → the '?' sits in the declared slot → nothing echoed
    assert query_params(["?"], ["?"], has_tags=False) == []
    # version ? → same
    assert query_params(["?", "?"], ["?"], has_tags=False) == [("_p1", "?")]


def test_surplus_tokens_echo_bare_or_tagged():
    # Request.pm:1049-1060: 'tags:AB' is a tagged param → 'tags%3AAB'
    assert query_params(["-", "1", "tags:AB"], ["_index", "_quantity"]) == [
        ("_index", "-"), ("_quantity", "1"), ("tags", "AB"),
    ]
    # no ':' → positional parameter, rendered as the bare value
    # (a request whose matching entry declares no parameter — the echo case —
    # treats every token as surplus: 'players ?' → '_p1' → 'players %3F')
    assert query_params(["?"], []) == [("_p0", "?")]


def test_missing_declared_slots_render_as_empty_tokens():
    # Perl: 'players' with no arguments answers 'players   count%3A4'
    # (two spaces — Request.pm:1027 assigns undef, :2253 concatenates '').
    assert query_params([], ["_index", "_quantity"]) == [
        ("_index", None), ("_quantity", None),
    ]
    assert render_line(terms=["status"],
                       params=query_params([], ["_index", "_quantity"]),
                       results=[("player_name", "Schlafzimmer")]) == PERL_STATUS_EMPTY_PARAMS


# ---------------------------------------------------------------------------
# handler level: every answer is ONE line (the Perl wire shape)
# ---------------------------------------------------------------------------
def test_ping_is_one_line():
    assert _dispatch("ping", []) == [PERL_PING]


def test_version_query_is_one_line_with_bare_version_result():
    assert _dispatch("version", ["?"]) == [f"version {__version__}"]


def test_version_without_trailing_question_marker_is_echoed():
    # Request.pm:1021-1024: only a trailing '?' selects the query entry.
    assert _dispatch("version", []) == ["version"]
    assert _dispatch("version", ["0"]) == ["version 0"]
    # …and a surplus '?' before the marker is echoed (live: 'version %3F 9.1.1').
    assert _dispatch("version", ["0", "?"]) == [f"version %3F {__version__}"]


def test_ver_alias_is_echoed_exactly_like_perl():
    # Perl has no 'ver' dispatch (Request.pm:474-637) — it echoes the request
    # (Plugin/CLI/Plugin.pm:657-663): live Perl answers 'ver 0 %3F'.
    assert _dispatch("ver", ["0", "?"]) == [PERL_VER_ECHO]


def test_player_count_query():
    _pm_with(_player())
    assert _dispatch("player", ["count", "?"]) == [f"player count 1"]


def test_players_is_one_line_with_inline_loop():
    _pm_with(_player())
    lines = _dispatch("players", ["0", "1"])
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("players 0 1 count%3A1 playerindex%3A0 playerid%3A")
    # the Perl field order of _addPlayersLoop (Queries.pm:2624-2663); the first
    # four tokens are the echoed request and 'count' (Queries.pm:2602)
    keys = [tok.split("%3A", 1)[0] for tok in line.split()]
    assert keys[:4] == ["players", "0", "1", "count"]
    assert keys[4:] == [
        "playerindex", "playerid", "uuid", "ip", "name", "seq_no", "model",
        "modelname", "power", "isplaying", "displaytype", "isplayer",
        "canpoweroff", "connected", "firmware",
    ]
    assert "uuid%3A" in line            # empty value kept
    assert "player index" not in line   # …and no human-readable leftover


def test_players_with_surplus_question_marker_is_echoed():
    _pm_with(_player())
    # 'players' declares only _index/_quantity (Request.pm:547) → '?' is
    # surplus and the query entry does not exist → Perl echoes (live).
    assert _dispatch("players", ["?"]) == [PERL_PLAYERS_QUESTION]
    assert _dispatch("players", [])[0].startswith("players   count%3A")


def test_mixer_volume_query_is_client_prefixed_and_bare():
    _pm_with(_player())
    assert _dispatch("mixer", ["volume", "?"]) == [
        "24%3A0A%3AC4%3A29%3A77%3A90 mixer volume 23"
    ]
    assert _dispatch("mixer", ["bass", "?"]) == [
        "24%3A0A%3AC4%3A29%3A77%3A90 mixer bass 0"
    ]


def test_mixer_volume_set_echoes_the_value_like_perl():
    pm = _pm_with(_player())
    calls: list[tuple[str, int]] = []

    async def _set_volume(player_id: str, volume: int) -> bool:
        calls.append((player_id, volume))
        return True

    pm.set_volume = _set_volume  # type: ignore[method-assign]

    def _dispatch_set() -> list[str]:
        handler = CLIHandler()
        ctx = CLIContext(player_id=MAC)
        ctx.command = "mixer"
        return asyncio.run(handler.dispatch(ctx, ("mixer", ["volume", "50"])))

    lines = _dispatch_set()
    assert len(lines) == 1
    # Request.pm:524 declares '_newvalue' for the command → echoed bare.
    assert lines[0] == "24%3A0A%3AC4%3A29%3A77%3A90 mixer volume 50"
    assert calls == [(MAC, 50)]


def test_trailing_question_marker_echoes_requests_without_a_query_entry():
    _pm_with(_player())
    assert _dispatch("status", ["?"]) == ["status %3F"]     # live Perl
    assert _dispatch("players", ["0", "?"]) == ["players 0 %3F"]  # live Perl


def test_status_is_one_line_client_id_first_with_playlist_loop_inline():
    _pm_with(_player())
    lines = _dispatch("status", ["-", "1"])
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(
        "24%3A0A%3AC4%3A29%3A77%3A90 status - 1 player_name%3ASchlafzimmer "
        "player_connected%3A1 player_ip%3A192.168.1.154%3A52091 power%3A1 "
        "signalstrength%3A49 mode%3Astop remote%3A1 "
        "current_title%3ASubstan%20-%20Chasing%20The%20Light time%3A0 rate%3A1"
    )
    # Perl's statusQuery order (Queries.pm:4172-4230): mixer volume, playlist
    # repeat/shuffle/mode, seq_no, playlist_cur_index, …_timestamp, …_tracks,
    # randomplay, digital_volume_control, use_volume_control.
    order = [tok.split("%3A", 1)[0] for tok in line.split() if "%3A" in tok]
    tail = ["mixer%20volume", "playlist%20repeat", "playlist%20shuffle",
            "playlist%20mode", "seq_no", "playlist_cur_index",
            "playlist_timestamp", "playlist_tracks", "randomplay",
            "digital_volume_control", "use_volume_control"]
    assert order[-len(tail):] == tail
    assert "playlist%20mode%3Aoff" in line   # Perl hardcodes 'off' (Queries.pm:4193)


def test_status_tag_tokens_are_echoed_escaped():
    _pm_with(_player())
    line = _dispatch("status", ["-", "1", "tags:Aa"])[0]
    # live Perl: '… status 24%3A0a… - 1 tags%3AABdejJKlrStTuxy player_name%3A…'
    assert " status - 1 tags%3AAa player_name%3A" in line
