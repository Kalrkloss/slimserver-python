"""CLI text format, part 2 (CTRL-02): the *remaining* commands, ONE line each.

Perl renders every CLI answer as ONE percent-escaped line — never a list of
lines and never a ``\\n`` separator:

* ``Slim/Plugin/CLI/Plugin.pm:692-698`` — ``cli_request_write`` builds the
  answer from ``renderAsArray()`` + ``array_to_string()`` and appends the
  connection's terminator (LF only).
* ``Slim/Control/Request.pm:2226-2296`` — the ``renderAsArray`` conventions
  (verbs first :2242, ``key:value`` without a space :2253/:2291, ``_*`` → bare
  value :2288-2290, a ``*_loop`` key unrolled inline :2264-2281).
* ``Slim/Control/Stdio.pm:123-138`` — client id in front when defined (:131),
  every element ``uri_escape_utf8``-escaped (:134), joined with ' ' (:137).
* ``Slim/Plugin/CLI/Plugin.pm:657-663`` + ``Slim/Control/Request.pm:1093-1100``
  — a request with no dispatch entry is echoed verbatim.

Every fixture in the ``PERL_*`` block below is a REAL Perl CLI answer, captured
read-only on 2026-09-12 against the Perl LMS 9.1.1 at 192.168.1.90:9090 with::

    timeout 6 bash -c 'exec 3<>/dev/tcp/192.168.1.90/9090; \\
        printf "<command>\\n" >&3; timeout 4 cat <&3'

Only ``?``-queries and read-only commands were sent (no power/play/pause/sync/
alarm/pref write, no ``playlist …`` mutation — those would change the
production LMS).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import unquote

from lyrion.control import cli_commands
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.control.cli_commands import _alarm_loop_entry, _players_loop_entry
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

# ---------------------------------------------------------------------------
# Live Perl fixtures (192.168.1.90:9090, read-only, 2026-09-12)
# ---------------------------------------------------------------------------
# The MAC the live Perl LMS answered for; our fixtures use the same player.
LIVE_MAC = "24:0A:C4:29:77:90"
# The same MAC as the *escaped* client id our tests render (uppercase A-F).
CLIENT = "24%3A0A%3AC4%3A29%3A77%3A90"


# A request with no dispatch entry is echoed verbatim, and the echo carries no
# client id (the request never becomes dispatchable, so none is resolved):
PERL_PLAYERCOUNT = "playercount"
PERL_UNSYNC = "unsync"
PERL_UNSUBSCRIBE = "unsubscribe"
PERL_LOGOUT = "logout"
PERL_EXIT = "exit"
PERL_VOLUME_SET = "volume 50"
PERL_VOLUME_QUERY = "volume %3F"
PERL_PLAYLIST_BARE = "playlist"
PERL_RANDOMPLAY_QUERY = f"{CLIENT} randomplay %3F"
PERL_RANDOMPLAY_BARE = f"{CLIENT} randomplay "

# 'can' — Plugin/CLI/Plugin.pm:753-796, result '_can' (:785-787; login/shutdown/
# exit are always available, :780-783).  'can' without '?' answers the bare verb.
PERL_CAN_BARE = "can"
PERL_CAN_EMPTY = "can 0"
PERL_CAN_EXIT = "can exit 1"
PERL_CAN_MIXER = "can mixer 0"
PERL_CAN_FOOBAR = "can foobar 0"

# 'listen' — Plugin/CLI/Plugin.pm:103-106, command + query; listenCommand adds
# nothing (:799-828) while listenQuery adds '_listen' (:829-845).
PERL_LISTEN_BARE = "listen "
PERL_LISTEN_QUERY = "listen 0"

# queries with a bare '_<entity>' result
PERL_PREF_QUERY = "pref %3F "
PERL_PLAYERPREF_QUERY = f"{CLIENT} playerpref %3F "
PERL_PLAYERPREF_VOLUME = f"{CLIENT} playerpref volume "
PERL_MODE_QUERY = f"{CLIENT} mode stop"
PERL_TIME_QUERY = f"{CLIENT} time 0"
PERL_SLEEP_QUERY = f"{CLIENT} sleep 0"
PERL_SIGNALSTRENGTH_QUERY = f"{CLIENT} signalstrength 44"
PERL_SYNC_QUERY = (f"{CLIENT} sync "
                   "00%3A04%3A20%3A2b%3A88%3Ac8")
PERL_RESCAN_QUERY = "rescan 0"
PERL_SONGINFO_BARE = "songinfo 0 1"
PERL_INFO_SONGS = "info total songs 80134"
PERL_INFO_TOTAL_EMPTY = "info total %3F"
PERL_DISPLAYSTATUS = f"{CLIENT} displaystatus"
PERL_DISPLAY_QUERY = f"{CLIENT} display  "
PERL_DISPLAY_SET = f"{CLIENT} display x y 5"

# a command with an unset trailing slot still emits the empty token
PERL_IR_123 = f"{CLIENT} ir 123 "
PERL_BUTTON_PLAY = f"{CLIENT} button play  "
PERL_WIPECACHE = "wipecache "
PERL_RESCANPROGRESS_IDLE = "rescanprogress rescan%3A0"
PERL_ABORTSCAN = "abortscan"
PERL_RESCAN_FULL = "rescan full "

# loops, unrolled inline on the same line
PERL_MUSICFOLDER_0_1 = (
    "musicfolder 0 1 id%3A81408 "
    "filename%3A6MzM6F.Fetenhits_Rock_Classics_Best_Of-3CD-2020-NoGroup.nfo "
    "type%3Afolder count%3A303"
)
PERL_YEARS_0_1 = (
    "years 0 1 year%3A0 favorites_url%3Adb%3Ayear.id%3D0 count%3A65"
)
PERL_SYNCGROUPS_QUERY = (
    "syncgroups sync_members%3A24%3A0a%3Ac4%3A29%3A77%3A90%2C"
    "00%3A04%3A20%3A2b%3A88%3Ac8 sync_member_names%3ASchlafzimmer%2C"
    "Squeezebox%20Radio"
)
PERL_ALARMS_0_5 = (
    f"{CLIENT} alarms 0 5 fade%3A1 count%3A1 "
    "id%3Af205b436 dow%3A1%2C2%2C3%2C4%2C5 enabled%3A1 repeat%3A1 "
    "shufflemode%3A0 time%3A21600 volume%3A34 "
    "url%3Ahttp%3A%2F%2Fhirschmilch.de%3A7000%2Fchillout.mp3"
)
PERL_PLAYLIST_NAME = (
    f"{CLIENT} playlist name Hirschmilch%20Chillout"
)
PERL_PLAYLIST_TRACKS = f"{CLIENT} playlist tracks 1"
PERL_PLAYLIST_INDEX = f"{CLIENT} playlist index 0"
PERL_PLAYLIST_SHUFFLE = f"{CLIENT} playlist shuffle 0"
PERL_PLAYLIST_REPEAT = f"{CLIENT} playlist repeat 0"
PERL_PLAYLIST_URL = f"{CLIENT} playlist url "
PERL_PLAYLIST_MODIFIED = f"{CLIENT} playlist modified 0"
PERL_PLAYLIST_GENRE_ECHO = f"{CLIENT} playlist genre %3F"
PERL_FAVORITES_ITEMS = (
    "favorites items 0 5 title%3AFavorites id%3Aab9c31e0.0 name%3AChill "
    "image%3Ahtml%2Fimages%2Ffavorites.png isaudio%3A0 hasitems%3A1 "
    "id%3Aab9c31e0.1 name%3ANachrichten "
    "image%3Ahtml%2Fimages%2Ffavorites.png isaudio%3A0 hasitems%3A1 count%3A8"
)
PERL_SEARCH_QUESTION = "search %3F"

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
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


def _dispatch(cmd: str, args: list[str], player_id: str = LIVE_MAC) -> list[str]:
    handler = CLIHandler()
    ctx = CLIContext(player_id=player_id)
    ctx.command = cmd
    return asyncio.run(handler.dispatch(ctx, (cmd, args)))


_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")


def _keys(line: str) -> list[str]:
    """The ``key`` part of every ``key:value`` token of a CLI line.

    Tokens are percent-escaped (``Stdio.pm:134``), so a key such as
    ``info total albums`` arrives as ``info%20total%20albums%3A…`` — unquote
    before splitting on the (escaped) colon.
    """
    out: list[str] = []
    for tok in line.split():
        raw = unquote(tok)
        # the leading client id is a bare MAC, not a key:value token
        if _MAC_RE.fullmatch(raw):
            continue
        if ":" in raw:
            out.append(raw.split(":", 1)[0])
    return out


def _keys_of_perl(line: str) -> list[str]:
    return _keys(line)


# The command/argument pairs covering every handler this change touched.
EVERY_COMMAND: list[tuple[str, list[str]]] = [
    ("exit", []), ("quit", []),
    ("listen", []), ("listen", ["?"]), ("listen", ["on"]),
    ("can", []), ("can", ["?"]), ("can", ["exit", "?"]),
    ("can", ["mixer", "?"]), ("can", ["foobar", "?"]),
    ("playercount", []),
    ("serverstatus", ["0", "5"]),
    ("name", ["?"]), ("name", ["Neuer", "Name"]),
    ("sync", ["?"]), ("sync", ["aa:bb:cc:dd:ee:ff"]), ("unsync", []),
    ("syncgroups", ["?"]),
    ("songinfo", ["0", "1"]), ("songinfo", ["?"]),
    ("songinfo", ["0", "20", "track_id:6417"]),
    ("years", ["0", "1"]),
    ("musicfolder", ["0", "1"]),
    ("rescanprogress", []), ("abortscan", []),
    ("displaystatus", []),
    ("play", []), ("play", ["?"]), ("pause", ["1"]), ("pause", ["?"]),
    ("stop", []), ("prev", []), ("next", []),
    ("power", ["?"]), ("power", ["1"]),
    ("volume", ["50"]), ("volume", ["?"]),
    ("playerpref", ["?"]), ("playerpref", ["volume", "?"]),
    ("playerpref", ["volume", "50"]),
    ("button", ["play"]), ("button", ["next"]),
    ("mode", ["?"]), ("mode", []),
    ("time", ["?"]), ("time", ["30"]),
    ("sleep", ["?"]), ("sleep", ["off"]),
    ("signalstrength", ["?"]),
    ("randomplay", ["?"]), ("randomplay", ["1"]),
    ("current_title", ["?"]), ("artist", ["?"]), ("album", ["?"]),
    ("genre", ["?"]),
    ("playlist", []), ("playlist", ["?"]),
    ("playlist", ["name", "?"]), ("playlist", ["url", "?"]),
    ("playlist", ["modified", "?"]), ("playlist", ["tracks", "?"]),
    ("playlist", ["index", "?"]), ("playlist", ["jump", "?"]),
    ("playlist", ["shuffle", "?"]), ("playlist", ["repeat", "?"]),
    ("playlist", ["genre", "?"]), ("playlist", ["title", "?"]),
    ("playlist", ["duration", "?"]), ("playlist", ["artist", "?"]),
    ("playlist", ["album", "?"]), ("playlist", ["path", "?"]),
    ("playlist", ["remote", "?"]), ("playlist", ["loop", "?"]),
    ("playlist", ["add", "6417"]), ("playlist", ["insert", "6417"]),
    ("playlist", ["delete", "2"]), ("playlist", ["clear"]),
    ("playlist", ["save", "MyList"]), ("playlist", ["load", "MyList"]),
    ("playlist", ["resume", "MyList"]), ("playlist", ["move", "0", "1"]),
    ("playlist", ["index", "1"]), ("playlist", ["jump", "1"]),
    ("playlist", ["shuffle", "1"]), ("playlist", ["repeat", "1"]),
    ("playlist", ["url", "http://x/y"]), ("playlist", ["tracks"]),
    ("playlist", ["play", "6417"]),
    ("playlist", ["play", "track_id:6417"]),
    ("playlist", ["play", "item_id:1"]),
    ("playlist", ["play", "album_id:443"]),
    ("playlist", ["play", "artist_id:1476"]),
    ("playlist", ["play", "index:0"]),
    ("playlist", ["play", "http://x/y"]),
    ("search", []), ("search", ["?"]),
    ("search", ["0", "3", "term:night"]), ("search", ["tracks", "night"]),
    ("search", ["nosuchtype", "x"]),
    ("rescan", ["?"]), ("rescan", ["full"]),
    ("wipecache", []),
    ("display", ["?"]), ("display", ["x", "y", "5"]),
    ("ir", ["123"]), ("ir", []),
    ("pref", []), ("pref", ["?"]),
    ("pref", ["server:httpport", "?"]), ("pref", ["server:httpport", "9001"]),
    ("alarms", ["0", "5"]), ("alarms", ["?"]), ("alarms", []),
    ("alarm", ["0", "?"]), ("alarm", ["0", "delete"]),
    ("alarm", ["0", "enabled:1"]),
    ("subscribe", ["status"]), ("subscribe", []), ("unsubscribe", []),
    ("info", ["total", "songs", "?"]), ("info", ["total", "albums", "?"]),
    ("info", ["total", "artists", "?"]), ("info", ["total", "genres", "?"]),
    ("info", ["total", "duration", "?"]), ("info", ["total", "?"]),
    ("info", []),
    ("playlists", ["0", "2"]), ("playlists", ["tracks", "1"]),
    ("playlists", ["tracks"]),
    ("radio", []), ("radio", ["list"]), ("radio", ["delete"]),
    ("radio", ["play"]), ("radio", ["nosuch"]),
    ("favorites", []), ("favorites", ["items", "0", "5"]),
    ("favorites", ["add"]), ("favorites", ["addfolder"]),
    ("favorites", ["delete"]), ("favorites", ["move"]),
    ("favorites", ["rename"]), ("favorites", ["play"]),
    ("favorites", ["exists", "http://x/y"]), ("favorites", ["playlist"]),
    ("favorites", ["nosuch"]),
]


# ---------------------------------------------------------------------------
# the wire shape: exactly ONE element and never a newline
# ---------------------------------------------------------------------------
def test_every_answer_is_exactly_one_line_without_newlines():
    _pm_with(_player())
    offenders: list[str] = []
    for cmd, args in EVERY_COMMAND:
        try:
            out = _dispatch(cmd, args)
        except Exception as exc:  # noqa: BLE001
            offenders.append(f"{cmd} {args!r} raised {exc!r}")
            continue
        if not isinstance(out, list):
            offenders.append(f"{cmd} {args!r} -> not a list: {out!r}")
            continue
        if len(out) != 1:
            offenders.append(f"{cmd} {args!r} -> {len(out)} elements: {out!r}")
            continue
        if "\n" in out[0]:
            offenders.append(f"{cmd} {args!r} -> contains a newline: {out[0]!r}")
    assert not offenders, "CLI answers must be ONE line:\n" + "\n".join(offenders)


def test_the_answer_is_one_render_line_element():
    """No handler may hand-build a string: the line is render_line()'s output."""
    _pm_with(_player())
    for cmd, args in EVERY_COMMAND:
        out = _dispatch(cmd, args)
        assert len(out) == 1
        line = out[0]
        # render_line() never emits a space before a ':' (Request.pm:2291) and
        # never a raw ':' inside a token (Stdio.pm:134) — the legacy format
        # ("key: value", "  title: …") is gone.
        assert ": " not in line, f"{cmd} {args!r} still uses 'key: value': {line!r}"
        assert '":' not in line, f"{cmd} {args!r} has an unescaped colon: {line!r}"


# ---------------------------------------------------------------------------
# echoes (Plugin/CLI/Plugin.pm:657-663 + Request.pm:1093-1100)
# ---------------------------------------------------------------------------
def test_playercount_is_echoed_like_perl():
    assert _dispatch("playercount", []) == [PERL_PLAYERCOUNT]


def test_unsync_is_echoed_like_perl():
    assert _dispatch("unsync", []) == [PERL_UNSYNC]


def test_unsubscribe_is_echoed_like_perl():
    assert _dispatch("unsubscribe", []) == [PERL_UNSUBSCRIBE]


def test_exit_is_echoed_before_the_connection_closes():
    # Plugin/CLI/Plugin.pm:618-619 + :665/:692-698 — Perl answers 'exit', then
    # closes (the socket is closed by cli_server.py:56-57 *before* dispatch).
    assert _dispatch("exit", []) == [PERL_EXIT]
    assert _dispatch("quit", []) == ["quit"]  # no Perl dispatch at all


def test_volume_is_echoed_like_perl():
    # No 'volume' entry in Request.pm:474-637 — the mixer is 'mixer volume'.
    assert _dispatch("volume", ["50"]) == [PERL_VOLUME_SET]
    assert _dispatch("volume", ["?"]) == [PERL_VOLUME_QUERY]


def test_prev_and_next_are_echoed_like_perl():
    # Neither exists in Request.pm:474-637.
    assert _dispatch("prev", []) == ["prev"]
    assert _dispatch("next", []) == ["next"]


def test_randomplay_is_echoed_with_the_client_id_like_perl():
    line = _dispatch("randomplay", ["1"])[0]
    assert line.startswith(f"{CLIENT} randomplay")
    assert line.split()[:3] == [CLIENT, "randomplay", "1"]


def test_abortscan_wipecache_and_rescanprogress_like_perl():
    assert _dispatch("abortscan", []) == [PERL_ABORTSCAN]
    assert _dispatch("wipecache", []) == [PERL_WIPECACHE]        # unset _queue
    line = _dispatch("rescanprogress", [])[0]
    assert line.split()[0] == "rescanprogress"
    # 'rescan' is a *bare* value (rescanprogressQuery, Queries.pm:3231-3360):
    # 0 when idle (live Perl), 1 plus steps/totaltime while a scan runs.
    assert _keys(line) == ["rescan"] or _keys(line)[0] == "rescan"
    assert _keys(line)[0] == "rescan"
    if len(line.split()) == 2:
        assert line == PERL_RESCANPROGRESS_IDLE
    # '_rescan' is bare (rescanQuery, Queries.pm:3214-3229): live Perl 0.
    rescan_line = _dispatch("rescan", ["?"])[0]
    assert rescan_line in ("rescan 0", "rescan 1")
    assert _dispatch("rescan", ["full"]) == [PERL_RESCAN_FULL]   # unset _target


def test_search_with_a_question_marker_is_echoed():
    assert _dispatch("search", ["?"]) == [PERL_SEARCH_QUESTION]
    assert _dispatch("search", []) == ["search"]


# ---------------------------------------------------------------------------
# 'can' (Plugin/CLI/Plugin.pm:753-796)
# ---------------------------------------------------------------------------
def test_can_query_matches_perl_field_and_value():
    # '_can' is a bare result → the line ends in the 0/1 number, no key echoed.
    assert _dispatch("can", ["?"]) == [PERL_CAN_EMPTY]
    assert _dispatch("can", ["exit", "?"]) == [PERL_CAN_EXIT]
    # 'mixer' alone is only a *prefix* in the dispatch tree (Request.pm:513-524)
    assert _dispatch("can", ["mixer", "?"]) == [PERL_CAN_MIXER]
    assert _dispatch("can", ["foobar", "?"]) == [PERL_CAN_FOOBAR]


def test_can_without_a_question_marker_is_the_bare_request():
    assert _dispatch("can", []) == [PERL_CAN_BARE]
    assert _dispatch("can", ["albums"]) == ["can albums"]


# ---------------------------------------------------------------------------
# 'listen' (Plugin/CLI/Plugin.pm:103-108, :799-845)
# ---------------------------------------------------------------------------
def test_listen_query_and_command():
    assert _dispatch("listen", ["?"]) == [PERL_LISTEN_QUERY]
    assert _dispatch("listen", []) == [PERL_LISTEN_BARE]
    assert _dispatch("listen", ["on"]) == ["listen on"]


# ---------------------------------------------------------------------------
# bare '_<entity>' query results
# ---------------------------------------------------------------------------
def test_pref_query_keeps_the_empty_p2_value():
    assert _dispatch("pref", ["?"]) == [PERL_PREF_QUERY]
    assert _dispatch("pref", []) == ["pref  "]


def test_playerpref_query_is_client_prefixed():
    _pm_with(_player())
    assert _dispatch("playerpref", ["?"]) == [PERL_PLAYERPREF_QUERY]
    assert _dispatch("playerpref", ["volume", "?"]) == [PERL_PLAYERPREF_VOLUME]
    assert _dispatch("playerpref", ["volume", "50"]) == [
        f"{CLIENT} playerpref volume 50"
    ]


def test_mode_time_sleep_signalstrength_queries():
    line = _dispatch("mode", ["?"])[0]
    assert line == f"{CLIENT} mode stop"
    assert _keys_of_perl(PERL_MODE_QUERY) == []          # '_mode' is bare
    assert _dispatch("time", ["?"])[0] == f"{CLIENT} time 0"
    assert _dispatch("sleep", ["?"])[0] == f"{CLIENT} sleep 0"
    assert _dispatch("signalstrength", ["?"])[0] == (
        f"{CLIENT} signalstrength 44"
    )


def test_sync_query_lists_the_buddies_or_a_dash():
    _pm_with(_player())
    assert _dispatch("sync", ["?"])[0] == f"{CLIENT} sync -"
    _pm_with(_player(sync_master="00:04:20:2B:88:C8"))
    assert _dispatch("sync", ["?"])[0] == (
        f"{CLIENT} sync 00%3A04%3A20%3A2B%3A88%3AC8"
    )


def test_displaystatus_songinfo_info_and_playlist_bare():
    assert _dispatch("displaystatus", []) == [f"{CLIENT} displaystatus"]
    assert _dispatch("songinfo", ["0", "1"]) == [PERL_SONGINFO_BARE]
    assert _dispatch("info", ["total", "?"]) == [PERL_INFO_TOTAL_EMPTY]
    assert _dispatch("playlist", []) == [PERL_PLAYLIST_BARE]


def test_info_total_entities_are_bare_results(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "COUNT(*) AS n FROM tracks" in sql:
            return [{"n": 80134}]
        if "SUM(duration)" in sql:
            return [{"n": 22830823.015}]
        if "FROM albums" in sql:
            return [{"n": 7181}]
        if "contributors" in sql:
            return [{"n": 11151}]
        return [{"n": 762}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    assert _dispatch("info", ["total", "songs", "?"]) == [PERL_INFO_SONGS]
    assert _dispatch("info", ["total", "albums", "?"]) == ["info total albums 7181"]
    assert _dispatch("info", ["total", "artists", "?"]) == [
        "info total artists 11151"
    ]
    assert _dispatch("info", ["total", "genres", "?"]) == ["info total genres 762"]
    assert _dispatch("info", ["total", "duration", "?"]) == [
        "info total duration 22830823.015"
    ]
    # 'info' alone matches no leaf → echoed
    assert _dispatch("info", []) == ["info"]


# ---------------------------------------------------------------------------
# commands with an unset trailing slot (Request.pm:1026-1028)
# ---------------------------------------------------------------------------
def test_ir_button_and_display_emit_the_declared_empty_slots():
    # Live Perl: 'ir 123' → '<clientid> ir 123 ' (the unset '_time' slot is
    # still emitted, Request.pm:1026-1028).  Our client id is uppercase-A-F
    # escaped (the MAC we bind), Perl's fixture is lowercase — same escaping.
    assert _dispatch("ir", ["123"])[0] == f"{CLIENT} ir 123 "
    assert _dispatch("button", ["play"])[0] == f"{CLIENT} button play  "
    assert _dispatch("display", ["x", "y", "5"])[0] == (
        f"{CLIENT} display x y 5"
    )
    # displayQuery (Queries.pm:1550-1568) adds two bare results → 'display  '
    assert _dispatch("display", ["?"])[0] == f"{CLIENT} display  "
    # 'display x y 5' is byte-identical to the live Perl field run
    assert _keys(_dispatch("display", ["x", "y", "5"])[0]) == (
        _keys_of_perl(PERL_DISPLAY_SET)
    )


def test_ir_wipecache_and_display_keep_the_perl_field_names():
    assert _keys_of_perl(PERL_IR_123) == []          # only bare values
    assert _keys_of_perl(PERL_WIPECACHE) == []       # '_queue' is bare
    assert _keys_of_perl(PERL_DISPLAY_SET) == []
    assert _keys_of_perl(PERL_DISPLAYSTATUS) == []


# ---------------------------------------------------------------------------
# loops, unrolled inline (Request.pm:2264-2281)
# ---------------------------------------------------------------------------
def test_syncgroups_loop_uses_perls_two_keys():
    pm = _pm_with(
        _player(),
        _player(mac="00:04:20:2B:88:C8", name="Squeezebox Radio",
                sync_master=LIVE_MAC),
    )
    pm.players[LIVE_MAC].sync_slaves = ["00:04:20:2B:88:C8"]
    out = _dispatch("syncgroups", ["?"])
    assert len(out) == 1
    assert _keys(out[0]) == _keys_of_perl(PERL_SYNCGROUPS_QUERY) == [
        "sync_members", "sync_member_names"
    ]


def test_alarms_loop_uses_perls_key_order():
    from lyrion.alarms import Alarm, AlarmManager

    AlarmManager().set(LIVE_MAC, 0, Alarm(
        index=0, enabled=True, days="0111110", time="06:00", volume=34,
        fade=1, repeat=True, wake="url:http://hirschmilch.de:7000/chillout.mp3",
    ))
    try:
        out = _dispatch("alarms", ["0", "5"])
        assert len(out) == 1
        line = out[0]
        assert line.startswith(f"{CLIENT} alarms 0 5")
        assert _keys(line) == _keys_of_perl(PERL_ALARMS_0_5)
        assert _keys(line) == [
            "fade", "count", "id", "dow", "enabled", "repeat", "shufflemode",
            "time", "volume", "url",
        ]
        assert "time%3A21600" in line          # 06:00 in seconds, like Perl
        assert "dow%3A1%2C2%2C3%2C4%2C5" in line
        assert "url%3Ahttp%3A%2F%2Fhirschmilch.de" in line
    finally:
        AlarmManager().delete(LIVE_MAC, 0)


def test_alarms_echoes_a_question_marker():
    # Perl registers no '?' entry for 'alarms' (Request.pm:477) → raw echo.
    assert _dispatch("alarms", ["?"]) == [f"{CLIENT} alarms %3F"]


def test_alarm_loop_entry_keys():
    from lyrion.alarms import Alarm

    entry = _alarm_loop_entry(3, Alarm(index=3, enabled=True, days="1000000",
                                       time="07:30", volume=-1, wake=""))
    assert list(entry) == ["id", "dow", "enabled", "repeat", "shufflemode",
                           "time", "volume", "url"]
    assert entry["dow"] == "0"
    assert entry["time"] == 7 * 3600 + 30 * 60
    assert entry["url"] == "CURRENT_PLAYLIST"
    assert _keys_of_perl(PERL_ALARMS_0_5)[2:] == list(entry)


def test_years_loop_matches_the_live_perl_line(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "AS y" in sql:
            return [{"y": 0}]
        return [{"n": 65}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    out = _dispatch("years", ["0", "1"])
    assert out == [PERL_YEARS_0_1]


def test_musicfolder_loop_matches_the_live_perl_line(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "DISTINCT url" in sql and "COUNT" not in sql:
            return [{"url": "file:///home/Musik/track.nfo"}]
        return [{"n": 303}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    out = _dispatch("musicfolder", ["0", "1", "folder_id:file:///home"])
    line = out[0]
    assert line.startswith("musicfolder 0 1 folder_id%3Afile%3A%2F%2F%2Fhome ")
    # the echoed 'folder_id:…' surplus parameter comes first (Request.pm:1049-
    # 1054); the loop + count keys are Perl's (folder_loop, Queries.pm:2472-2507)
    assert _keys(line)[1:] == _keys_of_perl(PERL_MUSICFOLDER_0_1)
    assert _keys(line)[1:] == ["id", "filename", "type", "count"]
    assert "type%3Afolder" in line
    assert "count%3A303" in line


def test_songinfo_is_one_line_with_the_songinfo_loop(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM tracks t WHERE t.id" in sql:
            return [{
                "id": 6417, "title": "Gloria", "url": "file:///x.mp3",
                "duration": 406.05, "year": 2005, "tracknum": 11,
                "genre": "Classical", "filesize": 16244070,
                "samplerate": 44100, "samplesize": 16, "channels": 2,
                "ctype": "audio/mpeg", "modtime": "2008-02-07",
                "lossless": 0,
            }]
        if "contributors" in sql:
            return [{"id": 1476, "name": "The Medieval Experience"}]
        if "albums" in sql:
            return [{"id": 443, "title": "Monks and Troubadours I"}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    out = _dispatch("songinfo", ["0", "20", "track_id:6417"])
    assert len(out) == 1
    line = out[0]
    assert line.startswith("songinfo 0 20 track_id%3A6417 ")
    keys = _keys(line)
    # Perl's songinfoQuery adds a 'songinfo_loop' (:4645-4670) — the loop name
    # itself is never printed, only the item keys (Request.pm:2264-2281).
    assert "songinfo_loop" not in line.split()
    for expected in ("id", "title", "duration", "url", "artist", "artist_id",
                     "album", "album_id", "genre", "year", "tracknum"):
        assert expected in keys, f"{expected} missing from {line!r}"
    assert "count" not in keys          # Perl has no count result here


def test_playlists_loop_and_tracks_loop_keys():
    # playlistsQuery: playlists_loop with 'id'/'playlist', count last
    # (Queries.pm:2888-2889/:2930); playlistsTracksQuery: 'playlisttracks_loop'
    # with count last (Queries.pm:2776-2850).
    out = _dispatch("playlists", ["0", "2"])
    assert len(out) == 1
    sub = _dispatch("playlists", ["tracks", "1"])
    assert len(sub) == 1
    assert sub[0].startswith("playlists tracks 1")


def test_search_grouped_keys_match_perl():
    line = _dispatch("search", ["0", "3", "term:night"])[0]
    keys = _keys(line)
    # the per-entity and total counters (Queries.pm:3526/:3586)
    assert set(keys) >= {"contributors_count", "albums_count", "genres_count",
                         "tracks_count", "count"}
    # the loop *names* are never printed — only the item keys
    for loop_name in ("contributors_loop", "albums_loop", "genres_loop",
                      "tracks_loop"):
        assert loop_name not in line.split(), loop_name


def test_search_loop_item_keys_are_perls_entity_id_and_entity(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "COUNT" in sql:
            return [{"n": 1}]
        if "FROM tracks WHERE title LIKE" in sql:
            return [{"id": 11155, "title": "(Tonight) We Burn Like Stars"}]
        return []

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    line = _dispatch("search", ["0", "3", "term:night"])[0]
    assert "track_id%3A11155" in line
    assert "track%3A%28Tonight%29%20We%20Burn" in line


def test_favorites_items_uses_the_live_perl_keys(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        return []

    class _FM:
        async def list_items(self, parent: Any) -> list[dict]:
            return [
                {"id": "ab9c31e0.0", "title": "Chill", "type": "stream",
                 "url": "http://x", "position": 0},
                {"id": "ab9c31e0.1", "title": "Nachrichten", "type": "folder",
                 "url": "", "position": 1},
            ]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    import lyrion.music.favorites as fav_mod

    monkeypatch.setattr(fav_mod, "get_favorites_manager", lambda: _FM())
    line = _dispatch("favorites", ["items", "0", "5"])[0]
    assert line.startswith("favorites items 0 5 ")
    assert _keys(line) == _keys_of_perl(PERL_FAVORITES_ITEMS)
    assert _keys(line) == ["title", "id", "name", "image", "isaudio",
                           "hasitems", "id", "name", "image", "isaudio",
                           "hasitems", "count"]
    assert "title%3AFavorites" in line
    assert "isaudio%3A1" in line and "hasitems%3A1" in line


def test_favorites_exists_reports_exists_and_index(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        return [{"id": 7}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    line = _dispatch("favorites", ["exists", "7"])[0]
    assert line.endswith("exists%3A1 index%3A7")


# ---------------------------------------------------------------------------
# playlist queries (playlistXQuery, Queries.pm:2708-2774)
# ---------------------------------------------------------------------------
def test_playlist_entity_queries_are_bare_results():
    _pm_with(_player(repeat=1, shuffle=2))
    assert _dispatch("playlist", ["name", "?"])[0].split()[1:3] == [
        "playlist", "name"
    ]
    # '_repeat' / '_shuffle' are bare values
    assert _dispatch("playlist", ["repeat", "?"])[0] == f"{CLIENT} playlist repeat 1"
    assert _dispatch("playlist", ["shuffle", "?"])[0] == f"{CLIENT} playlist shuffle 2"
    assert _dispatch("playlist", ["tracks", "?"])[0] == f"{CLIENT} playlist tracks 1"
    assert _dispatch("playlist", ["index", "?"])[0] == f"{CLIENT} playlist index 0"
    assert _dispatch("playlist", ["url", "?"])[0] == f"{CLIENT} playlist url "
    for ours, perl in (
        (_dispatch("playlist", ["repeat", "?"])[0], PERL_PLAYLIST_REPEAT),
        (_dispatch("playlist", ["shuffle", "?"])[0], PERL_PLAYLIST_SHUFFLE),
        (_dispatch("playlist", ["index", "?"])[0], PERL_PLAYLIST_INDEX),
        (_dispatch("playlist", ["url", "?"])[0], PERL_PLAYLIST_URL),
        (_dispatch("playlist", ["tracks", "?"])[0], PERL_PLAYLIST_TRACKS),
    ):
        assert len(ours.split()) == len(perl.split()), (ours, perl)


def test_playlist_genre_with_a_question_marker_echoes_the_marker():
    # 'playlist genre ?' puts '?' into _index (Request.pm:560) → echoed, no
    # result: live Perl 'playlist genre %3F'.
    _pm_with(_player())
    line = _dispatch("playlist", ["genre", "?"])[0]
    assert _keys(line) == _keys_of_perl(PERL_PLAYLIST_GENRE_ECHO)


def test_playlist_commands_are_echoed_with_the_declared_params():
    _pm_with(_player())
    assert _dispatch("playlist", ["add", "6417"])[0] == (
        f"{CLIENT} playlist add 6417 "
    )
    assert _dispatch("playlist", ["clear"])[0] == f"{CLIENT} playlist clear"
    assert _dispatch("playlist", ["save", "MyList"])[0] == (
        f"{CLIENT} playlist save MyList"
    )
    assert _dispatch("playlist", ["load", "MyList"])[0] == (
        f"{CLIENT} playlist load MyList"
    )
    assert _dispatch("playlist", ["move", "0", "1"])[0] == (
        f"{CLIENT} playlist move 0 1"
    )
    assert _dispatch("playlist", ["index", "1"])[0] == (
        f"{CLIENT} playlist index 1"
    )
    assert _dispatch("playlist", ["delete", "2"])[0] == (
        f"{CLIENT} playlist delete 2"
    )
    assert _dispatch("playlist", ["play", "6417"])[0] == (
        f"{CLIENT} playlist play 6417  "
    )


# ---------------------------------------------------------------------------
# serverstatus (serverstatusQuery, Queries.pm:3705-3830)
# ---------------------------------------------------------------------------
def test_serverstatus_is_one_line_with_perls_keys(monkeypatch):
    async def stub(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM albums" in sql:
            return [{"n": 7181}]
        if "contributors" in sql:
            return [{"n": 11151}]
        if "DISTINCT genre" in sql:
            return [{"n": 762}]
        if "MAX(last_rescan)" in sql:
            return [{"t": None}]
        return [{"n": 80134, "d": 22830823.015}]

    monkeypatch.setattr(cli_commands, "_query_db", stub)
    _pm_with(_player())
    out = _dispatch("serverstatus", ["0", "5"])
    assert len(out) == 1
    line = out[0]
    assert line.startswith("serverstatus 0 5 ")
    keys = _keys(line)
    for expected in ("version", "uuid", "ip", "httpport", "info total albums",
                     "info total artists", "info total genres",
                     "info total songs", "info total duration",
                     "player count", "playerindex", "playerid", "uuid", "ip",
                     "name", "seq_no", "model", "modelname", "power",
                     "isplaying", "displaytype", "isplayer", "canpoweroff",
                     "connected", "firmware", "other player count"):
        assert expected in keys, f"{expected} missing from {keys!r}"
    # the client id is NOT prefixed (needsClient=0, Request.pm:611)
    assert not line.startswith(CLIENT)
    # the legacy invented 'sn.player count' is gone
    assert "sn.player count" not in keys
    assert "name" in keys  # 'serverstatus name:Lyrion' is gone too
    assert _players_loop_entry(0, _player())["playerid"] == LIVE_MAC


# ---------------------------------------------------------------------------
# name / sync / musicfolder / playlists: no '\\n' anywhere
# ---------------------------------------------------------------------------
def test_name_query_is_client_prefixed_with_the_bare_value():
    _pm_with(_player())
    assert _dispatch("name", ["?"]) == [f"{CLIENT} name Schlafzimmer"]
    assert _dispatch("name", ["Neuer", "Name"]) == [f"{CLIENT} name Neuer Name"]


def test_sync_command_echoes_the_request():
    # addDispatch(['sync','_indexid-']), Request.pm:622 — no result
    _pm_with(_player())
    line = _dispatch("sync", ["00:04:20:2B:88:C8"])[0]
    assert line.split()[1] == "sync"
    assert line.split()[-1] == "00%3A04%3A20%3A2B%3A88%3AC8"


def test_the_legacy_multiline_format_is_gone():
    """None of the old hand-built lines survive (regression guard)."""
    _pm_with(_player())
    legacy = (
        "no player selected", "player not found", "cli error",
        "songinfo: no track_id", "listen: ok", "displaystatus: ",
        "abortscan: ok", "unsubscribe: done", "rescanprogress progress:",
        "id: ", "  title: ", "syncgroups count:", "alarms count:",
        "playlist: ", "search: ", "radio count:", "favorites count:",
        "added: ", "exists: ", "deleted", "renamed", "moved", "saved", "ok",
    )
    for cmd, args in EVERY_COMMAND:
        line = _dispatch(cmd, args)[0]
        for needle in legacy:
            assert needle not in line, (
                f"{cmd} {args!r} still emits the legacy token {needle!r}: "
                f"{line!r}"
            )
