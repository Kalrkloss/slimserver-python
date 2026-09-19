"""JSON-RPC API for Pyrion Music Server."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import time
import urllib.parse
from typing import Any, Callable, Optional

#: Perl's directory rows (``content_type = 'dir'``) — the folder-id model the
#: browse and the songs/titles filters use.  Imported at module level: it is
#: dependency-free (stdlib + ``lyrion.platform.paths``, which is loaded
#: lazily inside its functions), so there is no import cycle.
from lyrion.media import dir_rows

#: The CLI tags Perl's TuneIn importer registers as radio sub-feeds: one
#: dynamic OPMLBased plugin per TuneIn directory item
#: (``Slim/Plugin/InternetRadio/Plugin.pm:92-205`` with ``menu => 'radios'``)
#: plus Perl's ``sounds`` app feed (``Slim/Plugin/Sounds``).  The single source
#: of truth is :data:`lyrion.web.radiobrowser.RADIO_FEEDS`; that module has no
#: import-time dependency on this file, so the import stays cycle-free.
from lyrion.web.radiobrowser import RADIO_FEEDS as _RADIO_FEEDS

logger = logging.getLogger(__name__)

# Perl-Defaults der Client-Prefs, die die Jive-Settings-Seiten abfragen
# (Squeezebox2.pm:23-60 `%prefs`; ReplayGain-Werte :47-49). Nur die Werte, die
# eine `playerpref <name> ?`-Abfrage beantworten muss, ohne dass der Player
# die Pref je gesetzt hat.
_PLAYERPREF_DEFAULTS = {
    "replayGainMode": 0,          # Squeezebox2.pm:47
    "remoteReplayGain": -5,       # :48
    "localReplayGain": 0,         # :49
    "digitalVolumeControl": 1,
    "preampVolumeControl": 0,
    "bass": 0, "treble": 0, "pitch": 100,
    "transitionType": 0, "transitionDuration": 0,
}


# SqueezePlay sends locally maintained parameters (volume, power) with a
# `seq_no:<N>` param and expects the same number back in the playerstatus
# (`seq_no`) and in the audg frame. Perl stores it per client
# (Slim/Player/Client.pm:208-210, Commands.pm:559-562 mixer and
# :2586-2589 power) and emits it in the status (Queries.pm:4196).
# Player.lua:1223-1333 treats a mismatch as "out of sync", reverts the
# volume and re-sends — an unbroken seq_no makes the client loop forever.
_SEQ_NO_RE = re.compile(r"^seq_no:(\d+)$")

# Formats whose Perl format class implements canSeek, i.e. local files the
# player may seek in. Perl path: Queries.pm:4104-4107 (adds `can_seek` only
# when true) → Slim::Music::Info::canSeek (Info.pm:1147-1151) →
# Slim::Player::Song::canSeek/canDoSeek (Song.pm:839-865) →
# Slim::Player::Protocols::File::canSeek (File.pm:403-415: the format class
# must implement canSeek). Classes that do: MP3.pm:476, FLAC.pm:1011,
# Ogg.pm:333, OggOpus.pm:148, Wav.pm:118, AIFF.pm:126, DSD.pm:52 (dsf/dff),
# WMA.pm, Movie.pm:249 — and Formats.pm:63-64 maps aac/mp4/mp4x to
# Slim::Formats::Movie. APE/Musepack/WavPack have NO canSeek.
# Remote (HTTP) is a different rule: Protocols::HTTP::canSeek:1150-1155
# requires a KNOWN bitrate and duration.
SEEKABLE_FORMATS = frozenset({
    "mp3", "flac", "ogg", "oga", "ogf", "opus", "wav", "aif", "aiff",
    "wma", "dsf", "dff", "aac", "mp4", "m4a", "mp4x",
})


def _local_format_from_url(url: str) -> str:
    """File extension (lowercase, no dot) of a local track URL."""
    try:
        from urllib.parse import urlparse
        name = urlparse(str(url)).path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[1].lower() if "." in name else ""
    except Exception:
        return ""


def _seq_no_from_args(args) -> int | None:
    """Extract the client's `seq_no:<N>` param, if present."""
    for a in args or ():
        if isinstance(a, str):
            m = _SEQ_NO_RE.match(a)
            if m:
                return int(m.group(1))
    return None

# Global operational notice shown in the web Status bar (e.g. a missing
# ffmpeg). Set via set_server_notice(); read in the serverstatus response.
_SERVER_NOTICE: dict[str, str] = {"text": ""}


def set_server_notice(text: str) -> None:
    """Set (or clear, with '') the global Status-bar notice."""
    _SERVER_NOTICE["text"] = text

try:
    import orjson

    def _json_loads(data: bytes | str) -> Any:
        return orjson.loads(data)

    def _json_dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

except ImportError:
    import json

    def _json_loads(data: bytes | str) -> Any:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)

    def _json_dumps(obj: Any) -> bytes:
        return json.dumps(obj).encode("utf-8")


def _library_db_path() -> str:
    """Resolve the library DB path from the active config (test/dev runs use
    LYRION_SERVERDATA; the production default stays /root/.lyrion)."""
    try:
        from lyrion.config import get_config
        return str(get_config().db_path)
    except Exception:  # noqa: BLE001
        return "/root/.lyrion/Lyrion/Prefs/lyrion.db"


_LIBRARY_DB = "/root/.lyrion/Lyrion/Prefs/lyrion.db"


def _perl_status_duration(value: Any):
    """Perl's ``duration`` rule for ``status`` — ``Queries.pm:4100-4102``.

    ``Slim/Control/Queries.pm``::

        if (my $dur = $song->duration()) {
            $dur += 0;
            $request->addResult('duration', $dur);
        }

    The key exists only for a TRUTHY song length: the DB length (``LENGTH``
    tag) of a local track, the parsed length of a remote stream — and it is
    absent when that length is 0/undef.  ``$dur += 0`` numifies, so an
    integral length is published as an integer.

    Live Perl 9.1.1 (read-only, 2026-09-14, ``status - 1 tags:cdltoK``):
    stream "Dj Fada 2 - Life Breath (oct12)" (no known length) → no
    ``duration`` key at all; stream ``…/alarm.mp3`` (7 s) → ``"duration":7``;
    a local track → its DB length.

    Returns ``None`` when Perl would not publish the key.  NEVER synthesise a
    value from the elapsed position or a placeholder: SqueezeClient derives its
    seek slider's ``valueTo`` from ``duration`` and aborts with
    ``java.lang.IllegalStateException: Slider value(1006.49963) … valueTo
    (1006.497)`` (NowPlayingFragment.kt:440-479) when the position outgrows a
    fabricated ``valueTo``; with no known length the clients use Perl's
    disabled-slider branch.
    """
    try:
        dur = float(value or 0)
    except (TypeError, ValueError):
        return None
    if dur <= 0:
        return None
    return int(dur) if dur.is_integer() else dur

#: Perl's browse-session handle: ``XMLBrowser::getSID`` accepts any token
#: starting with 8 hex digits (``Slim/Control/XMLBrowser.pm:1739-1741``) and
#: ``getSID``/``createUUID`` builds it as a *short digest*
#: (``substr(sha1_hex(time . $$ . hostname), 0, 8)``,
#: ``Slim/Utils/Misc.pm:1557-1560``).  It is the ROOT handle of one browse
#: session: ``@crumbIndex = $sid ? ($sid) : ()`` (``XMLBrowser.pm:353``) and
#: every item id becomes ``<sid>.<index>[.<index>…]`` (``:1022``/``:1142``).
#: A client that taps the item echoes that id back; ``cliQuery`` then finds
#: the cached feed for the sid (``:225-229``) and strips it before walking
#: the index path (``:334-336``).  That is the shape the favorites ids must
#: have — our old synthetic root ``0`` is not a sid and every Perl client
#: (and every Perl answer) uses the session token instead.
_FAV_SID_RE = re.compile(r"^[a-f0-9]{8}$", re.IGNORECASE)


def _new_fav_sid() -> str:
    """A fresh browse-session handle (Perl ``createUUID``, Misc.pm:1557-1560)."""
    import secrets

    return secrets.token_hex(4)


def _is_fav_sid(token: str) -> bool:
    """``XMLBrowser::getSID`` — a root handle is 8 hex chars (``:1739-1741``)."""
    return bool(_FAV_SID_RE.match(str(token or "")))


def _status_state_fingerprint(cmd: str, pid: str | None) -> tuple | None:
    """State snapshot of the inputs a cached ``status`` answer was built from.

    The 1 s poll cache (``_status_cache``) exists against flood clients, but
    it must NEVER mask a state change: Perl re-runs every subscribed request
    after each command (``@notificationQueue``/``notify``,
    ``Slim/Control/Request.pm:1872-1876``/``:2005-2100``) and that push has to
    carry the new ``mode``/``time``.  A paused player's pushed status used to
    be served from a <1 s old pre-pause entry (``mode: play``), which left
    Squeezer's play/pause icon and its local 1 s progress ticker running
    (``BaseClient.parseStatus`` → ``updatePlayStatus(…, "mode")``,
    ``CometClient.postSongTimeChanged``).  Returns ``None`` when the state
    cannot be sampled — then the response is neither cached nor served.
    """
    try:
        from lyrion.player.manager import PlayerManager

        pm = PlayerManager()
        if cmd == "status":
            player = pm.get_player(pid) if pid else None
            if player is None:
                players = pm.get_all_players()
                player = players[0] if players else None
            if player is None:
                return ()
            return (
                str(player.mode),
                bool(player.power),
                round(float(getattr(player, "elapsed", 0) or 0), 3),
                round(float(getattr(player, "duration", 0) or 0), 3),
                int(getattr(player, "seq_no", 0) or 0),
                int(getattr(player, "volume", 0) or 0),
                int(getattr(player, "shuffle", 0) or 0),
                int(getattr(player, "repeat", 0) or 0),
                str(getattr(player, "current_title", "") or ""),
            )
        return tuple(sorted(
            (str(p.mac), str(p.mode), bool(p.power),
             bool(getattr(p, "connected", False)), str(getattr(p, "name", "")),
             int(getattr(p, "seq_no", 0) or 0))
            for p in pm.get_all_players()))
    except Exception:  # noqa: BLE001
        return None


def _db_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against the library DB (synchronous)."""
    import sqlite3

    con = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


# Perl's numeric conversion of a string (``0 + $str``): skip leading
# whitespace, then the longest leading ASCII number — optional sign,
# integer/fraction part and exponent (``"1e3"`` → 1000, ``"2.9"`` → 2.9,
# ``"010"`` → 10, ``"0x10"`` stops at 'x' → 0). A Unicode digit is NOT a
# digit (``"٢"`` → 0) and anything without a leading number is 0.
_PLAYCTL_NUM_RE = re.compile(
    r"[ \t]*([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
# Perl holds the value as a double, so a token longer than a double
# overflows to ±Inf and is out of range. Keep that as a plain int sentinel
# (never a float, never a Python ``int()`` digit-limit error).
_PLAYCTL_INF = 1 << 62


def _playctl_index(value: str) -> int:
    """Coerce the ``xmlbrowserPlayControl`` token the way Perl does.

    Perl feeds the raw param into arithmetic (``$i = $xmlbrowserPlayControl
    - $subFeed->{offset}``, Slim/Control/XMLBrowser.pm:808), so the token
    numifies exactly like ``0 + $str``: a leading number is used, anything
    else becomes 0 (``"abc"`` → item 0, ``"-1"`` stays negative → out of
    range). Two divergences from naive ``int()``/``\\d`` are deliberate and
    live-probed against Perl 9.1.1:

    * ``"٢"`` (Arabic-Indic 2) is *not* an ASCII digit → 0 (item 0), while
      Python's ``\\d`` matched it and addressed row 2;
    * a very long number (5000 digits) overflows a double to ``Inf`` →
      out of range (``count 0``, no ``item_loop``), while Python ``int()``
      raised and turned the request into JSON-RPC ``-32603``.
    """
    m = _PLAYCTL_NUM_RE.match(str(value))
    if not m:
        return 0
    text = m.group(1)
    try:
        # Perl keeps plain integer tokens exact (IV/UV). Python's only limit
        # is the int/str digit cap (~4300) — beyond that (and for any
        # exponent form, which is not an int literal) fall through to the
        # double path below.
        return int(text)
    except ValueError:
        pass
    try:
        num = float(text)
    except ValueError:  # cannot happen for the matched ASCII form
        return 0
    if num == float("inf"):
        return _PLAYCTL_INF
    if num == float("-inf"):
        return -_PLAYCTL_INF
    return int(num)  # int use truncates toward zero, like Perl


#: Perl's server/player default for ``defeatDestructiveTouchToPlay``
#: (``Slim/Utils/Prefs.pm:272``: ``'defeatDestructiveTouchToPlay' => 4``).
_DEFEAT_DEFAULT = 4


def _defeat_pref_default() -> Any:
    """The server pref ``defeatDestructiveTouchToPlay`` (Perl ``:1966``).

    ``XMLBrowser.pm:1966`` falls back to ``$prefs->get(
    'defeatDestructiveTouchToPlay')`` — the *server* pref, registered with
    the default ``4`` (``Slim/Utils/Prefs.pm:272``).  Reading the runtime
    store instead of the constant keeps an operator's setting effective
    (``1`` = always defeat, i.e. every station row becomes a play-control
    row); an unset/empty value keeps Perl's default.  Tests monkeypatch this
    function (pattern: ``_bmf_musicdir_pref``).
    """
    try:
        from lyrion.config import get_config
        value = get_config().get("defeatDestructiveTouchToPlay")
    except Exception:  # noqa: BLE001
        return _DEFEAT_DEFAULT
    if value is None or str(value).strip() == "":
        return _DEFEAT_DEFAULT
    return value


#: url → Perl-style negative remote-track id, and its reverse index.
_REMOTE_TRACK_IDS: dict[str, int] = {}
_REMOTE_TRACK_URLS: dict[int, str] = {}

#: Perl's cover-less remote fallback — the *skin-relative* spelling
#: ``Slim/Control/Queries.pm:5633`` (``_addJiveSong``) and
#: ``Slim/Player/Player.pm:620-623`` (display status) hand out literally
#: ``/html/images/radio.png``, and ``Slim/Schema/RemoteTrack.pm``'s default
#: artwork_url for a stream is the same path without the leading slash (live
#: Perl 9.1.1 ``status - 1 tags:ABCDEKJZlcuxyrtS`` on a plain stream:
#: ``"artwork_url": "html/images/radio.png"``, ``"coverid":
#: "-94115161401160"``).
#:
#: The path used to 404 here (``_serve_static`` had no skin fallback), so this
#: constant carried the ``/html/EN/…`` variant that happened to answer.  The
#: static resolver now serves Perl's spelling as well (``HTML/EN/html/images/``
#: skin fallback, see ``app._static_path_variants``), so the emitted path is
#: Perl's character for character.
RADIO_PLACEHOLDER_ICON = "/html/images/radio.png"

#: The same default for the ``artwork_url`` FIELD: Perl's ``$track->coverurl``
#: for a remote track without a stored logo returns the skin-relative path
#: (live Perl: ``"html/images/radio.png"`` for a bare stream URL,
#: ``"html/images/favorites.png"`` for a favourites entry that carries that
#: icon — the value comes from the stored station entry, so it is data, not a
#: constant).
REMOTE_ART_FALLBACK = "html/images/radio.png"

#: Marker for the ``icon``/``icon-id`` split of the *Menu* shape: our own
#: stand-in for the current cover-less stream item's ``artwork_url``.  It is
#: NOT artwork: Perl's precedence (``_addJiveSong``, Queries.pm:5618-5630)
#: must not mistake it for a real ``artwork_url``, otherwise the radio
#: placeholder never applies to the Menu-Status item.
_REMOTE_ART_PLACEHOLDER = REMOTE_ART_FALLBACK


def _remote_track_id(url: object) -> int:
    """Perl's ``id`` for a remote track (``Slim/Schema/RemoteTrack.pm:317``).

    ``RemoteTrack->new`` initialises the read-only accessor with
    ``$self->init_accessor(_url => $url, id => -int($self), …)`` — ``int``
    of the stringified object yields its address, so the ``id`` is a
    **negative integer that is stable for the object's lifetime** and maps
    back to the URL through ``%idIndex`` (``:412``, ``fetchById``
    ``:439-451``).  Live Perl 192.168.1.90 answers a stream item with exactly
    that shape (read-only ``status - 1 tags:ABdejJKlrStTuxy``):
    ``"id": "-94115161401160"``.

    We hand out the same *shape* — a negative integer of the same magnitude
    (12 hex digits, Perl's pointers are ≈1e14) — derived from the URL, so it
    is deterministic per process and resolvable back with
    :func:`_remote_url_for_id`.  Clients parse the field as a number; the URL
    string we used to send crashes them (Squeeze Client on a player-preview
    zoom).
    """
    key = str(url)
    tid = _REMOTE_TRACK_IDS.get(key)
    if tid is None:
        tid = -int(hashlib.sha1(key.encode("utf-8", "replace"))
                   .hexdigest()[:12], 16)
        _REMOTE_TRACK_IDS[key] = tid
        _REMOTE_TRACK_URLS[tid] = key
    return tid


def _remote_url_for_id(track_id: object) -> str | None:
    """``RemoteTrack->fetchById`` (``Slim/Schema/RemoteTrack.pm:439-451``).

    The server accepts its own remote-track id back and resolves it to the
    URL (Perl keeps ``%idIndex`` in memory and a 30-day disk cache,
    ``:413``).
    """
    try:
        return _REMOTE_TRACK_URLS.get(int(str(track_id)))
    except (TypeError, ValueError):
        return None


#: Playlist container extensions — Perl asks ``$song->isPlaylist()``
#: (``Slim/Player/Song.pm``), i.e. whether the playing URL is a playlist file
#: rather than a plain stream.
_PLAYLIST_EXT_RE = re.compile(r"\.(?:m3u8?|pls|asx|b4s|wpl)(?:$|[?#])",
                              re.IGNORECASE)


def _defeat_destructive_touch_to_play(rest: list, player=None,
                                      *, client_named: bool | None = None
                                      ) -> bool:
    """Perl ``_defeatDestructiveTouchToPlay`` (``XMLBrowser.pm:1951-1983``).

    Decides whether a *tap* on a touch-to-play row must NOT start playback
    blindly, but open the play-control context menu instead
    (``goAction: "playControl"`` + ``playControlParams`` — ``:1268-1272``).
    Order of resolution, verbatim from Perl:

    1. the request param ``defeatDestructiveTouchToPlay:<n>``
       (``:1964``) — controllers send it to force a branch;
    2. the *client's* stored pref (``:1965``);
    3. the server pref (``:1966``) with Perl's default ``4``
       (``Slim/Utils/Prefs.pm:272``).

    Values (``:1968-1973``): ``0`` never, ``1`` always, ``2`` playlist length
    > 1, ``3`` playing and length > 1, ``4`` playing and the current item is
    not a radio stream.

    ``client_named`` is ``$request->client``: ``False`` for a request that
    names no player at all, ``None`` (default) derives it from ``player``.
    Perl ``:1976`` ``return 1 if $pref == 1 || !$client`` — **a request with
    no client at all gets the defeated branch for every ``pref != 0``**, which
    is exactly what the live Perl server answers a plain
    ``favorites items 0 50 menu:favorites`` with (read-only probe
    2026-09-13: stream rows ``{"goAction": "playControl", "playControlParams":
    {"xmlbrowserPlayControl": "6"}, "params": {"item_id": "30a91dbd.6",
    "isContextMenu": 1}, "type": "audio"}``, no ``style``, no
    ``touchToPlay``).

    Documented deviations (measured, not assumed):

    * **a named but unknown client** resolves to "no client", exactly like
      Perl's ``$request->client``: ``PlayerManager().get_player(mac)`` only
      returns a player for a *connected* client, and Perl's
      ``Request->new`` clears ``_clientid`` (status 103, not dispatchable)
      for a mac it does not know.  Both therefore land on the ``!$client``
      branch at :1976.  Live 2026-09-14: Perl answers the favourites list of
      an unattributed request with ``goAction: "playControl"`` +
      ``playControlParams {xmlbrowserPlayControl: "<index>"}`` and no
      ``style``/``touchToPlay`` — the shape that makes a controller open the
      Play/Add/Play-next menu, which this port used to answer with the blind
      ``play`` row (the "Favoriten lassen sich nicht starten" symptom).
      A request can still force either branch with
      ``defeatDestructiveTouchToPlay:0|1`` (:1964).

    What this does **not** do: force the play-control branch for a *known*
    client.  Perl answers a client-attributed favourites request with the
    blind ``goAction: "play"`` row whenever the client is idle or playing a
    radio stream — measured live 2026-09-13 with the idle client
    ``00:00:00:00:00:00``: ``{"goAction": "play", "style": "itemplay",
    "params": {"item_id": …, "isContextMenu": 1, "touchToPlay": …,
    "touchToPlaySingle": 1}}``, because the ``$pref == 4`` clause (:1977)
    needs a *playing* item with a duration that is not a playlist, and a live
    stream reports ``duration 0``.  That branch stays as it is — the
    "favourites tap does nothing" symptom of the controllers is therefore
    *not* explained by the play/playControl choice for their own requests
    (their rows are field-for-field Perl's).

    The Perl ``< 7.6``-SqueezePlay exception (``:1955-1962``, UA sniffing)
    is not ported: the app version is not part of the request.
    """
    if client_named is None:
        client_named = player is not None
    pref: Any = None
    for a in rest or []:
        s = str(a)
        if s.startswith("defeatDestructiveTouchToPlay:"):
            pref = s[len("defeatDestructiveTouchToPlay:"):]
            break
    if pref is None and player is not None:
        pref = (getattr(player, "playerprefs", None) or {}).get(
            "defeatDestructiveTouchToPlay")
    if pref is None:
        pref = _defeat_pref_default()
    try:
        # Perl numifies the param: a non-numeric string becomes 0.
        num = int(str(pref).strip() or 0)
    except ValueError:
        num = 0
    if not num:
        return False                     # :1975 `return 0 if !$pref`
    if num == 1:
        return True                      # :1976 `$pref == 1`
    if not client_named:
        return True                      # :1976 `|| !$client`
    if player is None:
        return False                     # deviation: named, unknown client
    if num == 4:
        # :1977 `$client->isPlaying() && $client->playingSong()->duration()
        #        && !$client->playingSong()->isPlaylist()`
        url = str(getattr(player, "current_url", "") or "")
        return bool(getattr(player, "mode", "") == "play"
                    and float(getattr(player, "duration", 0) or 0) > 0
                    and not _PLAYLIST_EXT_RE.search(url))
    length = int(getattr(player, "playlist_total", 0) or 0)
    if length < 2:
        return False                     # :1979
    if num == 3 and getattr(player, "mode", "") != "play":
        return False                     # :1980
    return True


def _genres_table_populated(db=None) -> bool:
    """True when the ``genres`` table exists **and** holds rows (LIB-10).

    Perl's ``genresQuery`` reads ``genres`` unconditionally and hands out
    ``DISTINCT(genres.id)`` (``Slim/Control/Queries.pm:1809-1911``); the table
    is only filled by a (re)scan (``Slim/Schema/Genre.pm:91-132``, our
    ``media/importer.py``).  Libraries imported before LIB-10 therefore have an
    empty (or, for very old fixtures, no) ``genres`` table — callers degrade to
    the DISTINCT track-genre text list then (documented divergence: offsets
    instead of real ids until the next rescan).
    """
    try:
        if db is not None:
            return db.execute("SELECT 1 FROM genres LIMIT 1").fetchone() is not None
        return bool(_db_query("SELECT 1 FROM genres LIMIT 1"))
    except Exception:  # noqa: BLE001  (legacy DB without the genres table)
        return False


def _genre_id_to_text(genre_id) -> str:
    """Resolve a genre id to the genre name.

    LIB-10: the id Perl hands out is the real ``genres.id`` (``genresQuery``,
    ``Slim/Control/Queries.pm:1910-1973``) and every drill filter uses
    ``genres.id IN (…)`` (:1832-1836) / ``JOIN genre_track`` (:1828).  The id
    resolves against the ``genres`` table whenever it is populated; on a legacy
    library (empty table, no rescan yet) it falls back to the index into the
    sorted DISTINCT track-genre list — the order ``_library_rows`` /
    ``browselibrary`` emit in that mode (``ORDER BY genre COLLATE NOCASE``).
    """
    try:
        if str(genre_id).isdigit():
            rows = _db_query("SELECT name FROM genres WHERE id = ? LIMIT 1",
                             (int(genre_id),))
            if rows:
                return rows[0]["name"] or ""
    except Exception:  # noqa: BLE001  (legacy DB without the genres table)
        pass
    try:
        if str(genre_id).isdigit():
            rows = _db_query("SELECT DISTINCT genre FROM tracks "
                             "WHERE genre != '' ORDER BY genre COLLATE "
                             "NOCASE LIMIT 1 OFFSET ?", (int(genre_id),))
            return rows[0]["genre"] if rows else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _expand_track_ids(tagged: dict) -> list[int]:
    """Resolve album_id/artist_id/year/genre_id/folder_id filter tokens to
    the full ordered track-id list (SqueezePlay 'playlistcontrol' +
    playlist play/add expansion, Perl playlist control parity)."""
    where: list[str] = []
    params: list = []
    if tagged.get("album_id"):
        where.append("t.id IN (SELECT track FROM tracks_albums WHERE album = ?)")
        params.append(int(tagged["album_id"]))
    elif tagged.get("artist_id"):
        where.append("t.id IN (SELECT track FROM tracks_contributors "
                     "WHERE contributor = ? AND role = 1)")
        params.append(int(tagged["artist_id"]))
    if tagged.get("year"):
        where.append("t.year = ?")
        params.append(int(tagged["year"]))
    if tagged.get("genre_id"):
        genre_text = _genre_id_to_text(tagged["genre_id"])
        if genre_text:
            where.append("t.genre = ?")
            params.append(genre_text)
    if tagged.get("folder_id"):
        # „Musikordner“ (mode:bmf): every track below that directory —
        # Perl's folderId expansion in playlistControl.
        #
        # ``content_type != 'dir'``: the directory rows of the *sub*folders
        # carry URLs below this prefix too, and Perl never loads a directory
        # as a song (its deleted/changed sets and the songs query are filtered
        # the same way, ``Slim/Utils/Scanner/Local.pm:186`` /
        # ``Slim/Control/Queries.pm:4843``).
        where.append("t.url LIKE ?")
        where.append("(t.content_type IS NULL OR t.content_type != 'dir')")
        params.append(_bmf_encoded_prefix(tagged["folder_id"]) + "%")
    if not where:
        return []
    sql = ("SELECT t.id FROM tracks t WHERE " + " AND ".join(where)
           + " ORDER BY t.tracknum, t.title")
    return [int(r["id"]) for r in _db_query(sql, tuple(params))]


# ---------------------------------------------------------------------------
# „Musikordner“ (mode:bmf) — the folder tree, aggregated from the track URLs
# (never from a filesystem walk: the library lives on an SMB share with
# 100k+ files, so the directory levels are derived from tracks.url).
# ---------------------------------------------------------------------------


def _bmf_musicdir_pref() -> str:
    """The runtime ``musicdir`` preference ('' when unset).

    Read through lyrion.config's runtime preference store — the same source
    the importer/rescan uses — NOT the library DB: the prefs table lives in
    prefs.db, lyrion.db has none. Tests monkeypatch this function.
    """
    try:
        from lyrion.config import get_config
        return str(get_config().get("musicdir", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _bmf_path(value: str) -> str:
    """Decoded absolute path for a ``file://`` URI or plain path token.

    Track URLs are percent-encoded the way the importer stores them
    (``Path.as_uri()``: ``smb-share%3Aserver%3D…``, ``%20`` for spaces,
    ``%2B`` for '+'), while the ``musicdir`` pref and the drill tokens are
    plain paths — so every comparison decodes first. Exactly this
    encoding mismatch, plus taking the first URL component as a folder
    name, produced the bogus ``home``/``run`` top level.
    """
    s = str(value or "").strip()
    if s[:7].lower() == "file://":
        s = s[7:]
    if "%" in s:
        try:
            s = urllib.parse.unquote(s)
        except Exception:  # noqa: BLE001
            pass
    s = s.replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    if s and not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


def _bmf_encoded_prefix(directory: str) -> str:
    """``file://``-prefixed, percent-encoded ``directory`` + '/' (LIKE bound)."""
    return "file://" + urllib.parse.quote(_bmf_path(directory), safe="/") + "/"


def _bmf_count_under(root: str) -> int:
    """Tracks whose URL lives below ``root`` (0 on any error).

    Directory rows are not tracks (Perl's ``content_type != 'dir'``,
    ``Slim/Utils/Scanner/Local.pm:186``), so they never satisfy the
    "more than one track" rule below.
    """
    try:
        rows = _db_query("SELECT COUNT(*) AS n FROM tracks WHERE url LIKE ?"
                         " AND (content_type IS NULL OR content_type != 'dir')",
                         (_bmf_encoded_prefix(root) + "%",))
        return int(list(rows[0].values())[0]) if rows else 0
    except Exception:  # noqa: BLE001
        return 0


def _bmf_music_root() -> str:
    """Browse root of „Musikordner“.

    Preferred source is the runtime ``musicdir`` preference. Without it the
    longest common directory root of the ``file://`` track URLs is used —
    but only when it really holds more than one track and is not the
    filesystem root: a few tracks outside the library (e.g. three test
    files under ``~/Music``) must not drag the browse root up to ``/``.
    """
    pref = _bmf_path(_bmf_musicdir_pref())
    if pref:
        return pref
    dirs: list[str] = []
    try:
        for row in _db_query("SELECT DISTINCT url FROM tracks "
                             "WHERE url LIKE 'file://%'"):
            d = posixpath.dirname(_bmf_path(row.get("url") or ""))
            if d and d != "/":
                dirs.append(d)
    except Exception:  # noqa: BLE001
        return ""
    if not dirs:
        return ""
    root = posixpath.commonpath(dirs) if len(dirs) > 1 else dirs[0]
    if root in ("", "/"):
        # Unrelated trees side by side (library + strays): keep the tree
        # holding the most tracks and use its own common root.
        groups: dict[str, list[str]] = {}
        for d in dirs:
            parts = d.split("/")
            groups.setdefault(parts[1] if len(parts) > 1 else d, []).append(d)
        main = max(groups.values(), key=len)
        root = posixpath.commonpath(main) if len(main) > 1 else main[0]
    if root in ("", "/"):
        return ""
    # "nur wenn sie mehr als einen Track umfasst": a single track must not
    # define a browse root.
    return root if _bmf_count_under(root) > 1 else ""


def _bmf_resolve_dir(token: str, root: str) -> str | None:
    """Directory a ``mode:bmf`` drill token (folder_id/search/url) points at.

    Accepts an absolute path below ``root``, a ``file://`` URI or a path
    relative to ``root``; a purely numeric token is a folder id and is resolved
    through the directory's ``tracks`` row, exactly like Perl
    (``Slim/Control/Queries.pm:2311-2316`` → ``findAndScanDirectoryTree``,
    ``Slim/Utils/Misc.pm:1067-1074``; see :func:`_folder_dir_by_id`).

    ``None`` means "browse nothing" for a numeric token that has no directory
    row or whose row is not a directory — live Perl 9.1.1 answers
    ``{"count": 0}`` for both (``musicfolder 0 5 folder_id:99999999`` and
    ``… folder_id:1``, checked read-only).  A path/``file://`` token outside
    ``root`` still falls back to ``root`` (browse the top level instead of
    leaking a foreign directory — a documented deviation, Perl would list it).
    """
    raw = str(token or "").strip()
    if raw and raw.lstrip("-").isdigit():
        return _folder_dir_by_id(raw, root)
    p = _bmf_path(token)
    if not p or p == "/":
        return root
    if p == root or p.startswith(root + "/"):
        return p
    cand = _bmf_path(root + "/" + p.lstrip("/"))
    if cand == root or cand.startswith(root + "/"):
        return cand
    return root


# ---------------------------------------------------------------------------
# Folder ids
#
# Perl stores every directory it has browsed as a row in ``tracks``
# (``content_type = 'dir'``) and hands out that row's ``id``: live Perl 9.1.1
# ``musicfolder 0 5 tags:stuo`` → ``{"id": 204572, "filename": "Accept",
# "type": "folder", "url": "file:///mnt/media/Musik/Accept", "ct": "dir"}``
# (``Slim/Control/Queries.pm:2429-2430`` id/filename, ``:2472-2487`` type,
# ``:2494-2496`` ct).  ``Slim/Utils/Misc.pm:1060-1104``
# ``findAndScanDirectoryTree`` creates the browsed directory's own row
# (``objectForUrl({url, create => 1, readTags => 1, commit => 1})``) and
# ``Queries.pm:2263-2268`` creates the row of every child it *displays* — the
# first pass only checks existence (:2249-2257 "don't create the dir objects in
# the first pass - we can create them later when paging through the list").
# A ``folder_id`` comes back in through that row (``:2311-2316`` →
# ``Slim::Schema->find('Track', $params->{'id'})``).
#
# Our port stores the same rows (``lyrion.media.dir_rows``): the scan writes
# one per walked directory and the browse creates a missing row on demand, so
# a folder id is a real ``tracks.id`` and not a path-derived stand-in.  Only
# when the library DB cannot hold the row (unwritable DB, foreign schema
# without ``content_type``) does the item keep the pre-D4 ``file://`` URL —
# the drill accepts that token too, so nothing becomes unbrowsable.
# ---------------------------------------------------------------------------


def _folder_numeric_id(directory: str) -> int | None:
    """Perl folder id: the ``tracks.id`` of the directory's ``dir`` row.

    Creates the row when it is missing — Perl's browse does exactly that
    (``Slim/Control/Queries.pm:2263-2268``, ``Slim/Utils/Misc.pm:1082-1090``).
    ``None`` when the row could not be established.
    """
    from lyrion.media import dir_rows

    return dir_rows.ensure_dir_row(directory, db_path=_library_db_path())


def _folder_row_numeric_id(row: dict) -> int | None:
    """Stored folder id for a directory row of a browse loop, ``None`` = keep.

    Only rows of ``type 'folder'`` are considered: a *file* row carries a real
    ``tracks.id`` (Perl's own value, ``media/folders.py`` ``_child_item``) and
    must keep it.  The lookup is **read-only** — ``media/folders`` and the bmf
    browse already store the row while listing (Perl ``create => 1``); this
    only upgrades a folder row that still carries a URL (lean/stubbed input)
    when a row happens to exist, and never writes during a read request.
    """
    if str(row.get("type") or "") != "folder":
        return None
    rid = row.get("id")
    if isinstance(rid, int) or (isinstance(rid, str) and rid.lstrip("-").isdigit()):
        return None
    path = _bmf_path(str(row.get("url") or rid or ""))
    if not path or path == "/":
        return None
    return dir_rows.lookup_dir_id(path, db_path=_library_db_path())


def _bmf_subdir_paths(directory: str) -> list[str]:
    """Absolute paths of the immediate subdirectories of ``directory``.

    The one-prefix aggregate over ``tracks.url`` :func:`_bmf_children` is
    built from; the filesystem is never touched.
    """
    prefix = _bmf_encoded_prefix(directory)
    like = prefix + "%"
    off = len(prefix) + 1          # 1-based index behind "<dir>/"
    try:
        rows = _db_query(
            "SELECT seg, COUNT(*) AS n FROM ("
            " SELECT CASE WHEN instr(substr(url, ?), '/') > 0"
            "             THEN substr(url, ?, instr(substr(url, ?), '/') - 1)"
            "             ELSE NULL END AS seg"
            " FROM tracks WHERE url LIKE ?"
            ") WHERE seg IS NOT NULL AND seg != ''"
            " GROUP BY seg ORDER BY seg COLLATE NOCASE",
            (off, off, off, like))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for r in rows:
        name = urllib.parse.unquote(str(r["seg"]))
        path = _bmf_path(directory + "/" + name)
        if path == directory or not path.startswith(directory + "/"):
            continue               # encoding drift / foreign tree → drop
        out.append(path)
    return out


def _bmf_track_artwork(track_ids: list[int]) -> dict[int, int]:
    """``{tracks.id: albums.id}`` for the tracks that have album artwork.

    Perl's bmf feed gives an audio row ``image = 'music/' . coverid . '/cover'``
    and ``artwork_track_id = coverid`` (``Slim/Menu/BrowseLibrary.pm:2097-2100``
    inside ``_bmf``, only ``if $_->{coverid}``); ``XMLBrowser.pm:1160-1167``
    turns those two into the jive keys ``icon`` and ``icon-id``.  ``coverid`` is
    the track's own artwork id (``Slim/Schema/Track.pm:713-740``, truncated md5
    of url/mtime/size).  Our importer never fills ``tracks.artwork``/``cover``
    (0 rows in the live library), so — like every other cover field of this port
    (``JSONRPCAPI._artwork_id``) — the **album id** is the id our
    ``/music/<id>/cover(_<w>x<h>_<m|f>).jpg`` route accepts
    (``web/app.py:_serve_album_cover``).  Only albums with a real ``artwork``
    path are returned, so a cover-less album keeps Perl's "no icon" case (and
    with it ``windowStyle: text_list``, ``XMLBrowser.pm:1434-1441``).

    One batch query for a whole listing; a lean/foreign DB without the
    ``tracks_albums`` link degrades to "no artwork".
    """
    ids = sorted({int(t) for t in track_ids})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, int] = {}
    try:
        for row in _db_query(
                "SELECT ta.track AS tid, al.id AS aid"
                " FROM tracks_albums ta JOIN albums al ON al.id = ta.album"
                f" WHERE ta.track IN ({marks})"
                " AND al.artwork IS NOT NULL AND al.artwork != ''",
                tuple(ids)):
            out.setdefault(int(row["tid"]), int(row["aid"]))
    except Exception:  # noqa: BLE001 - lean DB without the link table
        return {}
    return out


def _bmf_dir_row_ids(directories: list[str]) -> dict[str, int]:
    """``{absolute directory: tracks.id}`` for one listing (Perl ``create=>1``)."""
    from lyrion.media import dir_rows

    return dir_rows.ensure_dir_ids(directories, db_path=_library_db_path())


def _folder_dir_by_id(value: object, root: str) -> str | None:
    """Directory a numeric ``folder_id`` points at, or ``None``.

    Perl resolves it through the directory's ``tracks`` row
    (``Slim/Control/Queries.pm:2311-2316`` → ``Slim::Schema->find('Track',
    $id)`` → ``$topLevelObj->path``, ``Slim/Utils/Misc.pm:1067-1074``): the row
    is looked up by id, whatever its content type, and its URL is the
    directory.  The path must sit below the browse root — the same containment
    rule :func:`_bmf_resolve_dir` applies to path tokens, so a foreign id
    cannot make the browse leave the configured tree.
    """
    from lyrion.media import dir_rows

    row = dir_rows.dir_row_by_id(value, db_path=_library_db_path())
    if not row or not root:
        return None
    path = str(row["path"])
    if not (path == root or path.startswith(root + "/")):
        return None
    if str(row.get("content_type") or "") != dir_rows.DIR_CONTENT_TYPE \
            and not os.path.isdir(path):
        # The id belongs to a *file* row (or any other non-directory row):
        # Perl's ``readDirectory`` on that path lists nothing and live Perl
        # answers ``{"count": 0}`` (probe of ``folder_id:1``, a track id,
        # read-only 2026-09-18).  A ``dir`` row is accepted without touching
        # the filesystem — a library whose SMB mount is away still browses
        # (the port derives the tree from ``tracks.url``, never from a walk).
        return None
    return path


def _bmf_index_dir(index_path: str, root: str) -> str | None:
    """Directory Perl's bmf ``item_id`` index path points at.

    Perl's „Musikordner" feed hands every row ``params.item_id`` — the
    *index path* of that folder inside the feed — and the client sends it
    straight back on the next ``browselibrary items`` request.  Live Perl
    9.1.1 (2026-09-14): the top rows carry ``item_id: "0"``/``"1"``/``"2"``,
    and the tapped child of row 0 answers with ``item_id: "0.0"`` — Perl
    resolves those against the feed it has cached in the browse session
    (``Slim/Control/XMLBrowser.pm``'s ``SID``-indexed feed cache).

    This port keeps no such cache, so an index path is resolved by walking
    the same folder tree in the same order the rows were listed in
    (:func:`_bmf_children`: folders sorted, then the directory's loose
    files).  ``None`` = not resolvable → the caller keeps its fallback.

    SqueezePlay's log line
    ``_getArtworkThumbSink(/html/images/genres_40x40_m.png)`` shows the same
    "index back into the current window" pattern; for the folder feed the
    value is what Perl sends, so it must at least resolve to a directory.
    """
    text = str(index_path or "").strip()
    if not text:
        return None
    directory = root
    for comp in text.split("."):
        comp = comp.strip()
        if not comp.isdigit():
            return None
        idx = int(comp)
        rows, _total = _bmf_children(directory, 0, idx + 1)
        if idx >= len(rows):
            return None
        row = rows[idx]
        if str(row.get("type") or "") != "folder":
            return None
        directory = str(row.get("path") or "")
        if not directory:
            return None
    return directory


#: Mode tokens that mark a request as the „Musikordner“ (Perl bmf) feed.
_BMF_MODES = ("bmf", "musicfolder")


def _is_bmf_tap(args: list) -> bool:
    """Is this ``browselibrary playlist <verb>`` request a „Musikordner“ tap?

    The bmf feed is the only browselibrary feed whose rows are touch-to-play:
    Perl stamps ``params.touchToPlay`` on every audio row
    (``Slim/Control/XMLBrowser.pm:1259-1267``) and the window's own base
    params carry ``mode: bmf`` (``Slim/Menu/BrowseLibrary.pm:2044-2046``).
    Both markers also arrive on the tap the client builds from
    ``base.actions.play`` (``XMLBrowser.pm:1429-1430``) — and neither is sent
    by the ``playlistcontrol`` actions of the other feeds, which must keep
    going through the generic ``playlist`` command.
    """
    for a in args:
        s = str(a)
        if s.startswith("touchToPlay:"):
            return True
        if s.startswith("mode:") and s[5:] in _BMF_MODES:
            return True
    return False


def _bmf_tap_tracks(tagged: dict) -> list[int]:
    """Track ids of the „Musikordner“ row a tap addressed.

    Perl resolves the tapped row from the request's ``item_id`` — the index
    path of the row inside the (cached) feed, ``XMLBrowser.pm:331-405`` — and
    plays the single track it finds there (``:667-702``); a folder row plays
    its whole contained track list (``BrowseLibrary.pm:2084`` ``playall``).

    This port resolves the same row without a feed cache:

    1. ``track_id`` — the resolvable token our own file rows add to
       ``params`` (Perl has no equivalent: it re-reads its cached feed);
    2. ``url`` — a row-less file keeps its ``file://`` URL as its id, and the
       URL also identifies the track row;
    3. ``folder_id`` + ``item_id``/``touchToPlay`` — the window's directory
       plus the row's absolute index, walked with :func:`_bmf_children` (the
       same listing that produced the row, in the same order).

    A directory resolves to every track below it, exactly like Perl's
    ``folder_id`` playlistcontrol expansion.
    """
    tid = str(tagged.get("track_id") or "").strip()
    if tid.isdigit():
        return [int(tid)]

    from lyrion.media.folders import _track_id_by_url

    url = str(tagged.get("url") or "").strip()
    if url:
        resolved = _track_id_by_url(url)
        if resolved is not None:
            return [resolved]

    fid = str(tagged.get("folder_id") or url or "").strip()
    directory = _bmf_resolve_dir(fid, _bmf_music_root()) if fid else None
    idx = next((str(tagged[k]).strip() for k in ("item_id", "touchToPlay")
                if str(tagged.get(k) or "").strip().isdigit()), "")
    if directory and idx:
        rows, _total = _bmf_children(directory, int(idx), 1)
        if rows:
            row = rows[0]
            if str(row.get("type")) == "audio":
                rid = row.get("id")
                if isinstance(rid, int):
                    return [rid]
                resolved = _track_id_by_url(str(rid))
                return [resolved] if resolved is not None else []
            # A folder row: Perl's ``playall`` loads the whole folder.
            directory = str(row.get("path") or directory)
    if not directory:
        return []
    return _expand_track_ids({"folder_id": directory})


def _bmf_window_folder_id(directory: str) -> str:
    """``folder_id`` Perl echoes into the browsed window's own base params.

    Live Perl 9.1.1 ``browselibrary items 0 2 menu:1 mode:bmf
    folder_id:204573`` answers ``base.actions.play.params {mode: 'bmf',
    folder_id: '204573', menu: 'browselibrary'}`` — the feed's
    ``$feed->{'query'}`` (``XMLBrowser.pm:899-901``) carries the drill token
    of the *window*, which is why the tap of a track inherits it.  Looked up
    read-only: the listing already stored its child rows
    (``_bmf_dir_row_ids``), so this browse never writes one for itself.
    """
    from lyrion.media import dir_rows

    rid = dir_rows.lookup_dir_id(directory, db_path=_library_db_path())
    return str(rid) if rid is not None else ""


def _bmf_children(directory: str, start: int = 0,
                  count: int = 200) -> tuple[list, int]:
    """Children of ``directory`` — Perl's ``readDirectory`` listing.

    Returns ``(rows, total)``: ``type 'folder'`` subdirectories (``id`` =
    the directory's ``tracks`` row id, ``path`` = absolute directory, ``name``
    = decoded folder name) and ``type 'audio'`` files of the directory, in the
    order Perl lists them (``sub 'order'`` = ``sortFilename``) — Perl's bmf
    lists files there too, and a folder id is the id of its ``dir`` row
    (``Slim/Control/Queries.pm:2429``, created by ``objectForUrl({url,
    create => 1})`` :2263-2268).

    **The child set is Perl's ``folder_loop`` set** — the ``mode:bmf`` feed is
    a thin wrapper around the ``musicfolder`` query
    (``Slim/Menu/BrowseLibrary.pm:2044-2046`` ``_generic(…, 'musicfolder',
    ['tags:cdus'…])`` → ``Queries.pm:2169-2507`` → ``Slim/Utils/Misc.pm:1150``
    ``readDirectory`` → :973-1043).  Deriving the list from ``tracks`` rows
    alone dropped every child that has no row: live 192.168.1.90 ``folder_id:
    <Bollywood>`` → Perl 13 children, ours 12 — the ``…-Songs Mar 18
    [2008].m3u`` playlist file was missing (Perl's first pass creates the row:
    ``Queries.pm:2263-2268`` ``objectForUrl({create => 1, playlist =>
    isPlaylist($url)})``), and the aggregated rows also re-listed files under
    their 8.3 short names (``folder_id:<Ambient>``: Perl 353, ours 358).  So
    the listing is the source and the rows only supply the ids.

    Only when the listing is empty — a share that is not mounted, or the
    synthetic roots the tests use — the tree is aggregated from the rows
    instead (the pre-``readDirectory`` behaviour): the scan's
    ``content_type='dir'`` rows plus the ancestor directories of the track
    URLs (:func:`_bmf_subdir_paths`).
    """
    prefix = _bmf_encoded_prefix(directory)
    like = prefix + "%"
    off = len(prefix) + 1          # 1-based index behind "<dir>/"
    # IDs of this directory's *file* rows — one query for the whole listing
    # (a file without a row keeps its file URL as ``id``, like
    # ``media/folders._child_item``; Perl would have created the row).
    file_ids: dict[str, int] = {}
    try:
        for r in _db_query(
                "SELECT id, url FROM tracks WHERE url LIKE ?"
                " AND instr(substr(url, ?), '/') = 0", (like, off)):
            p = _bmf_path(str(r.get("url") or ""))
            if p:
                file_ids.setdefault(p, int(r["id"]))
    except Exception:  # noqa: BLE001 - lean DB: ids stay the file URLs
        file_ids = {}
    # The cover of every audio child of this listing — one batch query
    # (``_bmf_track_artwork``): Perl's row gets ``image``/``artwork_track_id``
    # from the track's coverid (``BrowseLibrary.pm:2097-2100``), which is what
    # makes SqueezePlay ask for the thumbnail (``icon-id``).
    track_art = _bmf_track_artwork(list(file_ids.values()))
    from lyrion.media import folders

    try:
        listing = folders.list_directory_entries(directory)
    except Exception:  # noqa: BLE001 - unreadable share → row fallback
        listing = []
    if listing:
        fs_paths = [posixpath.join(directory, name) for name in listing]
        fs_types = [folders.item_type(p) for p in fs_paths]
        dir_ids = _bmf_dir_row_ids(
            [p for p, t in zip(fs_paths, fs_types) if t == "folder"])
        out: list[dict] = []
        for p, name, kind in zip(fs_paths, listing, fs_types):
            if kind == "folder":
                out.append({"id": dir_ids.get(p, p), "path": p,
                            "name": name, "title": name, "type": "folder"})
            else:
                # Perl's bmf feed shows a playlist child as 'audio'
                # (``BrowseLibrary.pm:2125-2138``: ``type 'audio'``,
                # ``playall = 1``); the musicfolder loop itself names it
                # 'playlist' (``Queries.pm:2437-2440``).
                tid = file_ids.get(p)
                row: dict = {"id": tid or folders.file_url_from_path(p),
                             "name": name, "title": name, "type": "audio"}
                if tid is not None and tid in track_art:
                    # ``BrowseLibrary.pm:2097-2100`` → ``XMLBrowser.pm:1160-1167``
                    row["artwork_id"] = track_art[tid]
                out.append(row)
        return out[start:start + count], len(out)
    paths: dict[str, str] = {}     # path → display name
    # (1) directory rows of the children (Perl stores every directory;
    #     their presence is also what makes an empty folder listable).
    try:
        drow = _db_query(
            "SELECT url FROM tracks WHERE content_type = 'dir'"
            " AND url LIKE ? AND instr(substr(url, ?), '/') = 0",
            (like, off))
    except Exception:  # noqa: BLE001 - lean DB without content_type
        drow = []
    for r in drow:
        path = _bmf_path(str(r.get("url") or ""))
        if path and path != directory and path.startswith(directory + "/"):
            paths[path] = posixpath.basename(path)
    # (2) directories aggregated from the track URLs (their rows may be
    #     missing in a library that was scanned before this change).
    for p in _bmf_subdir_paths(directory):
        paths.setdefault(p, posixpath.basename(p))
    folders_list = sorted(paths.items(),
                          key=lambda t: folders.name_collation_key(t[1]))
    dir_ids = _bmf_dir_row_ids([p for p, _ in folders_list])
    try:
        cnt = _db_query(
            "SELECT COUNT(*) AS n FROM tracks WHERE url LIKE ?"
            " AND instr(substr(url, ?), '/') = 0"
            " AND (content_type IS NULL OR content_type != 'dir')", (like, off))
        loose_total = int(list(cnt[0].values())[0]) if cnt else 0
    except Exception:  # noqa: BLE001
        loose_total = 0
    total = len(folders_list) + loose_total
    # Folders first, then the loose files of this directory (Perl returns
    # both in one list).  A directory row is never a loose *file*: it is
    # listed as a folder above.  Without a stored row the item keeps the
    # absolute path as its id — the drill accepts paths as well, so a browse
    # over a library without directory rows stays usable.
    out: list[dict] = [{"id": dir_ids.get(path, path),
                        "path": path, "name": name, "title": name,
                        "type": "folder"}
                       for path, name in folders_list[start:start + count]]
    left = count - len(out)
    if left > 0:
        t_off = 0 if start < len(folders_list) else start - len(folders_list)
        trows: list = []
        try:
            trows = _db_query(
                "SELECT t.id, t.url FROM tracks t WHERE t.url LIKE ?"
                " AND instr(substr(t.url, ?), '/') = 0"
                " AND (t.content_type IS NULL OR t.content_type != 'dir')"
                " ORDER BY t.url COLLATE NOCASE, t.id LIMIT ? OFFSET ?",
                (like, off, left, t_off))
        except Exception:  # noqa: BLE001
            trows = []
        t_art = _bmf_track_artwork([int(r["id"]) for r in trows])
        for r in trows:
            # Perl's bmf shows the decoded file name for files (tags are
            # what the album/year views are for).
            name = posixpath.basename(_bmf_path(r.get("url") or ""))
            row: dict = {"id": r["id"], "name": name, "title": name,
                         "type": "audio"}
            aid = t_art.get(int(r["id"]))
            if aid is not None:
                row["artwork_id"] = aid
            out.append(row)
    return out, total


class JSONRPCError(Exception):
    """JSON-RPC error exception."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }


def _mixer_value(player, entity: str):
    """Value of a mixer entity, Perl ``mixerQuery`` (Queries.pm:2118-2141).

    ``volume``/``muting`` come from the client prefs (mute = pref 'mute'),
    ``bass``/``treble``/``pitch`` from the player getters. ``None`` = unknown
    entity: Perl answers a bad dispatch (i.e. no result at all).
    """
    if entity == "volume":
        return int(getattr(player, "volume", 0) or 0) if player else 0
    if player is None:
        return None
    if entity == "muting":
        return 1 if getattr(player, "mute", False) else 0
    if entity in ("bass", "treble", "pitch"):
        return int(getattr(player, entity, 0) or 0)
    return None


def _mixer_range(entity: str) -> tuple[int, int]:
    """Perl's min/max for a mixer entity.

    bass/treble: Player.pm:366-367 (minBass 0 / maxBass 100); pitch:
    Client.pm:682-683 (minPitch == maxPitch == 100 — players without a real
    pitch control clamp every value to 100); volume: Player.pm:360-361.
    """
    if entity in ("bass", "treble"):
        return 0, 100
    if entity == "pitch":
        return 100, 100
    return 0, 100           # volume


def _mixer_new_value(old: int, raw: str):
    r"""Absolute or RELATIVE mixer value (Perl Commands.pm:601-606).

    ``if ($newvalue =~ /^[\+\-]/) { $newval = $oldval + $newvalue } else
    { $newval = $newvalue }``. ``None`` = not a number.
    """
    try:
        return old + int(raw) if raw[:1] in ("+", "-") else int(raw)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Jive settings menus — Perl Slim/Control/Jive.pm
# ===========================================================================
#
# Die Kommandos werden vom SqueezePlay-Controller für seine Einstellungsmenüs
# aufgerufen (Dispatch-Tabelle Jive.pm:57-162). Antwortform ist überall der
# Menü-Loop (count/offset/item_loop) aus sliceAndShip (Jive.pm:1338-1357) bzw.
# direkt addResult/addResultLoop. Die Feldtypen wurden gegen den Perl-LMS 9.x
# verifiziert (read-only Proben 192.168.1.90:9000, jsonrpc.js) — u. a. ist
# `offset` bei sliceAndShip-Antworten ein STRING (Perl reicht den rohen
# CLI-Parameter durch), `slider.initial` ist ein STRING (Pref-Getter) und
# `cmd`-Werte sind bei crossfade/replaygain STRINGS, sonst ZAHLEN.

#: Englische Fallback-Texte der Perl-Strings (`$client->string($key)`); die
#: Zeilennummern sind die Schlüsselzeilen in /tmp/lms-ref/strings.txt.
_JIVE_STRINGS: dict[str, str] = {
    "CHOICE_OFF": "Off",                                  # :1153
    "LOW": "Low",                                         # :11846
    "MEDIUM": "Medium",                                   # :11790
    "HIGH": "High",                                       # :11863
    "FIXED_VOLUME_100": "Fixed Volume 100%",              # :11662
    "ANALOGOUTMODE_HEADPHONE": "Headphones",              # :20213
    "ANALOGOUTMODE_SUBOUT": "Subwoofer",                  # :20205
    "ANALOGOUTMODE_ALWAYS_ON": "Always On",               # :20230
    "ANALOGOUTMODE_ALWAYS_OFF": "Always Off",             # :20247
    "TRANSITION_NONE": "None",                            # :6056
    "TRANSITION_CROSSFADE": "Crossfade",                  # :6074
    "TRANSITION_FADE_IN": "Fade in",                      # :6092
    "TRANSITION_FADE_OUT": "Fade out",                    # :6110
    "TRANSITION_FADE_IN_OUT": "Fade in and out",          # :6128
    "REPLAYGAIN_DISABLED": "No Volume Adjustment",        # :5672
    "REPLAYGAIN_TRACK_GAIN": "Track Gain",                # :5707
    "REPLAYGAIN_ALBUM_GAIN": "Album Gain",                # :5725
    "REPLAYGAIN_SMART_GAIN": "Smart Gain",                # :5743
    "SORT_ARTISTALBUM": "Artist, Album",                  # :19031
    "SORT_ARTISTYEARALBUM": "Artist, Year, Album",        # :19049
    "ALBUM": "Album",                                     # :12438
    "BRIGHTNESS_DARK": "Dark",                            # :15573
    "BRIGHTNESS_DIMMEST": "Dimmest",                      # :15487
    "BRIGHTNESS_BRIGHTEST": "Brightest",                  # :15593
    "BRIGHTNESS_AMBIENT": "Automatic",                    # :15504
    "SETUP_POWERONBRIGHTNESS_ABBR": "While Active",       # :4468
    "SETUP_POWEROFFBRIGHTNESS_ABBR": "While Off",         # :4486
    "SETUP_IDLEBRIGHTNESS_ABBR": "Idle",                  # :4504
    "SETUP_MINAUTOBRIGHTNESS": "Minimal Brightness (Automatic)",     # :4522
    "SETUP_SENSAUTOBRIGHTNESS": "Brightness Sensitivity (Automatic)",  # :4576
    "LIGHT": "Light",                                     # :11899
    "STANDARD": "Standard",                               # :11936
    "FULL": "Full",                                       # :11881
    "LIGHT_N": "Light Narrow",                            # :15521
    "STANDARD_N": "Standard Narrow",                      # :15539
    "FULL_N": "Full Narrow",                              # :15556
    "SMALL": "Small",                                     # :11770
    "LARGE": "Large",                                     # :11750
    "HUGE": "Huge",                                        # :11809
    # ── Alarm-/Sync-/Sleep-/Preset-Dialoge (Jive.pm:591-1162, 2412-2830) ──
    "OFF": "off",                                          # :11183
    "ON": "on",                                            # :11203
    "ALARM_ALL_ALARMS": "All Alarms",                      # :1855
    "ALARM_ADD": "Add Alarm",                              # :1675
    "ALARM_VOLUME": "Alarm Volume",                        # :1621
    "ALARM_FADE": "Fade Alarms In",                        # :1891
    "ALARM_ALARM_ENABLED": "Enabled",                      # :1747
    "ALARM_SET_TIME": "Set Time",                          # :1910
    "ALARM_SET_DAYS": "Choose Days",                        # :1928
    "ALARM_SELECT_PLAYLIST": "Alarm Sound",                # :1585
    "ALARM_DELETE": "Remove Alarm",                        # :1729
    "ALARM_ALARM": "Alarm",                                # :1509
    "ALARM_ALARM_REPEAT": "Repeat Alarm",                  # :1801
    "ALARM_ALARM_ONETIME": "One Time Alarm",               # :1819
    "ALARM_OFF": "Off",                                    # :1565
    "ALARM_DAY0": "Sunday",                                # :2147
    "ALARM_DAY1": "Monday",                                # :2166
    "ALARM_DAY2": "Tuesday",                               # :2185
    "ALARM_DAY3": "Wednesday",                             # :2204
    "ALARM_DAY4": "Thursday",                              # :2223
    "ALARM_DAY5": "Friday",                                # :2242
    "ALARM_DAY6": "Saturday",                              # :2261
    "ALARM_SHORT_DAY_0": "Su",                             # :2280
    "ALARM_SHORT_DAY_1": "Mo",                             # :2298
    "ALARM_SHORT_DAY_2": "Tu",                             # :2316
    "ALARM_SHORT_DAY_3": "We",                             # :2334
    "ALARM_SHORT_DAY_4": "Th",                             # :2352
    "ALARM_SHORT_DAY_5": "Fr",                             # :2370
    "ALARM_SHORT_DAY_6": "Sa",                             # :2388
    "JIVE_ALARMSET_HELP": (                                # :22609
        "Use the scroll wheel to change clock digits, then press the "
        "center button to select that digit. Press the center button "
        "after time is entered to set the alarm time."
    ),
    "SHUFFLE": "Shuffle",                                  # :11300
    "SHUFFLE_OFF": "Don't Shuffle Playlist",               # :11380
    "SHUFFLE_ON_SONGS": "Shuffle by Song",                 # :11320
    "SHUFFLE_ON_ALBUMS": "Shuffle by Album",               # :11340
    "CANCEL": "Cancel",                                    # :22746
    "EMPTY": "Empty",                                      # :445
    "SLEEP_CANCEL": "Cancel sleep",                        # :1249
    "SLEEPING_IN_X_MINUTES": "Sleeping in %s minutes",     # :1267
    "X_MINUTES": "%s minutes",                             # :1285
    "SLEEP_AT_END_OF_SONG": "Sleep at end of song",        # :1322
    "NOTHING_CURRENTLY_PLAYING": "Nothing currently playing",   # :1358
    "SYNC_ABOUT": (                                        # :23921
        "Add one or more additional Squeezeboxes to use the "
        "Synchronize feature and realize the full potential of "
        "multi-room audio. Visit Lyrion.org for more information."
    ),
    "SYNC_X_TO": "Sync %s to:",                            # :16814
    "DO_NOT_SYNC": "No Sync",                              # :16831
    "SYNCING_WITH": "Syncing with: %s",                    # :16868
    "UNSYNCING_FROM": "Unsyncing from: %s",                # :16885
    "RECENT_SEARCHES": "Recent Searches",                  # :22967
    "PRESET_ADDING": "Saving preset #%s...",               # :23803
    "PRESET": "Preset #%s",                                # :23841
    "PRESETS_NOT_DEFINED": "Preset #%s not defined.",      # :23784
    "JIVE_SET_PRESET_X": "Set Preset %s",                  # :22462
    "JIVE_OVERWRITE_PRESET_X": "Replace %s?",              # :22445
    "ADD": "Add",                                          # :11263
    "DELETE": "Delete",                                    # :16045
    # Plugin-Tabelle (Favorites/strings.txt:3, DE „Favoriten“) und HOME (:3140)
    "FAVORITES": "Favorites",
    "HOME": "Home",                                        # :3140
}

#: Perl-Defaults der Helligkeits-Prefs je Displayklasse
#: (Display/Boom.pm:118-120, Display/Graphics.pm:41-43, Display/Display.pm:68).
#: ``none`` = NoDisplay: dort gibt es nur idleBrightness; powerOn/powerOff sind
#: undef und ``undef == 0`` macht in Perl das Radio bei Option 0 aktiv
#: (Live-Probe gegen SqueezePlay und Squeezebox Radio).
_JIVE_BRIGHTNESS_DEFAULTS: dict[str, dict[str, int]] = {
    "boom": {"powerOnBrightness": 6, "powerOffBrightness": 6, "idleBrightness": 6},
    "graphics": {"powerOnBrightness": 4, "powerOffBrightness": 1, "idleBrightness": 2},
    "squeezeboxg": {"powerOnBrightness": 4, "powerOffBrightness": 1, "idleBrightness": 2},
    "none": {"powerOnBrightness": 0, "powerOffBrightness": 0, "idleBrightness": 1},
}

#: Alle Jive-Settings-Menü-Queries (Jive.pm:75-133)
_JIVE_QUERY_COMMANDS = frozenset({
    "jivetonesettings", "jivefixedvolumesettings", "jivestereoxl",
    "jivelineout", "crossfadesettings", "replaygainsettings",
    "jiveplayerbrightnesssettings", "jiveplayertextsettings",
    "jivealbumsortsettings", "date",
})

#: ``<feed>info`` → the contextmenu.py ``menu:<name>`` entity it maps onto.
#: Perl's ``contextmenu`` command is a wrapper that forwards to exactly this
#: feed (Slim/Control/Queries.pm:6196-6200), so both entry points share the
#: builder.  ``yearinfo`` is handled separately (menus.year_info_menu) —
#: ``folderinfo``/``systeminfo``/``playlistinfo`` stay unknown for now.
_INFO_FEED_MENUS = {
    "trackinfo": "track",
    "albuminfo": "album",
    "artistinfo": "artist",
    "genreinfo": "genre",
}

#: Perl ``Slim::Schema::Contributor->roleToType`` (Schema.pm) — the role id → role
#: name map used by ``roles <i> <q> tags:t`` (Queries.pm:3447-3452). Only the
#: roles our importer writes exist here (1 = artist); an unknown id keeps the
#: empty string instead of inventing a name.
_ROLE_NAMES = {
    1: "ARTIST",
}

#: Hard ceiling for one ``browselibrary`` library read. Perl answers a browse
#: from its cached SQL result sets; ours reads sqlite synchronously inside the
#: event loop, which also serves one full status push per connected cometd
#: client per second. Live 2026-09-13 17:34:16 the /cometd POST carrying
#: Squeezer's ``browselibrary items 0 512 mode:albums useContextMenu:1 menu:1``
#: never produced a response (dev log /tmp/lyrion-live.log:1519; no uvicorn
#: access line for 192.168.240.112 afterwards) — the app's album list spun
#: forever. Same guard as the radios fix (d6653d91c): the request path is
#: bounded and degrades to the Perl-shaped empty answer instead of holding the
#: connection open.
_LIBRARY_QUERY_TIMEOUT = 20.0

#: Entitäten von ``playlist <entity> ?`` mit ``_index`` in der Perl-Dispatch-
#: Form (``Request.pm:551``, ``:553``, ``:559``, ``:560``, ``:575``, ``:581``,
#: ``:588``): sie arbeiten auf dem Track an diesem Index, ohne Index auf dem
#: laufenden Titel (``Playlist.pm:62-73``:
#: ``$index = Slim::Player::Source::playingSongIndex($client)`` :72-73).
_PLAYLIST_INDEX_ENTITIES = frozenset({
    "album", "artist", "duration", "genre", "path", "remote", "title",
})

#: Alle ``playlist <entity> ?``-Entities (``playlistXQuery``,
#: ``Queries.pm:2708-2772``).
_PLAYLIST_QUERY_ENTITIES = _PLAYLIST_INDEX_ENTITIES | frozenset({
    "name", "url", "modified", "tracks", "repeat", "shuffle", "index", "jump",
})


def _playlist_local_id(entry: object) -> int | None:
    """DB-Track-Id eines Queue-Eintrags, sonst ``None`` (Remote-URL).

    Dieselbe Regel wie in ``_json_player_status``: eine Zahl oder ein reiner
    Ziffern-String ist ein lokaler Track (der CLI-/Plugin-Pfad liefert
    ``"51994"``), alles andere eine Stream-URL.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    text = str(entry)
    return int(text) if text.isdigit() else None


def _can_dispatch(tokens: list) -> bool:
    """Perl ``canQuery``: gibt es einen Dispatch-Eintrag für diese Tokens?

    Perl baut aus den ``_p1``…``_p5``-Parametern (leere und ``?`` werden
    vorher entfernt, ``Slim/Plugin/CLI/Plugin.pm:764-778``) einen
    ``Slim::Control::Request`` und antwortet ``_can`` 1, wenn eine Funktion
    gefunden wurde (:791); login/shutdown/exit sind immer erreichbar (:783).
    Ein leeres Token-Array ist nie dispatchbar — live gegen Perl 9.1.1:
    ``can ?`` → ``{"_can":0}``.
    """
    words = [str(t) for t in tokens if str(t) not in ("", "?")]
    if not words:
        return False
    name = words[0].lower()
    if name in _KNOWN_JSON_COMMANDS:
        return True
    try:
        from lyrion.control.cli import get_registered_commands
        return name in get_registered_commands()
    except Exception:  # noqa: BLE001
        return False


#: Kommandos, die nur der JSON-RPC-Pfad beantwortet (nicht in der
#: CLI-Registry) — für ``can <cmd> ?``.
_KNOWN_JSON_COMMANDS = frozenset({
    "jsonrpc", "displaystatus", "menustatus", "artworkspec", "apps", "radios",
    "radiosearch", "browse", "browsedb", "folderinfo", "folders", "readdirectory",
})


def _favorites_icon(url: str) -> str:
    """``$favs->icon($url)`` — the icon of a favourites entry.

    ``Slim/Plugin/Favorites/OpmlFavorites.pm:83-88``::

        return Slim::Player::ProtocolHandlers->iconForURL($url)
            || 'html/images/favorites.png';

    ``iconForURL`` asks the URL's protocol handler (``ProtocolHandlers.pm:
    138-153``); an http(s) stream answers ``HTTP.pm:1138-1147`` →
    ``'html/images/radio.png'``, which is the same fallback the port uses for
    stream artwork (:data:`REMOTE_ART_FALLBACK`).  Everything else (a file URL,
    a folder) gets the favourites icon.
    """
    from lyrion.web.favorites_menu import FAVORITES_ICON

    if str(url).lower().startswith(("http://", "https://")):
        return REMOTE_ART_FALLBACK
    return FAVORITES_ICON


def _jive_string(key: str) -> str:
    """Perl ``$client->string($key)`` (Slim/Utils/Strings.pm:525-536).

    Perl macht ``uc($token)`` vor dem Lookup ('light' → 'LIGHT'). Die aktive
    Sprache kommt aus dem Server-Pref ``language`` (Strings.pm:622-624); ein
    fehlender DE-Key fällt auf die Failsafe-Sprache EN zurück
    (Strings.pm:414-416) und danach auf unser bisheriges englisches Literal.
    """
    from lyrion.i18n import get_string, resolve_language
    token = str(key).upper()
    return get_string(token, lang=resolve_language(),
                      default=_JIVE_STRINGS.get(token, token))


# ---------------------------------------------------------------------------
# Android-/Jive-Controller-Kommandos in Perl-Form (nur lesend)
# ---------------------------------------------------------------------------
# Live-Proben gegen das Referenz-Perl-LMS 9.1.1 (192.168.1.90:9000,
# 2026-09-13, ausschließlich lesende Kommandos); jede wörtliche Antwort steht
# im Kopf von ``tests/test_controller_commands.py``. Perl beendet einen
# *nicht dispatchbaren* Aufruf (Status 102/103/104 — ``isNotQuery``/
# ``isNotCommand`` oder kein Funktionszeiger im Dispatch-Eintrag,
# Request.pm:1044-1084) mit geschlossenem Socket ohne Body (JSONRPC.pm:
# 497-517); dieser Port antwortet dann — wie schon bei mixer/info — mit dem
# leeren Dict.

#: Ein Kategorie-Token für ``debug <flag> ?`` — Perls ``isValidCategory``
#: prüft den Eintrag ``log4perl.logger.<flag>`` (Log.pm:475-488).
_DEBUG_CATEGORY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


def _client_string(token: str) -> str:
    """Perl ``$request->string($token)`` für ``getStringQuery`` (Queries.pm:1988-2016).

    Perl gibt den lokalisierten Text zurück, wenn das Token existiert, sonst
    ``''`` (:2004-2012). Unsere i18n-Tabellen tragen nur die Keys, die dieser
    Port selbst aussendet — ein Token, das nur Perl kennt, ergibt hier
    deshalb ``''``, also dieselbe Antwort wie Perls unbekanntes Token.
    """
    from lyrion.i18n import get_string, resolve_language
    return get_string(str(token).upper(), lang=resolve_language(), default="")


# ---------------------------------------------------------------------------
# Perl's date/time strings (Slim/Utils/DateTime.pm) and the jive display block
# (Slim/Player/Player.pm:488-706 currentSongLines)
# ---------------------------------------------------------------------------

#: ``strings.txt`` carries the locale NAME per language (``LOCALE``);
#: ``Strings.pm:712-728`` ``setLocale`` installs it on LC_TIME at startup:
#: ``setlocale(LC_TIME, string('LOCALE') . '.UTF-8')``.  Live Perl 9.1.1:
#: ``getstring LOCALE,LOCALE_WIN`` -> ``de_DE`` / ``deu_deu``.
_PERL_LOCALES: dict[str, str] = {"DE": "de_DE", "EN": "en_US"}

#: ``DateTime.pm:450-452`` seeds the prefs ``longdateFormat``/``timeFormat``
#: from the string table at startup; the values below are the ``DE``/``EN``
#: rows of ``strings.txt`` (``SETUP_LONGDATEFORMAT_DEFAULT`` /
#: ``SETUP_TIMEFORMAT_DEFAULT``).  Live Perl 9.1.1 (de_DE):
#: ``%A, |%d. %B %Y`` and ``%H:%M``.
_PERL_DATE_FORMATS: dict[str, tuple[str, str]] = {
    "DE": ("%A, |%d. %B %Y", "%H:%M"),
    "EN": ("%A, %B |%d, %Y", "|%I:%M %p"),
}

#: Perl's no-padding flag ``|``, as stripped AFTER strftime:
#: ``DateTime.pm:57`` ``$date =~ s/\|0*//`` (longDateF) and :102
#: ``$time =~ s/\|0?(\d+)/$1/`` (timeF).
_PIPE_STRIP_ALL_ZEROS = re.compile(r"\|0*")
_PIPE_STRIP_ONE_ZERO = re.compile(r"\|0?(\d+)")

#: LC_TIME already installed by :func:`_perl_set_time_locale` (Perl calls
#: ``setLocale`` once at startup, not per request).
_perl_time_locale_lang: str = ""


def _perl_set_time_locale(lang: str) -> str:
    """Install LC_TIME for ``lang`` — Perl ``Strings::setLocale`` (:712-728).

    ``setlocale(LC_TIME, string('LOCALE'))``; Perl appends ``.UTF-8`` when the
    current locale is UTF-8 (:715-716).  Returns the locale now in effect
    (``C`` when the system has no such locale, so ``strftime`` keeps the
    C names instead of raising).
    """
    global _perl_time_locale_lang
    if _perl_time_locale_lang == lang:
        return lang
    import locale as _locale

    name = _PERL_LOCALES.get(lang) or _PERL_LOCALES["EN"]
    try:
        _locale.setlocale(_locale.LC_TIME, name)
    except _locale.Error:
        for candidate in (f"{name}.UTF-8", f"{name}.utf8", "C"):
            try:
                _locale.setlocale(_locale.LC_TIME, candidate)
                break
            except _locale.Error:
                continue
    _perl_time_locale_lang = lang
    return lang


def _perl_date_part(fmt: str, tm: time.struct_time, one_zero: bool) -> str:
    """``strftime`` + Perl's ``|`` padding-flag strip (``DateTime.pm:52-105``).

    Perl renders with the pref format and then removes the flag from the
    OUTPUT string (``s/\\|0*//`` for longDateF, ``s/\\|0?(\\d+)/$1/`` for
    timeF), which is what glibc leaves behind for ``|%d``/``|%I``.
    """
    rendered = time.strftime(fmt, tm)
    if one_zero:
        return _PIPE_STRIP_ONE_ZERO.sub(r"\1", rendered)
    return _PIPE_STRIP_ALL_ZEROS.sub("", rendered)


def _perl_date_prefs() -> tuple[str, str]:
    """The prefs ``longdateFormat``/``timeFormat`` (seeded, ``DateTime.pm:450``).

    Our port does not seed them at startup, so the string-table defaults stand
    in — read through the i18n table so a later port of the startup seeding
    wins automatically.
    """
    from lyrion.i18n import get_string, resolve_language

    lang = resolve_language()
    long_default, time_default = _PERL_DATE_FORMATS.get(
        lang, _PERL_DATE_FORMATS["EN"])
    long_fmt = time_fmt = ""
    try:
        from lyrion.config import get_prefs

        prefs = get_prefs()
        long_fmt = str(prefs.get("longdateFormat") or "")
        time_fmt = str(prefs.get("timeFormat") or "")
    except Exception:  # noqa: BLE001 — Prefs dürfen die Anzeige nie stören
        pass
    if not long_fmt:
        long_fmt = get_string("SETUP_LONGDATEFORMAT_DEFAULT", lang,
                              default=long_default)
    if not time_fmt:
        time_fmt = get_string("SETUP_TIMEFORMAT_DEFAULT", lang,
                              default=time_default)
    return long_fmt, time_fmt


def _perl_added_time(now: float | None = None) -> str:
    """Perl ``Track::addedTime`` for a REMOTE track (``Track.pm:325-341``).

    ``addedTime`` is ``buildModificationTime($self->added_time)``, i.e.
    ``join(', ', longDateF($time), timeF($time))``.  A
    ``Slim::Schema::RemoteTrack`` never gets an ``added_time`` (the attribute
    exists, ``RemoteTrack.pm:46``, but only DB writes fill it —
    ``Schema.pm:1740``, local tracks), so both helpers fall back to *now*
    (``DateTime.pm:53`` ``my $time = shift || time()``).

    Live Perl 9.1.1 on the ``de_DE`` server: ``remoteMeta.addedTime`` =
    ``Montag, 14. September 2026, 14:19`` — and re-querying a minute later
    returned ``… 14:19`` again, i.e. the QUERY time, never a stored value.
    """
    from lyrion.i18n import resolve_language

    lang = resolve_language()
    _perl_set_time_locale(lang)
    long_fmt, time_fmt = _perl_date_prefs()
    tm = time.localtime(now if now is not None else time.time())
    return (f"{_perl_date_part(long_fmt, tm, one_zero=False)}, "
            f"{_perl_date_part(time_fmt, tm, one_zero=True)}")


def _perl_pretty_bitrate(bps: float | int | str | None,
                         vbr_scale: object = None) -> object:
    """Perl ``Track::buildPrettyBitRate`` (``Track.pm:353-363``).

    ``sprintf("%d", $bitrate / 1000) . string('KBPS') . ' ' . $mode`` where
    ``$mode`` is ``VBR`` when a ``vbr_scale`` exists, else ``CBR``; ``0`` when
    no bitrate is known (Perl returns the number 0).  Live Perl for the 1.FM
    stream (ICY ``icy-br: 256``, ``HTTP.pm:731-734`` scales < 8000 by 1000):
    ``"256kb/s CBR"``.
    """
    from lyrion.i18n import get_string, resolve_language

    try:
        value = float(bps or 0)
    except (TypeError, ValueError):
        value = 0.0
    if not value:
        return 0
    mode = "VBR" if vbr_scale is not None else "CBR"
    # strings.txt KBPS = "kb/s" (live Perl getstring KBPS -> "kb/s").
    return (f"{int(value / 1000)}"
            f"{get_string('KBPS', resolve_language(), default='kb/s')} {mode}")


#: Perl tag letter -> ``_songData`` result key for a remote track.  Only the
#: letters whose SOURCE exists in this port are listed; every other letter
#: yields no key for a stream in Perl either (live ``status - 1
#: tags:ABCDEKJZlcuxyrtSgad`` on a stream: artist, addedTime, artwork_url,
#: coverid, url, remote, year, bitrate, duration).
_REMOTE_TAG_KEYS: dict[str, str] = {
    "a": "artist", "A": "artist",
    "l": "album",
    "d": "duration",
    "D": "addedTime",
    "r": "bitrate",
    "u": "url",
    "x": "remote",
    "y": "year",
    "c": "coverid",
    "K": "artwork_url",
    "j": "coverart",
    "J": "artwork_track_id",
    "N": "remote_title",
}


def _perl_proxied_image(url: object, force: bool = False) -> object:
    """Perl ``Slim::Web::ImageProxy::proxiedImage`` (``ImageProxy.pm:407-425``).

    ``return $url unless $force || ($url && $url =~ /^https?:/);`` — only
    external URLs are wrapped; the extension is taken from the URL (default
    ``.png``, ``jpeg`` -> ``jpg``).
    """
    if not isinstance(url, str) or not url:
        return url
    if not (force or url.startswith("http:") or url.startswith("https:")):
        return url
    from urllib.parse import quote

    ext = ".png"
    m = re.search(r"(\.(?:jpg|jpeg|png|gif))", url)
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    # URI::Escape::uri_escape_utf8 leaves A-Za-z0-9-_. alone.
    return f"/imageproxy/{quote(url, safe='-_.')}/image{ext}"


def _stream_display_title(player: object, entry: object) -> str:
    """Perl ``$track->title`` for the playing entry — ``Player.pm:653``.

    For a remote stream that is the RemoteTrack's title (the ICY/stream title,
    live ``Life Breath (oct12)``); for a local track the DB title.  The raw ICY
    value in ``player.current_title`` is ``Artist - Title`` (Perl sends it as
    ``current_title``, Queries.pm:4089-4090) — the item/jive title is the part
    after the separator, exactly like the status builder's ``_cur_track``.
    """
    local_id = _playlist_local_id(entry)
    if local_id is not None:
        rows = _db_query("SELECT title FROM tracks WHERE id = ? LIMIT 1",
                         (local_id,))
        return str((rows[0]["title"] if rows else "") or "")
    url = str(getattr(player, "current_url", "")
              or (entry if isinstance(entry, str) else "") or "")
    title = str((getattr(player, "stream_titles", {}) or {}).get(url, "") or "")
    if title:
        return title
    raw = str(getattr(player, "current_title", "") or "")
    if " - " in raw:
        head, _, tail = raw.partition(" - ")
        if tail.strip():
            return tail.strip() if head.strip() else raw
    return raw or url


async def jive_now_playing_display(player: object) -> dict | None:
    """Perl ``Slim::Player::Player::currentSongLines`` jive block.

    ``Player.pm:506-673``::

        if ($playlistlen < 1) { $status = string('NOTHING'); … }      # :509-518
        else {
            if ($playmode eq "pause") { $status = string('PAUSED'); …} # :521-535
            elsif ($playmode eq "stop") { $status = string('STOPPED'); …} # :541-553
            else { $status = string('PLAYING'); … }                    # :571-585
            …
            $jive = { 'type' => 'icon',
                      'text' => [ $status, $track ? $track->title : undef ],
                      'style' => $jiveIconStyle,        # = $playmode
                      'play-mode' => $playmode,
                      'is-remote' => $track->isRemoteURL };  # :651-668
            if ($imgKey) { $jive->{$imgKey} = proxiedImage($artwork); } # :669-673
        }

    ``$jiveIconStyle`` defaults to ``$playmode`` (:507); the ``rew``/``fwd``
    styles belong to the jump command (Commands.pm:1026-1034) and are not used
    on a plain start.  ``$status`` is the BARE status string — the ``" (i OUT_OF
    n) "`` suffix is added to ``$lines[0]`` only (:530-535), never to the jive
    text.  Returns ``None`` when the playlist is empty: that branch never sets
    ``$jive`` (:509-518), so ``$parts->{'jive'}`` stays undefined and
    ``displaystatusQuery`` falls back to the line text (:1691-1695).
    """
    playlist = list(getattr(player, "playlist", None) or [])
    if len(playlist) < 1:
        return None
    from lyrion.player.manager import (      # Now-Playing strings of the port
        NOW_PLAYING_TEXT, PAUSED_TEXT, STOPPED_TEXT,
    )

    mode = str(getattr(player, "mode", "stop") or "stop")
    if mode == "pause":
        status = PAUSED_TEXT               # Player.pm:521-535
    elif mode == "stop":
        status = STOPPED_TEXT              # Player.pm:541-553
    else:
        status = NOW_PLAYING_TEXT          # Player.pm:571-585 (string('PLAYING'))
    index = int(getattr(player, "playlist_position", 0) or 0)
    entry = playlist[index] if 0 <= index < len(playlist) else None
    title = _stream_display_title(player, entry)
    is_remote = _playlist_local_id(entry) is None
    jive: dict[str, Any] = {
        "type": "icon",
        "text": [status, title or None],   # :651-655
        "style": mode,                     # :507 jiveIconStyle = playmode
        "play-mode": mode,
        "is-remote": 1 if is_remote else 0,
    }
    if is_remote:
        # Player.pm:584-630: a handler cover wins ('icon'), then a handler icon
        # ('icon-id'); without either, Perl falls back to the radio default
        # with 'icon-id' (:623-626).
        url = str(getattr(player, "current_url", "")
                  or (entry if isinstance(entry, str) else "") or "")
        art = str((getattr(player, "stream_images", {}) or {}).get(url, "") or "")
        if art:
            jive["icon"] = _perl_proxied_image(art)          # :594-596
        else:
            jive["icon-id"] = _perl_proxied_image(
                RADIO_PLACEHOLDER_ICON)                      # :623-626 + :670
    else:
        # Player.pm:628-631: `$imgKey = 'icon-id'; $artwork = $album->artwork || 0`
        art_id: Any = 0
        local_id = _playlist_local_id(entry)
        if local_id is not None:
            rows = _db_query(
                "SELECT al.id AS id FROM albums al "
                "JOIN tracks_albums ta ON ta.album = al.id "
                "WHERE ta.track = ? LIMIT 1", (local_id,))
            if rows and rows[0]["id"]:
                art_id = rows[0]["id"]
        jive["icon-id"] = _perl_proxied_image(art_id)        # :670-671
    return jive


def _debug_category_level(flag: str) -> str | None:
    """Antwortwert für ``debug <flag> ?`` (Perl ``debugQuery``, Queries.pm:1517-1549).

    Perl liefert den Level-Namen der Kategorie (:1538-1544); live
    ``debug scan ?`` → ``{"_value":"ERROR"}``. ``None`` heißt „ungültiges
    Flag" (:1526-1535 ``setStatusBadParams`` → Socket zu).

    Unser Port führt keine log4perl-Kategorienliste (Perl Log.pm:879-960);
    ein uns unbekanntes, aber syntaktisch gültiges Token beantworten wir
    deshalb mit Perls Default für eine unkonfigurierte Kategorie, ``ERROR``
    (Log.pm:868 ``|| 'ERROR'``), statt mit dem geschlossenen Socket.
    """
    token = str(flag or "").strip()
    if not _DEBUG_CATEGORY_RE.match(token):
        return None
    name = f"lyrion.{token}"
    if name in logging.Logger.manager.loggerDict:
        return logging.getLevelName(logging.getLogger(name).getEffectiveLevel())
    return "ERROR"


def _fs_page(index: Any, quantity: Any, count: int) -> tuple[int, int] | None:
    """Perl ``Slim::Control::Request::normalize`` (Request.pm:1805-1839).

    ``None`` heißt „nicht valide" — Perl füllt dann nur ``count``
    (Queries.pm:3165-3170).
    """
    def _num(value: Any) -> int | None:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    frm = _num(index)
    num = _num(quantity)
    if num is None and frm is not None:
        num = count
    if not num or not count:
        return None
    if frm is None:
        frm = 0
    if frm > count - 1:
        return None
    if frm < 0:
        frm = 0
    return frm, min(frm + num - 1, count - 1)


def _read_directory(args: list) -> dict:
    """``readdirectory <index> <quantity> [folder:<path>] [filter:<f>]``.

    Perl ``readDirectoryQuery`` (Queries.pm:3093-3213), Dispatch
    ``['readdirectory','_index','_quantity']`` (Request.pm:497). Antwort:
    ``count`` plus ``fsitems_loop`` mit ``path``/``name``/``isfolder``
    (:3201-3210), Ordner vor Dateien (:3183-3193). Live 2026-09-13:
    ``readdirectory 0 5`` → ``{"count":0}`` (ohne ``folder`` liest Perl ein
    leeres Verzeichnis), ``readdirectory 0 5 folder:/tmp`` →
    ``{"count":118,"fsitems_loop":[{"isfolder":"1","name":…,"path":…},…]}``.
    Perl serialisiert ``isfolder`` als ``"1"``/``"0"``; wir liefern die Zahl
    (Squeezer ruft das Kommando nicht auf).
    """
    folder = ""
    want = ""
    for a in (list(args)[2:] if len(args) > 2 else []):
        s = str(a)
        if s.startswith("folder:"):
            folder = s[7:]
        elif s.startswith("filter:"):
            want = s[7:]
    if not folder:
        # Perl: Slim::Utils::Misc::readDirectory(undef) → leere Liste.
        return {"count": 0}

    names: list[str] = []
    is_dir: dict[str, bool] = {}
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    names.append(entry.name)
                    is_dir[entry.name] = entry.is_dir()
                except OSError:
                    continue
    except OSError:
        return {"count": 0}

    if want == "foldersonly":
        names = [n for n in names if is_dir[n]]
    elif want == "filesonly":
        names = [n for n in names if not is_dir[n]]
    elif want.startswith("filetype:"):
        suffix = "." + want[len("filetype:"):].lower()
        names = [n for n in names if is_dir[n] or n.lower().endswith(suffix)]
    elif want and not want.startswith(("filename:", "filetype:")):
        try:
            pattern = re.compile(want, re.IGNORECASE)
        except re.error:
            pattern = None
        if pattern is not None:
            names = [n for n in names
                     if pattern.search(os.path.join(folder, n))]

    # Perl sortiert Ordner vor Dateien (Queries.pm:3183-3193); die
    # Namenssortierung davor macht die Reihenfolge bestimmt. Perl sortiert die
    # Namen in ``readDirectory`` mit ``sortFilename`` (Misc.pm:1037 →
    # OS.pm:334-353, Gross-/Kleinschreibung egal), der Ordner-vor-Dateien-Lauf
    # ist ein stabiler ``sort`` und hält diese Reihenfolge je Gruppe.
    from lyrion.media import folders

    names = folders.sort_filenames(names)
    ordered = ([n for n in names if is_dir[n]]
               + [n for n in names if not is_dir[n]])
    count = len(ordered)
    page = _fs_page(args[0] if args else None,
                    args[1] if len(args) > 1 else None, count)
    if page is None:
        return {"count": count}
    start, end = page
    loop = []
    for name in ordered[start:end + 1]:
        # Perl: catdir($folder, $item) bzw. $item ohne Ordner (:3194).
        loop.append({
            "path": os.path.join(folder, name),
            "name": name,
            "isfolder": 1 if is_dir[name] else 0,
        })
    return {"count": count, "fsitems_loop": loop}


def _last_scan_epoch() -> int:
    """Zeitpunkt des letzten Bibliotheks-Scans als Unix-Epoch.

    Perl: ``Slim::Music::Import->lastScanTime()`` aus
    ``metainformation.name='lastRescanTime'`` (Import.pm:290-300), von
    ``serverstatusQuery`` als ``lastscan`` ausgegeben (Queries.pm:3731);
    live ``{"lastscan":"1789309710"}``. Unser Schema hat keine
    ``metainformation``-Tabelle — nächste Entsprechung ist
    ``MAX(tracks.lastscanned)`` (UTC-Zeitstempel des zuletzt erfaßten
    Titels, ``importer.py``). Controller lesen den Wert als Zahl
    (Squeezer: ``Util.getLong(data, "lastscan")``, CometClient.java:355).
    """
    try:
        rows = _db_query("SELECT MAX(lastscanned) AS t FROM tracks")
    except Exception:  # noqa: BLE001 — kein Scan-Zeitstempel verfügbar
        return 0
    if not rows or not rows[0].get("t"):
        return 0
    try:
        from datetime import datetime, timezone
        stamp = str(rows[0]["t"]).split(".", 1)[0]
        return int(datetime.fromisoformat(stamp)
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _jive_params(args, names: list[str]) -> tuple[dict, dict]:
    """CLI-Token auf eine Perl-Dispatch-Paramliste abbilden (Request.pm:1006-1024).

    Die ersten ``len(names)`` Token sind die positionalen Parameter
    (``_index``, ``_quantity``, ``_whatFont``), alle weiteren sind „tagged“
    Parameter ``name:wert``; Token ohne Doppelpunkt werden ignoriert.
    """
    positional: dict[str, Any] = {}
    i = 0
    for name in names:
        positional[name] = args[i] if i < len(args) else None
        i += 1
    tagged: dict[str, str] = {}
    for tok in args[i:]:
        s = str(tok)
        if ":" in s:
            k, _, v = s.partition(":")
            tagged[k] = v
    return positional, tagged


def _jive_num(value, default=None):
    """Zahlenwert eines Perl-Skalars (undef/'?'/Text -> ``default``)."""
    if value is None:
        return default
    s = str(value).strip()
    if not s or s == "?":
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _jive_slice(items: list, index, quantity) -> dict:
    """Perl ``sliceAndShip`` (Jive.pm:1338-1357) + ``normalize`` (Request.pm:1805-1839).

    ``count`` ist die Gesamtzahl, ``offset`` der Startindex — als STRING, weil
    Perl den rohen CLI-Parameter durchreicht (Live-Probe: ``"offset":"1"``;
    bei negativem Index klemmt Perl auf die Zahl 0). Ungültige Anfragen
    (Index hinter dem Listenende, Quantity 0, kein Index) liefern nur ``count``.
    """
    count = len(items)
    if index is None:
        return {"count": count}
    num = quantity if quantity is not None else count
    if not num or not count or index > count - 1:
        return {"count": count}
    start = 0 if index < 0 else index
    end = min(start + num - 1, count - 1)
    return {
        "count": count,
        "offset": 0 if index < 0 else str(index),
        "item_loop": items[start:end + 1],
    }


def _jive_client_pref(player, key: str, default=None):
    """Perl ``$prefs->client($client)->get($key)``.

    Unser per-Player-Pref-Store ist ``PlayerState.playerprefs`` — derselbe
    Speicher, den das ``playerpref``-CLI-Kommando schreibt (cli_commands.py:963).
    """
    prefs = getattr(player, "playerprefs", None) or {}
    if key in prefs:
        return prefs[key]
    return default


def _jive_display_class(model: str) -> str:
    """Perl-Displayklasse eines Players, abgeleitet aus dem vfdmodel.

    ``squeezeplay``/``controller``/``receiver`` u. a. → NoDisplay,
    ``squeezebox2``/``3``/``transporter`` → Squeezebox2 (Grafik),
    ``boom`` → Boom, ``squeezebox`` (SB1) → SqueezeboxG.
    """
    from lyrion.player.manager import display_type_for
    dt = display_type_for(model)
    if dt == "graphic-160x32":
        return "boom"
    if dt == "graphic-280x16":
        return "squeezeboxg"
    if dt == "graphic-320x32":
        return "graphics"
    return "none"


def _jive_brightness_options(model: str) -> dict[int, str]:
    """Perl ``getBrightnessOptions`` (Display.pm:401-438).

    NoDisplay liefert ``maxBrightness() == undef`` → unveränderte Basis-Tabelle
    (0..4); Grafik-Displays haben eine brightnessMap mit 5 Einträgen
    (Squeezebox2.pm:209-211, SqueezeboxG.pm:135-137) → maxBrightness 4 und
    derselbe Text; nur Boom (brightnessMap mit 7 Einträgen, Display/Boom.pm:
    180-192) bekommt die Umgebungsstufe „Automatic" (Display.pm:425-437).
    """
    dark = _jive_string("BRIGHTNESS_DARK")
    dimmest = _jive_string("BRIGHTNESS_DIMMEST")
    brightest = _jive_string("BRIGHTNESS_BRIGHTEST")
    if _jive_display_class(model) == "boom":
        return {
            0: f"0 ({dark})", 1: f"1 ({dimmest})", 2: "2", 3: "3", 4: "4",
            5: f"5 ({brightest})", 6: _jive_string("BRIGHTNESS_AMBIENT"),
        }
    return {
        0: f"0 ({dark})", 1: f"1 ({dimmest})", 2: "2", 3: "3",
        4: f"4 ({brightest})",
    }


def _jive_fonts(display: str) -> dict[str, tuple[list[str], int]]:
    """Font-Prefs der Displayklasse: ``{pref: (fontnamen, default _curr)}``.

    Boom: Display/Boom.pm:126-131 (light_n/standard_n/full_n, idleFont_curr 2),
    Squeezebox2: Squeezebox2.pm:136-140 (light/standard/full, _curr 1),
    SqueezeboxG: SqueezeboxG.pm:45-50 (small/medium/large/huge, _curr 1).
    NoDisplay hat keine Font-Prefs (Perl iteriert dort ein undef-Array und
    stirbt → keine Antwort).
    """
    if display == "boom":
        narrow = ["light_n", "standard_n", "full_n"]
        return {"activeFont": (narrow, 1), "idleFont": (narrow, 2)}
    if display == "squeezeboxg":
        gfonts = ["small", "medium", "large", "huge"]
        return {"activeFont": (gfonts, 1), "idleFont": (gfonts, 1)}
    if display == "graphics":
        sfonts = ["light", "standard", "full"]
        return {"activeFont": (sfonts, 1), "idleFont": (sfonts, 1)}
    return {}


def _jive_brightness_slider(pref: str, key: str, lo: int, hi: int,
                            initial: int) -> dict:
    """Perl ``minAutoBrightness``/``sensAutoBrightness`` (Jive.pm:1706-1772)."""
    return {
        "text": _jive_string(key),
        "count": 1,
        "offset": 0,
        "item_loop": [{
            "slider": 1,
            "min": lo,
            "max": hi,
            "initial": initial,
            "actions": {"do": {
                "player": 0,
                "cmd": ["playerpref", pref],
                "params": {"valtag": "value"},
            }},
        }],
    }


# ===========================================================================
# Jive: Alarme, Sync/Sleep und Listen — Perl Slim/Control/Jive.pm
# ===========================================================================
#
# Dieselben Menü-Regeln wie oben (sliceAndShip/normalize, Jive.pm:1338-1357),
# aber die Handler lesen zusätzlich Alarme (``Slim::Utils::Alarm``), die
# Sync-Gruppen (``Slim::Player::Client``/``Sync``) und Client-Prefs.
# Live-Proben gegen den Perl-LMS 9.1.1 (192.168.1.90:9000, read-only) sind
# pro Handler im Docstring vermerkt; wo der Live-Server nicht befragt werden
# konnte, steht die Perl-Quelle als Beleg.

#: Menü-Queries ohne Nebenwirkung (Jive.pm:64-133, 157-162)
_JIVE_MENU_COMMANDS = frozenset({
    "alarmsettings", "jiveupdatealarm", "jiveupdatealarmdays",
    "jivealarmvolume", "syncsettings", "sleepsettings", "jivepresets",
    "jivefavorites", "jiveplaylists", "jiverecentsearches",
    "firmwareupgrade", "jiveapplets", "jivewallpapers", "jivesounds",
    "jivepatches",
})

#: Jive-Kommandos mit Wirkung (Jive.pm:95-100, 102-104, 124-125) — Antwort {}
_JIVE_ACTION_COMMANDS = frozenset({"jivealarm", "jiveendoftracksleep", "jivesync"})

#: Modul-Array ``@recentSearches`` (Jive.pm:39); gefüllt wird es von
#: ``cacheSearch`` (Jive.pm:2711-2719) aus den Suchmenüs heraus
#: (``XMLBrowser.pm:1491``), gelesen von ``jiveRecentSearchQuery``
#: (Jive.pm:2768-2797) und ``recentSearchMenu`` (Jive.pm:2721-2766).
#: In-Memory wie in Perl (kein Persistieren).
_JIVE_RECENT_SEARCHES: list = []


def _jive_cache_search(search: Any) -> None:
    """``cacheSearch`` (Jive.pm:2711-2719).

    Perl hängt einen Such-Eintrag nur an, wenn er ``text`` UND
    ``actions.go.cmd`` trägt; ``unshift`` ⇒ der jüngste Eintrag steht vorne.
    Die Einträge baut der Such-/Feed-Code (``XMLBrowser.pm:1475-1489``).
    """
    if not isinstance(search, dict):
        return
    go = (search.get("actions") or {}).get("go") or {}
    if search.get("text") and go.get("cmd"):
        _JIVE_RECENT_SEARCHES.insert(0, search)


def _jive_recent_search_menu() -> list[dict]:
    """``recentSearchMenu($client, 1)`` (Jive.pm:2721-2766).

    Zwei Einträge für das Home-Menü (``homeSearchRecent``/``myMusicSearchRecent``
    → ``cmd ['jiverecentsearches']``), aber nur wenn ``@recentSearches`` GENAU
    EINEN Eintrag hat (``scalar(@recentSearches) == 1``, Jive.pm:2728 — Perl
    prüft echt auf ``== 1``, nicht ``>= 1``).
    """
    if len(_JIVE_RECENT_SEARCHES) != 1:
        return []
    text = _jive_string("RECENT_SEARCHES")
    return [
        {
            "text": text,
            "id": "homeSearchRecent",
            "node": "home",
            "weight": 111,
            "actions": {"go": {"cmd": ["jiverecentsearches"]}},
            "window": {"text": text},
        },
        {
            "text": text,
            "id": "myMusicSearchRecent",
            "node": "myMusicSearch",
            "noCustom": 1,
            "weight": 50,
            "actions": {"go": {"cmd": ["jiverecentsearches"]}},
            "window": {"text": text},
        },
    ]


def _jive_perl_day(days: str, day: int) -> bool:
    """Perl ``$alarm->day($day)`` (``Slim/Utils/Alarm.pm:189-203``).

    Perl zählt ``0 = Sonntag .. 6 = Samstag`` (``displayStr`` iteriert
    ``1..6, 0``, Alarm.pm:952); unser ``Alarm.days`` ist Montag-first
    (``alarms.py`` ``day_int``: Bit 0 = Montag).
    """
    if not days:
        return False
    return days[:7].ljust(7, "0")[_jive_day_index(day)] == "1"


def _jive_day_index(day: int) -> int:
    """Perl-Tag (0=So..6=Sa) → Index in unserem ``Alarm.days`` (0=Mo)."""
    return 6 if day == 0 else day - 1


def _jive_alarm_seconds(alarm) -> int:
    """``$alarm->time`` = Sekunden seit Mitternacht (Alarm.pm:285-305).

    Perl alarms ``time`` ist die Sekundenzahl (das Jive-Zeitfeld
    ``_inputStyle => 'time'`` bekommt sie als ``initialText``, Live-Probe:
    25200 = 7:00). Unser Modell speichert 'HH:MM' → hier umgerechnet.
    """
    hh, _, mm = str(getattr(alarm, "time", "") or "0:0").partition(":")
    return _jive_num(hh, 0) * 3600 + _jive_num(mm, 0) * 60


def _jive_alarm_days_string(alarm) -> str:
    """``join(',', @days)`` aus ``getCurrentAlarms`` (Jive.pm:660-663)."""
    return ",".join(str(d) for d in range(7)
                    if _jive_perl_day(getattr(alarm, "days", ""), d))


def _jive_alarm_playlist(alarm) -> Any:
    """``$alarm->playlist || 0`` (Jive.pm:687).

    Perls Alarm-``playlist`` ist eine URL (oder 0 für „aktuelle
    Wiedergabeliste“, Commands.pm:169-176). Unser Modell kennt nur ``wake``;
    ``url:``/``fr:``-Quellen werden auf ihre URL abgebildet, alles andere
    (inkl. 'track:') auf 0.
    """
    wake = str(getattr(alarm, "wake", "") or "")
    if wake.startswith("url:"):
        return wake[4:]
    return 0


def _jive_alarm_display_str(alarm) -> str:
    """``$alarm->displayStr`` (``Slim/Utils/Alarm.pm:946-963``).

    ``timeStr`` (unsere 'HH:MM') + Kurztage in der Reihenfolge ``1..6, 0``,
    wenn nicht jeden Tag; ausgeschaltete Alarme bekommen ``ALARM_OFF`` davor.
    """
    text = str(getattr(alarm, "time", "") or "")
    all_days = [getattr(alarm, "days", "") or ""][0]
    if not (len(all_days) == 7 and all_days == "1" * 7):
        for day in [1, 2, 3, 4, 5, 6, 0]:
            if _jive_perl_day(all_days, day):
                text += " " + _jive_string(f"ALARM_SHORT_DAY_{day}")
    if not getattr(alarm, "enabled", False):
        text = f"{_jive_string('ALARM_OFF')} ({text})"
    return text


def _jive_current_alarms(player) -> list[dict]:
    """``getCurrentAlarms`` (Jive.pm:658-693).

    Eine Zeile je Alarm: ``"<ALARM_ALARM> <n>: <displayStr>"``; die Aktion
    springt mit id/enabled/days/time/playlist nach ``jiveupdatealarm``.
    Perls ``enabled`` kommt aus der Prefs-Datei und ist dort ein String ("1");
    unser Alarm-Modell hält einen Bool, deshalb steht im Menü die ZAHL 1/0
    (Perl-Literal ``$alarm->enabled || 0`` würde als Zahl ebenfalls passen).
    Perl sortiert nach ``_createTime`` (Alarm.pm:1269-1291); wir haben keine
    Anlegezeit und sortieren nach Alarm-Index.
    """
    from lyrion.alarms import AlarmManager

    mgr = AlarmManager()
    mac = getattr(player, "mac", "") or ""
    items: list[dict] = []
    count = 0
    for idx in sorted(mgr.alarms_for(mac)):
        alarm = mgr.get(mac, idx)
        if alarm is None:                                  # Perl: next unless id
            continue
        count += 1
        items.append({
            "text": f"{_jive_string('ALARM_ALARM')} {count}: "
                    f"{_jive_alarm_display_str(alarm)}",
            "actions": {"go": {
                "cmd": ["jiveupdatealarm"],
                "params": {
                    "id": str(idx),
                    "enabled": 1 if getattr(alarm, "enabled", False) else 0,
                    "days": _jive_alarm_days_string(alarm),
                    "time": _jive_alarm_seconds(alarm),
                    "playlist": _jive_alarm_playlist(alarm),
                },
                "player": 0,
            }},
        })
    return items


def _jive_str(key: str, *args: Any) -> str:
    """``$client->string($token, @args)`` — sprintf, wenn Argumente kommen."""
    text = _jive_string(key)
    return text % args if args else text


def _jive_sleep_hash(minutes: int) -> dict:
    """``sleepInXHash`` (Jive.pm:2282-2300).

    ``SLEEP_CANCEL`` bei 0 Minuten, sonst ``X_MINUTES``; die Aktion setzt den
    Sleep-Timer mit Sekunden (``$sleepTime*60``), ``setSelectedIndex`` ist in
    Perl das STRING-Literal ``'1'``.
    """
    return {
        "text": _jive_str("SLEEP_CANCEL") if minutes == 0
                else _jive_str("X_MINUTES", minutes),
        "actions": {"go": {"player": 0, "cmd": ["sleep", minutes * 60]}},
        "nextWindow": "refresh",
        "setSelectedIndex": "1",                           # Jive.pm:2298
    }


def _jive_sync_group_names(pm, player, include_self: bool) -> str:
    """``syncedWithNames`` (``Slim/Player/Client.pm:1398-1410``).

    ``join(' & ', …)`` über alle anderen Player derselben Sync-Gruppe; mit
    ``include_self`` steht der Player selbst am Ende (Live-Probe:
    „Squeezebox Radio & Schlafzimmer“ für den Master).
    """
    group = [p for p in pm.get_sync_group(player.mac) if p.mac != player.mac]
    if include_self:
        group = group + [player]
    return " & ".join(str(getattr(p, "name", "") or p.mac) for p in group)


def _jive_is_synced(player) -> bool:
    """``$client->isSynced()`` (``Slim/Player/Client.pm:1375-1381``)."""
    value = getattr(player, "is_synced", False)
    return bool(value() if callable(value) else value)


def _jive_players_to_sync_with(pm, player) -> list[dict]:
    """``getPlayersToSyncWith`` (Jive.pm:1993-2086).

    Erste Zeile: ``SYNC_X_TO`` mit dem eigenen Namen (``itemNoAction``).
    Danach alle Optionen: die eigene Sync-Gruppe (wenn gesynct, Radio=1),
    fremde Master mit ihrem Gruppennamen (``syncedWithNames(1)``) und freie
    Player — sortiert nach Namen (Perl ``sort { $a->{name} cmp … }``); zuletzt,
    nur wenn gesynct, ``DO_NOT_SYNC`` mit ``syncWith 0``.
    """
    items: list[dict] = [{
        "text": _jive_str("SYNC_X_TO", getattr(player, "name", "") or ""),
        "style": "itemNoAction",
    }]

    own_group = {p.mac for p in pm.get_sync_group(player.mac)}
    currently_synced_with: Any = 0                        # Jive.pm:2010
    options: list[dict] = []

    if _jive_is_synced(player):
        name = _jive_sync_group_names(pm, player, False)
        options.append({"id": player.mac, "name": name, "isSyncedWith": 1})
        currently_synced_with = name

    for other in list(pm.players.values()):
        if not getattr(other, "is_player", True):
            continue
        # Perl: next if $eachclient->isSyncedWith($client) → dieselbe Gruppe
        # (unser eigenes Konto eingeschlossen).
        if other.mac in own_group:
            continue
        other_synced = _jive_is_synced(other)
        other_is_master = bool(getattr(other, "sync_slaves", None)) \
            and not getattr(other, "sync_master", None)
        if other_synced and other_is_master:              # Sync.pm:123-128
            options.append({"id": other.mac,
                            "name": _jive_sync_group_names(pm, other, True),
                            "isSyncedWith": 0})
        elif not other_synced:
            options.append({"id": other.mac,
                            "name": getattr(other, "name", "") or other.mac,
                            "isSyncedWith": 0})

    for opt in sorted(options, key=lambda o: o["name"]):
        items.append({
            "text": opt["name"],
            "radio": 1 if opt["isSyncedWith"] == 1 else 0,
            "actions": {"do": {
                "player": 0,
                "cmd": ["jivesync"],
                "params": {
                    "syncWith": opt["id"],
                    "syncWithString": opt["name"],
                    "unsyncWith": currently_synced_with,
                },
            }},
            "nextWindow": "refresh",
        })

    if _jive_is_synced(player):       # Jive.pm:2066-2082
        items.append({
            "text": _jive_str("DO_NOT_SYNC"),
            "radio": 0,
            "actions": {"do": {
                "player": 0,
                "cmd": ["jivesync"],
                "params": {
                    "syncWith": 0,
                    "syncWithString": 0,
                    "unsyncWith": currently_synced_with,
                },
            }},
            "nextWindow": "refresh",
        })
    return items


def _jive_presets_pref(player) -> list | None:
    """Client-Pref ``presets`` (``$prefs->client($client)->get('presets')``).

    Perl hält dort eine ARRAY-Ref mit 10 Slots (Jive.pm:2532); unser
    per-Player-Store kann zusätzlich einen JSON-String enthalten.
    """
    raw = _jive_client_pref(player, "presets", None)
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw if isinstance(raw, list) else None


def _is_remote_url(url: str) -> bool:
    """``Slim::Music::Info::isRemoteURL`` (``Slim/Music/Info.pm:1115-1124``).

    Perl asks the protocol-handler registry whether the URL scheme is a
    registered remote handler. Our port speaks remote protocols over http/
    https (``networking/protocol.py:2417`` accepts exactly those two).
    """
    m = re.match(r"^([a-zA-Z0-9\-]+):", url or "")
    return bool(m) and m.group(1).lower() in ("http", "https")


def _jive_preset_actions(player, jive_preset: int, title, url, ptype,
                         parser) -> dict:
    """``key => $jive_preset; favorites_{url,title,type}; parser`` (Jive.pm:2546-2570).

    Live-Probe: ``key`` ist ein STRING (Perl stringifiziert ``$preset + 1``
    vorher für ``JIVE_SET_PRESET_X``), ``parser`` ist ``null`` ohne Angabe.
    """
    return {
        "go": {
            "player": 0,
            "cmd": ["jivefavorites", "set_preset"],
            "params": {
                "key": str(jive_preset),                   # Live: "1".."10"
                "favorites_url": url,
                "favorites_title": title,
                "favorites_type": ptype,
                "parser": parser,
            },
        },
    }


class JSONRPCAPI:
    """JSON-RPC 2.0 API handler.

    Handles both single requests and batch requests. Methods are registered
    via register() and are called with positional parameters from the params
    array.
    """

    # Standard JSON-RPC error codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    def __init__(self) -> None:
        self._methods: dict[str, Callable] = {}
        # Short-TTL cache for status/serverstatus/players polls
        # (misbehaving clients can flood the server otherwise).
        # key -> (timestamp, state fingerprint, answer); the fingerprint
        # (see _status_state_fingerprint) keeps a state change from being
        # masked by the 1 s window.
        self._status_cache: dict[tuple, tuple[float, tuple | None, Any]] = {}
        # P4-4: active display popup (showBriefly) + expiry timestamp
        self._popup: Optional[dict] = None
        self._popup_expires: float = 0.0
        self._register_default_methods()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register_default_methods(self) -> None:
        """Register the built-in method set."""
        self.register("server.version", self._server_version)
        self.register("server.status", self._server_status)
        self.register("player.list", self._player_list)
        self.register("player.count", self._player_count)
        self.register("player.name", self._player_name)
        self.register("player.power", self._player_power)
        self.register("player.volume", self._player_volume)
        self.register("player.mode", self._player_mode)
        self.register("player.status", self._player_status)
        self.register("playlist.tracks", self._playlist_tracks)
        self.register("playlist.play", self._playlist_play)
        self.register("playlist.stop", self._playlist_stop)
        self.register("playlist.pause", self._playlist_pause)
        self.register("playlist.next", self._playlist_next)
        self.register("playlist.prev", self._playlist_prev)
        self.register("slim.request", self._slim_request)
        self.register("rescan", self._rescan)
        self.register("abortscan", self._abortscan)

    def register(self, name: str, method: Callable) -> None:
        """Register a method.

        Args:
            name: Fully-qualified method name (e.g. "player.power").
            method: Async callable accepting *params.
        """
        self._methods[name] = method

    def unregister(self, name: str) -> None:
        """Remove a registered method."""
        self._methods.pop(name, None)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def handle_request(self, request_data: bytes | str) -> bytes:
        """Parse and handle a JSON-RPC request.

        Args:
            request_data: Raw request body.

        Returns:
            JSON-RPC response as bytes.
        """
        try:
            request = _json_loads(request_data)

            # Batch request
            if isinstance(request, list):
                responses = [await self._handle_single(r) for r in request]
                # Filter out null results that some transports prefer
                responses = [r for r in responses if r is not None]
                return _json_dumps(responses) if responses else b"[]"

            response = await self._handle_single(request)
            return _json_dumps(response)

        except (ValueError, Exception) as e:
            return _json_dumps({
                "jsonrpc": "2.0",
                "error": {
                    "code": self.PARSE_ERROR,
                    "message": f"Parse error: {e}",
                },
                "id": None,
            })

    async def _handle_single(self, request: dict) -> Optional[dict]:
        """Handle one JSON-RPC request/notification.

        Compatible with real LMS: accepts requests with or without the
        "jsonrpc" version field ("2.0" or "1.0" both accepted) — SqueezeCtrl,
        Squeezer and similar apps omit it. Responses always include "2.0".
        """
        jsonrpc = request.get("jsonrpc")
        if jsonrpc not in (None, "2.0", "1.0"):
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INVALID_REQUEST,
                    "message": "Invalid JSON-RPC version",
                },
                "id": request.get("id"),
            }

        method_name = request.get("method", "")
        params = request.get("params", [])
        id = request.get("id")

        # Notification (no id) — don't send response
        if id is None and method_name in self._methods:
            try:
                await self._methods[method_name](*params)
            except Exception:
                pass
            return None

        if method_name not in self._methods:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.METHOD_NOT_FOUND,
                    "message": f"Method not found: {method_name}",
                },
                "id": id,
            }

        try:
            result = await self._methods[method_name](*params)
            return {"jsonrpc": "2.0", "result": result, "id": id}
        except TypeError as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INVALID_PARAMS,
                    "message": str(e),
                },
                "id": id,
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INTERNAL_ERROR,
                    "message": str(e),
                },
                "id": id,
            }

    # ------------------------------------------------------------------
    # Built-in methods
    # ------------------------------------------------------------------

    async def _server_version(self) -> str:
        from lyrion.version import __version__
        return __version__

    async def _server_status(self) -> dict:
        from lyrion import __version__
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            players = pm.get_connected_count()
        except Exception:
            players = 0
        return {"version": __version__, "players": players}

    async def _player_list(self) -> list[dict]:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            return [p.to_dict() for p in pm.get_all_players()]
        except Exception:
            return []

    async def _player_count(self) -> int:
        try:
            from lyrion.player import PlayerManager
            return PlayerManager().get_connected_count()
        except Exception:
            return 0

    async def _player_name(self, mac: str) -> str:
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            return player.name if player else ""
        except Exception:
            return ""

    async def _player_power(self, mac: str, power: Optional[bool] = None) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if power is None:
                player = pm.get_player(mac)
                return player.power if player else False
            pm.set_power(mac, power)
            return power
        except Exception:
            return False

    async def _player_volume(
        self, mac: str, level: Optional[int] = None
    ) -> int:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if level is None:
                player = pm.get_player(mac)
                return player.volume if player else 0
            await pm.set_volume(mac, level)
            return level
        except Exception:
            return 0

    async def _player_mode(self, mac: str) -> str:
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            return player.mode if player else "stop"
        except Exception:
            return "stop"

    async def _player_status(self, mac: str) -> dict:
        """Full playback status for a player (used by the web UI)."""
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is None:
                return {"mode": "stop", "playlist_cur_index": -1}
            mode_map = {"play": "playing", "pause": "paused",
                        "stop": "stopped", "loading": "loading"}
            return {
                "mode": mode_map.get(player.mode, player.mode),
                "player_connected": bool(player.connected),
                "power": bool(player.power),
                "volume": player.volume,
                "playlist_tracks": player.playlist_total,
                "playlist_cur_index": player.playlist_position,
                "current_track_id": player.current_track_id,
                "current_title": getattr(player, "current_title", None),
                "current_url": getattr(player, "current_url", None),
                # Stream metadata (StreamTitle) → now playing song + artist.
                "current_artist": getattr(player, "remote_meta", {}).get("artist", ""),
                "remote_meta": getattr(player, "remote_meta", {}),
            }
        except Exception:
            return {"mode": "stop", "playlist_cur_index": -1}

    async def _playlist_tracks(self, mac: str) -> list[dict]:
        """Return the player's playlist as JSON objects (used by the web UI)."""
        try:
            from lyrion.player import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is None:
                return []
            tracks = player.playlist
            out: list[dict] = []
            # Track titles from the DB (one query for all local tracks)
            track_ids = [e for e in tracks if not isinstance(e, str)]
            titles: dict[int, str] = {}
            art_by_track: dict[int, str] = {}
            if track_ids:
                try:
                    from sqlalchemy import select
                    from lyrion.database.schema import Track
                    from lyrion.database.sqlite_helper import db_session
                    async with db_session() as session:
                        result = await session.execute(
                            select(Track.id, Track.title).where(Track.id.in_(track_ids))
                        )
                        titles = {tid: t for tid, t in result.all()}
                except Exception:
                    titles = {}
                # Album artwork per track (one bulk query) — the player
                # shows the cover in Now Playing and the playlist.
                try:
                    from sqlalchemy import select as _sel
                    from lyrion.database.schema import Album, tracks_albums
                    from lyrion.database.sqlite_helper import db_session
                    async with db_session() as session:
                        rows = (await session.execute(
                            _sel(tracks_albums.c.track,
                                 tracks_albums.c.album,
                                 Album.artwork)
                            .join(Album, Album.id == tracks_albums.c.album)
                            .where(tracks_albums.c.track.in_(track_ids))
                        )).all()
                        for tid, album_id, art in rows:
                            if art:
                                art_by_track[tid] = f"/music/{album_id}/cover.jpg"
                except Exception:
                    art_by_track = {}
            for i, entry in enumerate(tracks):
                if isinstance(entry, str):
                    stream_name = getattr(player, "stream_titles", {}).get(entry, "")
                    art = getattr(player, "stream_images", {}).get(entry, "")
                    out.append({
                        "index": i,
                        "url": entry,
                        "title": stream_name
                        or (player.current_title
                            if i == player.playlist_position and player.current_title
                            else "Radio Stream"),
                        # Senderlogo: explizit hinterlegtes Bild oder Radio-Icon.
                        # static_dir enthält bereits "html/", also ohne /html/-
                        # Präfix im URL-Pfad (sonst doppelt -> 404).
                        "artwork_url": art or "/images/radio.svg",
                    })
                else:
                    out.append({
                        "index": i,
                        "track_id": entry,
                        "title": titles.get(entry, f"Track {entry}"),
                        "artwork_url": art_by_track.get(entry, ""),
                    })
            return out
        except Exception:
            return []

    async def _playlist_play(self, mac: str, index: int = 0) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            # If the index points into a populated playlist, send a strm frame
            player = pm.get_player(mac)
            if player is not None and getattr(player, "playlist", []):
                await self._play_playlist_item(pm, player, index)
                return True
            # Fallback: direct track play
            return await pm.play_track(mac, index)
        except Exception:
            return False

    async def _playlist_stop(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            # Real SlimProto frame (strm 'q') — send_command with CLI text
            # does nothing on Squeezelite. stop_player also sets mode=stop.
            return await PlayerManager().stop_player(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_stop failed: %s", e)
            return False

    async def _playlist_pause(self, mac: str, state: int = -1) -> bool:
        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            if state == 1:
                # pause on -> real strm 'p' frame
                return await pm.pause_player(mac, True)
            if state == 0:
                # resume -> strm 'p' 0 (Squeezelite resumes in place)
                return await pm.pause_player(mac, False)
            # toggle
            player = pm.get_player(mac)
            currently_paused = player is not None and player.mode == "pause"
            return await pm.pause_player(mac, not currently_paused)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_pause failed: %s", e)
            return False

    async def _playlist_next(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            return await PlayerManager().playlist_next(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_next failed: %s", e)
            return False

    async def _playlist_prev(self, mac: str) -> bool:
        try:
            from lyrion.player import PlayerManager
            return await PlayerManager().playlist_prev(mac)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lyrion.web.api").warning("_playlist_prev failed: %s", e)
            return False

    async def _fav_position_path(self, fm: Any, db_id: int) -> str:
        """Session-prefixed position path of a root favourite (Perl's id).

        A caller that addresses the parent by its DB id (Web UI:
        ``['favorites','items','<parent_id>']``) gets an id of the same shape
        as every other one: ``<sid>.<index>`` where ``index`` is the item's
        position in the sorted root list — the path ``resolve_path`` walks.
        """
        sid = _new_fav_sid()
        try:
            items = await fm.list_items(None)
        except Exception:  # noqa: BLE001
            return sid
        for i, it in enumerate(items):
            try:
                if int(it.get("id")) == int(db_id):
                    return f"{sid}.{i}"
            except (TypeError, ValueError):
                continue
        return sid

    @staticmethod
    def _fav_item_id(loop: list[dict], index: int) -> Optional[str]:
        """The browse-session id of the favourites row at ``index``.

        Menu-shape rows carry it as ``params.item_id`` (stations,
        ``XMLBrowser.pm:1142``) or ``actions.go.params.item_id`` (folders,
        ``:1243``); ``xmlbrowserPlayControl`` addresses the row by its
        position (``:808``), so the context-menu answer needs the id of that
        row back.
        """
        if index < 0 or index >= len(loop):
            return None
        item = loop[index]
        params = item.get("params")
        if isinstance(params, dict) and params.get("item_id") is not None:
            return str(params["item_id"])
        go = (item.get("actions") or {}).get("go") or {}
        go_params = go.get("params")
        if isinstance(go_params, dict) and go_params.get("item_id") is not None:
            return str(go_params["item_id"])
        return None

    async def _fav_items_loop(self, fm: Any, parent: Optional[int],
                              parent_path: str, feed_mode: bool,
                              menu_mode: bool = False,
                              use_play_control: bool = False,
                              menu: str = "favorites") -> list[dict]:
        """Build the favorites loop with Perl's session item ids.

        Two shapes, mirroring Perl's ``XMLBrowser::_cliQuery_done``:

        * ``menu_mode=True`` (the request carried a ``menu:`` token, i.e.
          SqueezePlay/Android browse menus): the jive menu item shape —
          ``text``/``addAction``/``icon-id``/``actions`` for folders and
          ``text``/``type``/``style``/``goAction``/``icon-id``/``params``/
          ``presetParams`` for stations (``XMLBrowser.pm:1051-1267``).
          See :mod:`lyrion.web.favorites_menu`.
        * ``menu_mode=False``: the classic flat shape (``id``/``name``/
          ``image``/``isaudio``/``hasitems``, ``XMLBrowser.pm:1378-1410``)
          other controllers (SqueezeTray, Squeezer, SPA) read.

        id = Perl's ``<8-hex browse-session id>.<position>[.<position>…]``
        (``XMLBrowser.pm:353/1022/1142``, root handle ``getSID`` :1739-1741);
        dbid carries the internal DB id; feed_mode embeds children in
        'items' arrays.
        """
        try:
            items = await fm.list_items(parent)
        except Exception:
            return []
        loop: list[dict] = []
        path = parent_path  # hierarchical prefix for the item ids
        for i, it in enumerate(items):
            is_folder = it["type"] == "folder"
            hier = path + f".{i}"
            if menu_mode:
                from lyrion.web import favorites_menu
                if is_folder:
                    item = favorites_menu.folder_item(it["title"], hier, menu=menu)
                else:
                    item = favorites_menu.audio_item(
                        it["title"], hier, url=it["url"] or "",
                        use_play_control=use_play_control, index=i)
            else:
                # LMS reference format (lyrion.org): hierarchical id
                # '<root>.<position>' (the apps re-send it as item_id:),
                # name/image/isaudio/hasitems, type 'audio' for streams.
                item = {
                    "id": hier,
                    "name": it["title"],
                    "image": "html/images/favorites.png",
                    "isaudio": 0 if is_folder else 1,
                    "hasitems": 1 if is_folder else 0,
                    "position": i,
                }
                if not is_folder:
                    item["type"] = "audio"
                    item["url"] = it["url"] or ""
                    item["id_hierarchical"] = hier
                    item["dbid"] = str(it["id"])
                if is_folder:
                    # Folder: go opens the folder's items (session id).
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["favorites", "items"],
                               "params": {"item_id": hier}},
                    }
                else:
                    # Stream: play/do plays the favorite.  Perl's only
                    # favorites play route is ['favorites','playlist',
                    # '_method'] (Favorites/Plugin.pm:81) — a plain
                    # ['playlist','play'] cannot resolve a favourites id
                    # (the playlist command knows track_id/album_id filters,
                    # Commands.pm:1313-1323), i.e. the tap stayed ineffective.
                    item["actions"] = {
                        "play": {"player": 0,
                                 "cmd": ["favorites", "playlist", "play"],
                                 "params": {"item_id": hier}},
                        "do": {"player": 0,
                               "cmd": ["favorites", "playlist", "play"],
                               "params": {"item_id": hier}},
                    }
            if feed_mode and is_folder:
                item["items"] = await self._fav_items_loop(
                    fm, int(it["id"]), hier, feed_mode, menu_mode,
                    use_play_control)
            loop.append(item)
        return loop


    async def _json_favorites_items(self, pid: str | None,
                                    rest: list) -> dict:
        """``['favorites','items',<start>,<qty>,…]`` — see favorites_menu.

        Two answer shapes, exactly like Perl's ``XMLBrowser::_cliQuery_done``:

        * a ``menu:`` token (SqueezePlay/Android browse menus) ⇒ the jive
          menu shape with ``base``/``window`` — including
          ``base.actions.play.nextWindow == "nowPlaying"``, which is what
          makes SqueezePlay switch to "Aktueller Titel" after a tap
          (SlimBrowserApplet.lua:1924-1928, 2044-2048).
        * no ``menu:`` token ⇒ the classic ``loop_loop`` shape other
          controllers read (unchanged).

        ``item_id:`` accepts Perl's session form ('<8-hex-sid>.3.1'), our
        legacy virtual root ('0.3.1') and a bare DB id; a bare numeric first
        argument (Web UI) is a folder id.
        """
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            # XMLBrowser.pm:802 — the destructive-tap defeat decides whether a
            # station row is a touch-to-play row (goAction "play") or the
            # play-control row (goAction "playControl", the shape whose tap
            # opens Play/Add/Play-next).  Perl resolves it per request
            # (:1951-1983); our player object carries the playing state it
            # looks at.  ``client_named`` is Perl's ``$request->client``: a
            # request without a player token at all always lands on the
            # defeated branch (:1976 `|| !$client`).
            player = None
            if pid:
                try:
                    from lyrion.player.manager import PlayerManager
                    player = PlayerManager().get_player(pid)
                except Exception:  # noqa: BLE001
                    player = None
            use_play_control = _defeat_destructive_touch_to_play(
                rest, player, client_named=player is not None)
            parent = None
            # Perl's XMLBrowser roots every item id in a fresh browse-session
            # handle: `my @crumbIndex = $sid ? ($sid) : ()` + `push
            # @crumbIndex, $i` → `<sid>.<index>[.<index>…]`
            # (XMLBrowser.pm:341-353/387-394, id at :1022/:1142).  The client
            # echoes it back as item_id: and the server strips the handle
            # before walking the index path (:334-336).  Our former root was
            # the synthetic "0" — no Perl client ever sees that form.
            parent_path = _new_fav_sid()
            menu_mode = any(str(a) == "menu" or str(a).startswith("menu:")
                            for a in rest)
            feed_mode = any(str(a).startswith("feedMode:")
                            and str(a)[9:] == "1" for a in rest)
            # Perl answers feedMode with a nested outline feed
            # ({"favorites":…,"items":…,"type":…}) — neither shape is parity,
            # so leave that path exactly as it was before.
            if feed_mode:
                menu_mode = False
            # item_id:<n> (SqueezeTray folder children) — highest priority;
            # accepts Perl's '<sid>.<path>', the legacy '0.<path>' and the
            # bare DB id (Perl finds the feed through the sid, XMLBrowser.pm:225).
            for a in rest:
                if str(a).startswith("item_id:"):
                    val = str(a)[8:]
                    if "." in val:
                        parent = await fm.resolve_path(val)
                        if parent is not None:
                            # keep the client's own handle → stable child ids
                            parent_path = val
                        else:
                            parent_path = _new_fav_sid()
                    elif val.isdigit():
                        parent = int(val)
                        parent_path = await self._fav_position_path(fm, parent)
                    break
            if parent is None and len(rest) == 1 and str(rest[0]).isdigit():
                # Web UI: ['favorites','items','<parent_id>'] — a bare
                # number is the folder id (SqueezeTray sends multiple
                # args: start/count/want_url — never a bare parent).
                parent = int(str(rest[0]))
                parent_path = await self._fav_position_path(fm, parent)
            # ── A tap on a *leaf* (a station row) never descends ──────────
            # Perl's Favorites feed is an OPML feed: the tapped item has no
            # children, so ``XMLBrowser`` answers the item itself —
            #   * ``xmlBrowseInterimCM:1`` (the tap on a touch-to-play row,
            #     ``XMLBrowser.pm:854-859``) → the play-control menu plus the
            #     feed's own rows (add/insert/play, delete, Titel/URL), and
            #   * a plain ``useContextMenu:1`` drill → the two info rows of
            #     ``@mapAttributes`` (:245-252).
            # This port answered the (empty) child list of the leaf instead:
            # the on-screen menu stayed empty and the controller never got a
            # playable row ("Favoriten starten geht nicht", live client log
            # 2026-09-14 16:48 with ``item_id:<sid>.0.0 isContextMenu:1
            # touchToPlay:… xmlBrowseInterimCM:1``).
            if menu_mode and parent is not None:
                from lyrion.web import favorites_menu as _fav_menu
                from lyrion.web import menus as _menus
                try:
                    leaf = await fm.get(parent)
                except Exception:  # noqa: BLE001 — no get()/row vanished
                    leaf = None
                if leaf is not None and leaf.get("type") == "stream":
                    name = str(leaf.get("title") or "")
                    url = str(leaf.get("url") or "")
                    path = parent_path or str(parent)
                    index = path.rsplit(".", 1)[-1]
                    if any(str(a) == "xmlBrowseInterimCM:1" for a in rest):
                        return _menus.interim_context_menu(
                            path, name=name, url=url,
                            icon=_fav_menu.FAVORITES_ICON,
                            favorite_type="audio",
                            item_index=index if index.isdigit() else "")
                    return _menus.leaf_info_menu(name, url)
            loop = await self._fav_items_loop(fm, parent, parent_path,
                                              feed_mode, menu_mode,
                                              use_play_control)
            if menu_mode:
                from lyrion.web import favorites_menu
                # XMLBrowser.pm:805-830 — the tap on a touch-to-play row
                # arrives as `xmlbrowserPlayControl:<itemIndex>` (the token is
                # numified at :808); Perl answers with the play-control menu
                # for exactly that row (`_playlistControlContextMenu`,
                # :1811-1844), not with the list again.
                play_ctl = next((str(a) for a in rest if str(a).startswith(
                    "xmlbrowserPlayControl:")), None)
                if play_ctl is not None:
                    idx = _playctl_index(play_ctl[len("xmlbrowserPlayControl:"):])
                    fav_id = self._fav_item_id(loop, idx)
                    if fav_id is None:
                        # Out of range: Perl emits only window/offset/count
                        # (:813-830) — count 0, no item_loop.
                        return {"window": dict(favorites_menu.WINDOW_TEXT_LIST),
                                "offset": 0, "count": 0}
                    return favorites_menu.play_control_context_menu(fav_id)
                # Perl echoes the request's tagged params into the
                # playControl base action (XMLBrowser.pm:978).  The two
                # leading positionals are the named slots of
                # ``['favorites','items','_index','_quantity']``
                # (Request.pm:75 / Favorites/Plugin.pm:75).
                tagged: dict[str, Any] = {}
                positional = [str(a) for a in rest[:2]]
                if len(positional) == 2 and all(p.isdigit()
                                                for p in positional):
                    tagged["_index"], tagged["_quantity"] = positional
                for a in rest:
                    s = str(a)
                    if ":" in s:
                        k, _, v = s.partition(":")
                        tagged[k] = v
                return favorites_menu.render_favorites_menu(
                    loop, playcontrol_params=tagged,
                    use_play_control=use_play_control)
            resp = self._browse_response(loop)
            # LMS reference: 'title' on the response level.
            resp["title"] = _jive_string("FAVORITES")
            return resp
        except Exception:
            from lyrion.web import favorites_menu
            if any(str(a) == "menu" or str(a).startswith("menu:")
                   for a in (rest or [])):
                return favorites_menu.render_favorites_menu([])
            resp = self._browse_response([])
            resp["title"] = _jive_string("FAVORITES")
            return resp

    async def _json_favorites_add(self, pid: str | None, args: list) -> dict:
        """``favorites add url:<url> title:<title> [type:<t> icon:<i>]``.

        Perl ``cliAdd`` (``Slim/Plugin/Favorites/Plugin.pm:821-922``) reads
        only the *tagged* params ``url``/``title``/``icon``/``item_id``/
        ``type``/``hotkey`` (:831-837) and:

        * rejects the request without ``title`` **and** ``url``
          (``setStatusBadParams``, :872-877 → no result on the wire);
        * stores ``type || 'audio'`` (:854) and ``icon || $favs->icon($url)``
          (:855);
        * inserts at ``item_id`` when given, otherwise appends at the end
          (:880-893);
        * answers ``count`` 1 (:859) after ``$favs->save`` (:895);
        * fires the jive feedback popup ("show feedback to jive", :902-912)
          — ``showBriefly`` with the ``favorite`` style hash below.

        ``addlevel`` creates a *folder* (:861-870, title only).  Positional
        fallbacks (``favorites add <url> <title> [<parent_id>]``) are additive —
        Perl ignores them, our CLI documented them.
        """
        command = str(args[0])
        tokens = [str(a) for a in args[1:]]
        tagged: dict = {}
        positional: list = []
        for token in tokens:
            if ":" in token and token.split(":", 1)[0] in (
                    "url", "title", "type", "icon", "parser", "item_id",
                    "parent", "hotkey"):
                key, _, value = token.partition(":")
                tagged[key] = value
            else:
                positional.append(token)
        title = tagged.get("title") or (positional[0] if positional else "")
        url = tagged.get("url") or (positional[1] if len(positional) > 1 else "")
        if command == "addlevel":
            if not title:
                return {}                       # Perl: bad params (:872-877)
            url = None
        elif not (title and url):
            return {}
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            parent = None
            target = tagged.get("item_id") or tagged.get("parent")
            if target:
                if str(target).isdigit():
                    parent = int(str(target))
                else:
                    parent = await fm.resolve_path(str(target))
            new_id = await fm.add(str(title), url, parent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("favorites add failed: %s", exc)
            return {}
        if new_id is None:
            return {}
        logger.info("favorites add: %s (%s) -> %s", title, url, new_id)
        # Perl fires the popup for BOTH commands — it sits after ``save`` and
        # outside the add/addlevel branch (Plugin.pm:895-912); an ``addlevel``
        # has no url, so ``icon($url)`` falls back to favorites.png
        # (OpmlFavorites.pm:87-88).
        await self._favorites_add_feedback(
            pid, str(title), str(url or ""), tagged.get("icon"))
        return {"count": 1}                     # Favorites/Plugin.pm:859

    async def _favorites_add_feedback(self, pid: str | None, title: str,
                                      url: str, icon: str | None) -> None:
        """The jive popup Perl shows after ``favorites add``.

        ``Slim/Plugin/Favorites/Plugin.pm:902-912``::

            # show feedback to jive
            if ($request->source && $request->source =~ /\\/slim\\/request/) {
                $client->showBriefly({
                    jive => {
                        type => 'mixed',
                        style => 'favorite',
                        'icon' => $icon || $favs->icon($url),
                        text => [ $client->string('FAVORITES_ADDING'), $title ],
                    },
                });
            }

        ``showBriefly`` then (``Slim/Display/Display.pm``):

        * caches the popup for the ``showBriefly`` field of ``status``
          (``renderCache->{showBriefly}``, :280-283, 15 s ttl — the port's
          ``set_pending_display``);
        * fires ``notify('showbriefly', $parts, $duration)`` (:285-288) →
          ``['displaynotify', 'showbriefly', $parts, $duration]`` →
          ``displaystatus`` subscribers (:905-913), which is exactly the event
          the controller renders as the confirmation popup.

        The recipients are the controllers subscribed to ``displaystatus`` for
        this player (Perl needs ``notifyLevel >= 1``, i.e. a
        ``subscribe:showbriefly`` registration, ``Queries.pm:1758-1759``);
        ``CometdManager.notify_display`` applies that same filter, so a caller
        that is not a jive controller (plain ``/jsonrpc.js``, CLI) — which Perl
        excludes via the source check above — receives nothing either.
        """
        if not pid:
            return
        jive: dict = {
            "type": "mixed",                                # Plugin.pm:906
            "style": "favorite",                            # Plugin.pm:907
            "icon": icon or _favorites_icon(url),           # Plugin.pm:908
            "text": [_jive_string("FAVORITES_ADDING"),      # Plugin.pm:909
                     str(title)],
        }
        self.set_pending_display(jive, "showbriefly")       # Display.pm:280-283
        try:
            from lyrion.web.cometd import get_manager
            mgr = get_manager()
            if mgr is not None:
                # Display.pm:285-288 → notify_display → 'displaynotify' on the
                # displaystatus channel (Display.pm:905-913).
                await mgr.notify_display(pid, "showbriefly", jive)
        except Exception as exc:  # noqa: BLE001 — a popup must never break the add
            logger.debug("favorites add popup for %s failed: %s", pid, exc)

    async def _json_favorites_delete(self, args: list) -> dict:
        """``favorites delete [url:<url>] [title:<t>] [item_id:<id>]``.

        Perl ``cliDelete`` (``Slim/Plugin/Favorites/Plugin.pm:925-970``)::

            my $index  = $request->getParam('item_id');
            my $url    = $request->getParam('url');
            ...
            if (!defined $index || !defined $favs->entry($index)) {
                if ($url) {
                    $favs->deleteUrl($url);
                } else {
                    $request->setStatusBadParams();
                    return;
                }
            }
            else {
                $favs->deleteIndex($index);
            }

        The *index* form addresses Perl's OPML crumb (an ``item_id`` the client
        echoed from the favourites menu); the *URL* form is what the radio
        feeds' play-control row ("Favorit löschen", ``XMLBrowser.pm:1871-1900``)
        and ``jiveFavoritesCommand``'s confirmation item
        (``Slim/Control/Jive.pm:2670-2690``) send.  Both are honoured; a request
        that names neither an existing entry nor a URL is bad params and stays
        resultless (``setStatusBadParams`` → nothing on the wire).  On success
        Perl only calls ``setStatusDone`` (no result keys) plus the jive popup
        for a ``/slim/request`` source (:957-967) — the same empty answer we
        give; the popup is delivered by the displaystatus path.
        """
        tokens = [str(a) for a in args[1:]]
        tagged: dict = {}
        for token in tokens:
            if ":" in token and token.split(":", 1)[0] in (
                    "url", "title", "item_id", "icon"):
                key, _, value = token.partition(":")
                tagged[key] = value
        url = str(tagged.get("url") or "")
        target = tagged.get("item_id")
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            fav_id: Optional[int] = None
            if target:
                if str(target).isdigit():
                    fav_id = int(str(target))
                elif "." in str(target):
                    # Perl's session crumb '<sid>.<index>…' (XMLBrowser.pm:225).
                    fav_id = await fm.resolve_path(str(target))
                if fav_id is not None and await fm.get(fav_id) is None:
                    fav_id = None                # ``!$favs->entry($index)``
            if fav_id is None:
                if not url:
                    return {}                    # bad params (Plugin.pm:950-952)
                fav_id = await fm.find_url(url)  # ``$favs->deleteUrl($url)``
            if fav_id is None:
                return {}
            deleted = await fm.delete(fav_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("favorites delete failed: %s", exc)
            return {}
        if not deleted:
            return {}
        logger.info("favorites delete: id=%s url=%s", fav_id, url)
        return {}                                # ``setStatusDone`` — no result

    async def _displaystatus(self, pid: str | None, args: list[str]) -> dict:
        """displaystatus — now-playing popup / display status.

        Perl ``displaystatusQuery`` (Queries.pm:1630-1757, Dispatch
        ``[1,1,1]`` Request.pm:495). Ohne gespeicherte Anzeige beendet Perl
        den Request ohne Result → ``{}``; live 2026-09-13
        ``displaystatus 0 2`` (mit Client) → ``{"result":{}}`` und ohne
        Client → Socket zu. Liegt eine Anzeige vor, antwortet Perl mit dem
        ``type`` der notification (``$request->addResult('type', $type)``,
        :1666) und einem ``display``-Record mit ``text``/``duration`` im
        jive-Format (:1673-1687).

        Squeezer liest genau das: ``Util.getRecord(data, "display")``
        (``CometClient.java:475-487``) — der frühere Schlüssel ``jive`` wurde
        deshalb nie gefunden. ``showBriefly:<text> [<dauer>]`` setzt das
        Popup (Standarddauer 5 s).

        Die gespeicherte notification läuft NICHT ab: Perl hält sie in
        ``privateData`` der Subskription und ersetzt sie erst durch die
        nächste ``displaynotify`` (``displaystatusQuery_filter`` :1616-1620).
        Nur das ``showBriefly``-FELD von ``status`` hat die 15-s-Grenze
        (``renderCache->{showBriefly}{ttl}``, Display.pm:280-283 +
        Queries.pm:4062-4066).
        """
        now = time.time()
        for i, a in enumerate(args or []):
            s = str(a)
            if s.startswith("showBriefly:"):
                text = s[12:]
                duration = 5
                nxt = str(args[i + 1]) if i + 1 < len(args) else ""
                if nxt.isdigit():
                    duration = int(nxt)
                self._popup = {
                    "jive": None, "kind": "showbriefly",
                    "line": [text], "duration": duration,
                }
                self._popup_expires = now + duration
        if not self._popup:
            return {}
        # Perl ``displaystatusQuery`` (:1653-1697) gibt den jive-Teil UNVERÄNDERT
        # als ``display`` aus: bei einer ``displaynotify`` vom Typ
        # 'showbriefly' ist ``$parts`` der ``$parts``-Hash des
        # ``showBriefly``-Aufrufs, und ``$parts->{'jive'}`` gilt, sobald er ein
        # HASH/ARRAY/CODE ist (:1683-1689 ``addResult('display', …)``) — der
        # Text bleibt dort eine LISTE (die Popup-Aufrufer setzen
        # ``text => [ $string ]``, z. B. Commands.pm:1567/:1923/:2357).
        # Fehlt der jive-Teil (``display <line1> <line2>`` → Commands.pm:444-472
        # ``showBriefly({line => [$line1, $line2]})``, kein ``jive``), nimmt
        # Perl ``$screen1->{'line'}`` als Text-LISTE und ergänzt ``duration``
        # (:1691-1695).
        jive = self._popup.get("jive")
        kind = str(self._popup.get("kind") or "showbriefly")
        if isinstance(jive, dict) and jive:
            return {"type": kind, "display": jive}
        line: Any = self._popup.get("line")
        if isinstance(line, (list, tuple)):
            lines: list[Any] = list(line)
        else:
            lines = ["" if line is None else str(line)]
        display: dict[str, Any] = {"text": lines}
        duration = self._popup.get("duration")
        if duration:
            # Perl fügt duration nur in diesem Zweig hinzu (:1694).
            display["duration"] = int(duration)
        return {"type": kind, "display": display}

    def set_pending_display(self, jive: dict | None, kind: str = "showbriefly",
                            duration: int | None = None,
                            ttl: float = 15.0) -> None:
        """Store a display notification for the next ``displaystatus`` answer.

        Perl keeps the notification in the subscription's ``privateData``
        (``displaystatusQuery_filter``, Queries.pm:1616-1620) and answers the
        next ``displaystatus`` from it (:1655-1697).  ``jive`` is the
        notification's ``$parts->{'jive'}`` (the icon block of
        ``currentSongLines`` or a popup hash); without it only ``duration``
        and a line list can be published.  ``ttl`` mirrors the 15 s
        ``renderCache->{showBriefly}{ttl}`` Perl writes in ``Display.pm:280-283``.
        """
        self._popup = {"jive": jive, "kind": str(kind or "showbriefly")}
        if duration:
            self._popup["duration"] = int(duration)
        self._popup_expires = time.time() + max(0.0, float(ttl))


    async def _slim_request(self, player_id: str, command: list[str]) -> Any:
        """LMS-compatible slim.request — returns structured JSON dicts.

        SqueezeCtrl / Squeezer / SqueezeTray expect dict responses
        (players_loop, playlist_loop, mixer volume, etc.), not text lines.
        """
        if not command:
            return {}
        cmd = command[0]
        args = command[1:] if len(command) > 1 else []
        pid = player_id if player_id and player_id != "-" else None

        # Status/serverstatus polls from remote apps (a misbehaving
        # SqueezeTray can issue hundreds of identical requests per
        # second) — cache the response for 1s to keep the server
        # responsive for everyone else.
        cacheable = cmd in ("status", "serverstatus", "players")
        cache_key: tuple | None = None
        if cacheable:
            cache_key = (str(pid), str(cmd), json.dumps(args, sort_keys=True))
            cached = self._status_cache.get(cache_key)
            now = time.time()
            fingerprint = _status_state_fingerprint(cmd, pid)
            if (cached and fingerprint is not None and now - cached[0] < 1.0
                    and cached[1] == fingerprint):
                return cached[2]
            self._cache_hit = False

        try:
            from lyrion.player.manager import (
                PlayerManager,
                display_type_for,
                model_name_for,
            )
            pm = PlayerManager()
        except Exception:
            pm = None

        # ── players ────────────────────────────────────────────────
        if cmd == "players":
            players = pm.get_all_players() if pm else []
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 100
            loop = []
            for i, p in enumerate(players[start:start + count]):
                # Field set and ORDER copied from Perl's players_loop
                # (Slim/Control/Queries.pm:2624-2663): playerindex, playerid,
                # uuid, ip, name, seq_no (if defined), model, modelname,
                # power, isplaying, displaytype (unless model eq 'http'),
                # isplayer, canpoweroff, connected, firmware.
                model = getattr(p, "model", "squeezebox") or "squeezebox"
                entry: dict = {
                    "playerindex": start + i,
                    "playerid": p.mac,
                    # Perl: $eachclient->uuid() — null when the client
                    # sent none (live Perl LMS answers null, not the MAC).
                    "uuid": getattr(p, "uuid", "") or None,
                    "ip": f"{p.ip}:{p.port}" if getattr(p, "port", 0) else p.ip,
                    "name": getattr(p, "name", "") or p.mac,
                    "model": model,
                    # Perl: caps ModelName (SqueezePlay.pm:82), else the
                    # per-class modelName() — empty for classes that do not
                    # override it (Client.pm:936).
                    "modelname": getattr(p, "model_name", "")
                    or model_name_for(model),
                    "power": 1 if p.power else 0,
                    "isplaying": 1 if p.mode == "play" else 0,
                    "isplayer": 1 if getattr(p, "is_player", True) else 0,
                    "canpoweroff": 1 if getattr(p, "can_power_off", True) else 0,
                    "connected": 1 if p.connected else 0,
                    "firmware": getattr(p, "firmware", "2.0.0") or "1",
                }
                # seq_no is emitted only when the client has one
                # (Queries.pm:2633-2637); SqueezePlay needs its own value back
                # to consider volume/power in sync.
                seq_no = getattr(p, "seq_no", None)
                if seq_no is not None:
                    entry["seq_no"] = int(seq_no or 0)
                # displaytype = vfdmodel(); omitted for the 'http' model
                # (Queries.pm:2647-2649).
                dtype = display_type_for(model)
                if dtype is not None:
                    entry["displaytype"] = dtype
                if getattr(p, "needs_upgrade", False):
                    entry["player_needs_upgrade"] = 1
                if getattr(p, "is_upgrading", False):
                    entry["player_is_upgrading"] = 1
                loop.append(entry)
            result = {"count": len(players), "players_loop": loop}
            if cacheable:
                _fp = _status_state_fingerprint(cmd, pid)
                if _fp is not None:
                    self._status_cache[cache_key] = (time.time(), _fp, result)
            return result

        # ── serverstatus ───────────────────────────────────────────
        if cmd == "serverstatus":
            from lyrion import __version__
            from lyrion.config import get_config
            players = pm.get_all_players() if pm else []
            # The ONE public port (native frontend) — same value the TLV
            # discovery and the JSON presence beacon announce.
            try:
                from lyrion.config import public_http_port
                http_port = public_http_port()
            except Exception:
                http_port = 9000
            # Local IP + stable UUID like the real LMS (prefs 'server_uuid').
            local_ip = "127.0.0.1"
            try:
                import socket as _s
                _probe = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                _probe.settimeout(0.5)
                try:
                    _probe.connect(("192.168.1.1", 1))
                    local_ip = _probe.getsockname()[0]
                finally:
                    _probe.close()
            except Exception:
                pass
            try:
                prefs = get_config()
                server_uuid = str(prefs.get("server_uuid", "") or "")
                if not server_uuid:
                    import uuid as _uuid
                    server_uuid = str(_uuid.uuid4())
                    await prefs._prefs.set("server_uuid", server_uuid)
                server_name = str(prefs.get("server_name", "") or "Pyrion")
            except Exception:
                server_uuid = "lyrion-server-0001"
                server_name = "Pyrion"
            result = {
                "version": __version__,
                "uuid": server_uuid,
                "name": server_name,
                # Perl reports its ONE HTTP port here (live: 9000). Now we
                # report ours: `public_http_port` — the native, pipelining
                # frontend, which is also what the TLV and the JSON presence
                # beacon announce. Jive/SqueezePlay reads this field and
                # switches to it for its further connections, so all three
                # announcements MUST name the same port: while the TLV said
                # 9080 and this field 9000, SqueezePlay held two sessions in
                # parallel (ASGI plus native) and fluttered — on the ASGI path
                # the streaming response ended after RETRY_DELAY (5 s) of
                # silence without events => abort, re-handshake, "Verbindung
                # geht mal und mal nicht" (measured 2026-09-14). The frontend
                # answers Jive's Bayeux POSTs on its own socket and relays
                # /jsonrpc.js, artwork and /stream.mp3 to the internal ASGI
                # app (networking/cometd_stream.py `_relay_request`), so one
                # port is enough for every client.
                "httpport": http_port,
                "ip": local_ip,
                "player count": len(players),
                "other player count": 0,
                # Perl Queries.pm:3731: Import->lastScanTime() (Prefs
                # metainformation 'lastRescanTime'); live {"lastscan":"1789309710"}.
                # Squeezer liest den Wert als Zahl (CometClient.java:355).
                "lastscan": _last_scan_epoch(),
                # SqueezeClient's ServerStatusResponse requires mediadirs
                "mediadirs": [],
                # P4-3: real library totals (were hardcoded 0)
                "info total genres": 0,
                "info total artists": 0,
                "info total albums": 0,
                "info total songs": 0,
                "info total duration": 0,
                # Operational notice surfaced in the web Status bar, e.g.
                # "ffmpeg not found - transcoding not possible".
                "notice": _SERVER_NOTICE.get("text", ""),
            }
            # Live library-scan progress for the status bar ("Scan läuft,
            # N Dateien gefunden"). The SPA polls serverstatus anyway.
            try:
                from lyrion.media.scan_state import SCAN_STATE
                snap = SCAN_STATE.snapshot()
                result["scan"] = snap
            except Exception:
                pass
            try:
                # Perl's serverstatus totals are ``Slim::Schema->totals`` (the
                # *titles* query, Queries.pm:4843) and ``totalTime`` over
                # ``tracks.audio = 1`` (Schema.pm:2186-2190) — directory rows
                # count as neither.
                r = _db_query(
                    "SELECT COUNT(*) AS n FROM tracks WHERE "
                    f"{dir_rows.songs_clause('tracks')}")
                if r:
                    result["info total songs"] = r[0]["n"]
                r = _db_query(
                    "SELECT COALESCE(SUM(duration),0) AS d FROM tracks WHERE "
                    f"{dir_rows.duration_clause('tracks')}")
                if r:
                    result["info total duration"] = int(r[0]["d"])
                r = _db_query(
                    "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
                    "JOIN tracks_contributors tc ON tc.contributor = c.id "
                    "AND tc.role = 1"
                )
                if r:
                    result["info total artists"] = r[0]["n"]
                r = _db_query("SELECT COUNT(*) AS n FROM albums")
                if r:
                    result["info total albums"] = r[0]["n"]
                r = _db_query(
                    "SELECT COUNT(DISTINCT genre) AS n FROM tracks WHERE genre != ''"
                )
                if r:
                    result["info total genres"] = r[0]["n"]
            except Exception:
                pass
            # P4-3: subscribe:<seconds> tag — the Cometd layer pushes fresh
            # serverstatus on player connect/disconnect for these clients.
            if any(str(a).startswith("subscribe:") for a in (args or [])):
                result["subscribe"] = "60"
            # Jive controllers subscribe with
            # ['serverstatus', 0, 50, 'subscribe:60'] and expect the
            # player list in players_loop (like the real LMS). Also
            # return it without args (Squeezer queries plain serverstatus).
            result["count"] = len(players)
            # Same field values as the players loop above — Perl builds both
            # from Queries.pm:2624-2663, so modelname/uuid/displaytype/firmware
            # must not diverge (an invented "None"/None here showed up in the
            # jive app's server info).
            ss_loop = []
            for i, p in enumerate(players):
                model = getattr(p, "model", "squeezebox") or "squeezebox"
                entry: dict = {
                    "playerindex": str(i),
                    "playerid": p.mac,
                    "uuid": getattr(p, "uuid", "") or None,
                    "ip": f"{p.ip}:{p.port}" if p.port else p.ip,
                    "name": p.name,
                    "model": model,
                    "modelname": getattr(p, "model_name", "")
                    or model_name_for(model),
                    "power": 1 if p.power else 0,
                    "isplaying": 1 if p.mode == "play" else 0,
                    "isplayer": 1,
                    "canpoweroff": 1,
                    "connected": 1 if p.connected else 0,
                    "firmware": getattr(p, "firmware", "") or 0,
                }
                dtype = display_type_for(model)
                if dtype is not None:
                    entry["displaytype"] = dtype
                entry["seq_no"] = int(getattr(p, "seq_no", 0) or 0)
                ss_loop.append(entry)
            result["players_loop"] = ss_loop
            if cacheable:
                _fp = _status_state_fingerprint(cmd, pid)
                if _fp is not None:
                    self._status_cache[cache_key] = (time.time(), _fp, result)
            return result

        # ── status (player) ────────────────────────────────────────
        if cmd == "status":
            result = await self._json_player_status(pm, pid, args)
            if cacheable:
                _fp = _status_state_fingerprint(cmd, pid)
                if _fp is not None:
                    self._status_cache[cache_key] = (time.time(), _fp, result)
            return result

        # ── menu (home menu for Jive/Material/OpenSqueeze apps) ────
        # LMS 'menu <start> <count> [direct:1]' returns the root browse
        # items in item_loop. Apps hang on 'Loading Menus…' without it.
        if cmd == "menu":
            client = pm.get_player(pid) if (pm is not None and pid) else None
            items = self._home_menu(client)
            start = int(args[0]) if args and str(args[0]).isdigit() else 0
            count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 512
            loop = items[start:start + count]
            for i, it in enumerate(loop):
                it["index"] = start + i
            return {
                "item_loop": loop,
                "count": len(items),
                # SqueezeClient's JiveHomeItemListResponse requires offset
                "offset": start,
                "base": {"id": "", "name": _jive_string("HOME")},
                "title": _jive_string("HOME"),
            }

        # ── menustatus ─────────────────────────────────────────────
        # Perl-Dispatch ['menustatus','_data','_action'] zeigt auf einen
        # Stub, der nur ``warn "menustatus query"`` macht (Jive.pm:150-152) —
        # der Request endet ohne Result: live 2026-09-13 ``menustatus 0 2``
        # UND ``menustatus`` → {"result":{}}. Echte Menüdaten erreichen
        # Controller ausschließlich als menustatus-*Notification* über cometd
        # (Jive.pm:1972 notifyFromArray ['menustatus',$items,'add',$id]).
        if cmd == "menustatus":
            if args:
                return {}
            # Dokumentierte Abweichung: das argumentlose ["menustatus"] ist
            # bei uns der Seed-Request der Cometd-Subscription
            # /<cid>/slim/menustatus/<mac> (cometd.py:333-336), und Squeezer
            # liest daraus data[1] als Item-Array (CometClient.java:492-505,
            # parseMenuStatus). Die Query-Form mit Argumenten bleibt
            # Perl-gleich.
            client = pm.get_player(pid) if (pm is not None and pid) else None
            return [None, self._home_menu(client), "add", pid or ""]

        # displaystatus mit '?' am Ende — ['displaystatus'] ist in Slot 0
        # registriert (Request.pm:495); endet die Anfrage auf '?', greift der
        # leere Query-Slot → live Socket ohne Body (``displaystatus ?``).
        if cmd == "displaystatus" and args and str(args[-1]) == "?":
            return {}

        # ── Jive-Settings-/Menü-Queries (Slim/Control/Jive.pm) ─────
        # SqueezePlay/Controller rufen sie für ihre Einstellungsmenüs auf
        # (Ton, feste Lautstärke, Stereo XL, Line-Out, Crossfade, ReplayGain,
        # Display-Helligkeit/-Schrift, Album-Sortierung, Uhrzeit).
        if cmd in _JIVE_QUERY_COMMANDS:
            return await self._jive_settings_query(cmd, pm, pid, args)

        # ── Jive-Alarm-/Sync-/Sleep-/Listen-Menüs (Jive.pm:64-133) ──
        # SqueezePlay-Controller: Wecker, Synchronisieren, Sleep-Timer,
        # Presets/Favoriten/Playlists, Firmware- und Applet-Listen.
        if cmd in _JIVE_MENU_COMMANDS:
            return await self._jive_menu_query(cmd, pm, pid, args)

        # ── Jive-Aktionen mit Wirkung (Jive.pm:2459-2488, 1091-1114,
        #    2088-2131): Alarm-Snooze/Stop, Sleep am Titelende, Sync/Unsync.
        #    Perl antwortet nach setStatusDone ohne Ergebnis → {}.
        if cmd in _JIVE_ACTION_COMMANDS:
            if cmd == "jivealarm":
                self._jive_alarm_action(pm, pid, args)
            elif cmd == "jiveendoftracksleep":
                await self._jive_end_of_track_sleep(pm, pid)
            else:  # jivesync
                self._jive_sync_action(pm, pid, args)
            return {}

        # ── Controller-Kommandos in Perl-Form ──────────────────────
        # Live-Proben (nur lesend) gegen Perl 9.1.1, 2026-09-13; die
        # wörtlichen Antworten stehen in tests/test_controller_commands.py.
        # Perl-Fundstellen je Kommando: Request.pm-Dispatch + Queries.pm-Handler.

        # getstring <tokens,komma-getrennt>  (Queries.pm:1988-2016)
        # Dispatch ['getstring','_tokens'] (Request.pm:500) ist KEINE
        # '?'-Query — ein zusätzliches '?' ist nicht dispatchbar.
        # Live: getstring PLAY,BROWSE,NOSUCH_TOKEN_XYZ →
        #   {"BROWSE":"Durchsuchen","PLAY":"Wiedergabe","NOSUCH_TOKEN_XYZ":""}
        if cmd == "getstring":
            if len(args) != 1 or str(args[0]) in ("", "?"):
                return {}
            return {token: _client_string(token)
                    for token in str(args[0]).split(",")}

        # readdirectory <index> <quantity> [folder:<path>] [filter:<f>]
        # Live: readdirectory 0 5 → {"count":0}
        # Perl wählt den Dispatch-Eintrag über das LETZTE Token
        # (Request.pm:1011-1024): endet die Anfrage auf '?', greift der
        # Query-Slot — den gibt es für ['readdirectory','_index','_quantity']
        # nicht (Request.pm:497), live schließt Perl dann den Socket
        # (``readdirectory 0 ?``, ``readdirectory ?``).
        if cmd == "readdirectory":
            if args and str(args[-1]) == "?":
                return {}
            return _read_directory(list(args))

        # works <index> <quantity>  (Queries.pm:5062-…, Request.pm:633)
        # Live: ``works`` und ``works 0 5`` → {"count":0}; ``works ?`` →
        # Socket zu. Unser Schema hat keine works-Tabelle; Perls SQL braucht
        # tracks.work + works (Queries.pm:5097-5105), der Zähler bleibt 0 —
        # die Form ist damit Perl-gleich.
        if cmd == "works":
            if args and str(args[-1]) == "?":
                return {}
            return {"count": 0}

        # libraries [getid]  (Queries.pm:2074-2097, Request.pm:509-510)
        # Live: libraries → {}; libraries getid → {"id":0};
        #       libraries getid ohne Client → Socket zu (needClient=1).
        if cmd == "libraries":
            if (args and str(args[0]) == "getid" and pm is not None and pid
                    and pm.get_player(pid) is not None):
                return {"id": 0}
            return {}

        # player <entity> [?]  — playerXQuery (Queries.pm:2514-2576).
        # Dispatch Request.pm:534-543: ['player', <entity>, '_IDorIndex', '?']
        # für address|displaytype|id|uuid|ip|model|isplayer|name|canpoweroff,
        # ['player','count','?'] ohne _IDorIndex (:535).
        # Live Perl 9.1.1 (nur lesend, 2026-09-13):
        #   player count ? -> {"_count":3}         (Client.pm clientCount)
        #   player ip ?    -> {"_ip":"192.168.1.130:45614"}   (ipport)
        #   player model ? -> {"_model":"squeezelite"}
        # Ein unbekannter Client liefert KEIN Result (Perl :2553-2556, da
        # $client undef bleibt) -> leeres Dict.
        if cmd == "player" and args:
            entity = str(args[0]).lower()
            if entity == "count":
                return {"_count": len(pm.get_all_players()) if pm else 0}
            if entity in ("address", "displaytype", "id", "uuid", "ip", "model",
                          "isplayer", "name", "canpoweroff"):
                client = None
                # _IDorIndex: MAC direkt nach der Entität (Queries.pm:2533-2545)
                if len(args) > 1 and str(args[1]) not in ("", "?"):
                    client = pm.get_player(str(args[1])) if pm is not None else None
                if client is None and pm is not None and pid:
                    client = pm.get_player(pid)
                if client is None:
                    return {}
                if entity in ("address", "id"):
                    return {f"_{entity}": client.mac}
                if entity == "name":
                    return {"_name": client.name or ""}
                if entity == "model":
                    return {"_model": getattr(client, "model", "") or ""}
                if entity == "ip":
                    # Perl ipport() — "ip:port" der Steuerverbindung.
                    return {"_ip": f"{client.ip}:{getattr(client, 'port', 0) or 0}"}
                if entity == "isplayer":
                    return {"_isplayer": 1 if getattr(client, "is_player", True) else 0}
                if entity == "canpoweroff":
                    return {"_canpoweroff": 1
                            if getattr(client, "can_power_off", True) else 0}
                if entity == "displaytype":
                    from lyrion.player.manager import display_type_for
                    dt = display_type_for(getattr(client, "model", "") or "")
                    return {"_displaytype": dt} if dt is not None else {}
                return {"_uuid": getattr(client, "uuid", "") or None}

        # rescan [?]  — rescanQuery (Queries.pm:3214-3229) für ['rescan','?']
        # (Dispatch Request.pm:606): addResult('_rescan',
        # stillScanning() ? 1 : 0) (:3225). Der Kommando-Eintrag
        # ['rescan','_mode','_target'] (:607) fügt kein Result hinzu und läuft
        # über die Kommando-Verdrahtung (lyrion.control.rescan) — dieselbe wie
        # am CLI-Port, wie Perls eine rescanCommand
        # (Slim/Web/JSONRPC.pm:400-520). Live Perl 9.1.1, read-only,
        # 2026-09-13: rescan ? → {"_rescan":0}.
        if cmd == "rescan" and args and str(args[0]) == "?":
            from lyrion.control import rescan as _scan

            return {"_rescan": 1 if _scan.is_scanning() else 0}

        # rescanprogress  — rescanprogressQuery (Queries.pm:3231-3360),
        # Dispatch Request.pm:608 (keine Parameter). Ohne Scan: addResult
        # 'rescan' 0 (:3355-3357); während des Scans rescan 1 + Schritt-Prozent
        # + steps + totaltime (:3242-3285); im Idle mit zurückgebliebener
        # failure-Zeile zusätzlich lastscanfailed (:3291-3296, _scanFailed
        # :6247-6258). Live Perl 9.1.1, 2026-09-13:
        #   rescanprogress -> {"rescan":0}
        if cmd == "rescanprogress":
            from lyrion.control import rescan as _scan

            return dict(_scan.progress_results())

        # roles [<index> <quantity>] [tags:t]  — rolesQuery (Queries.pm:3302-3473),
        # Dispatch Request.pm:634. SQL :3393-3400 (ohne track_id):
        #   SELECT DISTINCT contributor_album.role FROM contributors JOIN
        #   contributor_album … — bei uns die Rollenspalte der Join-Tabelle
        #   (tracks_contributors.role, role 1 = Artist).
        # :3471 count zuletzt; das roles_loop (:3433-3460) enthält role_id
        # (num) und mit tags:t zusätzlich role_name (:3447-3452).
        # Live Perl 9.1.1, 2026-09-13: roles -> {"count":6}
        if cmd == "roles":
            rows = _db_query(
                "SELECT DISTINCT role AS r FROM tracks_contributors "
                "WHERE role IS NOT NULL ORDER BY role")
            roles = [int(r["r"]) for r in rows if r["r"] is not None]
            total = len(roles)
            idx_given = bool(args) and str(args[0]).lstrip("-").isdigit()
            qty_given = len(args) > 1 and str(args[1]).lstrip("-").isdigit()
            if not idx_given or total <= 0:
                return {"count": total}
            start = max(0, int(str(args[0])))
            if start > total - 1 or (qty_given and int(str(args[1])) <= 0):
                return {"count": total}
            limit = int(str(args[1])) if qty_given else total - start
            tags = next((str(a)[5:] for a in args if str(a).startswith("tags:")), "")
            loop = []
            for role in roles[start:start + limit]:
                item: dict = {"role_id": role}
                if "t" in tags:
                    item["role_name"] = _ROLE_NAMES.get(role, "")
                loop.append(item)
            return {"count": total, "roles_loop": loop}

        # years [<index> <quantity>]  — yearsQuery (Queries.pm:4949-5056),
        # Dispatch Request.pm:632. DISTINCT year (ohne hasAlbums über
        # tracks.year, :4974-4976), count zuletzt (:5055), Loop
        # 'years_loop' mit year (num) + favorites_url 'db:year.id=<n>'
        # (:5051-5054). Dieselbe Datenquelle wie der CLI-Pfad
        # (control/cli_commands.py cmd_years: year > 0, DESC).
        # Live Perl 9.1.1, 2026-09-13: years 0 2 -> {"count":65,
        #   "years_loop":[{"year":0,…},{"year":2026,…}]}
        if cmd == "years":
            has_albums = any(str(a) == "hasAlbums:1" for a in args)
            year_filter = next((str(a)[5:] for a in args
                                if str(a).startswith("year:")), None)
            table = "albums" if has_albums else "tracks"
            where = "year IS NOT NULL AND year > 0"
            params: tuple = ()
            if year_filter is not None and str(year_filter).lstrip("-").isdigit():
                where += " AND year = ?"
                params = (int(str(year_filter)),)
            rows = _db_query(
                f"SELECT DISTINCT year AS y FROM {table} WHERE {where} "
                "ORDER BY year DESC", params)
            years = [int(r["y"]) for r in rows if r["y"] is not None]
            total = len(years)
            rescan = 0
            try:
                from lyrion.media.scan_state import SCAN_STATE
                rescan = 1 if SCAN_STATE.snapshot().get("scanning") else 0
            except Exception:  # noqa: BLE001
                pass
            result: dict = {} if not rescan else {"rescan": 1}
            idx_given = bool(args) and str(args[0]).lstrip("-").isdigit()
            qty_given = len(args) > 1 and str(args[1]).lstrip("-").isdigit()
            start = int(str(args[0])) if idx_given else 0
            limit = int(str(args[1])) if qty_given else total
            if (idx_given or qty_given) and total > 0 and start <= total - 1 \
                    and limit > 0:
                result["years_loop"] = [
                    {"year": y, "favorites_url": f"db:year.id={y}"}
                    for y in years[start:start + min(limit, total)]
                ]
            result["count"] = total
            return result

        # apps [<index> <quantity>]  — OPML-basierte Menü-Queries.
        # Dispatch entsteht pro is_app-Plugin als ['apps','_index','_quantity']
        # (Slim/Plugin/OPMLBased.pm:26-28, :125-132) mit cliRadiosQuery
        # (:181-280) als Handler; Item-Form ohne menu-Parameter (:252-258)
        # {cmd, name, type, icon, weight}, weight-Default 1000 (:34),
        # type 'xmlbrowser' für link / 'xmlbrowser_search' für search
        # (:249-254). Loop-Name und count kommen aus dynamicAutoQuery
        # (Queries.pm:5384 '$query . "s_loop"', :5443 count zuletzt).
        # Live Perl 9.1.1, 2026-09-13: apps 0 5 -> {"count":1,
        #   "appss_loop":[{"type":"xmlbrowser","cmd":"sounds","weight":90,
        #                  "name":"Sounds & Effekte","icon":"plugins/…"}]}
        # Dieser Port hat keine is_app/OPML-Plugins → leere Liste, aber die
        # Perl-Form (count + appss_loop).
        if cmd == "apps":
            return await self._json_apps(args)

        # lastscan …  — Perl hat dafür KEINEN Dispatch-Eintrag (Request.pm)
        # und schließt den Socket ohne Body; live für ["lastscan","?"] und
        # ["lastscan"]. Der Controller liest den Wert aus 'serverstatus'
        # (Squeezer: CometClient.java:355).
        if cmd == "lastscan":
            return {}

        # debug <flag> ?  (Queries.pm:1517-1549, Request.pm:489)
        # Live: debug scan ? → {"_value":"ERROR"}; debug ? → Socket zu
        # (_debugflag wäre hier '?' → bad params).
        if cmd == "debug":
            level = _debug_category_level(str(args[0]) if args else "")
            return {} if level is None else {"_value": level}

        # irenable ?  (Queries.pm:2058-2072, Request.pm:507)
        # Live: {"_irenable":1} — Client-Zustand mit Default 1
        # (Slim/Player/Client.pm:201).
        if cmd == "irenable" and args and str(args[0]) == "?":
            player = pm.get_player(pid) if (pm is not None and pid) else None
            if player is None:
                return {}
            prefs = dict(getattr(player, "playerprefs", {}) or {})
            return {"_irenable": int(prefs.get("irenable", 1) or 0)}

        # linesperscreen ?  (Queries.pm:2100-2112, Request.pm:511)
        # Live: {"_linesperscreen":0} für alle angebundenen Spieler.
        if cmd == "linesperscreen" and args and str(args[0]) == "?":
            player = pm.get_player(pid) if (pm is not None and pid) else None
            if player is None:
                return {}
            return {"_linesperscreen": int(
                getattr(player, "lines_per_screen", 0) or 0)}

        # gototime ?  — Perl dispatcht 'gototime' auf timeQuery
        # (Request.pm:669-670); der Ergebnisschlüssel ist '_time' aus
        # songTime (Queries.pm:4786-4800). Live: {"_time":0}.
        if cmd == "gototime" and args and str(args[0]) == "?":
            player = pm.get_player(pid) if (pm is not None and pid) else None
            if player is None:
                return {}
            return {"_time": int(getattr(player, "elapsed", 0) or 0)}

        # artworkspec …  — nur ['artworkspec','add','_spec','_name'] ist
        # dispatchbar (Request.pm:482); jede andere Form ist bad dispatch
        # (Commands.pm:232-235) → Socket zu, live bestätigt. Ein gültiges
        # 'add' endet ebenfalls nach setStatusDone ohne Result
        # (Commands.pm:253-256) → leeres Dict.
        if cmd == "artworkspec":
            return {}

        # playlist <entity> ?  — playlistXQuery (Queries.pm:2708-2772,
        # Dispatch Request.pm:548-591). Squeezer/SqueezeCtrl lesen hier die
        # Repeat-/Shuffle-/Queue-Länge (`playlist repeat ?`, `playlist shuffle ?`,
        # `playlist tracks ?`); ohne diesen Zweig fiel die Form in den
        # Control-Pfad und antwortete leer.
        if (cmd == "playlist" and len(args) >= 2
                and str(args[-1]) == "?"
                and str(args[0]).lower() in _PLAYLIST_QUERY_ENTITIES):
            return await self._json_playlist_entity(pm, pid, [str(a) for a in args])

        # can <cmd> ?  — canQuery (Slim/Plugin/CLI/Plugin.pm:751-790,
        # Dispatch Request.pm:400-401). `_can` ist ein 0/1-INT (:783, :791);
        # live Perl 9.1.1: `can ?` → {"_can":0}.
        if cmd == "can" and args and str(args[-1]) == "?":
            return {"_can": 1 if _can_dispatch(list(args[:-1])) else 0}

        # pref <name> ? / pref ? / playerpref ?  — prefQuery
        # (Queries.pm:3009-3051, Dispatch Request.pm:602). Der Ergebnisschlüssel
        # ist IMMER `_p2` (:3045-3048) und der Wert die Pref; eine unbekannte
        # Pref ist undef → JSON null (live Perl 9.1.1: `pref ?` und
        # `pref audiodir ?` → {"_p2":null}). Die namensraumlose Form ist keine
        # sinnvolle Abfrage (Report 'Methode & Grenzen'), die KEY-Form war aber
        # bei uns erfunden (`{"audiodir":""}`).
        if cmd in ("pref", "playerpref") and args and str(args[-1]) == "?":
            name = str(args[0])
            if name == "?":
                # `pref ?`/`playerpref ?`: der gesuchte Name ist '?' selbst →
                # in Perl undef (Queries.pm:3045-3048).
                return {"_p2": None}
            if cmd == "pref":
                from lyrion.config import get_config
                return {"_p2": get_config().get(name, None)}
            # `playerpref <pref> ?` selbst wird weiter unten beantwortet
            # (_PLAYERPREF_DEFAULTS, Tests in tests/test_playerpref.py).

        # ── Control commands (return {} — LMS convention) ──────────
        # ── CLI query commands: <cmd> ? → {"_<cmd>": value} ──────
        # LMS JSON-RPC convention (ioBroker.squeezeboxrpc, Squeezer,
        # SqueezeClient): single-value queries are answered with the
        # command name prefixed by '_' as the result key.
        if args and len(args) == 1 and str(args[0]) == "?":
            player = pm.get_player(pid) if pid else None
            if player is not None:
                val: Any = ""
                if cmd == "mode":
                    val = player.mode
                elif cmd == "time":
                    val = int(getattr(player, "elapsed", 0) or 0)
                elif cmd == "duration":
                    val = float(getattr(player, "duration", 0) or 0)
                elif cmd == "name":
                    val = player.name or ""
                elif cmd == "power":
                    val = 1 if player.power else 0
                elif cmd == "current_title":
                    val = getattr(player, "current_title", "") or ""
                elif cmd in ("current_url", "url"):
                    val = getattr(player, "current_url", "") or ""
                elif cmd == "playlist":
                    val = len(getattr(player, "playlist", []) or [])
                elif cmd == "version":
                    # Orange Squeeze probes the server with 'version ?'
                    # and requires {"_version": "..."} to connect at all.
                    from lyrion import __version__
                    val = __version__
                elif cmd in ("artist", "album"):
                    tid = getattr(player, "current_track_id", None)
                    if tid is not None:
                        info = await self._load_tracks([tid])
                        val = (info.get(tid, {}) or {}).get(cmd, "") or ""
                return {f"_{cmd}": val}
            # Player-independent queries (Orange Squeeze sends
            # ["", ["version", "?"]] to probe the server).
            if cmd == "version":
                from lyrion import __version__
                return {"_version": __version__}
            return {f"_{cmd}": ""}

        # mixer <entity> ?  (Perl mixerQuery, Queries.pm:2118-2141)
        # Live-Probe Perl 2026-09-12: {"_volume": "23"}, {"_bass": "0"},
        # {"_treble": "0"}, {"_pitch": "100"} — Werte als Strings.
        if cmd == "mixer" and len(args) >= 2 and str(args[1]) == "?":
            entity = str(args[0]).lower()
            player = pm.get_player(pid) if pid else None
            value = _mixer_value(player, entity)
            if value is None:
                # Perl: isNotQuery -> bad dispatch (keine Antwort). Ein leeres
                # Result verhindert, dass ein Client auf eine Antwort wartet.
                return {}
            return {f"_{entity}": str(value)}

        # prefset — Material Skin + controllers subscribe to the player's
        # preference set; return the per-player prefs as {key: value}.
        if cmd == "prefset":
            player = pm.get_player(pid) if pid else None
            prefs = dict(getattr(player, "playerprefs", {}) or {}) if player else {}
            return prefs

        # pref <key> [?|<value>] — server preference query/set.
        if cmd == "pref" and args:
            key = str(args[0])
            if len(args) > 1 and str(args[1]) != "?":
                from lyrion.config import get_prefs
                await get_prefs().set(key, args[1])
                return {f"_pref_{key}": str(args[1])}
            from lyrion.config import get_config
            val = get_config().get(key, "")
            return {key: val}

        # playerpref <pref> [?] — Leseform. Perl beantwortet den Wert unter dem
        # Schlüssel `_p<N>`, wobei N der Index des `?`-Tokens in
        # (command + args) ist; live gegen Perl 9.1.1 geprüft:
        #   playerpref replayGainMode ?  →  {"_p2":"0"}
        if cmd == "playerpref" and args and any(str(a) == "?" for a in args):
            player = pm.get_player(pid) if pid else None
            pref = str(args[0])
            prefs = dict(getattr(player, "playerprefs", {}) or {}) if player else {}
            val = prefs.get(pref, _PLAYERPREF_DEFAULTS.get(pref, ""))
            idx = ["playerpref"] + [str(a) for a in args]
            return {f"_p{idx.index('?')}": str(val)}

        if cmd in ("pause", "power", "play", "stop", "mixer", "sync",
                   "unsync", "pref", "playerpref", "display", "button",
                   "signalstrength", "client", "mode", "name",
                   "playlist", "playlistcontrol",
                   "jivesetalbumsort", "jiveblankcommand", "jivedummycommand"):
            # Invalidate the status cache: a poll right after a control
            # command must see the NEW state, not the stale cached one
            # (Squeezer otherwise shows 'playing' until the TTL expires).
            self._status_cache.clear()
            await self._json_control(pm, pid, cmd, args)
            return {}

        # ── favorites ──────────────────────────────────────────────
        # JSON-RPC clients (SqueezeTray/SqueezeCtrl/SPA) expect the LMS
        # loop_loop format: {"count": N, "loop_loop": [{id, name, url,
        # hasitems, ...}]}. items is DB-backed (FavoritesManager); the
        # other subcommands go through the CLI handler.

        # favorites exists [<dB-id>|<url>]  (Perl cliExists,
        # Slim/Plugin/Favorites/Plugin.pm:786-815, Dispatch :82
        # ['favorites','exists','_id']). Eine numerische ID wird über die
        # Track-Tabelle zur URL aufgelöst; eine unbekannte ID ist bad params
        # → Socket zu. Live 2026-09-13: favorites exists → {"exists":0};
        # favorites exists abc → {"exists":0}; favorites exists 1 → Socket zu
        # (Track 1 existiert nicht in Perls Bibliothek).
        if cmd == "favorites" and args and str(args[0]) == "exists":
            # ``favorites exists ?`` endet auf '?' → Perl sucht den
            # Query-Slot, den es für ['favorites','exists','_id'] nicht gibt
            # (Plugin.pm:82) → live Socket ohne Body.
            if str(args[-1]) == "?":
                return {}
            favourite = str(args[1]) if len(args) > 1 else ""
            if favourite.isdigit():
                try:
                    rows = _db_query("SELECT url FROM tracks WHERE id = ?",
                                     (int(favourite),))
                except Exception:  # noqa: BLE001
                    rows = []
                if not rows:
                    return {}
                favourite = str(rows[0].get("url") or "")
            # Perl sucht die URL in der flachen Favoritenliste
            # (OpmlFavorites::findUrl:372-390) und liefert deren Index.
            flat: list[dict] = []

            def _collect(nodes) -> None:
                for node in nodes or []:
                    flat.append(node)
                    _collect(node.get("children"))

            try:
                from lyrion.music.favorites import get_favorites_manager
                _collect(await get_favorites_manager().list_tree())
            except Exception:  # noqa: BLE001
                pass
            for i, item in enumerate(flat):
                if str(item.get("url") or "") == favourite:
                    return {"exists": 1, "index": i}
            return {"exists": 0}

        # favorites add  |  favorites addlevel   (Perl cliAdd)
        # Dispatch ['favorites','add'] / ['favorites','addlevel']
        # (Slim/Plugin/Favorites/Plugin.pm:76-77) → cliAdd (:821-922): the
        # command needs ``url`` AND ``title`` (:847, bad params otherwise
        # :872-877), stores ``type`` (default 'audio', :854) and the icon
        # (:855), appends at the end when no item_id is given (:890-893) and
        # answers ``count`` 1 (:859).  Our former path handed the CLI echo
        # (a text list) back, which the JSON clients cannot read.
        if cmd == "favorites" and args and str(args[0]) in ("add", "addlevel"):
            return await self._json_favorites_add(pid, args)

        # favorites delete — Perl dispatch ['favorites','delete']
        # (Slim/Plugin/Favorites/Plugin.pm:80 addDispatch → cliDelete :925-970).
        if cmd == "favorites" and args and str(args[0]) == "delete":
            return await self._json_favorites_delete(args)

        if cmd == "favorites" and args and str(args[0]) == "items":
            return await self._json_favorites_items(pid, args[1:])

        # favorites changed — event subscription (SqueezeCtrl): the app
        # watches this channel and reloads the list when a 'changed' event
        # arrives. Answer empty/ok (no 'unknown command').
        if cmd == "favorites" and args and str(args[0]) == "changed":
            return {}

        if cmd == "favorites":
            try:
                from lyrion.control.cli import CLIHandler, CLIContext
                async with CLIHandler() as cli:
                    ctx = CLIContext(player_id=pid or "-")
                    result = await cli.dispatch(ctx, (cmd, args))
                    return result if isinstance(result, list) else [str(result)]
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}

        # ── serverpref: get/set server preferences (e.g. library paths) ──
        #   ["serverpref", "musicdir"]            → {"musicdir": "/mnt/..."}
        #   ["serverpref", "musicdir", "/pfad"]   → set + return
        if cmd == "serverpref":
            try:
                from lyrion.config import PreferenceStore
                prefs = PreferenceStore.instance()
                if len(args) >= 2:
                    value = " ".join(str(a) for a in args[1:])
                    await prefs.set(args[0], value)
                    return {str(args[0]): value}
                if args:
                    return {str(args[0]): prefs.get(str(args[0])) or ""}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
            return {}

        # ── Radio sub-feeds (Perl's dynamic OPMLBased plugins) ─────────
        # Perl's TuneIn importer creates one plugin per directory item and
        # registers ``[<tag>,'items','_index','_quantity']`` and
        # ``[<tag>,'playlist','_method']`` for each
        # (``Slim/Plugin/OPMLBased.pm:117-125``); the item rows of the
        # ``radios`` menu then send exactly these two request forms
        # (``base.actions.go`` / ``base.actions.play``, :204-219 / :960-990).
        # The tag list is the TuneIn ``MENUS`` table plus Perl's ``sounds``
        # app feed (``Slim/Plugin/Sounds``) — see lyrion.web.radiobrowser.
        # ``search items …`` must be checked *before* the library search:
        # Perl resolves the literal token before the ``_index`` slot
        # (``Request.pm:1007-1032``), so ``search items`` is the Radio search
        # while ``search <start> <count> term:<x>`` stays the library query.
        if cmd in _RADIO_FEEDS and args:
            if str(args[0]) == "items":
                return await self._json_radio_feed(cmd, args[1:], pid)
            if str(args[0]) == "playlist":
                return await self._json_radio_playlist(cmd, args[1:], pid)
            # Perl registers nothing else for a radio tag → bad dispatch.
            return {}

        # ── search (LMS format: search <start> <count> term:<begriff>) ──
        if cmd == "search":
            return await self._json_search(args)

        # ── alarms / alarm (LMS alarm-clock API, JSON for controllers) ──
        # SqueezePlay/Material poll ['alarms'] and expect an attribute
        # response wrapping alarm_loop of alarms for the player.
        if cmd == "alarms":
            return await self._json_alarms(pid, args)
        if cmd == "alarm":
            return await self._json_alarm(pid, args)

        # ── Saved playlists (Perl playlistsQuery / Commands.pm parity) ──
        if cmd == "playlists":
            return await self._json_playlists(pid, args)

        # ── info total <entity> ? (Perl infoTotalQuery) ─────────────
        # Perl registriert GENAU fünf Formen ['info','total','<e>','?'] auf
        # infoTotalQuery (Slim/Control/Request.pm:501-505 -> Queries.pm:2019-2055).
        # Ergebnis: genau ein Schlüssel "_<entity>" (:2039-2051), Wert aus
        # Slim::Schema->totals (Schema.pm:3305-3332: album/contributor/genre/
        # track) bzw. ->totalTime (Schema.pm:2173-2199) für duration.
        # Live Perl 9.x, nur lesend 2026-09-13:
        #   ["info","total","songs","?"]    -> {"_songs":80218}
        #   ["info","total","albums","?"]   -> {"_albums":7189}
        #   ["info","total","artists","?"]  -> {"_artists":11170}
        #   ["info","total","genres","?"]   -> {"_genres":762}
        #   ["info","total","duration","?"] -> {"_duration":22851708.851}
        # ALLE anderen Formen (ohne '?', unbekannte Entität, Extra-Argumente)
        # matchen keinen Dispatch-Eintrag; Perls JSON-RPC schließt dann den
        # Socket ohne Body ("Empty reply from server") — hier leeres Dict
        # statt der irreführenden Browse-Leerform (count/offset/loop_loop).
        if cmd == "info":
            return await self._json_info_total(args)

        # ── Browse commands (library) ──────────────────────────────
        # tracks → titlesQuery (Request.pm:629) — dieselbe Antwort wie
        # songs/titles, nur ein anderer Dispatch-Alias.
        if cmd == "tracks":
            return await self._json_browse("titles", args)
        if cmd in ("albums", "artists", "genres", "songs", "titles",
                   "musicfolder", "radios", "songinfo",
                   "contributors", "browse"):
            if cmd == "radios":
                return await self._json_radios(cmd, args)
            return await self._json_browse(cmd, args)

        # ── radiosearch (controller Radio search; an additive alias) ──
        # Perl has no ``radiosearch`` dispatch (its radio search is the
        # ``search items … menu:search search:<term>`` form above); this alias
        # answers with that very envelope so Squeezer/OrangeSqueeze get
        # touch-to-play rows instead of the former inert ones.
        if cmd == "radiosearch":
            return await self._json_radiosearch(cmd, args, pid)

        # ── browselibrary (SqueezePlay My-Music children; LMS 'browselibrary
        #    items <start> <count> mode:<albums|artists|genres|years|bmf|search>') ──
        if cmd == "browselibrary":
            return await self._json_browselibrary(cmd, args, pid)

        # ── contextmenu (SqueezePlay press-and-hold context menus) ─────
        # Perl parity: a *wrapper* around '<menu>info items <index> <qty>
        # <params>' (Slim/Control/Queries.pm:6171 contextMenuQuery). Without
        # this the modal window opened empty (black dialog, only X).
        # See lyrion/web/contextmenu.py.
        if cmd == "contextmenu":
            from lyrion.web.contextmenu import handle_contextmenu
            return await handle_contextmenu(self, pm, pid, args)

        # ── *info feeds (the target of Jive's "more" action, MENU-03) ───
        # Perl dispatches ['<feed>info', 'items', '_index', '_quantity']
        # (Slim/Menu/AlbumInfo.pm:32-37, TrackInfo.pm:32, ArtistInfo/GenreInfo/
        # YearInfo/FolderInfo) and a long-press on a browse item sends exactly
        # this (base.actions.more → cmd [<feed>info, 'items']).  The
        # track/album/artist/genre feeds are served by our context-menu
        # builder (contextmenu.py — Perl forwards ``contextmenu`` there too,
        # Queries.pm:6196-6200), the year feed by ``menus.year_info_menu``.
        if cmd in _INFO_FEED_MENUS or cmd == "yearinfo":
            return await self._info_feed(cmd, pm, pid, args)

        # ── displaystatus (Squeezer subscribes with a request) ──────
        # Squeezer's parseDisplayStatus does getDataAsMap() — an
        # 'unknown command' list response crashes it. Empty map is fine.
        if cmd == "displaystatus":
            return await self._displaystatus(pid, args)

        # ── playerstatus (SqueezeCtrl/Orange Squeeze subscribe) ─────
        # The apps subscribe to /<cid>/slim/playerstatus/<player> and
        # expect the player status as event data — without it they show
        # no player status and no stream info.
        if cmd == "playerstatus":
            return await self._json_player_status(pm, pid, args)

        # ── Fallback: text CLI passthrough ─────────────────────────
        try:
            from lyrion.control.cli import CLIHandler, CLIContext
            async with CLIHandler() as cli:
                ctx = CLIContext(player_id=player_id)
                result = await cli.dispatch(ctx, (cmd, args))
                # Unknown commands must NOT be answered with the text
                # list — apps (Squeezer) cast the response data and
                # crash on Object[].
                if isinstance(result, list) and result and str(result[0]).startswith("unknown command"):
                    return {}
                return result if isinstance(result, list) else [str(result)]
        except Exception as e:
            return {"error": str(e)}

    async def _jive_settings_query(self, cmd: str, pm, pid: str | None,
                                   args: list) -> Any:
        """Jive-Settings-Menüs des SqueezePlay-Controllers.

        Perl-Handler in ``Slim/Control/Jive.pm``: ``toneSettingsQuery``
        (:1259-1292), ``fixedVolumeSettingsQuery`` (:1221-1256),
        ``stereoXLQuery`` (:1164-1190), ``lineOutQuery`` (:1192-1218),
        ``crossfadeSettingsQuery`` (:1294-1315), ``replaygainSettingsQuery``
        (:1317-1335), ``playerBrightnessMenu`` (:1774-1838),
        ``playerTextMenu`` (:1840-1877), ``albumSortSettingsMenu`` (:339-369),
        ``dateQuery`` (:2136-2181). Feldtypen/Leerantworten: Live-Proben gegen
        Perl-LMS 9.x (siehe tests/test_jive_settings_commands.py).

        Die Handler brauchen (bis auf ``date``) einen Client (Dispatch
        ``needClient = 1``); ohne Player antwortet Perl mit Status 103, also
        ohne Result → hier leeres Dict.
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None

        # ── date (Jive.pm:132-133, needClient = 0) ────────────────────
        if cmd == "date":
            _, tagged = _jive_params(args, [])
            new_time = tagged.get("set")
            # Jive.pm:2149-2161: $newTime = getParam('set') || 0 — "0"/"" sind
            # falsy, dann time(); ein gesetztes Datum wird als String
            # durchgereicht (Live-Probe: date_epoch "1700000000").
            if new_time not in (None, "", "0"):
                epoch: Any = new_time
            else:
                epoch = int(time.time())
            return {
                "date_epoch": epoch,
                # 7.3-und-älter-Platzhalter (Jive.pm:2173)
                "date": "0000-00-00T00:00:00+00:00",
            }

        if player is None:
            return {}

        model = getattr(player, "model", "") or ""
        index, quantity = None, None

        # ── Ton-Einstellungen (Jive.pm:1259-1292) ─────────────────────
        if cmd == "jivetonesettings":
            pos, tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            tone = tagged.get("cmd")
            # Jive.pm:1264-1266: $tone = getParam('cmd'); $val = $client->$tone()
            if tone not in ("bass", "treble", "pitch"):
                # Perl: $client->undef() stirbt → gar keine Antwort
                return {}
            item = {
                "slider": 1,
                "min": -23,          # Jive.pm:1271
                "max": 23,           # Jive.pm:1272
                "adjust": 24,        # Jive.pm:1273
                # $val ist der Pref-Getter → STRING (Live: initial "50")
                "initial": str(_mixer_value(player, tone) or 0),
                "actions": {"do": {
                    "player": 0,
                    "cmd": ["playerpref", tone],
                    "params": {"valtag": "value"},   # Jive.pm:1281
                }},
            }
            return _jive_slice([item], index, quantity)

        # ── Feste Lautstärke (Jive.pm:1221-1256) ──────────────────────
        if cmd == "jivefixedvolumesettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            dvc = _jive_client_pref(player, "digitalVolumeControl", None)
            if dvc is None:
                # Player.pm:38 digitalVolumeControl => 1; unser PlayerState-
                # Feld ist das Modell dieses Prefs (protocol.py:2617).
                dvc = 1 if getattr(player, "digital_volume_control", True) else 0
            # Jive.pm:1230-1233: checkbox 1 nur wenn digitalVolumeControl == 0
            checkbox = 1 if _jive_num(dvc, 1) == 0 else 0
            item = {
                "text": _jive_string("FIXED_VOLUME_100"),
                "checkbox": checkbox,
                "actions": {
                    "on": {"player": 0,
                           "cmd": ["playerpref", "digitalVolumeControl", 0]},
                    "off": {"player": 0,
                            "cmd": ["playerpref", "digitalVolumeControl", 1]},
                },
            }
            return _jive_slice([item], index, quantity)

        # ── Stereo XL (Jive.pm:1164-1190) ─────────────────────────────
        if cmd == "jivestereoxl":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            # Boom.pm:41 'stereoxl' => minXL() (0); ohne gesetztes Pref ist es
            # ein undef → Perl zählt 0 als aktuellen Wert (Live-Probe).
            cur = _jive_num(_jive_client_pref(player, "stereoxl", None), 0)
            strings = ["CHOICE_OFF", "LOW", "MEDIUM", "HIGH"]   # Jive.pm:1170
            items = [
                {
                    "text": _jive_string(key),
                    "radio": 1 if cur == i else 0,          # Jive.pm:1175
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["playerpref", "stereoxl", i],   # Zahl (Jive.pm:1179)
                    }},
                }
                for i, key in enumerate(strings)
            ]
            return _jive_slice(items, index, quantity)

        # ── Line-Out (Jive.pm:1192-1218) ──────────────────────────────
        if cmd == "jivelineout":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            raw = _jive_client_pref(player, "analogOutMode", None)
            if raw is None:
                # Boom.pm:38 analogOutMode => -1 (kein Radio aktiv); bei allen
                # anderen Klassen ist das Pref undef und `undef == 0` in Perl
                # wahr → Radio bei Option 0 (Live-Probe).
                cur = -1 if _jive_display_class(model) == "boom" else 0
            else:
                cur = _jive_num(raw, 0)
            strings = [                                     # Jive.pm:1198
                "ANALOGOUTMODE_HEADPHONE", "ANALOGOUTMODE_SUBOUT",
                "ANALOGOUTMODE_ALWAYS_ON", "ANALOGOUTMODE_ALWAYS_OFF",
            ]
            items = [
                {
                    "text": _jive_string(key),
                    "radio": 1 if cur == i else 0,
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["playerpref", "analogOutMode", i],  # Zahl (Jive.pm:1207)
                    }},
                }
                for i, key in enumerate(strings)
            ]
            return _jive_slice(items, index, quantity)

        # ── Crossfade (Jive.pm:1294-1315 + transitionHash 2302-2316) ──
        if cmd == "crossfadesettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            # Squeezebox2.pm:44 transitionType => 0
            cur = _jive_num(_jive_client_pref(player, "transitionType", None), 0)
            strings = [                                     # Jive.pm:1300-1304
                "TRANSITION_NONE", "TRANSITION_CROSSFADE",
                "TRANSITION_FADE_IN", "TRANSITION_FADE_OUT",
                "TRANSITION_FADE_IN_OUT",
            ]
            items = [
                {
                    "text": _jive_string(key),
                    "radio": 1 if cur == i else 0,          # Jive.pm:2307
                    "actions": {"do": {
                        "player": 0,
                        # Jive.pm:2311 cmd => ['playerpref','transitionType',"$thisValue"]
                        "cmd": ["playerpref", "transitionType", str(i)],
                    }},
                }
                for i, key in enumerate(strings)
            ]
            return _jive_slice(items, index, quantity)

        # ── ReplayGain (Jive.pm:1317-1335 + replayGainHash 2318-2332) ─
        if cmd == "replaygainsettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            # Squeezebox2.pm:47 replayGainMode => 0
            cur = _jive_num(_jive_client_pref(player, "replayGainMode", None), 0)
            strings = [                                     # Jive.pm:1323-1326
                "REPLAYGAIN_DISABLED", "REPLAYGAIN_TRACK_GAIN",
                "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_SMART_GAIN",
            ]
            items = [
                {
                    "text": _jive_string(key),
                    "radio": 1 if cur == i else 0,          # Jive.pm:2323
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["playerpref", "replayGainMode", str(i)],  # :2327 String
                    }},
                }
                for i, key in enumerate(strings)
            ]
            return _jive_slice(items, index, quantity)

        # ── Helligkeit (Jive.pm:1774-1838) ────────────────────────────
        if cmd == "jiveplayerbrightnesssettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            return _jive_slice(self._jive_brightness_items(player), index, quantity)

        # ── Schriftgrößen (Jive.pm:1840-1877) ─────────────────────────
        if cmd == "jiveplayertextsettings":
            pos, _tagged = _jive_params(args, ["_whatFont", "_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            what_font = pos.get("_whatFont")
            entry = _jive_fonts(_jive_display_class(model)).get(what_font or "")
            if entry is None:
                # Jive.pm:1851 iteriert $prefs->client($client)->get($whatFont):
                # bei NoDisplay (und bei unbekanntem Pref) ist das undef und der
                # Handler stirbt → keine Antwort (Live-Probe).
                return {}
            names, default_curr = entry
            cur = _jive_num(
                _jive_client_pref(player, f"{what_font}_curr", None), default_curr)
            items = [
                {
                    "text": _jive_string(name),
                    "radio": 1 if cur == value else 0,      # Jive.pm:1862
                    "actions": {"do": {
                        "player": 0,
                        # Jive.pm:1865 cmd => ['playerpref',$whatFont.'_curr',$value]
                        "cmd": ["playerpref", f"{what_font}_curr", value],
                    }},
                }
                for value, name in enumerate(names)
            ]
            return _jive_slice(items, index, quantity)

        # ── Album-Sortierung (Jive.pm:339-369) ────────────────────────
        if cmd == "jivealbumsortsettings":
            from lyrion.config import get_prefs
            # Prefs.pm:271 jivealbumsort => 'album' (Server-Pref, nicht pro Client)
            sort = get_prefs().get("jivealbumsort", "album")
            methods = {                                     # Jive.pm:344-348
                "artistalbum": "SORT_ARTISTALBUM",
                "artflow": "SORT_ARTISTYEARALBUM",
                "album": "ALBUM",
            }
            items = [
                {
                    "text": _jive_string(methods[key]),
                    "radio": 1 if str(sort or "") == key else 0,   # Jive.pm:354
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["jivesetalbumsort"],            # Jive.pm:359
                        "params": {"sortMe": key},
                    }},
                }
                for key in sorted(methods)     # Jive.pm:352 sort keys
            ]
            # Jive.pm:349-350: count/offset direkt — KEIN sliceAndShip, deshalb
            # werden _index/_quantity ignoriert (Live-Probe: "… 3 1" liefert
            # trotzdem count 3, offset 0 und alle Items).
            return {"count": len(items), "offset": 0, "item_loop": items}

        return {}

    async def _jive_menu_query(self, cmd: str, pm, pid: str | None,
                               args: list) -> Any:
        """Jive-Alarm-/Sync-/Sleep-/Listen-Menüs des SqueezePlay-Controllers.

        Perl-Handler in ``Slim/Control/Jive.pm``: ``alarmSettingsQuery``
        (:591-696), ``jiveAlarmVolumeSlider`` (:1029-1062), ``alarmUpdateMenu``
        (:700-923), ``alarmUpdateDays`` (:925-1005), ``syncSettingsQuery``
        (:1064-1089), ``sleepSettingsQuery`` (:1116-1162), ``jivePresetsMenu``
        (:2490-2596), ``jiveFavoritesCommand`` (:2601-2690),
        ``jivePlaylistsCommand`` (:2412-2457), ``jiveRecentSearchQuery``
        (:2768-2797), ``firmwareUpgradeQuery`` (:2196-2229),
        ``extensionsQuery`` (:2830-2885). Menü-Loop/Slicing: ``sliceAndShip``
        (:1338-1357) + ``normalize`` (Request.pm:1805-1839).

        Feldtypen gegen den Perl-LMS 9.1.1 (192.168.1.90:9000, read-only)
        verifiziert: ``alarmsettings`` (count 5 mit Alarm, offset "0"),
        ``jiveupdatealarm``/``jiveupdatealarmdays`` (8 bzw. 7 Items),
        ``jivealarmvolume`` (``offset`` 0 als ZAHL, ``initial`` "50" STRING),
        ``syncsettings`` (count 3, offset "0"), ``sleepsettings`` (5 Items,
        ``setSelectedIndex`` "1"), ``jivepresets`` (10 Items, ``key`` STRING),
        ``jivefavorites``/``jiveplaylists`` (count 2, ``offset`` 0 als ZAHL),
        ``jiverecentsearches`` (count "1" STRING + Empty-Item), ``firmwareupgrade``
        (``firmwareUpgrade`` 0), ``jivewallpapers``/``jivesounds`` (``count`` 0).

        Ohne Client (Perl ``needClient = 1`` → Status 103) kommt keine Antwort,
        d. h. leeres Dict.
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None

        # ── extension queries (Jive.pm:2830-2885, needClient = 0) ─────
        # Perl zieht den Typ aus dem Kommandonamen; ohne registrierten
        # Provider (``registerExtensionProvider``, Jive.pm:2807-2817 — unser
        # Server hat keine Applet-/Wallpaper-Quelle) bleibt nur ``count 0``.
        if cmd in ("jiveapplets", "jivewallpapers", "jivesounds",
                   "jivepatches"):
            import re
            if re.search(r"jive(applet|wallpaper|sound|patche)s", cmd) is None:
                return {}                      # setStatusBadDispatch
            return {"count": 0}                # Live: jivewallpapers/jivesounds

        # ── firmwareupgrade (Jive.pm:2196-2229, needClient = 0) ───────
        # Perl liefert firmwareUrl/relativeFirmwareUrl nur, wenn für das
        # Modell ein Firmware-File vorliegt (Firmware.pm:288-310); ohne
        # Download-Quelle ist ``need_upgrade`` undef → 0. Unser Server
        # verteilt keine Jive-Firmware (kein /firmware/-Route) → keinen URL
        # anbieten, sonst schickt der Client ein Upgrade ins Leere.
        if cmd == "firmwareupgrade":
            return {"firmwareUpgrade": 0}

        # ── jiverecentsearches (Jive.pm:2768-2797, needClient = 0) ────
        if cmd == "jiverecentsearches":
            total = len(_JIVE_RECENT_SEARCHES)
            if total == 0:                     # _jiveNoResults (Jive.pm:2692-2699)
                return {
                    "count": "1",              # Perl-Literal STRING
                    "offset": 0,
                    "item_loop": [{
                        "text": _jive_string("EMPTY"),
                        "style": "itemNoAction",
                        "action": "none",
                    }],
                }
            total = 199 if total > 200 else total     # Jive.pm:2779-2780
            items: list[dict] = []
            for i in range(total + 1):         # Jive.pm:2783-2789
                if i >= len(_JIVE_RECENT_SEARCHES) or not _JIVE_RECENT_SEARCHES[i]:
                    break
                items.append(dict(_JIVE_RECENT_SEARCHES[i]))
            return {"count": total, "offset": 0, "item_loop": items}

        if player is None:
            return {}

        # ── alarmsettings (Jive.pm:591-696) ───────────────────────────
        if cmd == "alarmsettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            items = []
            # All Alarms on/off: choiceStrings sind ucfirst(string(OFF|ON))
            strings = ["OFF", "ON"]                              # Jive.pm:601
            choices = [(s := _jive_string(k))[:1].upper() + s[1:]
                       for k in strings]
            enabled = _jive_num(                                 # Client.pm:42
                _jive_client_pref(player, "alarmsEnabled", None), 1)
            alarm_items = _jive_current_alarms(player)            # Jive.pm:611
            items.append({
                "text": _jive_string("ALARM_ALL_ALARMS"),
                "choiceStrings": choices,
                "selectedIndex": enabled + 1,      # Jive.pm:604 (+1 wie Lua)
                "actions": {"do": {"choices": [
                    {"player": 0, "cmd": ["alarm", "disableall"]},
                    {"player": 0, "cmd": ["alarm", "enableall"]},
                ]}},
            })
            items += alarm_items
            items.append({                                       # Jive.pm:622-650
                "text": _jive_string("ALARM_ADD"),
                "input": {
                    "initialText": 25200,                        # 7:00
                    "title": _jive_string("ALARM_ADD"),
                    "_inputStyle": "time",
                    "len": 1,
                    "help": {"text": _jive_string("JIVE_ALARMSET_HELP")},
                },
                "actions": {"do": {
                    "player": 0,
                    "cmd": ["alarm", "add"],
                    "params": {"time": "__TAGGEDINPUT__", "enabled": 1},
                }},
                "nextWindow": "refresh",
            })
            # Bug 9226: bei fester Lautstärke (digitalVolumeControl == 0)
            # keine Weckerlautstärke anbieten (Jive.pm:654-661).
            dvc = _jive_client_pref(player, "digitalVolumeControl", None)
            if dvc is None:
                dvc = 1 if getattr(player, "digital_volume_control", True) else 0
            if _jive_num(dvc, 1) != 0:
                items.append({
                    "text": _jive_string("ALARM_VOLUME"),
                    "actions": {"go": {"player": 0, "cmd": ["jivealarmvolume"]}},
                })
            fade = _jive_num(                                    # Client.pm:45
                _jive_client_pref(player, "alarmfadeseconds", None), 1)
            items.append({                                       # Jive.pm:663-689
                "text": _jive_string("ALARM_FADE"),
                "checkbox": 0 if fade == 0 else 1,
                "actions": {
                    "on": {"player": 0, "cmd": ["jivealarm"],
                           "params": {"fadein": 1}},
                    "off": {"player": 0, "cmd": ["jivealarm"],
                            "params": {"fadein": 0}},
                },
            })
            return _jive_slice(items, index, quantity)

        # ── jiveupdatealarm (Jive.pm:700-923) ─────────────────────────
        if cmd == "jiveupdatealarm":
            pos, tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            aid = tagged.get("id")
            alarm = self._jive_find_alarm(player, aid)
            if alarm is None:
                # Perl: getAlarm → undef, dann stirbt ``$alarm->enabled()``
                # (Live-Probe: Verbindung schließt ohne Antwort).
                return {}
            enabled = 1 if getattr(alarm, "enabled", False) else 0
            items = [{                                           # Jive.pm:711-738
                "text": _jive_string("ALARM_ALARM_ENABLED"),
                "checkbox": enabled,
                "onClick": "refreshOrigin",
                "actions": {
                    "on": {"player": 0, "cmd": ["alarm", "update"],
                           "params": {"id": aid, "enabled": 1}},
                    "off": {"player": 0, "cmd": ["alarm", "update"],
                            "params": {"id": aid, "enabled": 0}},
                },
            }, {                                                 # Jive.pm:740-769
                "text": _jive_string("ALARM_SET_TIME"),
                "input": {
                    # Live: der vom Client mitgeschickte time-Parameter
                    # (Sekunden), z. B. "22500"; ohne Angabe null.
                    "initialText": tagged.get("time"),
                    "title": _jive_string("ALARM_SET_TIME"),
                    "_inputStyle": "time",
                    "len": 1,
                    "help": {"text": _jive_string("JIVE_ALARMSET_HELP")},
                },
                "actions": {"do": {
                    "player": 0,
                    "cmd": ["alarm", "update"],
                    "params": {"id": aid, "time": "__TAGGEDINPUT__"},
                }},
                "nextWindow": "parent",
            }, {                                                 # Jive.pm:772-786
                "text": _jive_string("ALARM_SET_DAYS"),
                "actions": {"go": {
                    "player": 0,
                    "cmd": ["jiveupdatealarmdays"],
                    "params": {"id": aid},
                }},
            }, {                                                 # Jive.pm:788-802
                "text": _jive_string("ALARM_SELECT_PLAYLIST"),
                "actions": {"go": {
                    "player": 0,
                    "cmd": ["alarm", "playlists"],
                    "params": {"id": aid, "menu": 1},
                }},
            }]
            # Shuffle mode: the alarm stores ``shufflemode`` (Alarm.pm:258-275,
            # persisted :1085/:1360); the menu radios the stored value
            # (``my $currentShuffleMode = $alarm->shufflemode;`` Jive.pm:795,
            # ``radio => ($currentShuffleMode == N) + 0`` :799/:815/:831).
            shuffle = [("SHUFFLE_OFF", 0), ("SHUFFLE_ON_SONGS", 1),
                       ("SHUFFLE_ON_ALBUMS", 2)]
            current_shuffle = int(getattr(alarm, "shufflemode", 0) or 0)
            items.append({                                       # Jive.pm:804-870
                "text": _jive_string("SHUFFLE"),
                "count": len(shuffle),
                "offset": 0,
                "item_loop": [{
                    "text": _jive_string(key),
                    "radio": 1 if mode == current_shuffle else 0,
                    "onClick": "refreshOrigin",
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["alarm", "update"],
                        "params": {"id": aid, "shufflemode": mode},
                    }},
                    "nextWindow": "refresh",
                } for key, mode in shuffle],
            })
            repeat = 1 if getattr(alarm, "repeat", False) else 0
            for key, value in (("ALARM_ALARM_REPEAT", 1),
                               ("ALARM_ALARM_ONETIME", 0)):      # :873-914
                items.append({
                    "text": _jive_string(key),
                    "radio": 1 if repeat == value else 0,
                    "onClick": "refreshOrigin",
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["alarm", "update"],
                        "params": {"id": aid, "repeat": value},
                    }},
                })
            items.append({                                       # Jive.pm:916-943
                "text": _jive_string("ALARM_DELETE"),
                "count": 2,
                "offset": 0,
                "item_loop": [
                    {
                        "text": _jive_string("CANCEL"),
                        "actions": {"go": {"player": 0,
                                           "cmd": ["jiveblankcommand"]}},
                        "nextWindow": "parent",
                    },
                    {
                        "text": _jive_string("ALARM_DELETE"),
                        "actions": {"go": {
                            "player": 0,
                            "cmd": ["alarm", "delete"],
                            "params": {"id": aid},
                        }},
                        "nextWindow": "grandparent",
                    },
                ],
            })
            return _jive_slice(items, index, quantity)

        # ── jiveupdatealarmdays (Jive.pm:925-1005) ────────────────────
        if cmd == "jiveupdatealarmdays":
            pos, tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            aid = tagged.get("id")
            alarm = self._jive_find_alarm(player, aid)
            if alarm is None:
                return {}
            days = getattr(alarm, "days", "") or ""
            items = []
            # Perl zählt 0=Sonntag..6=Samstag (Alarm.pm:116-118/189-203,
            # ALARM_DAY0..6) und schickt genau diese Zahl als ``dowAdd``/
            # ``dowDel`` zurück (Jive.pm:939-963 ``dowAdd => $day``); das
            # ``alarm update``-Kommando legt sie durch ``$alarm->day()``
            # (Commands.pm:200-207) mit derselben Zählung ab. Die Zahl wird
            # daher NICHT auf unseren Montag-first-Index umgerechnet.
            for day in range(7):
                active = 1 if _jive_perl_day(days, day) else 0
                items.append({
                    "text": _jive_string(f"ALARM_DAY{day}"),
                    "checkbox": active,                          # Jive.pm:960
                    "onClick": "refreshGrandparent",
                    "actions": {
                        "on": {"player": 0, "cmd": ["alarm", "update"],
                               "params": {"id": aid, "dowAdd": str(day)}},
                        "off": {"player": 0, "cmd": ["alarm", "update"],
                                "params": {"id": aid, "dowDel": str(day)}},
                    },
                })
            return _jive_slice(items, index, quantity)

        # ── jivealarmvolume (Jive.pm:1029-1062) ───────────────────────
        if cmd == "jivealarmvolume":
            vol = _jive_num(                                     # Client.pm:43
                _jive_client_pref(player, "alarmDefaultVolume", None), None)
            vol = vol or 50                                      # Jive.pm:1035
            return {
                "offset": 0,                                     # addResult-Zahl
                "count": 1,
                "item_loop": [{
                    "slider": 1,
                    "min": 1,
                    "max": 100,
                    "sliderIcons": "volume",
                    "initial": str(vol),       # Pref-Getter → STRING (Live "50")
                    "actions": {"do": {
                        "player": 0,
                        "cmd": ["alarm", "defaultvolume"],
                        "params": {"valtag": "volume"},
                    }},
                }],
            }

        # ── syncsettings (Jive.pm:1064-1089) ──────────────────────────
        if cmd == "syncsettings":
            others = [p for p in pm.players.values()
                      if p.mac != player.mac
                      and getattr(p, "is_player", True)]
            if not others:                       # Bug 16030: kein Sync-Partner
                return {
                    "window": {"textarea": _jive_string("SYNC_ABOUT")},
                    "count": 0,
                }
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            return _jive_slice(_jive_players_to_sync_with(pm, player),
                               index, quantity)

        # ── sleepsettings (Jive.pm:1116-1162) ─────────────────────────
        if cmd == "sleepsettings":
            pos, _tagged = _jive_params(args, ["_index", "_quantity"])
            index = _jive_num(pos.get("_index"))
            quantity = _jive_num(pos.get("_quantity"))
            remaining = int(getattr(player, "sleep_remaining", 0) or 0)
            items: list[dict] = []
            if remaining > 0:
                # currentSleepTime ist in MINUTEN (Commands.pm:2955) und
                # sleepTime ein Epochenwert (Client.pm:302-303) → Anzeige
                # int(($then-$now)/60)+1 (Jive.pm:1126-1130).
                items.append({
                    "text": _jive_str("SLEEPING_IN_X_MINUTES",
                                      int(remaining / 60) + 1),
                    "style": "itemNoAction",
                })
                items.append(_jive_sleep_hash(0))                # SLEEP_CANCEL
            # Bug 15675: „Ende des Titels“ nur bei laufendem Titel mit Dauer.
            if (getattr(player, "mode", "") == "play"
                    and int(getattr(player, "duration", 0) or 0)):
                items.append({                                   # Jive.pm:1137-1149
                    "text": _jive_string("SLEEP_AT_END_OF_SONG"),
                    "actions": {"go": {"player": 0,
                                       "cmd": ["jiveendoftracksleep"]}},
                    "nextWindow": "refresh",
                    "setSelectedIndex": 1,   # Perl-Literal → ZAHL
                })
            for minutes in (15, 30, 45, 60, 90):                 # Jive.pm:1151-1155
                items.append(_jive_sleep_hash(minutes))
            return _jive_slice(items, index, quantity)

        # ── jivepresets (Jive.pm:2490-2596) ───────────────────────────
        if cmd == "jivepresets":
            _pos, tagged = _jive_params(args, ["_index", "_quantity"])
            title = tagged.get("title")
            url = tagged.get("url")
            ptype = tagged.get("type")
            parser = tagged.get("parser")
            if tagged.get("playlist_index") is not None:         # Jive.pm:2513-2518
                track = await self._jive_playlist_track(
                    player, tagged.get("playlist_index"))
                if track is not None:
                    url, title, ptype = track[0], track[1], "audio"
            if ptype is not None and ptype != "playlist":        # Jive.pm:2521-2523
                ptype = "audio"
            if title is None or url is None:                     # Jive.pm:2524-2527
                return {}
            presets = _jive_presets_pref(player)
            items = []
            for slot in range(10):                               # Jive.pm:2530 onward
                jive_preset = slot + 1
                entry = (presets[slot]
                         if presets is not None and slot < len(presets)
                         else None)
                if entry is not None:
                    cur_text = (entry.get("text") if isinstance(entry, dict)
                                else "") or ""
                    items.append({
                        "text": _jive_str("JIVE_SET_PRESET_X", jive_preset),
                        "count": 2,
                        "offset": 0,
                        "isContextMenu": 1,
                        "item_loop": [
                            {
                                "text": _jive_string("CANCEL"),
                                "actions": {"go": {"player": 0,
                                                   "cmd": ["jiveblankcommand"]}},
                                "nextWindow": "parent",
                            },
                            {
                                "text": _jive_str("JIVE_OVERWRITE_PRESET_X",
                                                  cur_text),
                                "actions": _jive_preset_actions(
                                    player, jive_preset, title, url, ptype, parser),
                                "nextWindow": "presets",
                            },
                        ],
                    })
                else:
                    items.append({
                        "text": _jive_str("JIVE_SET_PRESET_X", jive_preset),
                        "actions": _jive_preset_actions(
                            player, jive_preset, title, url, ptype, parser),
                        "nextWindow": "presets",
                    })
            # addResult-Literale: offset 0 und count 10 als ZAHLEN.
            return {"offset": 0, "count": len(items), "item_loop": items}

        # ── jivefavorites (Jive.pm:2601-2690) ─────────────────────────
        if cmd == "jivefavorites":
            pos, tagged = _jive_params(args, ["_cmd"])
            command = pos.get("_cmd")
            if command == "set_preset":
                # Jive.pm:2615-2655: Preset auf dem Client ablegen
                preset = _jive_num(tagged.get("key"), 0) or 0
                if preset == 0:
                    preset = 10                                  # Jive.pm:2617-2619
                title = tagged.get("favorites_title")
                url = tagged.get("favorites_url")
                ptype = tagged.get("favorites_type")
                parser = tagged.get("parser")
                if tagged.get("playlist_index") is not None:
                    track = await self._jive_playlist_track(
                        player, tagged.get("playlist_index"))
                    if track is not None:
                        url, title, ptype = track[0], track[1], "audio"
                if ptype is not None and ptype != "playlist":
                    ptype = "audio"
                if title is None or url is None:
                    return {}
                self._jive_set_preset(player, preset, title, url, ptype, parser)
                self._popup = {"jive": {
                    "type": "popupplay",
                    "text": [_jive_str("PRESET_ADDING", preset), title],
                }}
                self._popup_expires = time.time() + 5
                return {}
            # Jive.pm:2657-2687: Bestätigungs-Menü ADD/DELETE
            title = tagged.get("title")
            url = tagged.get("url")
            ptype = tagged.get("type")
            parser = tagged.get("parser")
            token = str(command or "").upper()
            params: dict = {"title": title, "url": url, "type": ptype,
                            "parser": parser}
            if tagged.get("icon"):
                params["icon"] = tagged["icon"]                  # Jive.pm:2678
            if tagged.get("item_id") is not None:
                params["item_id"] = tagged["item_id"]            # Jive.pm:2679
            return {
                "offset": 0,
                "count": 2,
                "item_loop": [
                    {
                        "text": _jive_string("CANCEL"),
                        "actions": {"go": {"player": 0,
                                           "cmd": ["jiveblankcommand"]}},
                        "nextWindow": "parent",
                    },
                    {
                        "text": f"{_jive_string(token)} {title}",
                        "actions": {"go": {
                            "player": 0,
                            "cmd": ["favorites", command],
                            "params": params,
                        }},
                        "nextWindow": "grandparent",
                    },
                ],
            }

        # ── jiveplaylists (Jive.pm:2412-2457) ─────────────────────────
        if cmd == "jiveplaylists":
            pos, tagged = _jive_params(args, ["_cmd"])
            command = pos.get("_cmd")
            token = str(command or "").upper()
            return {
                "offset": 0,
                "count": 2,
                "item_loop": [
                    {
                        "text": _jive_string("CANCEL"),
                        "actions": {"go": {"player": 0,
                                           "cmd": ["jiveblankcommand"]}},
                        "nextWindow": "parent",
                    },
                    {
                        "text": f"{_jive_string(token)} {tagged.get('title')}",
                        "actions": {"go": {
                            "player": 0,
                            "cmd": ["playlists", "delete"],
                            "params": {
                                "playlist_id": tagged.get("playlist_id"),
                                "title": tagged.get("title"),
                                "url": tagged.get("url"),
                            },
                        }},
                        "nextWindow": "grandparent",
                    },
                ],
            }

        return {}

    # ------------------------------------------------------------------
    # Jive-Aktionen (Alarm-Snooze/Stop, Sleep am Titelende, Sync)
    # ------------------------------------------------------------------

    @staticmethod
    def _jive_find_alarm(player, aid):
        """``Slim::Utils::Alarm->getAlarm($client, $id)`` (Alarm.pm:1298-1306).

        Jive/SqueezePlay reicht die Alarm-``id`` durch; unsere Alarme sind mit
        dem Index adressiert (``alarms.py``), den ``getCurrentAlarms`` als
        ``id`` ausgibt (auch in der ``<idx>-``-Form der Clients).
        """
        if aid is None:
            return None
        try:
            idx = int(str(aid).split("-")[0])
        except (TypeError, ValueError):
            return None
        from lyrion.alarms import AlarmManager
        return AlarmManager().get(getattr(player, "mac", "") or "", idx)

    async def _jive_playlist_track(self, player, playlist_index):
        """``Slim::Player::Playlist::track($client, $index)`` (Jive.pm:2513-2517).

        Liefert ``(url, title)`` des Titels an dieser Wiedergabelisten-Position
        oder ``None``, wenn wir ihn nicht auflösen können.
        """
        try:
            idx = int(str(playlist_index).split("-")[0])
        except (TypeError, ValueError):
            return None
        playlist = list(getattr(player, "playlist", None) or [])
        if idx < 0 or idx >= len(playlist):
            return None
        try:
            info = await self._load_tracks([playlist[idx]])
        except Exception:                      # noqa: BLE001 — ohne DB kein Titel
            return None
        track = info.get(playlist[idx]) or {}
        url = track.get("url") or ""
        if not url:
            return None
        return url, (track.get("title") or "")

    def _jive_set_preset(self, player, slot: int, title, url, ptype,
                         parser) -> None:
        """``$client->setPreset({slot, URL, text, type, parser})`` (Client.pm:1323-1340).

        Perl legt den Eintrag in ``presets->[slot-1]`` ab (Schlüssel URL/text/
        type, ``parser`` nur wenn gesetzt). Unser Player-Pref-Store ist
        ``playerprefs``; die Wiedergabe eines Presets (Perl: Preset-Taste
        startet die URL) hat unser Server nicht — die Ablage wirkt nur auf
        dieses Menü (UNKLAR).
        """
        from lyrion.player.playerprefs import apply_player_pref

        presets = _jive_presets_pref(player)
        presets_list: list[Any] = list(presets) if presets is not None else [None] * 10
        while len(presets_list) < 11:
            presets_list.append(None)
        preset: dict[str, Any] = {
            "URL": url,
            "text": title,
            "type": ptype or "audio",                            # Client.pm:1331
        }
        if parser:
            preset["parser"] = parser                            # Client.pm:1333
        presets_list[slot - 1] = preset
        apply_player_pref(player, "presets", presets_list)

    def _jive_alarm_action(self, pm, pid: str | None, args: list) -> None:
        """``jiveAlarmCommand`` (Jive.pm:2459-2488) — Snooze/Stop/Fadein.

        Perl: ``my $alarm = Slim::Utils::Alarm->getCurrentAlarm($client)``
        (:2471) — the alarm currently sounding (``alarmData->{currentAlarm}``,
        Alarm.pm:1241-1246) — then ``$alarm->snooze()`` when the ``snooze`` tag
        is truthy (:2474-2475) else ``$alarm->stop($continueAudio)`` when
        ``stop`` is (:2476-2477). Both are no-ops without a current alarm
        (Alarm.pm:741/868); the snooze length is the client pref
        ``alarmSnoozeSeconds`` (Alarm.pm:750, default 540 — Client.pm:44).
        ``fadein`` is a client pref (``alarmfadeseconds``) and is stored.
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None
        if player is None:
            return
        _, tagged = _jive_params(args, [])
        from lyrion.alarms import DEFAULT_SNOOZE_SECONDS, AlarmManager

        mgr = AlarmManager()
        if tagged.get("snooze"):            # Perl: getParam('snooze') ? 1 : undef
            seconds = _jive_num(            # Client.pm:44 alarmSnoozeSeconds
                _jive_client_pref(player, "alarmSnoozeSeconds", None),
                DEFAULT_SNOOZE_SECONDS)
            mgr.snooze(player.mac, seconds)
        elif tagged.get("stop"):            # Perl: elsif (defined $stop)
            mgr.stop(player.mac, bool(tagged.get("continueAudio")))
        if tagged.get("fadein") is not None:
            from lyrion.player.playerprefs import apply_player_pref
            apply_player_pref(player, "alarmfadeseconds", tagged["fadein"])

    async def _jive_end_of_track_sleep(self, pm, pid: str | None) -> None:
        """``endOfTrackSleepCommand`` (Jive.pm:1091-1114).

        Läuft ein Titel, setzt Perl den Sleep-Timer auf die Restlaufzeit
        (``$client->execute(['sleep', $remaining])`` → unser
        ``player.sleep_remaining``, cli_commands.py:1602-1631). Sonst zeigt es
        den Popup ``NOTHING_CURRENTLY_PLAYING`` (``showBriefly`` mit
        jive-Typ ``popupplay``).
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None
        if player is None:
            return
        if getattr(player, "mode", "") == "play":
            duration = float(getattr(player, "duration", 0) or 0)
            elapsed = float(getattr(player, "elapsed", 0) or 0)
            player.sleep_remaining = int(duration - elapsed)
        else:
            self._popup = {"jive": {
                "type": "popupplay",
                "text": [_jive_string("NOTHING_CURRENTLY_PLAYING")],
            }}
            self._popup_expires = time.time() + 5

    def _jive_sync_action(self, pm, pid: str | None, args: list) -> None:
        """``jiveSyncCommand`` (Jive.pm:2088-2131).

        Erst unsyncen (``unsyncWith``, Perl ``sync -`` auf dem Client), dann
        syncen: Perl lässt den *anderen* Client ``sync <unsere id>`` ausführen
        — unser ``PlayerManager.sync_players(master, [slave])`` (manager.py:640)
        bildet genau das ab. Abschließend der Popup-Text aus
        ``UNSYNCING_FROM``/``SYNCING_WITH`` (mit ``\n`` verbunden).
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None
        if player is None:
            return
        _, tagged = _jive_params(args, [])
        messages: list[str] = []
        # Perl: $request->getParam('unsyncWith') || undef → "0" ist falsy.
        unsync_with = tagged.get("unsyncWith")
        if unsync_with and str(unsync_with) != "0":
            pm.unsync_player(player.mac)
            messages.append(_jive_str("UNSYNCING_FROM", unsync_with))
        sync_with = tagged.get("syncWith")
        if sync_with and str(sync_with) != "0":
            other = pm.get_player(str(sync_with))
            if other is not None:
                pm.sync_players(player.mac, [other.mac])
                messages.append(_jive_str("SYNCING_WITH",
                                          tagged.get("syncWithString")))
        self._popup = {"jive": {
            "type": "popupplay",
            "text": ["\n".join(messages)],
        }}
        self._popup_expires = time.time() + 5

    def _jive_brightness_items(self, player) -> list[dict]:
        """Menüpunkte von ``playerBrightnessMenu`` (Jive.pm:1774-1838).

        Drei Pref-Gruppen (While Active / While Off / Idle), jede mit den
        Helligkeits-Optionen des Displays als Radio-Loop; Boom bekommt
        zusätzlich die beiden Automatik-Slider (Jive.pm:1826-1832).
        """
        model = getattr(player, "model", "") or ""
        display = _jive_display_class(model)
        options = _jive_brightness_options(model)
        defaults = _JIVE_BRIGHTNESS_DEFAULTS.get(
            display, _JIVE_BRIGHTNESS_DEFAULTS["none"])

        items: list[dict] = []
        for pref, key in (                                  # Jive.pm:1782-1795
            ("powerOnBrightness", "SETUP_POWERONBRIGHTNESS_ABBR"),
            ("powerOffBrightness", "SETUP_POWEROFFBRIGHTNESS_ABBR"),
            ("idleBrightness", "SETUP_IDLEBRIGHTNESS_ABBR"),
        ):
            cur = _jive_num(_jive_client_pref(player, pref, None), defaults.get(pref))
            radios = [
                {
                    "text": options[level],
                    "radio": 1 if cur == level else 0,      # Jive.pm:1806
                    "actions": {"do": {
                        "player": 0,
                        # Jive.pm:1809 cmd => ['playerpref',$pref,$setting] — $setting
                        # ist der HASH-KEY der Optionsliste, in Perl also ein
                        # STRING (Live-Probe: "powerOnBrightness","4").
                        "cmd": ["playerpref", pref, str(level)],
                    }},
                }
                for level in sorted(options, reverse=True)   # Jive.pm:1803
            ]
            items.append({
                "text": _jive_string(key),
                "count": len(radios),
                "offset": 0,                                 # Hash-Literal → Zahl
                "item_loop": radios,
            })

        if display == "boom":                                # Jive.pm:1826-1832
            mab = _jive_num(_jive_client_pref(player, "minAutoBrightness", None), 2)
            sab = _jive_num(_jive_client_pref(player, "sensAutoBrightness", None), 10)
            # Boom.pm:57-58 minAutoBrightness 2 / sensAutoBrightness 10
            items.append(_jive_brightness_slider(
                "minAutoBrightness", "SETUP_MINAUTOBRIGHTNESS", 1, 5, mab))
            items.append(_jive_brightness_slider(
                "sensAutoBrightness", "SETUP_SENSAUTOBRIGHTNESS", 1, 20, sab))

        return items

    async def _rescan(self, mode: str = "full") -> Any:
        """Direct rescan — starts the library scan like the ``rescan`` command.

        ``mode`` mirrors the LMS rescan modes (Commands.pm rescanCommand):
        the default full rescan reconciles deletions; other modes are
        additive refreshes that never delete.  Uses the very same wiring the
        CLI command uses (:mod:`lyrion.control.rescan`), so a scan that is
        already running queues this one instead of racing it
        (Commands.pm:2712-2720) and a failure is logged with its traceback.
        """
        from lyrion.control import rescan as scan

        state = scan.request_scan(mode)
        return {"status": f"rescan {state}", "mode": scan.normalise_mode(mode)}

    async def _abortscan(self) -> Any:
        """Direct abortscan — stops the running library scan (Perl parity)."""
        from lyrion.media.scan_state import SCAN_STATE
        SCAN_STATE.request_abort()
        return {"status": "abort requested"}

    # ─────────────────────────────────────────────────────────────
    # slim.request JSON helpers
    # ─────────────────────────────────────────────────────────────

    async def _album_replaygain(self, album_id: int):
        """(replay_gain, replay_peak) eines Albums aus der DB (oder None)."""
        try:
            from lyrion.database.sqlite_helper import db_session
            from sqlalchemy import text as _sql_text

            async with db_session() as session:
                row = (await session.execute(
                    _sql_text("SELECT replay_gain, replay_peak FROM albums "
                              "WHERE id = :aid"), {"aid": int(album_id)},
                )).fetchone()
                if row and (row[0] is not None or row[1] is not None):
                    return row[0], row[1]
        except Exception as exc:  # noqa: BLE001
            logger.debug("album replaygain lookup failed: %s", exc)
        return None

    async def _json_player_status(self, pm, pid: str | None, args: list[str]) -> dict:
        """Build a player status dict (LMS 'status' command)."""
        from lyrion.player.manager import PlayerManager
        pm = pm or PlayerManager()
        if not pid:
            players = pm.get_all_players()
            pid = players[0].mac if players else None
        player = pm.get_player(pid) if pid else None
        if player is None:
            return {"mode": "stop", "power": 0, "player_name": "", "playlist_tracks": 0}

        # Track metadata from DB for the playlist loop (local tracks only).
        # A playlist entry is local when it is an int — or a digit-only
        # string (CLI/plugin paths hand us "51994"; a bare number is never
        # a stream URL, so the coercion is safe). Without it a numeric
        # string was mistaken for a radio URL: title = "51994", duration 0,
        # no artwork, no params.track_id — the exact SqueezePlay symptoms
        # (progress bar at maximum, no title switch).
        def _local_id(entry: object) -> int | None:
            """Return the DB track id for a local playlist entry, else None."""
            if isinstance(entry, bool):
                return None
            if isinstance(entry, int):
                return entry
            text = str(entry)
            return int(text) if text.isdigit() else None

        loop = []
        playlist_ids = getattr(player, "playlist", []) or []
        int_ids = [x for x in (_local_id(e) for e in playlist_ids) if x is not None]
        track_rows = await self._load_tracks(int_ids) if int_ids else {}

        # tags:<code> — songinfo letter codes (lyrion.org CLI docs).
        # Each returned tag is identified by a letter; the default tags
        # value for status is 'gald'. Without a tags parameter we return
        # all available fields (Web UI/SqueezeTray rely on that).
        TAG_FIELDS: dict[str, str] = {
            "a": "artist", "A": "artist", "s": "artist_id", "S": "artist_id",
            "l": "album", "e": "album_id",
            "d": "duration", "y": "year", "t": "tracknum", "u": "url",
            "r": "bitrate", "T": "samplerate", "I": "samplesize",
            "x": "remote", "g": "genre", "p": "genre_id",
            "c": "coverid", "j": "coverart", "J": "artwork_track_id",
            "K": "artwork_url", "i": "disc", "N": "remote_title",
            "o": "type", "f": "filesize", "k": "comment", "w": "lyrics",
        }
        tags = next((str(a)[5:] for a in (args or []) if str(a).startswith("tags:")), "")
        # 'title' has no letter code (always returned by songinfo).
        def tag_ok(code: str) -> bool:  # noqa: N802
            return (not tags) or code in tags

        # Pre-compute values for enriching the CURRENT playlist item (SqueezePlay
        # Now-Playing renders from it). Computed here (before the loop) because
        # cur_info/elapsed are derived later in this function.
        _cur = player.playlist_position or 0
        _cur_tid = playlist_ids[_cur] if 0 <= _cur < len(playlist_ids) else None
        _cur_title = getattr(player, "current_title", "") or ""
        _cur_artist = ""
        _cur_track = _cur_title
        if _cur_tid is not None and _local_id(_cur_tid) is None and " - " in _cur_title:
            _artist, _track = _cur_title.split(" - ", 1)
            _cur_artist = _artist.strip()
            _cur_track = _track.strip()

        # Perl's ``remote_title`` for a URL stream — the station/entry title
        # (``$parentTrack->title`` resp. ``$track->title``, _songData
        # Queries.pm:5968-5981). Our store for it is ``remote_media.name``
        # (the radio row the importer writes), the player's ``stream_titles``
        # map, else the URL host (this file's earlier stand-in). Perl feeds it
        # in as the stream's ALBUM when there is no album metadata
        # (_addJiveSong Queries.pm:5611-5615: ``$album = $remote_title``) and
        # Squeezer renders that ``album`` as the second Now-Playing line
        # (Song.java:75 + CurrentTrack.java:54-60).
        station_title = ""
        if _cur_tid is not None and _local_id(_cur_tid) is None:
            _cur_url = str(getattr(player, "current_url", "") or _cur_tid)
            station_title = (getattr(player, "stream_titles", {}) or {}).get(
                _cur_url, "") or ""
            if not station_title:
                try:
                    _rows = _db_query(
                        "SELECT name FROM remote_media WHERE url = ? LIMIT 1",
                        (_cur_url,))
                    station_title = (_rows[0]["name"] if _rows else "") or ""
                except Exception:  # noqa: BLE001
                    station_title = ""
            if not station_title:
                # Perl's ``$track->title`` for a bare URL stream (what the
                # playlist entry carries): our stand-in is the same URL host
                # the item title uses below.
                try:
                    from urllib.parse import urlparse
                    station_title = (urlparse(_cur_url).hostname or "").replace(
                        "www.", "")
                except Exception:  # noqa: BLE001
                    station_title = ""

        def np_text(title: str, artist: str, album: str) -> str:
            """Perl ``_addJiveSong``'s multi-line NP text (Queries.pm:5603-5620).

            ``@secondLine = artist, album`` joined with ' - ' and appended to
            the title as ``$text = $title . "\\n" . $secondLine`` (:5620).
            Squeezer splits that text on '\\n' into ``name``/``text2``
            (JiveItem.java:498-508); ``CurrentTrack.name`` is the title line,
            ``CurrentTrack.text2()``/``album()`` (CurrentTrack.java:54-60) use
            the second line — the "artist - album" line the app shows.
            """
            return title + "\n" + " - ".join(p for p in (artist, album) if p)

        def _artwork_id(inf: dict) -> object | None:
            """The id our ``/music/<id>/cover.jpg`` route accepts for a track.

            Perl publishes ``tracks.coverid`` / ``albums.artwork`` there
            (``Slim/Control/Queries.pm:5713-5731`` tag map c/J, ``:5789-5791``
            colMap; live Perl ``songinfo … tags:KJcj`` on a track with art:
            ``{"artwork_track_id": "232bde3b"}`` ``{"coverid": "232bde3b"}``
            ``{"coverart": "1"}``).  Our importer never fills the schema's
            ``tracks.cover``/``coverid`` columns, so the ALBUM id — the value
            every other cover field in this port already uses
            (``_load_tracks`` sets ``artwork_url = /music/<album_id>/cover.jpg``)
            — is the id we publish.  ``None`` = the album has no artwork, and
            then Perl omits the fields too (``addResultLoopIfValueDefined``,
            ``:864``).
            """
            if inf.get("artwork_url") and inf.get("album_id"):
                return inf["album_id"]
            cover = inf.get("cover")
            if cover not in (None, "", 0, "0"):
                return cover
            return None

        # Welche Playlist-Einträge RemoteTracks sind (`$track->remote`): wird
        # im Menü-Zweig gebraucht (Queries.pm:5576) und darf NICHT als Feld im
        # `playlist_loop`-Item landen (live 192.168.1.90 hat dort kein
        # `trackType`).
        _remote_flags: list[bool] = []

        for i, tid in enumerate(playlist_ids):
            tid_local = _local_id(tid)
            _remote_flags.append(tid_local is None)
            _simg = ""
            if tid_local is not None:
                info = track_rows.get(tid_local, {})
                title = info.get("title", "Unknown")
                url = info.get("url", "")
                duration = info.get("duration", 0) or 0
            else:
                # Remote stream URL (radio) — title from the URL host
                title = str(tid)
                url = str(tid)
                duration = 0
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    if host:
                        title = host.replace("www.", "")
                except Exception:
                    pass
                info = {"remote": 1}
            item: dict = {
                # Perl `_addSong` → `$returnHash{'id'} = $track->id`
                # (Queries.pm:5971/_songData:5880): a DB integer for a local
                # track, and for a REMOTE track the negative RemoteTrack id
                # (RemoteTrack.pm:317).  Never the URL string — a client that
                # parses the field as a number (Squeeze Client crashed on the
                # player-preview zoom) cannot read it.
                "id": (tid_local if tid_local is not None
                       else _remote_track_id(tid)),
                "playlist index": i,
            }
            # title is always present. Perl's ``playlist_loop`` (the NON-menu
            # _addSong shape, Queries.pm:4353) carries NEITHER ``text`` NOR
            # ``trackType`` — live 192.168.1.90 `status 0 2 tags:d|gald|galdu`
            # answered a remote stream item with exactly {id,title,duration,
            # artist,url,playlist index}. Those two fields belong to the
            # MENU shape (_addJiveSong, Queries.pm:5576/:5620), which the
            # menu branch below builds into ``item_loop``.
            item["title"] = title
            # SqueezePlay now-playing reads _track.track/.artist/.album —
            # provide them for LOCAL tracks (a remote stream intentionally
            # omits `track` so SqueezePlay falls back to text + current_title).
            if tid_local is not None:
                item["track"] = title
                item["artist"] = info.get("artist", "")
                item["album"] = info.get("album", "")
                item["duration"] = duration
            else:
                # Perl's remote-track field set (`_songData`, Queries.pm:
                # 5940-6120 over a RemoteTrack — live Perl 9.1.1 answered a
                # stream item with exactly `id`, `title`, `url`, `remote`,
                # `coverid`, `artwork_url`): the artwork fields are NOT
                # optional there.  `coverid` is the RemoteTrack id (the same
                # negative number as `id`, RemoteTrack.pm:317 → live
                # `"coverid": "-94115161401160"`) and `artwork_url` is
                # `$track->coverurl`: the stored station logo, else the
                # skin-relative default `html/images/radio.png`.
                _simg = str(getattr(player, "stream_images", {}).get(str(tid), "")
                            or "")
                if _simg.startswith("/"):
                    # Perl's field value is skin-relative (no leading slash);
                    # SqueezePlay then asks for /html/EN/<value> (the skin
                    # base), Squeeze Client for the origin + "/" + value — both
                    # resolve against the fixed static resolver.
                    _simg = _simg[1:]
                item["coverid"] = item["id"]
                item["artwork_url"] = _simg or REMOTE_ART_FALLBACK
                if tag_ok("x"):
                    # Perl emits `remote` only for tag x (:5994-5996), but the
                    # ARTWORK fields above stay unconditional: this port's
                    # clients read them without listing the tag letter (the
                    # crash class this change fixes), and a missing field is
                    # what kills them.
                    item["remote"] = 1
                if i == player.playlist_position:
                    # Enrich the CURRENT stream item so SqueezePlay's
                    # Now-Playing has artist/duration/url to render.
                    item["url"] = str(tid)
                    item["track"] = _cur_track or item.get("title", "")
                    item["artist"] = _cur_artist or ""
                    item["album"] = ""
                # Perl publishes a RemoteTrack's `duration` as a STRING: live
                # 192.168.1.90 `status 0 2 tags:galdu` → `"duration": "0"`
                # (RemoteTrack keeps it in a text column, Queries.pm:5920
                # passes it through unchanged). A live stream has no duration
                # — the port used to put the PLAYING TIME (`_elapsed`) there,
                # which Perl never sends.
                item["duration"] = "0"
            for code, field in TAG_FIELDS.items():
                if not tag_ok(code):
                    continue
                if code in ("c", "j", "J"):
                    # coverid / coverart / artwork_track_id — the three fields
                    # Perl fills from the DB (Queries.pm:5713-5731 tag map,
                    # live `songinfo … tags:KJcj`: artwork_track_id + coverid +
                    # coverart "1").  Keyed on the TAG letter, not on the
                    # field name: the previous check compared the field
                    # against "cover", a name no TAG_FIELDS entry uses — so
                    # every local row silently lost its cover id.
                    # The value does NOT come from ``info["cover"]`` either:
                    # our importer never fills the schema's ``tracks.cover``
                    # column, so the album-artwork id is derived instead (see
                    # _artwork_id).
                    art_id = _artwork_id(info)
                    if art_id is not None:
                        if tag_ok("c"):
                            item["coverid"] = art_id
                        if tag_ok("j"):
                            item["coverart"] = 1
                        if tag_ok("J"):
                            item["artwork_track_id"] = art_id
                    elif tag_ok("j"):
                        # Perl: `'j' => sub { $c->{'tracks.cover'} ? 1 : 0 }`
                        # (:5782) — coverart is present for every row; 1 only
                        # for a local track whose album carries artwork, and
                        # "0" for a remote stream (live Perl status of a
                        # stream: `"coverart": "0"`).  A client that reads it
                        # to decide between cover art and the default icon
                        # needs the field, not its absence.
                        item["coverart"] = 0
                    continue
                value = info.get(field)
                if value is None or value == "":
                    continue
                if field == "remote":
                    item["remote"] = 1 if value else 0
                else:
                    item[field] = value
            # Jive item params (Perl _addJiveSong, Queries.pm:5637-5641):
            # SqueezePlay's Now-Playing reads params.track_id
            # (Player.lua:269-282, NowPlayingApplet.lua:117) — without it the
            # "current title" screen never follows the playing track.
            params: dict = {"playlist_index": i}
            if tid_local is not None:
                params["track_id"] = tid_local
                if info.get("album_id"):
                    params["album_id"] = info["album_id"]
                if info.get("artist_id"):
                    params["artist_id"] = info["artist_id"]
            else:
                # Perl: 'track_id' => ($songData->{'id'} + 0) — a URL
                # stringifies to 0, so a remote item carries track_id 0.
                params["track_id"] = 0
            item["params"] = params
            # Perl marks every playable jive item as style 'itemplay'.
            item["style"] = "itemplay"
            # Artwork for the JIVE clients (Perl _addJiveSong, Queries.pm:
            # 5618-5633): artwork_url -> 'icon', else coverid/artwork_track_id
            # -> 'icon-id', else the radio placeholder for a cover-less remote
            # item.  SqueezePlay reads both (Player.lua:282), Squeezer reads
            # icon-id first (JiveItem.java:250).
            if tid_local is None:
                if _simg:
                    item["icon"] = _simg
                else:
                    # "send radio placeholder art for remote tracks with no
                    # art" — Queries.pm:5633, Perl's literal path.
                    item["icon-id"] = RADIO_PLACEHOLDER_ICON
            else:
                _art = info.get("artwork_url") or ""
                _aid = _artwork_id(info)
                if _art:
                    # A defined ``artwork_url`` wins in Perl (Queries.pm:
                    # 5625-5627 sends it as 'icon' before looking at the ids).
                    item["icon"] = _art
                elif _aid is not None:
                    # Jive builds '/music/' .. iconId .. '/cover' .. size from
                    # `icon-id`/`icon` (SlimServer.lua:1189) — a URL here would
                    # be concatenated into garbage and the cover never loads.
                    item["icon-id"] = str(_aid)
                    item["icon"] = f"music/{_aid}/cover"
            loop.append(item)

        # A negative/absent position must never index the list (-1 would hit
        # the LAST entry, an empty playlist raises IndexError). A stopped
        # player with an empty queue is the normal case after the client
        # starts — it must still answer with a valid status.
        cur = player.playlist_position if player.playlist_position is not None else 0
        cur_valid = 0 <= cur < len(playlist_ids)
        cur_local = _local_id(playlist_ids[cur]) if cur_valid else None
        if cur_local is not None:
            cur_info = track_rows.get(cur_local, {})
        else:
            cur_info = {}
        if cur_valid and cur_local is None:
            # Radio stream: title = station name (current_title if set,
            # else host) — never the full URL.
            url_str = str(playlist_ids[cur])
            try:
                from urllib.parse import urlparse
                host = urlparse(url_str).hostname or url_str
                title = host.replace("www.", "")
            except Exception:
                title = url_str
            cur_info = {"title": title, "url": url_str}
        # Only a remote stream's StreamTitle may override the now-playing
        # line — a local track's title comes from the DB row (a stale radio
        # StreamTitle must not linger over local tracks).
        if (getattr(player, "current_title", "")
                and cur_valid
                and cur_local is None):
            cur_info["title"] = player.current_title

        # Playback progress. Perl's Slim::Player::Source::songTime() keeps
        # the position while paused (StreamingController::playingSongElapsed
        # returns resumeTime) and reports 0 (int) when stopped
        # (Squeezebox2::songElapsedSeconds returns 0 if isStopped, else
        # elapsedMilliseconds/1000 — a fractional value). SqueezePlay must
        # not reset the progress bar on pause.
        elapsed = float(getattr(player, "elapsed", 0) or 0)
        if player.mode == "stop":
            elapsed = 0

        # remoteMeta: Perl's statusQuery adds `_songData($request, $track,
        # $tags)` for a non-local remote URL (Queries.pm:4385-4391
        # `if ( _notLocalTrackAndRemoteUrl($track) ) { my $metadata =
        # _songData($request, $track, $tags);
        # $request->addResult('remoteMeta', $metadata); }`): the SAME tag-driven
        # field set as the playlist item, in the SAME order — `_songData` ties
        # its result hash to Tie::IxHash (:5898-5900), so the insertion order IS
        # the tag order.
        #
        # Effective tag set (Queries.pm:4012 `my $tags =
        # $request->getParam('tags') || '';` + :4356-4364): menuMode forces
        # 'aAlKNcxJ', otherwise the `tags:` param verbatim — the
        # `$tags = 'gald' if !defined $tags` fallback (:4363) never fires
        # because `|| ''` already made $tags defined.  Live Perl 9.1.1:
        #   `status - 1`                             -> {id,title}
        #   `status - 1 tags:gald`                   -> {id,title,artist,duration}
        #   `status - 1 tags:d`                      -> {id,title,"duration":"0"}
        #   `status - 10 menu:menu useContextMenu:1` -> {id,title,artist,
        #        artwork_url,remote_title,coverid,remote}
        #   `status - 1 tags:ABCDEKJZlcuxyrtSgad`    -> {id,title,artist,
        #        addedTime,artwork_url,coverid,url,remote,year,bitrate,duration}
        # A value is published when it is defined AND non-empty (:6104-6108) —
        # `0` counts as a value; `id`/`title` always lead (:5970-5971).
        remote_meta = {}
        cur_url = getattr(player, "current_url", None)
        if cur_url and cur_local is None:
            # Station logo if one is stored (playlist add image:<path>), else
            # the skin-relative default Perl's `$track->coverurl` yields
            # (html/images/radio.png / favorites.png — data of the stored
            # entry, not a constant).
            _stream_img = getattr(player, "stream_images", {}).get(cur_url, "") or ""
            if _stream_img.startswith("html/"):
                _stream_img = _stream_img[len("html/"):]
            # Live Perl sends the stream duration as a STRING ("0", "7").
            _rm_duration = cur_info.get("duration", 0) or 0
            try:
                if float(_rm_duration).is_integer():
                    _rm_duration = str(int(float(_rm_duration)))
                else:
                    _rm_duration = str(_rm_duration)
            except (TypeError, ValueError):
                _rm_duration = str(_rm_duration)
            _remote_values: dict[str, Any] = {
                "a": _cur_artist or cur_info.get("artist", ""),
                "A": _cur_artist or cur_info.get("artist", ""),
                "l": cur_info.get("album", ""),
                # `$remoteMeta->{d} = ($remoteMeta->{duration} ||
                # $remoteMeta->{secs} || 0) + 0` (:5934)
                "d": _rm_duration,
                # `$remoteMeta->{D}` is never set for a stream, so tag 'D'
                # falls through to `$track->addedTime` (:5937, :6100-6102) —
                # which for a RemoteTrack is the QUERY time (Track.pm:325-341).
                "D": _perl_added_time(),
                # `$remoteMeta->{r} = $remoteMeta->{bitrate}` (:5937): the ICY
                # bitrate the stream handler read (HTTP.pm:731-734).  Without a
                # known bitrate Perl's `$track->prettyBitRate` answers 0
                # (Track.pm:353-363).
                "r": _perl_pretty_bitrate(
                    getattr(player, "stream_bitrate", 0) or 0),
                "u": cur_url,
                "x": 1,
                "y": "0",      # `$remoteMeta->{y}` = handler year; live "0"
                "c": _remote_track_id(cur_url),
                "K": _stream_img or REMOTE_ART_FALLBACK,
                # Tag 'j' → `$track->coverArtExists`; a RemoteTrack answers a
                # hard 0 (``Slim/Schema/RemoteTrack.pm`` ``sub coverArtExists
                # {0}``) — live Perl ``status - 1 tags:…j…`` on a stream:
                # ``"coverart": "0"`` (a STRING), artwork present or not.
                "j": "0",
                "N": station_title,
            }
            _meta: dict[str, Any] = {
                "id": _remote_track_id(cur_url),                   # :5970
                "title": _cur_track or cur_info.get("title", ""),  # :5971
            }
            _seen_tags: set[str] = set()
            for _code in ("aAlKNcxJ" if "menu:menu" in (args or [])
                          else str(tags or "")):
                if _code in _seen_tags:     # `next if $seen{$tag}++` (:5966)
                    continue
                _seen_tags.add(_code)
                _key = _REMOTE_TAG_KEYS.get(_code)
                if _key is None or _key in _meta:
                    continue
                _value = _remote_values.get(_code)
                if _value is None or _value == "":
                    continue
                _meta[_key] = _value
            remote_meta = _meta

        menu_block = None
        if "menu:menu" in (args or []):
            # Squeezer's parseMenuStatus expects 'menu' to be the item
            # ARRAY directly ((Object[]) record.get("menu")) — not an
            # object with item_loop.
            menu_block = self._home_menu(player)

        # P4-1: sync fields (only when synced) + optional status fields
        sync_fields: dict[str, str] = {}
        if getattr(player, "sync_master", None):
            sync_fields["sync_master"] = player.sync_master
        if getattr(player, "sync_slaves", None):
            sync_fields["sync_slaves"] = ",".join(player.sync_slaves)

        # Perl returns the requested window, and its FIRST item is the
        # playing track for `status - <qty>`: SqueezePlay's _whatsPlaying
        # reads item_loop[1], Material's server.js:606 / SqueezeJS Base.js:389
        # read playlist_loop[0] as the current song, and the Material queue
        # pages through `status <start> <count>` (lmsList). Each item keeps
        # its absolute "playlist index" so the client can map window → queue.
        # (Perl: normalize($index, $quantity, $songCount), Queries.pm:4425.)
        win_start, win_end = self._status_window(args, int(cur), len(playlist_ids))

        # Perl: `my $menuMode = defined $menu;` (Queries.pm:4013) — menu:menu
        # schaltet item_loop/count/offset/preset_loop zu (Request.pm-Param).
        menu_mode = "menu:menu" in (args or [])
        window_loop = loop[win_start:win_end + 1] if win_start <= win_end else []
        window_remote = _remote_flags[win_start:win_end + 1] if win_start <= win_end else []
        item_loop = window_loop
        if menu_mode:
            # Perl's MENU mode builds its own loop through _addJiveSong
            # (Queries.pm:4411/:4416 into ``item_loop``, tags forced to
            # 'aAlKNcxJ' :4358) — NOT through _addSong. Every item carries the
            # discrete NP fields ``track``/``album``/``artist`` (:5637-5651),
            # the multi-line ``text`` (:5620/:5653) and ``trackType``
            # radio|local (:5576). Squeezer's parsePlayerStatus reads
            # messageData["item_loop"][0] (CometClient.java:419-426) and its
            # Song takes the title from ``track``, the artist from ``artist``
            # and the album from ``album`` (Song.java:63-75,
            # CurrentTrack.java:33) — precisely the fields that were missing
            # for a hirschmilch.de radio stream ("Unknown artist/album").
            np_loop: list[dict] = []
            for it, is_remote in zip(item_loop, window_remote):
                np_it = dict(it)
                line1 = it.get("title") or ""
                # `is_remote` = `$track->remote` (Perl Queries.pm:5576) — siehe
                # `_remote_flags`; das Item selbst führt das Feld nicht mehr.
                if is_remote and it.get("playlist index") == int(cur):
                    np_track = _cur_track or line1
                    np_artist = _cur_artist
                    # Perl's guard for the station-as-album substitution
                    # (_addJiveSong :5613): only when the station title
                    # differs from the track title and no album metadata
                    # exists (``$remote_title ne $title && !$album``).
                    np_album = station_title if (station_title
                                                 and station_title != np_track
                                                 and not it.get("album")) else ""
                elif is_remote:
                    np_track, np_artist, np_album = line1, "", ""
                else:
                    np_track = line1
                    np_artist = it.get("artist", "") or ""
                    np_album = it.get("album", "") or ""
                np_it["track"] = np_track
                np_it["artist"] = np_artist
                np_it["album"] = np_album
                np_it["trackType"] = "radio" if is_remote else "local"
                if is_remote and station_title:
                    # Perl's 'N' tag / _songData:5968-5981; SqueezeJS reads
                    # playlist_loop[0].remote_title (SqueezeJS Base.js:718).
                    np_it["remote_title"] = station_title
                np_it["text"] = np_text(np_track, np_artist, np_album)
                np_loop.append(np_it)
            item_loop = np_loop

        result: dict = {
            "mode": player.mode,
            "power": 1 if player.power else 0,
            "player_name": player.name or player.mac,
            # SqueezeClient's PlayerStatusResponse requires this
            "player_connected": 1,
            # SqueezePlay reads playerStatus.remote == 1 to append the live
            # current_title for a radio stream.
            "remote": 1 if cur_local is None else 0,
            # Perl Queries.pm:4186-4190 sends these as INTEGERS
            # (`$repeat += 0; $shuffle += 0`) — live probe (docs/
            # status-parity-analysis.md, gap-analysis CTRL-03) confirms
            # `"playlist shuffle": 0` / `"playlist repeat": 0` as ints.
            "playlist shuffle": int(getattr(player, "shuffle", 0) or 0),
            "playlist repeat": int(getattr(player, "repeat", 0) or 0),
            "mixer volume": player.volume or 50,
            "playlist_tracks": len(playlist_ids),
            # Perl sends this as a STRING (`"playlist_cur_index": "0"`,
            # Queries.pm:4208 + live probe gap-analysis CTRL-04);
            # SqueezePlay does tonumber(event.data.playlist_cur_index).
            "playlist_cur_index": str(int(cur)),
            "time": elapsed,
            # Perl: `rate` is a hardcoded 1 and is emitted ONLY inside
            # `if (my $song = $client->playingSong())` (Queries.pm:4086-4097,
            # "backward compatibility with older SBC firmware"). No song →
            # no field, and never 0.
            "rate": 1,
            # Perl: `playlist mode` is the constant string 'off'
            # (Queries.pm:4192-4193 "Backwards compatibility - now obsolete");
            # the repeat state lives in `playlist repeat` only.
            "playlist mode": "off",
            "randomplay": int(getattr(player, "shuffle", 0) or 0),
            "digital_volume_control": 1,
            "use_volume_control": 1,
            "signalstrength": 0,
            # Echo the client's last seq_no param (Perl Queries.pm:4196):
            # Player.lua:1223-1333 compares it and re-sends volume/power
            # forever when it does not match.
            "seq_no": int(getattr(player, "seq_no", 0) or 0),
            "playlist_timestamp": time.time(),
            # The tags window (Perl's _addSong shape / statusQuery:4425-4470)
            # stays exactly as it is; the MENU loop above is the separate
            # _addJiveSong shape Perl serves as item_loop.
            "playlist_loop": window_loop,
        }
        # Perl adds `can_seek` inside the playingSong() branch and only when
        # the song can actually seek (Queries.pm:4086/4104-4107). Song.pm:
        # 849-870 (canDoSeek) delegiert an den Protokoll-Handler: für lokale
        # Dateien an die Formatklasse (`sub canSeek { 1 }`, z. B. MP3.pm:476),
        # für einen Remote-Stream an Slim/Player/Protocols/HTTP.pm:1150-1165 —
        # seekbar nur, wenn Bitrate UND Dauer bekannt sind.
        if player.mode != "stop":
            if cur_local is not None:
                fmt = _local_format_from_url(str(cur_info.get("url") or ""))
                if fmt in SEEKABLE_FORMATS:
                    result["can_seek"] = 1
            else:
                _bitrate = int(
                    (cur_info.get("bitrate") if isinstance(cur_info, dict) else 0)
                    or getattr(player, "bitrate", 0) or 0)
                if not _bitrate:
                    # Dieselbe Quelle wie die Radio-Liste: die gespeicherte
                    # Station (remote_media.bitrate).
                    try:
                        _rows = _db_query(
                            "SELECT bitrate FROM remote_media WHERE url = ? "
                            "LIMIT 1",
                            (str(getattr(player, "current_url", "") or ""),))
                        _bitrate = int((_rows[0]["bitrate"] if _rows else 0) or 0)
                    except Exception:  # noqa: BLE001
                        _bitrate = 0
                _stream_dur = float(getattr(player, "duration", 0) or 0)
                if _bitrate and _stream_dur > 0:
                    result["can_seek"] = 1
        # Perl: `my $trackGain = $song->replayGain(); if (defined $trackGain)
        # { addResult('replay_gain', $trackGain) }` (Queries.pm:4109-4112). Der
        # Wert ist das Ergebnis von ReplayGain->fetchGainMode (ReplayGain.pm:
        # 22-79), das der StreamingController beim Start ablegt
        # (StreamingController.pm:1282-1284); Modus 0 (Default) → Feld fehlt.
        try:
            from lyrion.player.replaygain import fetch_gain_mode

            _prefs = getattr(player, "playerprefs", None) or {}
            _tg = cur_info.get("replay_gain") if isinstance(cur_info, dict) else None
            _tp = cur_info.get("replay_peak") if isinstance(cur_info, dict) else None
            _ag = _ap = None
            if isinstance(cur_info, dict) and cur_info.get("album_id"):
                _al = await self._album_replaygain(int(cur_info["album_id"]))
                if _al:
                    _ag, _ap = _al
            _gain = fetch_gain_mode(
                _prefs, track_gain=_tg, track_peak=_tp,
                album_gain=_ag, album_peak=_ap,
                remote=bool(getattr(player, "remote", 0)),
            )
            if _gain is not None:
                result["replay_gain"] = _gain
        except Exception as exc:  # noqa: BLE001 — RG ist optional
            logger.debug("status replay_gain failed: %s", exc)
        # Perl only adds `rate` inside the playingSong() branch
        # (Queries.pm:4086-4097); a stopped player has no such field.
        if player.mode == "stop":
            result.pop("rate", None)
        # Perl sendet die Songfelder des laufenden Titels NICHT auf der
        # Antwort-Ebene: `current_title` nur beim Remote-Stream
        # (Queries.pm:4084-4090), album/artist über die tags am Item des
        # aktuellen Titels in playlist_loop (:4425-4470). Wir reichern daher
        # das aktuelle Item an, statt Felder nach oben lecken zu lassen
        # (Harness-Befund: album, artist, count, current_album,
        # current_artist, current_url).
        np_title = cur_info.get("title", "")
        np_artist = cur_info.get("artist", "")
        np_album = cur_info.get("album", "")
        if cur_local is None:
            # Perl sends the RAW ICY StreamTitle as ``current_title`` for a
            # remote stream: Queries.pm:4089-4090
            # ``Slim::Music::Info::getCurrentTitle($client, $url)`` (Info.pm:
            # 556-581 formats artist and title with the client separator).
            # Live Perl 9.1.1 (Player 00:00:00:00:00:00, 1.FM stream):
            # ``current_title: "Goabert - pulchra somnium"`` while remoteMeta
            # carries the SPLIT ``title: "pulchra somnium"`` /
            # ``artist: "Goabert"`` and the playlist item ``artist`` +
            # ``title``. The player object keeps exactly that raw value in
            # ``current_title``; the split below only feeds the item fields.
            raw_icy = getattr(player, "current_title", "") or ""
            result["current_title"] = raw_icy or np_title
            if not np_artist and np_title and " - " in np_title:
                head, _, tail = np_title.partition(" - ")
                if tail:
                    np_artist = head.strip()
                    np_title = tail.strip()
        # Die Felder des laufenden Titels gehören an das Item; die Item-Objekte
        # sind dieselben, die in result["playlist_loop"] hängen.
        for it in item_loop:
            if it.get("playlist index") == int(cur):
                it.setdefault("track", np_title)
                if np_artist:
                    it.setdefault("artist", np_artist)
                if np_album:
                    it.setdefault("album", np_album)
                if cur_local is None:
                    it["current_album"] = np_album
                    it["current_artist"] = np_artist
                    it["current_url"] = getattr(player, "current_url", "") or ""
                break
        # count/offset hängen bei Perl am menuMode (`if ($menuMode) { …
        # addResult("count", $menuCount) }` Queries.pm:4325-4332; offset
        # :4401) — der reine Status trägt sie nicht. HINWEIS: Squeezer liest
        # für ein Jive-Menü weiterhin count (menu:menu), andere Abfragen
        # bekommen jetzt die Perl-Form.
        if menu_mode:
            result["count"] = len(item_loop)
            result["offset"] = str(win_start)
            # Perl's menu-mode jive base (Queries.pm:4322-4343): with
            # useContextMenu a 'more' action pointing at the item's own
            # ``params`` (_contextMenuBase :6229-6245 + context 'playlist'
            # :4325-4327), else a 'go' action for trackinfo (:4329-4341).
            # Squeezer resolves base.actions.more through itemsParams
            # (JiveItem.java:280 + :585-596) into ``song.moreAction``, and the
            # fullscreen player status dereferences it WITHOUT a null check:
            # NowPlayingFragment.java:805 ``pluginItems(song.moreAction, …)``
            # → SqueezeService.java:1434 ``action.action.cmd``. Without
            # ``base`` moreAction stays null and opening the fullscreen
            # Now-Playing throws the NPE the user sees as a crash.
            # ``params`` is the itemsParams target; every item_loop item
            # carries it (:5655-5659).
            if any(str(a) == "useContextMenu:1" for a in (args or [])):
                result["base"] = {"actions": {"more": {
                    "player": 0,
                    "cmd": ["contextmenu"],
                    "itemsParams": "params",
                    "params": {"menu": "track", "context": "playlist"},
                    "window": {"isContextMenu": 1},
                }}}
            else:
                result["base"] = {"actions": {"go": {
                    "player": 0,
                    "cmd": ["trackinfo", "items"],
                    "itemsParams": "params",
                    "params": {"menu": "nowhere", "useContextMenu": 1,
                               "context": "playlist"},
                }}}
        # Player IP:port (Perl sends 'ip:port' of the control connection).
        try:
            result["player_ip"] = f"{player.ip}:{getattr(player, 'port', 0) or 0}"
        except Exception:
            pass
        # Web-UI-only conveniences (the Perl LMS does NOT send these in
        # status; our SPA/SqueezeTray read them). Kept out of the strict
        # parity path — apps that compare key sets see Perl shape.
        # album/artist stehen bei Perl ausschließlich am playlist_loop-Item
        # (tags l/a) und wurden oben dorthin verschoben.
        if not tags or True:  # cheap: keep for local UI consumers
            result.setdefault("title", cur_info.get("title", ""))
            # Duration: Perl publishes the key ONLY when the current song
            # reports a truthy length —
            #   Slim/Control/Queries.pm:4100-4102
            #       if (my $dur = $song->duration()) {
            #           $dur += 0;
            #           $request->addResult('duration', $dur);
            #       }
            # — i.e. the DB length (LENGTH tag) for a local track and the
            # parsed length of a remote stream, and NOTHING AT ALL when that
            # length is 0/undef.  Live Perl 9.1.1 (read-only, 2026-09-14):
            #   * player 00:04:20:2b:88:c8, stream "Dj Fada 2 - Life Breath
            #     (oct12)" (remoteMeta/playlist_loop duration "0") →
            #     `status - 1 tags:cdltoK` has NO 'duration' key;
            #   * player ca:c8:c7:26:6d:38, stream …/alarm.mp3 (length known,
            #     7 s) → `"duration":7`.
            # Never synthesise one from the elapsed position or a 1 s
            # placeholder: SqueezeClient builds its seek slider from
            # `duration` as valueTo and dies with
            #   java.lang.IllegalStateException: Slider value(1006.49963) …
            #   valueTo(1006.497)
            # (NowPlayingFragment.kt:440-479) when the position runs away
            # from a fabricated valueTo, and SqueezePlay's progress bar pins
            # at maximum.  With no known length the clients take Perl's
            # disabled-slider branch, which is exactly what the Perl server
            # makes them do.
            dur = _perl_status_duration(cur_info.get("duration"))
            if dur is not None:
                result["duration"] = dur
            # Never emit an EMPTY item_loop: Jive's `_whatsPlaying`
            # (share/jive/jive/slim/Player.lua:272-273) does
            # `if obj.item_loop then obj.item_loop[1].params ...` — with an
            # empty array `item_loop[1]` is nil and Lua raises
            # "attempt to index field '?' (a nil value)", which aborts the
            # artwork/now-playing sink (seen live as a missing cover and
            # `RequestHttp.lua:71 Response sink` error). Perl omits the key
            # in that case.
            # Perl builds ``item_loop`` ONLY in menuMode: `my $loop =
            # $menuMode ? 'item_loop' : 'playlist_loop'` (Queries.pm:4353),
            # and the menu loop is added through _addJiveSong (:4411/:4416).
            # A plain `status - 1 tags:…` therefore carries playlist_loop and
            # NOT item_loop — live Perl 192.168.1.90 answered 25 keys with no
            # item_loop (and no title) where we sent 26.  Only the MENU status
            # has it, and that is the frame the clients read it from
            # (SqueezePlay's _whatsPlaying, Player.lua:272-273; Squeezer's
            # parsePlayerStatus, CometClient.java:419-426; the playerstatus
            # subscription is always `status - 1 menu:menu useContextMenu:1`).
            if menu_mode and item_loop:
                result["item_loop"] = item_loop
        result |= sync_fields
        if remote_meta:
            result["remoteMeta"] = remote_meta
        if menu_block:
            result["menu"] = menu_block
        return result

    @staticmethod
    def _status_window(args: list[str] | None, cur: int, total: int) -> tuple[int, int]:
        """Perl ``Slim::Control::Request::normalize`` for the status loop.

        ``status - <qty>`` (index ``-``) starts the window at the playing
        track (Queries.pm:4425 ``normalize($playlist_cur_index, $quantity,
        $songCount)``); ``status <n> <qty>`` starts at ``n``. Returns an
        inclusive ``(start, end)`` pair; ``start > end`` means "no items".
        """
        tokens = [str(a) for a in (args or [])]
        index_tok = tokens[0] if tokens else "-"
        qty = 0
        if len(tokens) > 1 and tokens[1].lstrip("-").isdigit():
            qty = int(tokens[1])
        if index_tok == "-":
            start = int(cur)
        elif index_tok.lstrip("-").isdigit():
            start = int(index_tok)
        else:
            start = 0
        if qty <= 0:
            qty = int(total)          # Perl: $numofitems = $count when undef
        if not total:
            return 0, -1
        last = int(total) - 1
        if start > last:
            return 0, -1
        if start < 0:
            start = 0
        return start, min(start + qty - 1, last)

    def _home_menu(self, player: Any = None) -> list[dict]:
        """The root browse menu (Home) — Perl ``mainMenu``
        (Slim/Control/Jive.pm:261-320).

        The controllers recognise the canonical item ids (myMusic, favorites,
        radios, myMusicMusicFolder...), the node values (home/myMusic/settings)
        and the browselibrary navigation command.  Items with isANode become
        expandable nodes; the myMusic children are emitted in the SAME
        item_loop and nested under the myMusic node by the controller.

        ``player`` is the connected client the menu is built for (Perl's
        ``mainMenu($client)``): with it the player-bound items (``playerpower``
        and the ``settings`` entries, Jive.pm:2239-2275 / :1395-1600) are
        emitted — without it (tests, disconnected clients) the menu stays the
        library-only subset.  Titles are localized via ``menus.menu_title``.
        """
        from lyrion.web import menus

        def _go(cmd: list[str], params: dict | None = None) -> dict:
            """A home-menu node's navigation action — ``go`` ONLY.

            Perl never mirrors a navigation action into ``do``: the live home
            menu (``menu 0 100 direct:1`` against 192.168.1.90) carries

            * ``favorites``  -> ``{"actions": {"go": {"cmd": ["favorites", "items"],
              "params": {"menu": "favorites"}}}}``  (no ``player``, no ``do``)
            * ``radios``     -> ``{"actions": {"go": {"cmd": ["radios"], "params":
              {"menu": "radio"}}}}``
            * ``myMusic*``/``settingsInformation``/``globalSearch`` -> ``go`` only

            — and ``do`` appears on exactly three entries whose ``do`` is NOT a
            navigation: ``settingsRepeat``/``settingsShuffle`` (``do.choices``)
            and ``settingsPlayerNameChange`` (``do.params.playername ==
            "__INPUT__"``).

            The mirrored ``do`` made both Android controllers execute the row as
            an *action* instead of opening it, because ``do`` wins over ``go``:

            * Squeeze Client (maniac103/squeezeclient, GPL-3, read-only clone
              ``/tmp/squeezeclient-src``) —
              ``ui/itemlist/JiveHomeListItemFragment.kt:109-132`` tests
              ``item.doAction != null -> connectionHelper.executeAction(...)``
              BEFORE ``item.goAction != null -> listener.onGoAction(...)`` and
              before ``listener.onNodeSelected(item.id)``; ``executeAction``
              (``cometd/ConnectionHelper.kt:280-281``) publishes an
              ``ExecuteActionRequest`` and *discards* the answer, so the row
              never opens a list;
            * Squeezer — same rule, ``model/JiveItem.java:268-270`` ("``do``
              wins over ``go``/``goAction``"), the rule our own port of the tap
              path uses (``tests/test_squeezer_favorites_tap.py:jive_go_action``).

            Live symptom it caused: tapping "Favoriten"/"Radio" in either app
            sent ``["favorites","items","menu:favorites","useContextMenu:1"]``
            and showed nothing — no list ever opened.
            """
            go: dict = {"player": 0, "cmd": cmd}
            if params:
                go["params"] = params
            return {"go": go}

        items: list[dict] = [
            # My Music node — SqueezePlay expands it into the myMusic
            # children (which share this item_loop, node=myMusic).
            {"id": "myMusic", "text": menus.menu_title("MY_MUSIC"),
             "node": "home", "isANode": 1, "weight": 11, "hasitems": 1},
            {"id": "favorites", "text": _jive_string("FAVORITES"),
             "node": "home", "weight": 100,
             "actions": _go(["favorites", "items"], {"menu": "favorites"})},
        ]

        if player is not None:
            # mainMenu order: playerPower and playerSettingsMenu come after
            # the favorites entry and before internetRadioMenu (Jive.pm:281-291).
            items.append(menus.power_node(
                getattr(player, "name", "") or getattr(player, "mac", ""),
                bool(getattr(player, "power", False))))
            items.extend(menus.settings_nodes(
                player_name=getattr(player, "name", "") or "",
                power_on=bool(getattr(player, "power", False)),
                repeat=int(getattr(player, "repeat", 0) or 0),
                shuffle=int(getattr(player, "shuffle", 0) or 0)))

        items.append(
            # internetRadioMenu (Jive.pm:1360-1393): Perl only emits it when
            # the radios query returns a non-empty list; menuStyle 'album'.
            {"id": "radios", "text": menus.menu_title("RADIO"), "node": "home",
             "weight": 20, "window": {"menuStyle": "album"},
             "actions": _go(["radios"], {"menu": "radio"})})

        # The Radio node's CHILDREN — Perl's @pluginMenus (Jive.pm:286), one
        # ``opml<tag>`` row per directory sub-node with ``node: 'radios'``.
        # SqueezePlay ignores our own ``radios`` row ("shown locally",
        # SlimMenusApplet.lua:569-570) and only puts its locally created Radio
        # node into the home menu once such a child arrives
        # (ui/HomeMenu.lua:567-580) — without these rows the client shows no
        # Radio entry at all.  Perl registers them unconditionally per
        # generated plugin (OPMLBased.pm:48-52); our directory structure is
        # static (radiobrowser.ROOT_NODES), so the node and its children are
        # always emitted together.
        from lyrion.web import radiobrowser

        items.extend(radiobrowser.home_menu_items())

        # myMusicMenu(1, $client) (Jive.pm:316) → BrowseLibrary nodes.
        # The Perl per-node conditions are mirrored by the flags: this port
        # has no unified-artists pref, no works model and no playlist store,
        # so only the Album-Artists/All-Artists pair is emitted (exactly what
        # the live Perl menu with default prefs shows).
        items.extend(menus.my_music_nodes())

        # Jive.pm:316 hängt ``recentSearchMenu($client, 1)`` ans Home-Menü
        # (nur bei genau einem gecachten Such-Eintrag, Jive.pm:2728).
        items.extend(_jive_recent_search_menu())
        return items

    @staticmethod
    def _alarm_to_item(index: int, alarm) -> dict:
        """Convert an Alarm to the JSON alarm_loop entry shape."""
        return {
            "id": index,
            "enabled": 1 if (alarm and alarm.enabled) else 0,
            "day": alarm.day_int() if alarm else 0,
            "hour": int(alarm.time.split(":")[0]) if alarm else 0,
            "minute": int(alarm.time.split(":")[1]) if alarm else 0,
            "times": 1,
            "fade": alarm.fade if alarm else 0,
            "volume": alarm.volume if alarm else -1,
            "duration": alarm.duration if alarm else 0,
            "repeat": 1 if (alarm and alarm.repeat) else 0,
            "shufflemode": int(getattr(alarm, "shufflemode", 0) or 0) if alarm else 0,
            "wake": (alarm.wake if alarm else "") or "",
            # 'url:' only → url; 'track:' → track id; 'fr:' → favorite id
            "url": (alarm.wake[4:] if alarm and alarm.wake.startswith("url:")
                    else ""),
            "track": (alarm.wake[6:] if alarm and alarm.wake.startswith("track:")
                      else ""),
            "favorite": (alarm.wake[3:] if alarm and alarm.wake.startswith("fr:")
                         else ""),
        }

    async def _json_alarms(self, pid: str | None, args: list) -> dict:
        """['alarms'] → list all alarms for the player (LMS JSON shape)."""
        from lyrion.alarms import AlarmManager

        mac = pid if pid not in ("-", None) else ""
        mgr = AlarmManager()
        alarms = mgr.alarms_for(mac)
        loop = [self._alarm_to_item(idx, alarms.get(idx)) for idx in sorted(alarms)]
        return {
            "count": len(loop),
            "fade": 0,
            "alarm_loop": loop,
        }

    async def _json_alarm(self, pid: str | None, args: list) -> dict:
        """['alarm', '<idx>'] / ['alarm', '<idx>', '<k:v>…'] / Perl cmds.

        Perl alarmCommand forms (Commands.pm:55, used by Material):
          ['alarm', 'add',    'time:HHMM', 'dow:0,2', 'url:<stream>']
          ['alarm', 'update', 'id:<idx>',  'k:v'…]
          ['alarm', 'delete', 'id:<idx>']
          ['alarm', 'enableall' | 'disableall']
        plus the index form (own SPA/SqueezePlay) incl. a bare 'delete'.
        """
        from lyrion.alarms import AlarmManager, _alarm_from_parts, Alarm

        mac = pid if pid not in ("-", None) else ""
        mgr = AlarmManager()
        first = str(args[0]) if args else ""

        if first in ("add", "update", "delete", "enableall", "disableall",
                     "defaultvolume"):
            return await self._alarm_command(mac, first, args[1:])

        # ── index form ────────────────────────────────────────────────
        # Some clients pass '<idx>' as '<idx>-' (row selection).
        idx_str = first.split("-")[0]
        try:
            idx = int(idx_str)
        except ValueError:
            return {"error": "invalid alarm index"}

        if len(args) == 1 or (len(args) >= 2 and args[1] in ("?", "query")):
            return self._alarm_to_item(idx, mgr.get(mac, idx))

        # Own web UI delete: ['alarm', '<idx>', 'delete']
        if args[1] in ("delete", "delete:1", "del"):
            mgr.delete(mac, idx)
            return {"deleted": idx, "id": idx}

        parts: dict[str, str] = {}
        for tok in args[1:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v
        current = mgr.get(mac, idx)
        a = _alarm_from_parts(idx, parts, base=current)
        if current:
            for f in ("enabled", "days", "time", "volume", "fade", "duration",
                      "repeat", "shufflemode", "wake"):
                if f not in parts:
                    # If hour/minute were given, don't clobber the time
                    # we just built from them.
                    if f == "time" and ("hour" in parts or "minute" in parts):
                        continue
                    # If an integer day-mask was given, its derived 'days'
                    # string must not be overwritten from current.
                    if f == "days" and ("day" in parts or "dow" in parts
                                        or "dowAdd" in parts or "dowDel" in parts):
                        continue
                    # 'url:'/'track:' build the wake value; don't clobber it.
                    if f == "wake" and ("url" in parts or "track" in parts):
                        continue
                    setattr(a, f, getattr(current, f))
        mgr.set(mac, idx, a)
        return self._alarm_to_item(idx, a)

    async def _alarm_command(self, mac: str, cmd: str, toks: list) -> dict:
        """Perl alarmCommand: add/update/delete/enableall/disableall."""
        from lyrion.alarms import AlarmManager, _alarm_from_parts

        mgr = AlarmManager()
        parts: dict[str, str] = {}
        for tok in toks:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v

        if cmd == "delete":
            raw = parts.get("id")
            if raw is None:
                return {"error": "alarm delete needs id:<index>"}
            try:
                idx = int(str(raw).split("-")[0])
            except ValueError:
                return {"error": "invalid alarm index"}
            mgr.delete(mac, idx)
            return {"deleted": idx, "id": idx}

        if cmd == "enableall":
            for i in list(mgr.alarms_for(mac)):
                a = mgr.get(mac, i)
                if a:
                    a.enabled = True
                    mgr.set(mac, i, a)
            return {"count": len(mgr.alarms_for(mac))}

        if cmd == "disableall":
            for i in list(mgr.alarms_for(mac)):
                a = mgr.get(mac, i)
                if a:
                    a.enabled = False
                    mgr.set(mac, i, a)
            return {"count": len(mgr.alarms_for(mac))}

        if cmd == "add":
            existing = mgr.alarms_for(mac)
            idx = max(existing) + 1 if existing else 0
            a = _alarm_from_parts(idx, parts)
            mgr.set(mac, idx, a)
            return self._alarm_to_item(idx, a)

        # update — Perl requires id:<idx>; merge like the index set-form.
        raw = parts.get("id")
        if raw is None:
            return {"error": "alarm update needs id:<index>"}
        try:
            idx = int(str(raw).split("-")[0])
        except ValueError:
            return {"error": "invalid alarm index"}
        current = mgr.get(mac, idx)
        a = _alarm_from_parts(idx, parts, base=current)
        if current:
            for f in ("enabled", "days", "time", "volume", "fade", "duration",
                      "repeat", "shufflemode", "wake"):
                if f not in parts:
                    if f == "time" and ("hour" in parts or "minute" in parts):
                        continue
                    if f == "days" and ("day" in parts or "dow" in parts
                                        or "dowAdd" in parts or "dowDel" in parts):
                        continue
                    if f == "wake" and ("url" in parts or "track" in parts):
                        continue
                    setattr(a, f, getattr(current, f))
        mgr.set(mac, idx, a)
        return self._alarm_to_item(idx, a)

    # ─────────────────────────────────────────────────────────────
    # Saved playlists (Perl: playlistsQuery → 'playlists_loop' with
    # id + playlist, playlists tracks → 'playlist_tracks_loop')
    # ─────────────────────────────────────────────────────────────

    async def _json_playlists(self, pid: str | None, args: list) -> dict:
        """['playlists', <start>, <count>, 'tags:…'] and the subcommands
        'new' / 'rename' / 'delete' / 'tracks' (Slim/Control/Commands.pm
        playlistsNew/Rename/DeleteCommand + playlistsQuery parity)."""
        if args and str(args[0]).lower() in ("new", "rename", "delete",
                                             "tracks"):
            sub = str(args[0]).lower()
            if sub == "tracks" and len(args) >= 2 and str(args[1]).isdigit():
                return await self._json_playlist_tracks(int(args[1]))
            return await self._json_playlist_mutation(sub, args[1:])

        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 10
        tags = next((str(a)[5:] for a in args if str(a).startswith("tags:")), "")
        import sqlite3
        db = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id, playlist, name, remote FROM playlists "
                "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (count, start)).fetchall()
            total = db.execute(
                "SELECT COUNT(*) AS n FROM playlists").fetchone()["n"]
        finally:
            db.close()
        loop = []
        for r in rows:
            item = {"id": int(r["id"]),
                    "playlist": (r["name"] or r["playlist"] or "")}
            if "x" in tags:
                item["remote"] = int(r["remote"] or 0)
            loop.append(item)
        return {"count": int(total or 0), "playlists_loop": loop}

    async def _json_playlist_tracks(self, pid: int) -> dict:
        """['playlists', 'tracks', <id>] → playlist_tracks_loop."""
        import sqlite3
        db = sqlite3.connect(f"file:{_library_db_path()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT pi.position, pi.track, pi.url, pi.title, "
                "t.title AS ttitle, t.duration AS tduration "
                "FROM playlist_items pi "
                "LEFT JOIN tracks t ON t.id = pi.track "
                "WHERE pi.playlist = ? ORDER BY pi.position",
                (pid,)).fetchall()
        finally:
            db.close()
        loop = []
        for r in rows:
            title = r["title"] or r["ttitle"]
            item: dict = {}
            if r["track"] is not None:
                item["id"] = int(r["track"])
            else:
                item["id"] = r["url"] or ""
            if title:
                item["title"] = title
            if r["url"]:
                item["url"] = r["url"]
            if r["tduration"]:
                item["duration"] = int(r["tduration"] or 0)
            loop.append(item)
        return {"count": len(loop), "playlist_tracks_loop": loop}

    async def _json_playlist_entity(self, pm, pid: str | None, args: list[str]) -> dict:
        """``playlist <entity> ?`` — Perl ``playlistXQuery``.

        Perl ``Slim/Control/Queries.pm:2708-2772`` (Dispatch
        ``Request.pm:548-591``); jede Form antwortet mit dem nackten Schlüssel
        ``_<entity>``. Presence-Regeln wie Perl — **ein Feld ohne Wert erzeugt
        keinen Schlüssel**, und der aktuelle Titel kommt aus
        ``Slim::Player::Playlist::track($client, $index)`` mit ``$index``
        undef → ``Source::playingSongIndex`` (``Playlist.pm:62-73``):

        * ``repeat``/``shuffle`` → ``Playlist::repeat``/``shuffle``
          (:2724-2725/:2727-2728) — immer vorhanden (Wert 0/1/2).
        * ``index``/``jump`` → ``playingSongIndex`` (:2730-2731).
        * ``tracks`` → ``Playlist::count`` (:2743-2744).
        * ``modified`` → ``currentPlaylistModified`` (:2740-2741) — undef,
          solange die Queue nie verändert wurde (JSON null).
        * ``url`` → ``$client->currentPlaylist()`` (:2736-2738); ohne
          gespeicherte Playlist undef → null (live Perl: ``{"_url":null}``).
        * ``path`` → ``Playlist::url`` bzw. 0 (:2746-2748).
        * ``remote`` → nur bei definierter URL (:2750-2753).
        * ``name`` → Titel der gespeicherten Playlist (:2733-2734) bzw.
          ``remote_title`` des Streams (:2766-2767); bei einem lokalen Track
          gibt Perl KEIN Result.
        * ``title``/``duration``/``artist``/``album``/``genre`` → das
          ``_songData``-Feld des Tracks (:2755-2768, Tags ``dalgN``).
        """
        player = pm.get_player(pid) if (pm is not None and pid) else None
        if player is None:
            return {}
        entity = str(args[0]).lower()
        playlist = list(getattr(player, "playlist", []) or [])

        if entity == "repeat":
            return {"_repeat": int(getattr(player, "repeat", 0) or 0)}
        if entity == "shuffle":
            return {"_shuffle": int(getattr(player, "shuffle", 0) or 0)}
        if entity in ("index", "jump"):
            return {f"_{entity}": int(getattr(player, "playlist_position", 0) or 0)}
        if entity == "tracks":
            return {"_tracks": len(playlist)}
        if entity == "modified":
            flag = getattr(player, "playlist_modified", None)
            return {"_modified": None if flag is None else int(flag)}
        if entity == "url":
            # Ohne gespeicherte Playlist ist currentPlaylist() undef → null.
            url = getattr(player, "current_playlist_url", None)
            return {"_url": str(url) if url else None}

        # _index nur bei den Dispatch-Formen mit `_index` (Request.pm:551-588);
        # ohne Index ist es der laufende Titel (Playlist.pm:72-73).
        index: int | None = None
        if (entity in _PLAYLIST_INDEX_ENTITIES and len(args) >= 3
                and re.fullmatch(r"-?\d+", str(args[1]))):
            index = int(str(args[1]))
        if index is None:
            index = int(getattr(player, "playlist_position", 0) or 0)
        entry = playlist[index] if 0 <= index < len(playlist) else None
        url_str = "" if entry is None else str(entry)

        if entity == "path":
            # Perl: url($client, $index) || 0 (:2746-2748) — ohne Eintrag 0.
            if entry is None:
                return {"_path": 0}
            tid = _playlist_local_id(entry)
            if tid is None:
                return {"_path": url_str or 0}
            rows = await self._load_tracks([tid])
            return {"_path": str((rows.get(tid) or {}).get("url") or 0)}
        if entity == "remote":
            # Perl nur bei definierter URL (:2750-2753).
            if entry is None:
                return {}
            return {"_remote": 1 if _is_remote_url(url_str) else 0}

        # title/duration/artist/album/genre/name — _songData des Tracks.
        if entry is None:
            return {}
        tid = _playlist_local_id(entry)
        info: dict = {}
        is_remote = tid is None
        if tid is not None:
            info = dict((await self._load_tracks([tid])).get(tid) or {})
        else:
            # Remote-Stream: Titel des laufenden Items wie in
            # _json_player_status (current_title = ICY/Stationstitel, sonst
            # der Host) — Perls ``$remoteMeta->{title} || $track->title``.
            title = str(getattr(player, "current_title", "") or "")
            if not title:
                try:
                    from urllib.parse import urlparse
                    title = (urlparse(url_str).hostname or url_str).replace("www.", "")
                except Exception:  # noqa: BLE001
                    title = url_str
            info = {"title": title, "url": url_str, "duration": 0}
            if index == int(getattr(player, "playlist_position", 0) or 0):
                info["artist"] = str(getattr(player, "current_artist", "") or "")

        if entity == "duration":
            # Auch 0 ist definiert → der Schlüssel bleibt (:2763-2764).
            return {"_duration": float(info.get("duration", 0) or 0)}
        if entity == "name":
            # Tag 'N' → remote_title; nur ein Stream hat einen Namen
            # (:2766-2767). Ein lokaler Track liefert kein Result.
            value = info.get("title") if is_remote else None
        else:
            value = info.get(entity)
        if value in (None, ""):
            return {}
        return {f"_{entity}": value}

    async def _json_playlist_mutation(self, sub: str, toks: list) -> dict:
        """new/rename/delete on saved playlists (Perl Commands.pm)."""
        parts: dict[str, str] = {}
        for tok in toks:
            if ":" in tok:
                k, v = tok.split(":", 1)
                parts[k] = v
        import sqlite3
        db = sqlite3.connect(_library_db_path(), timeout=30)
        try:
            if sub == "new":
                name = parts.get("name", "").strip()
                if not name:
                    return {"error": "playlists new needs name:<title>"}
                try:
                    cur = db.execute(
                        "INSERT INTO playlists (playlist, name, changed, "
                        "pl_type, remote, disabled) "
                        "VALUES (?, ?, datetime('now'), 0, 0, 0)",
                        (name, name))
                    db.commit()
                    return {"playlist_id": int(cur.lastrowid)}
                except sqlite3.IntegrityError:
                    db.rollback()
                    return {"error": "playlist already exists"}
            if sub == "delete":
                pid = int(parts.get("id", "-1").split("-")[0])
                if pid < 0:
                    return {"error": "playlists delete needs id:<n>"}
                db.execute("DELETE FROM playlist_items WHERE playlist = ?",
                           (pid,))
                db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
                db.commit()
                return {"deleted": pid}
            if sub == "rename":
                pid = int(parts.get("id", "-1").split("-")[0])
                name = parts.get("name", "").strip()
                if pid < 0 or not name:
                    return {"error": "playlists rename needs id:<n> name:<x>"}
                db.execute(
                    "UPDATE playlists SET name = ?, playlist = ? WHERE id = ?",
                    (name, name, pid))
                db.commit()
                return {"renamed": pid, "name": name}
            return {"error": f"unknown playlists subcommand {sub}"}
        finally:
            db.close()

    #: Die Loop-Namen, die Perls ``*Query``-Handler tatsächlich ausgeben
    #: (``Queries.pm``: ``albums_loop`` :746, ``artists_loop`` :1021,
    #: ``genres_loop`` :1945, ``titles_loop`` :4908, ``years_loop`` :4996).
    #: Ein Aufrufer, der ``plural`` nicht setzt, beschreibt damit die
    #: generische Browse-Form (Perls ``loop_loop`` aus der XMLBrowser-Feed-
    #: Schicht, ``XMLBrowser.pm:582/846``).
    _PERL_PLURAL_LOOPS = frozenset({
        "albums_loop", "artists_loop", "genres_loop", "titles_loop",
        "years_loop", "folder_loop", "playlists_loop", "radioss_loop",
        "search_loop", "contributors_loop", "works_loop",
    })

    @staticmethod
    def _browse_response(loop: list, total: int | None = None,
                         plural: str | None = None) -> dict:
        """Browse/menu response in **Perl's** shape: ``count`` plus exactly
        ONE loop.

        Perl's library queries each answer a single, category-specific loop
        and nothing else — live 192.168.1.90::

            albums  0 3            → {"albums_loop": […], "count": 7189}
            artists 0 3            → {"artists_loop": […], "count": 11170}
            titles  0 3            → {"titles_loop": […], "count": 80218}
            genres  0 3            → {"genres_loop": […], "count": 762}
            years   0 3            → {"years_loop": […], "count": …}

        Queries.pm:746 ``my $loopname = 'albums_loop';`` / :1021 artists /
        :1939 genres / :4908 titles — ``Request.pm:2264-2281`` unrolls that
        one loop; there is no ``item_loop``/``loop_loop`` twin. The
        ``loop_loop``/``item_loop`` pair only exists in the XMLBrowser feed
        layer (``XMLBrowser.pm:582/846``: ``$menuMode ? 'item_loop' :
        'loop_loop'``), which is why ``browselibrary items … menu:1`` answers
        ``item_loop`` alone and the flat form answers ``loop_loop`` alone.

        The port used to publish the SAME list under all three names. That
        triples the bytes for every page, and for the un-paged probe Squeeze
        Client issues when opening *Eigene Musik → Alben*
        (``albums 0 2147483647 … tags:yE``, measured live 2026-09-18) it was
        22.07 MB versus Perl's 1.01 MB — the app died with
        ``java.lang.OutOfMemoryError`` (192 MB heap, reproduced in Waydroid).
        One loop per answer, as in Perl, removes the aliases.

        Items keep the compatibility fields ``text``/``type``/``hasitems``
        that the Android controllers read for the display line and the row
        kind (unknown keys are ignored by their deserializers); whatever
        native fields the caller built — including the Jive ``actions`` tree
        of the library queries — are left untouched. (Perl's XMLBrowser feed
        lets ``Slim/Menu/BrowseLibrary.pm`` attach actions in the
        ``item_loop`` form; the JSON catalog queries carry none, which is a
        documented leftover divergence here.) ``count`` stays the total
        number of matches, not the page length."""
        for it in loop:
            if not isinstance(it, dict):
                continue
            # Display title: controllers read 'text'; fall back to the
            # LMS-native display field used by this item type.
            text = it.get("text") or it.get("title") or it.get("album") \
                or it.get("artist") or it.get("name") or ""
            it.setdefault("text", text)
            it.setdefault("title", text)
            # Row type: 'outline' (folder-ish) is a valid SlimBrowseItemType
            # understood by every controller. Never leave it as a raw
            # artist/album/song/genre/radio value the enum can't decode.
            if it.get("type") in (None, "artist", "album", "song", "genre",
                                  "radio", "track", "folder"):
                it["type"] = "outline"
            it.setdefault("hasitems", 1)
            # Artwork hint if the item carries a cover already.
            if it.get("coverid") and not it.get("icon"):
                it["icon"] = f"/music/{it['coverid']}/cover.jpg"
                it["icon-id"] = it["coverid"]
        resp: dict = {"count": len(loop) if total is None else total,
                      "offset": 0}
        # Exactly ONE loop, named as Perl names it. A caller that passes a
        # plural asks for a library query answer (albums/artists/…); a caller
        # without one wants the generic browse feed, whose Perl name is
        # `loop_loop` (XMLBrowser.pm:582/846, non-menuMode).
        loop_name = plural if plural in JSONRPCAPI._PERL_PLURAL_LOOPS \
            else "loop_loop"
        resp[loop_name] = loop
        return resp

    async def _load_tracks(self, track_ids: list[int]) -> dict:
        """Load track metadata for ids (songinfo/status tag fields)."""
        result: dict = {}
        if not track_ids:
            return result
        try:
            import sqlite3
            # Read-only connection: status polls from many clients must
            # never block on (or lock) the writer (aiosqlite session).
            db = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(track_ids))
            # Track fields (songinfo tags: d,y,t,u,r,T,I,x,g,c,j,J,K,i,o,f,k,w)
            rows = db.execute(
                f"""SELECT id, title, url, duration, year, tracknum, bitrate,
                           samplerate, bitspersample, genre, cover, remote,
                           disc, filesize, comment, lyrics, content_type
                    FROM tracks WHERE id IN ({placeholders})""",
                track_ids,
            ).fetchall()
            # Artists (role 1 = artist → a=name, s=id) and albums
            # (l=name, e=id) in bulk — one query per join table. The join
            # tables use 'track'/'contributor'/'album' columns and the
            # role is an INTEGER (1 = artist), not a string.
            artists: dict[int, dict] = {}
            for r in db.execute(
                f"""SELECT tc.track, c.id AS artist_id, c.name AS artist
                    FROM tracks_contributors tc
                    JOIN contributors c ON c.id = tc.contributor
                    WHERE tc.track IN ({placeholders}) AND tc.role = 1""",
                track_ids,
            ):
                artists.setdefault(r["track"], {})["artist_id"] = r["artist_id"]
                artists.setdefault(r["track"], {})["artist"] = r["artist"]
            albums: dict[int, dict] = {}
            for r in db.execute(
                f"""SELECT ta.track, a.id AS album_id, a.title AS album,
                           a.artwork AS album_artwork
                    FROM tracks_albums ta
                    JOIN albums a ON a.id = ta.album
                    WHERE ta.track IN ({placeholders})""",
                track_ids,
            ):
                albums.setdefault(r["track"], {})["album_id"] = r["album_id"]
                albums.setdefault(r["track"], {})["album"] = r["album"]
                if r["album_artwork"]:
                    albums.setdefault(r["track"], {})[
                        "artwork_url"] = f"/music/{r['album_id']}/cover.jpg"
            for row in rows:
                tid = row["id"]
                info: dict = {
                    "title": row["title"] or "",
                    "url": row["url"] or "",
                    "duration": row["duration"] or 0,
                    "year": row["year"],
                    "tracknum": row["tracknum"],
                    "bitrate": row["bitrate"],
                    "samplerate": row["samplerate"],
                    "samplesize": row["bitspersample"],
                    "genre": row["genre"] or "",
                    "cover": row["cover"],
                    "remote": 1 if row["remote"] else 0,
                    "disc": row["disc"],
                    "filesize": row["filesize"],
                    "comment": row["comment"],
                    "lyrics": row["lyrics"],
                    "type": row["content_type"],
                }
                info.update(artists.get(tid, {}))
                info.update(albums.get(tid, {}))
                result[tid] = info
            db.close()
        except Exception:
            logger.exception("_load_tracks failed for %d ids", len(track_ids))
        return result

    async def _json_button(self, pm, pid: str | None, args: list) -> None:
        """``button <code>`` → ``Slim::Hardware::IR::executeButton``.

        ``buttonCommand`` (``Slim/Control/Commands.pm:263-291``) passes the
        ``_buttoncode`` straight to ``Slim::Hardware::IR::executeButton`` with
        ``_orFunction`` defaulting to 1 (:288); the Jive/SqueezeBox preset keys
        dispatch ``playPreset_<n>`` (``Slim/Buttons/Common.pm:908``) and are
        handled below (``playPreset``, Common.pm:825-877), every other code
        goes through the shared map/function table
        (:func:`lyrion.player.buttons.execute_named_button`). That is what the
        controllers actually send: SqueezeJS' next/prev buttons post
        ``['button','jump_fwd']`` / ``['button','jump_rew']``
        (``HTML/EN/html/SqueezeJS/UI.js:297``, ``:260``), i.e. the FUNCTION
        name, which ``IR.pm:1061-1064`` keeps as-is when the map has no entry
        and ``Common::getFunction`` splits into ``jump`` + ``fwd``/``rew``
        (Common.pm:1338-1360).
        """
        code = str(args[0]) if args else ""
        if not code.lower().startswith("playpreset"):
            from lyrion.player.buttons import execute_named_button

            await execute_named_button(code, pid, manager=pm)
            return
        digits = "".join(ch for ch in code if ch.isdigit())
        digit = int(digits) if digits else 0
        if digit == 0:
            digit = 10                          # Common.pm:828-830
        if pid is None:
            return
        player = pm.get_player(pid)
        if player is None:
            return
        presets = _jive_presets_pref(player)
        preset = presets[digit - 1] if presets and len(presets) >= digit else None
        if not isinstance(preset, dict):
            logger.info("Can't play preset number %s - not set", digit)
            return
        ptype = str(preset.get("type") or "")
        url = preset.get("URL") or ""
        title = preset.get("text") or ""
        # Common.pm:834: only audio/playlist entries are playable.
        if not re.search(r"audio|playlist", ptype) or not url:
            logger.info("Can't play preset number %s - not an audio entry", digit)
            return
        parser = preset.get("parser")
        if parser or (ptype == "playlist" and _is_remote_url(str(url))):
            # Common.pm:838-850 routes these through Slim::Buttons::XMLBrowser
            # ::playItem (a parser/OPML feed). Our port has no XMLBrowser
            # player, so nothing is started (UNKLAR).
            logger.info("Preset %s needs XMLBrowser playback - not supported",
                        digit)
            return
        # Common.pm:851-859: title, then ``playlist play <url>``.
        await pm.play_url(pid, str(url), str(title))
        self._popup = {"jive": {
            "type": "popupplay",                # Common.pm:860-868
            "text": [_jive_str("PRESET", digit), str(title)],
        }}
        self._popup_expires = time.time() + 5

    async def _json_control(self, pm, pid: str | None, cmd: str, args: list[str]) -> None:
        """Execute control commands (pause/power/play/stop/mixer/playlist)."""
        from lyrion.player.manager import PlayerManager
        pm = pm or PlayerManager()
        if not pid:
            players = pm.get_all_players()
            pid = players[0].mac if players else None
        if not pid:
            return

        # Record the client's sequence number for the locally maintained
        # parameters (volume/power) — Perl does this in mixerCommand
        # (Commands.pm:559-562) and powerCommand (:2586-2589) and echoes the
        # value in the status/audg frame. Without it SqueezePlay sees
        # "out of sync" and re-sends the command forever.
        if cmd in ("power", "mixer"):
            seq_no = _seq_no_from_args(args)
            if seq_no is not None:
                p = pm.get_player(pid)
                if p is not None:
                    p.seq_no = seq_no

        def send(cmd_str: str) -> None:
            try:
                pm.send_command(pid, cmd_str)
            except Exception:
                pass

        if cmd == "power":
            val = str(args[0]) if args else "1"
            player = pm.get_player(pid)
            if player is not None:
                player.power = val in ("1", "on", "toggle", "")
                pm.set_power(pid, player.power)
            else:
                send(f"power {val}")
        elif cmd == "playerpref":
            # Perl prefCommand (Commands.pm:2631-2677): '_prefname' ist der
            # Token nach dem Kommando, der Wert kommt als 'value'/'_newvalue'
            # (das valtag der Jive-Action), ein 'value:'-Präfix wird entfernt;
            # für playerpref ist ein Client Pflicht (sonst bad dispatch).
            # Die Wirkung entsteht über die setChange-Callbacks (Player.pm:79)
            # — bei uns apply_player_pref() (audg-Bytes, Mixer-Werte).
            from lyrion.player.playerprefs import apply_player_pref

            want = None
            if args:
                want = str(args[0])
                if want.startswith("value:"):
                    want = want[6:]
            player = pm.get_player(pid)
            if player is None or not want:
                return
            value = None
            for token in args[1:]:
                t = str(token)
                if t.startswith("value:"):
                    value = t[6:]
                    break
            if value is None and len(args) > 1:
                value = str(args[1])
            if value is None:
                return
            result = apply_player_pref(player, want, value)
            logger.debug("playerpref %s=%s -> %s", want, value, result)
        elif cmd == "pause":
            player = pm.get_player(pid)
            if player is None:
                send(f"pause {args[0]}" if args else "pause")
            else:
                # Perl parity (Slim/Control/Commands.pm:731-753):
                #   explicit value -> truthy = 'pause', falsy = 'play'
                #   no value       -> toggle ('play' from pause/stop, else 'pause')
                #   'play' while paused becomes 'resume' (position kept),
                #   'play' while stopped starts the current playlist item.
                curmode = player.mode or "stop"
                if args:
                    raw = str(args[0])
                    try:
                        want = "pause" if float(raw) != 0 else "play"
                    except ValueError:
                        want = "pause" if raw else "play"
                else:
                    want = "play" if curmode in ("pause", "stop") else "pause"
                if want == "pause":
                    # only from 'play' (Perl: pause cannot pause a stopped player)
                    if curmode == "play":
                        await pm.pause_player(pid, True)
                else:
                    if curmode == "pause":
                        await pm.pause_player(pid, False)   # Perl's 'resume'
                    elif curmode == "stop":
                        await pm.power_on_for_playback(player)
                        pm.set_mode(pid, "play")
                        await self._play_playlist_item(
                            pm, player, player.playlist_position or 0
                        )
        elif cmd == "play":
            player = pm.get_player(pid)
            if player is None:
                send("play")
            else:
                # Perl playcontrolCommand (Slim/Control/Commands.pm:697-781):
                #   * 'play' while PAUSED becomes 'resume'
                #     (``$wantmode = 'resume' if ($curmode eq 'pause' &&
                #     $wantmode eq 'play')``, :743) → ``Source::playmode``'s
                #     resume branch (Slim/Player/Source.pm:83-85,
                #     ``$controller->resume``) → ``strm 'u'``
                #     (Squeezebox2.pm:1104-1110): the paused output continues,
                #     the file is NOT streamed again and the position is kept.
                #   * 'play' from stop/loading goes through
                #     ``['playlist', 'jump', playingSongIndex]`` (:758-763);
                #     playlistJumpCommand powers the player on (:942-944) and
                #     starts the item at that index (``controller()->play``,
                #     :1020). ``playingSongIndex`` is never negative — a
                #     negative stored index must not swallow the command.
                #   * 'play' while already playing is a NO-OP: the whole
                #     action sits behind ``if ($curmode ne $wantmode)`` (:756).
                curmode = player.mode or "stop"
                if curmode == "pause":
                    await pm.pause_player(pid, False)          # Perl 'resume'
                elif curmode != "play":
                    await pm.power_on_for_playback(player)
                    idx = int(player.playlist_position or 0)
                    if idx < 0:
                        # Perl's playingSongIndex (Source.pm:229-233:
                        # ``return $song ? $song->index() : 0``) never
                        # returns a negative index; a stale -1 (e.g. after a
                        # failed strm send) must restart at the first item
                        # instead of silently doing nothing.
                        idx = 0
                    await self._play_playlist_item(pm, player, idx)
        elif cmd == "stop":
            player = pm.get_player(pid)
            if player is not None:
                player.mode = "stop"
                pm.set_mode(pid, "stop")
                # Real frame to the player (strm 'q') — state alone does
                # not stop Squeezelite.
                await pm.stop_player(pid)
            else:
                send("stop")
        elif cmd == "mixer":
            # Perl mixerCommand (Commands.pm:545-665): entities volume, muting,
            # treble, bass, pitch, stereoxl; a leading +/- is RELATIVE to the
            # current value, every entity is clamped to the player's range.
            entity = str(args[0]).lower() if args else ""
            player = pm.get_player(pid)
            if player is None and args and len(args) > 1:
                # no local player object: pass the raw command through
                send(f"mixer {entity} {args[1]}")
            elif player is not None and len(args) > 1 and entity:
                raw = str(args[1])
                if entity == "muting":
                    # Perl: no value / 'toggle' toggles; a real mute is a
                    # temporary gain of 0 that keeps the volume pref.
                    new_mute = (not player.mute) if raw in ("", "toggle") \
                        else raw not in ("0", "false", "off")
                    if new_mute != player.mute:
                        player.mute = new_mute
                        # fade_volume(±0.3125) in Perl: mute -> gain 0,
                        # unmute -> the unchanged volume pref.
                        await pm.set_volume(pid, 0 if new_mute else player.volume)
                elif entity in ("volume", "bass", "treble", "pitch"):
                    lo, hi = _mixer_range(entity)
                    new = _mixer_new_value(int(getattr(player, entity) or 0), raw)
                    if new is not None:
                        new = max(lo, min(hi, new))
                        setattr(player, entity, new)
                        if entity == "volume":
                            # audg frame — the SlimProto channel has no text CLI
                            await pm.set_volume(pid, new)

        elif cmd == "playlistcontrol":
            # SqueezePlay's My-Music play/add/insert (base.actions → cmd
            # playlistcontrol cmd:load|add|insert + the item's commonParams
            # ids). Route onto the playlist command with the same filter
            # tokens.  Perl's allowed cmds: load|insert|add|delete
            # (Slim/Control/Commands.pm:1887); MENU-04 is the 'insert' path —
            # before this it fell through to 'add' and appended instead of
            # playing next.
            tokens = [str(a) for a in args]
            op = next((t.split(":", 1)[1] for t in tokens
                       if t.startswith("cmd:")), "load")
            sub = {"load": "play", "insert": "insert"}.get(op, "add")
            rest = [t for t in tokens
                    if not t.startswith(("cmd:", "menu:", "useContextMenu"))]
            await self._json_control(pm, pid, "playlist", [sub] + rest)
        elif cmd == "playlist":
            sub = args[0] if args else ""
            rest = args[1:] if len(args) > 1 else []
            # Queue-verändernde Subs setzen bei Perl das Modified-Flag
            # (``$client->currentPlaylistModified(1)`` in playlistXitemCommand/
            # playlistJumpCommand, Commands.pm:831/:912/:1058/:1506); die
            # Query ``playlist modified ?`` antwortet damit (Queries.pm:2740-2741).
            # Ein aus einer gespeicherten Playlist geladener Queue setzt 0
            # (Commands.pm:1162) — deshalb nur add/insert/play/delete/move.
            if sub in ("add", "insert", "play", "delete", "move", "zap"):
                _pl = pm.get_player(pid)
                if _pl is not None:
                    setattr(_pl, "playlist_modified", 1)
            if sub == "add" and rest:
                # Accept a DB track id, a 'track_id:<n>' tag (controllers),
                # a plain URL, or album/artist/year/genre filters (SqueezePlay
                # playlistcontrol add) that expand to every matching track.
                player = pm.get_player(pid)
                if player is not None:
                    tagged = {}
                    for _a in rest:
                        _s = str(_a)
                        if ":" in _s:
                            _k, _, _v = _s.partition(":")
                            tagged[_k] = _v
                    if any(k in tagged for k in
                           ("album_id", "artist_id", "year", "genre_id",
                            "folder_id")):
                        try:
                            _ids = _expand_track_ids(tagged)
                            for _tid in _ids:
                                if _tid not in player.playlist:
                                    player.playlist.append(_tid)
                            player.playlist_total = len(player.playlist)
                            player.last_activity = time.time()
                            return
                        except Exception:  # noqa: BLE001
                            pass
                    pending = ""
                    for item in rest:
                        low = str(item).lower()
                        if low.startswith("track_id:") or low.startswith("item_id:"):
                            tid = low.split(":", 1)[1]
                            if tid.isdigit():
                                player.playlist.append(int(tid))
                        elif low.startswith("url:"):
                            # The VALUE must come from the original token: a
                            # lowercased URL is a different URL (paths are
                            # case-sensitive). Perl keeps the item verbatim
                            # (playlistXitemCommand, Commands.pm:1354-1359).
                            pending = str(item).split(":", 1)[1]
                        elif low.startswith("title:"):
                            title = str(item).split(":", 1)[1]
                            # Append the pending bare URL (it was held waiting
                            # for a paired title) and record its display name.
                            if pending:
                                player.playlist.append(pending)
                                self._set_stream_title(player, pending, title)
                                pending = ""
                            else:
                                # 'url:<x> title:<y>' form — URL already parsed.
                                self._set_stream_title(player, pending or "", title)
                        elif low.startswith("image:"):
                            # Paired logo path for the pending stream URL.
                            self._set_stream_image(player, pending or "", str(item).split(":", 1)[1])
                        elif str(item).isdigit():
                            player.playlist.append(int(item))
                        else:
                            # bare URL — remember it and wait for a paired title
                            if pending:
                                player.playlist.append(pending)
                            pending = str(item)
                        player.last_activity = time.time()
                    if pending:
                        player.playlist.append(pending)
                    player.playlist_total = len(player.playlist)
            elif sub == "insert" and rest:
                # MENU-04: Perl's "play next" (``playlistcontrol cmd:insert``).
                # Perl appends the new tracks and then moves that block to
                # ``playingSongIndex + 1`` (Slim/Player/Playlist.pm
                # ``addTracks``/``_insert_done``:992-1050) — the rows land
                # directly after the currently playing song, not at the end.
                player = pm.get_player(pid)
                if player is not None:
                    tagged = {}
                    for _a in rest:
                        _s = str(_a)
                        if ":" in _s:
                            _k, _, _v = _s.partition(":")
                            tagged[_k] = _v
                    new_ids: list = []
                    if any(k in tagged for k in
                           ("album_id", "artist_id", "year", "genre_id",
                            "folder_id")):
                        try:
                            new_ids = list(_expand_track_ids(tagged))
                        except Exception:  # noqa: BLE001
                            new_ids = []
                    else:
                        for item in rest:
                            low = str(item).lower()
                            if low.startswith(("track_id:", "item_id:")):
                                _tid = low.split(":", 1)[1]
                                if _tid.isdigit():
                                    new_ids.append(int(_tid))
                            elif str(item).isdigit():
                                new_ids.append(int(item))
                    if new_ids:
                        playlist = list(player.playlist or [])
                        pos = int(player.playlist_position or 0) + 1
                        pos = max(0, min(pos, len(playlist)))
                        playlist[pos:pos] = new_ids
                        player.playlist = playlist
                        player.playlist_total = len(playlist)
                        player.last_activity = time.time()
                        return
            elif sub in ("index", "jump") and rest:
                # Perl serves BOTH verbs from playlistJumpCommand
                # (Commands.pm:925) and takes an absolute index as well as a
                # relative '+n'/'-n' offset (:970-1014). The old port only
                # accepted a plain digit here, so SqueezeJS' HTTP-client form
                # ``playlist index +1`` (UI.js:306) and every ``playlist jump``
                # were silently swallowed.
                from lyrion.player.manager import jump_target

                player = pm.get_player(pid)
                if player is not None:
                    target = jump_target(player, rest[0])
                    if target is not None:
                        await self._play_playlist_item(pm, player, target)
            elif sub == "play":
                # LMS-compatible 'playlist play [<index>|track_id:<n>|item_id:<n>|album_id:<n>|artist_id:<n>|<url>]'
                player = pm.get_player(pid)
                if player is not None:
                    tagged = {}
                    for a in rest:
                        s = str(a)
                        if ":" in s:
                            k, _, v = s.partition(":")
                            tagged[k] = v
                    if "album_id" in tagged or "artist_id" in tagged \
                            or "year" in tagged or "genre_id" in tagged \
                            or "folder_id" in tagged:
                        # Expand to all matching tracks (one query).
                        try:
                            ids = _expand_track_ids(tagged)
                            if ids:
                                player.playlist = list(ids)
                                player.playlist_total = len(ids)
                                player.playlist_position = 0
                                await self._play_playlist_item(pm, player, 0)
                                return
                        except Exception:
                            pass
                    idx = player.playlist_position or 0
                    first = str(rest[0]).lower() if rest else ""
                    if first.startswith("item_id:"):
                        # Favorite by hierarchical id (SqueezePlay/Squeezer).
                        # route through the favorite manager so the CORRECT
                        # stream plays (resolve_path uses dbid), not the
                        # current playlist index.
                        try:
                            from lyrion.music.favorites import get_favorites_manager
                            from lyrion.control.cli_commands import _fav_resolve_id
                            fm = get_favorites_manager()
                            fav_id = await _fav_resolve_id(fm, str(rest[0]).split(":", 1)[1])
                            if fav_id is not None:
                                await fm.play(pid, fav_id)
                                return
                        except Exception:
                            pass
                    elif "://" in str(rest[0]) if rest else False:
                        # Stream URL. The Jive action tags it ('url:http://…',
                        # see the same token in _set_stream_title below); the
                        # tag is transport metadata, the URL is the value.
                        # Perl uses the positional item VERBATIM as the URL —
                        # only whitespace is stripped, nothing is lowercased
                        # or prefixed (playlistXitemCommand, Commands.pm:1354-
                        # 1359: ``my $url = blessed($item) ? $item->url : $item``).
                        _url = str(rest[0])
                        if _url[:4].lower() == "url:":
                            _url = _url[4:]
                        await pm.play_url(pid, _url, "")
                        return
                    elif rest and str(rest[0]).isdigit():
                        idx = int(rest[0])
                    elif rest and first.startswith("track_id:"):
                        # Flush a bare track id as a one-item playlist entry.
                        tid = str(rest[0]).split(":", 1)[1]
                        self._playlist_flush_track(pm, player, pid, tid)
                        idx = player.playlist_position or 0
                    else:
                        idx = player.playlist_position or 0
                    player.playlist_position = idx
                    await self._play_playlist_item(pm, player, idx)
            elif sub == "stop":
                player = pm.get_player(pid)
                if player is not None:
                    await pm.stop_player(pid)
                else:
                    send("stop")
            elif sub == "clear":
                player = pm.get_player(pid)
                if player is not None:
                    player.playlist = []
                    player.playlist_total = 0
            elif sub == "shuffle":
                player = pm.get_player(pid)
                if player is not None and rest:
                    try:
                        player.shuffle = max(0, min(2, int(str(rest[0]))))
                    except ValueError:
                        pass
            elif sub == "repeat":
                player = pm.get_player(pid)
                if player is not None and rest:
                    try:
                        player.repeat = max(0, min(2, int(str(rest[0]))))
                    except ValueError:
                        pass
        elif cmd == "button":
            # Perl buttonCommand (Commands.pm:263-291) → IR::executeButton;
            # the preset keys arrive as 'playPreset_<n>' (Common.pm:908).
            await self._json_button(pm, pid, args)
        elif cmd == "jivesetalbumsort":
            # Perl jiveSetAlbumSort (Jive.pm:329-335): Server-Pref
            # 'jivealbumsort' auf den sortMe-Parameter setzen.
            sort = ""
            for tok in args:
                s = str(tok)
                if s.startswith("sortMe:"):
                    sort = s.split(":", 1)[1]
            if sort:
                from lyrion.config import get_prefs
                try:
                    await get_prefs().set("jivealbumsort", sort)
                except RuntimeError:
                    # Prefs-Store ohne DB (z. B. Test-/CLI-Prozess): Wert wie
                    # set() im Cache halten, damit die Menü-Abfrage ihn sieht.
                    get_prefs()._cache["jivealbumsort"] = sort
        elif cmd in ("jiveblankcommand", "jivedummycommand"):
            # Perl-Stubs: jiveblankcommand => sub { return 1 } (Jive.pm:159-160),
            # jiveDummyCommand => return (Jive.pm:2799-2801) → kein Result
            # (Live-Probe: beide antworten mit {}).
            return
        else:
            send(f"{cmd} {' '.join(args)}")

    @staticmethod
    def _set_stream_title(player, url: str, title: str) -> None:
        """Associate a display title with a stream URL held in the playlist.

        'playlist add <url> title:<name>' stores the station name so playlist
        rendering shows it instead of the generic 'Radio Stream'. The title is
        keyed by URL on the player, not baked into the playlist entry (which
        stays a str URL to keep the slimproto/CLI paths simple).
        """
        if not url or not title:
            return
        try:
            titles = getattr(player, "stream_titles", None)
            if titles is None:
                titles = {}
                player.stream_titles = titles
            titles[url] = title
        except Exception:
            pass

    @staticmethod
    def _set_stream_image(player, url: str, image: str) -> None:
        """Associate a logo/artwork path with a stream URL in the playlist.

        'playlist add <url> image:<path>' stores the station logo so the Now
        Playing panel renders it instead of the generic radio icon. The stored
        path is normalized to a URL relative to static_dir (which already
        contains 'html/') — an 'html/...' prefix would otherwise be doubled
        and 404 when the path is re-composed.
        """
        if not url or not image:
            return
        try:
            # Drop a leading 'html/' so '/html/images/x' -> '/images/x'.
            image = str(image)
            if image.startswith("html/"):
                image = image[len("html/"):]
            images = getattr(player, "stream_images", None)
            if images is None:
                images = {}
                player.stream_images = images
            images[url] = image
        except Exception:
            pass

    async def _play_playlist_item(self, pm, player, idx: int) -> None:
        """Send a strm frame for playlist item idx (track id or stream URL)."""
        try:
            items = getattr(player, "playlist", []) or []
            if idx < 0 or idx >= len(items):
                return
            item = items[idx]
            handler = getattr(pm, "_protocol_handler", None)
            if handler is None:
                return
            # Playing implies power-on (like real LMS, which also switches
            # the audio outputs on: Player.pm:268 -> aude(1)).
            await pm.power_on_for_playback(player)
            # Set the mode SYNCHRONOUSLY BEFORE the (async) stream send, so
            # player.status reports 'playing' immediately after a play command
            # (real LMS does this). Otherwise a remote/stream start lags and a
            # status poll right after the command still reports the old mode,
            # causing the UI icon to flip back momentarily.
            player.mode = "play"
            player.playlist_position = idx
            if isinstance(item, int):
                player.current_track_id = item
                player.remote = 0  # local track
            else:
                player.remote = 1  # live stream: never "track end"
            pm.set_mode(player.mac, "play")
            ok = True
            if isinstance(item, int):
                ok = await handler.send_strm_to_player(player.mac, item)
            else:
                from lyrion.networking.protocol import SlimProtoClient

                codec = SlimProtoClient._guess_codec_from_url(str(item))
                ok = await handler.send_remote_stream(player.mac, str(item), codec)
            # If the strm could not be delivered (player disconnected /
            # writer gone), don't leave the player stuck in 'play': revert
            # to 'stop' so the UI poll flips the icon back. This mirrors the
            # real LMS, which only reports 'playing' once the stream is
            # actually accepted.
            if not ok:
                logger = __import__("logging").getLogger("lyrion.web.api")
                logger.warning("Failed to send strm to %s (item %d) — reverting to stop", player.mac, idx)
                player.mode = "stop"
                player.playlist_position = -1
                pm.set_mode(player.mac, "stop")
                return
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.info("Playing playlist item %d (%r) on %s", idx, item, player.mac)
        except Exception as exc:
            logger = __import__("logging").getLogger("lyrion.web.api")
            logger.warning("_play_playlist_item failed: %s", exc)

    def _playlist_flush_track(self, pm, player, pid: str, tid: str) -> None:
        """Replace the playlist with a single track id and start it.

        Handles 'playlist play track_id:<n>' where the controllers send a
        bare tagged id (no playlist yet) — LMS play-by-track-id semantics.

        Perl resolves a COMMA separated id list the way playlistcontrol
        ``cmd:load`` does: the ids are split (``split(/,/, $track_id_list)``,
        Commands.pm:2027-2035), the queue is replaced
        (``Slim::Player::Playlist::stopAndClear``, Commands.pm:1941/:1694-1696,
        followed by ``addTracks``, :1752-1753) and playback starts with the
        FIRST loaded item (``playlist jump`` with an undefined index,
        playlistXtracksCommand :1796 → ``controller()->play(0)``, :1020).
        The sent order is kept (``%track_ids_order``, :2030-2049).
        """
        ids = [int(t) for t in str(tid).split(",") if str(t).strip().isdigit()]
        if not ids:
            return
        player.playlist = ids
        player.playlist_total = len(ids)
        player.playlist_position = 0
        player.last_activity = time.time()

    async def _json_search(self, args: list[str]) -> dict:
        """LMS 'search <start> <count> term:<begriff>' — grouped results.

        Returns count plus artists_count/albums_count/genres_count/
        tracks_count and the per-group loops (id + name), like the real
        LMS grouped search response.
        """
        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 20
        term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
        empty = {
            "count": 0, "artists_count": 0, "albums_count": 0,
            "genres_count": 0, "tracks_count": 0, "contributors_count": 0,
            "artists_loop": [], "contributors_loop": [], "albums_loop": [],
            "genres_loop": [], "tracks_loop": [],
        }
        if not term:
            return empty
        like = f"%{term}%"
        try:
            artists = _db_query(
                "SELECT DISTINCT c.id, c.name FROM contributors c "
                "JOIN tracks_contributors tc ON tc.contributor = c.id "
                "AND tc.role = 1 WHERE c.name LIKE ? "
                "ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, count, start),
            )
            albums = _db_query(
                "SELECT id, title FROM albums WHERE title LIKE ? "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                (like, count, start),
            )
            genres = _db_query(
                "SELECT DISTINCT genre AS name FROM tracks "
                "WHERE genre LIKE ? ORDER BY genre COLLATE NOCASE "
                "LIMIT ? OFFSET ?",
                (like, count, start),
            )
            tracks = _db_query(
                "SELECT id, title, url, duration FROM tracks "
                "WHERE title LIKE ? ORDER BY title COLLATE NOCASE "
                "LIMIT ? OFFSET ?",
                (like, count, start),
            )
        except Exception:
            return empty
        a_count = _db_query(
            "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
            "JOIN tracks_contributors tc ON tc.contributor = c.id "
            "AND tc.role = 1 WHERE c.name LIKE ?", (like,))
        al_count = _db_query(
            "SELECT COUNT(*) AS n FROM albums WHERE title LIKE ?", (like,))
        # LIB-10: echte Genre-IDs aus der genres-Tabelle (Perls
        # ``genres_loop`` trägt sie, Queries.pm:1971-1973). Solange die
        # Tabelle leer ist (kein Rescan seit ihrer Einführung), bleibt es beim
        # Textpfad — dann wird ``genre_id`` bewusst NICHT erfunden.
        try:
            # Unsere Spalte heißt ``sortkey`` (Perls ``namesort``,
            # importer.py:88-95/_sort_string; die ``genres``-Tabelle hat KEIN
            # ``namesort`` — hier stand faelschlich der Perl-Spaltenname, was
            # die gesamte Suchantwort auf {} fallen liess).
            g_rows = _db_query(
                "SELECT id, name FROM genres WHERE namespell LIKE ? "
                "ORDER BY sortkey", (like,))
        except Exception as exc:  # noqa: BLE001 — Legacy-DB ohne Spalte
            logger.debug("genres search unavailable, text fallback: %s", exc)
            g_rows = []
        if g_rows:
            g_count = [{"n": len(g_rows)}]
        else:
            g_count = _db_query(
                "SELECT COUNT(DISTINCT genre) AS n FROM tracks "
                "WHERE genre LIKE ?", (like,))
            g_rows = _db_query(
                "SELECT NULL AS id, genre AS name FROM tracks "
                "WHERE genre LIKE ? GROUP BY genre", (like,))
        genres = g_rows          # Loop-Quelle: echte IDs, wenn vorhanden
        t_count = _db_query(
            "SELECT COUNT(*) AS n FROM tracks WHERE title LIKE ?", (like,))
        # Perl parity for the search loops: the Perl LMS returns MINIMAL
        # items — contributors_loop {contributor, contributor_id},
        # albums_loop {album, album_id}, tracks_loop {track, track_id}.
        # Extra keys (id/title/url) are additive; controllers ignore them.
        return {
            "count": (a_count[0]["n"] if a_count else 0)
                     + (al_count[0]["n"] if al_count else 0)
                     + (g_count[0]["n"] if g_count else 0)
                     + (t_count[0]["n"] if t_count else 0),
            "artists_count": a_count[0]["n"] if a_count else 0,
            "contributors_count": a_count[0]["n"] if a_count else 0,
            "albums_count": al_count[0]["n"] if al_count else 0,
            "genres_count": g_count[0]["n"] if g_count else 0,
            "tracks_count": t_count[0]["n"] if t_count else 0,
            "artists_loop": [{"contributor": r["name"] or "",
                              "contributor_id": r["id"]}
                             for r in artists],
            "contributors_loop": [{"contributor": r["name"] or "",
                                   "contributor_id": r["id"]}
                                  for r in artists],
            "albums_loop": [{"album": r["title"] or "",
                             "album_id": r["id"]} for r in albums],
            "genres_loop": [
                # genre_id nur, wenn die genres-Tabelle ihn liefert
                # (Perl-Feld, Queries.pm:1971-1973).
                ({"genre": r["name"] or "", "genre_id": r["id"]}
                 if r.get("id") is not None
                 else {"genre": r["name"] or ""})
                for r in genres],
            "tracks_loop": [{"track": r["title"] or "",
                             "track_id": r["id"]} for r in tracks],
        }

    # ── Radio directory (Perl's TuneIn OPML menu) ──────────────────────
    # Perl reference (read-only checkout ``/tmp/lms91/slimserver-public-9.1``):
    # ``Slim/Plugin/InternetRadio/Plugin.pm:42-59`` fetches the TuneIn
    # directory index and ``:78-205`` creates one *dynamic* OPMLBased plugin
    # per directory item (``tag => lc $subclass``, ``menu => 'radios'``,
    # ``weight``, ``type``); ``Slim/Plugin/OPMLBased.pm:117-132`` registers
    # ``[<tag>,'items','_index','_quantity']`` / ``[<tag>,'playlist','_method']``
    # on ``Slim::Control::XMLBrowser::cliQuery`` plus the ``radios`` menu query;
    # ``Slim/Plugin/InternetRadio/TuneIn.pm:33-81`` is the tag → {icon, weight}
    # table, :151-164 the unshifted "My Presets" entry.
    #
    # Data source (user decision, approved deviation): radio-browser.info
    # instead of TuneIn — see :mod:`lyrion.web.radiobrowser`.  The former
    # handler answered ``radios`` with the favourites streams, which is why the
    # controllers never reached the radio sub-menus.

    @staticmethod
    def _radio_feed_tokens(rest: list) -> tuple[int, int, dict]:
        """``(start, quantity, tagged)`` of a ``<feed> items`` request.

        The two leading positionals are the named slots of
        ``['<tag>','items','_index','_quantity']`` (``OPMLBased.pm:117-120``);
        every ``key:value`` token is a tagged param Perl reads with
        ``$request->getParam`` (``XMLBrowser.pm:303-310``: ``_index``,
        ``_quantity``, ``search``, ``want_url``, ``item_id``, ``menu``,
        ``xmlbrowserPlayControl``).
        """
        nums = [str(a) for a in rest if str(a).lstrip("-").isdigit()]
        start = int(nums[0]) if nums else 0
        qty = int(nums[1]) if len(nums) > 1 else 0
        tagged: dict = {}
        for a in rest:
            s = str(a)
            if ":" in s:
                key, _, value = s.partition(":")
                tagged[key] = value
        return start, qty, tagged

    @staticmethod
    def _radio_node(feed: str, search: str):
        """The root node of a radio sub-feed (Perl's per-tag feed URL)."""
        from lyrion.web import radiobrowser

        if feed == "search":
            # A search feed without a term has no data — Perl answers the
            # "Leer" placeholder (measured live 2026-09-14,
            # ``search items 0 6 menu:search`` → count 1, one itemNoAction row).
            if not search:
                return radiobrowser.RadioNode("empty", feed, "", "")
            return radiobrowser.RadioNode("stations", feed, search, "")
        if feed in ("podcast", "sounds"):
            return radiobrowser.RadioNode(
                "empty", feed, "", radiobrowser.FEED_TITLES.get(feed, ""))
        if feed == "presets":
            return radiobrowser.RadioNode("presets", feed)
        return radiobrowser.RadioNode(
            "index", feed, "", radiobrowser.FEED_TITLES.get(feed, ""))

    async def _radio_play_control(self, rest: list, pid: str | None) -> bool:
        """``_defeatDestructiveTouchToPlay`` for a radio sub-feed request.

        ``XMLBrowser.pm:1951-1983`` — same resolution as the favourites feed
        (:func:`_defeat_destructive_touch_to_play`): a request without a client
        lands on the defeated branch, a named idle/playing client on the plain
        ``play`` row.
        """
        player = None
        if pid:
            try:
                from lyrion.player.manager import PlayerManager
                player = PlayerManager().get_player(pid)
            except Exception:  # noqa: BLE001
                player = None
        return _defeat_destructive_touch_to_play(
            rest, player, client_named=player is not None)

    async def _json_radios(self, cmd: str, args: list[str]) -> dict:
        """``radios [<start> <count>] [menu:…]`` — Perl's Radio directory menu.

        Perl registers ``['radios','_index','_quantity']`` on
        ``OPMLBased::cliRadiosQuery`` (``Slim/Plugin/OPMLBased.pm:129-132``,
        handler :181-280) and answers through ``dynamicAutoQuery``, whose loop
        is ``$query . 's_loop'`` → ``radioss_loop`` with ``count`` last
        (``Slim/Control/Queries.pm:5384``, :5443).  With a ``menu:`` token the
        same handler emits the jive item form (``OPMLBased.pm:200-247``).

        Live Perl 9.1.1 (192.168.1.90:9000, read-only 2026-09-14)::

            radios 0 3     -> {"count":10,"radioss_loop":[{"cmd":"presets",
                                "name":"Eigene Voreinstellungen",
                                "type":"xmlbrowser",
                                "icon":"/plugins/TuneIn/html/images/radiopresets.png",
                                "weight":5}, …]}             (slice, count total)
            radios 3 3     -> Sport, Nachrichten, Talksendungen
            radios 0 3 menu:radio
                           -> {"count":10,"item_loop":[{"text":…,"weight":5,
                                "icon-id":…,"window":{"titleStyle":"album"},
                                "actions":{"go":{"cmd":["presets","items"],
                                "params":{"menu":"presets"}}}}, …],"offset":0}
        """
        from lyrion.web import menus as _menus
        from lyrion.web import radiobrowser

        start = (int(str(args[0]))
                 if args and str(args[0]).lstrip("-").isdigit() else 0)
        qty = (int(str(args[1]))
               if len(args) > 1 and str(args[1]).lstrip("-").isdigit() else 0)
        menu_mode = any(str(a) == "menu" or str(a).startswith("menu:")
                        for a in args)
        nodes = radiobrowser.root_level()
        total = len(nodes)
        if qty > 0:
            page = nodes[start:start + qty] if start <= total - 1 else []
        else:
            page = nodes[start:]
        if menu_mode:
            # OPMLBased.pm:221-246 — the search entry's input block carries the
            # translated help/softbutton strings (``$request->string(...)``).
            texts = {
                "help": _menus.menu_title("JIVE_SEARCHFOR_HELP"),
                "softbutton1": _menus.menu_title("INSERT"),
                "softbutton2": _menus.menu_title("DELETE"),
            }
            items = [
                radiobrowser.root_menu_item(
                    str(n["tag"]), str(n["text"]), int(n["weight"]),
                    str(n["icon"]), search=str(n["type"]) == "search", **texts)
                for n in page]
            return {"count": total, "item_loop": items, "offset": start}
        return {
            "count": total,
            "radioss_loop": [
                radiobrowser.root_plain_item(
                    str(n["tag"]), str(n["text"]), int(n["weight"]),
                    str(n["icon"]), search=str(n["type"]) == "search")
                for n in page],
        }

    async def _json_radio_feed(self, feed: str, rest: list,
                               pid: str | None) -> dict:
        """``<feed> items <start> <count> <params…>`` — one Radio sub-feed.

        The feed tag is one of Perl's dynamically created OPMLBased plugins
        (``InternetRadio/Plugin.pm:92-205``); its CLI dispatch is
        ``[<tag>,'items','_index','_quantity']`` (``OPMLBased.pm:117-120``) and
        the handler is ``Slim::Control::XMLBrowser::cliQuery`` (:111-114), which
        renders through ``_cliQuery_done`` (``Slim/Control/XMLBrowser.pm``
        :274-1450).  Live Perl 9.1.1, read-only 2026-09-14::

            local items 0 6 menu:local
              -> {"offset":0,"title":"Lokale Sender","base":{…},"item_loop":[
                  {"addAction":"go","actions":{"go":{"cmd":["local","items"],
                   "params":{"menu":"local","item_id":"<sid>.0"}}},"text":"Sender"},
                  {"type":"link","addAction":"go",…,"text":"Alle Deutschland"}],
                  "count":2,"window":{"windowStyle":"text_list"}}
            local items 0 4 menu:local item_id:<sid>.0       (the station level)
              -> {"offset":0,"title":"Sender","base":{…},"item_loop":[
                  {"type":"audio","text":…,"presetParams":{"favorites_url":…,
                   "favorites_title":…,"favorites_type":"audio","icon":…},
                   "icon":"/imageproxy/…/image.png","style":"itemplay",
                   "goAction":"play","params":{"item_id":"<sid>.0.0",
                   "isContextMenu":1,"touchToPlay":"<sid>.0.0",
                   "touchToPlaySingle":1}}],"count":194,
                  "window":{"windowStyle":"icon_list"}}
            local items …, item_id:<sid>.0.0                 (a station row)
              -> the row's info menu (Titel/URL/Bitrate); with
                 ``xmlBrowseInterimCM:1`` the play-control menu with
                 "In Favoriten speichern" (``menus.interim_context_menu``)

        ``count`` on a station level is the feed's **total** (194 TuneIn
        stations), not the number of rows in the window — Perl's
        ``$subFeed->{'total'}`` (``XMLBrowser.pm:792-793``), the value a client
        pages against.  With radio-browser as the source the total comes from
        the facet index (``radiobrowser.node_total``; live 2026-09-19: country
        ``DE`` 6397, tag ``pop`` 6257).  A window past the end answers
        ``count``/``offset`` without rows (Perl's invalid ``normalize()``
        branch); the ``Leer`` row belongs to a feed that is empty itself.
        """
        from lyrion.web import favorites_menu, radiobrowser

        start, qty, tagged = self._radio_feed_tokens(rest)
        use_play_control = await self._radio_play_control(rest, pid)
        search = str(tagged.get("search") or "")
        if search in ("__TAGGEDINPUT__", "__INPUT__"):
            # the placeholder the client substitutes (OPMLBased.pm:231)
            search = ""

        if feed == "presets":
            return await self._radio_presets_menu(rest, tagged, start, qty,
                                                  use_play_control)

        item_id = str(tagged.get("item_id") or "")
        node = None
        station = None
        if item_id:
            root, indices, crumb_search = radiobrowser.parse_item_id(item_id)
            if root is None or root.feed != feed:
                root = self._radio_node(feed, search or crumb_search)
            if root is not None:
                node, station, _index = await radiobrowser.resolve(root, indices)
        else:
            node = self._radio_node(feed, search)

        if station is not None:
            return await self._radio_station_leaf(feed, station, item_id, rest)

        if node is None:
            # Unknown/expired browse session or an out-of-range path: Perl
            # answers the feed's empty form (XMLBrowser.pm:837-846) instead of
            # an error.
            return favorites_menu.render_menu(
                feed, [], playcontrol_params=tagged,
                use_play_control=use_play_control, empty_placeholder=True)

        level = await radiobrowser.level_for(
            node, start=start, qty=qty, use_play_control=use_play_control)
        # ``count`` is the feed's total, ``offset`` the requested window — the
        # pair Perl's ``normalize()``/``dynamicAutoQuery`` answers with
        # (``Slim/Control/Request.pm:1805-1839``, ``XMLBrowser.pm:851``).  The
        # ``Leer`` row belongs to a feed that is empty *itself*
        # (``XMLBrowser.pm:837-846`` ``$menuMode && !$count``, ``$count = 1``);
        # a window *past* the feed's end is Perl's invalid branch instead:
        # ``count``/``offset`` (and ``window``) without a single row, no
        # placeholder — live Perl 9.1.1 ``browselibrary items 303 100 menu:1
        # mode:bmf`` answers exactly ``{count, offset, window}``.
        if not level.items and not level.total:
            return favorites_menu.render_menu(
                feed, [], title=level.title, playcontrol_params=tagged,
                use_play_control=use_play_control, count=1,
                offset=start, empty_placeholder=True)
        return favorites_menu.render_menu(
            feed, level.items, title=level.title, playcontrol_params=tagged,
            use_play_control=use_play_control,
            count=level.total,
            offset=start)

    async def _radio_station_leaf(self, feed: str, station, item_id: str,
                                  rest: list) -> dict:
        """The answer for a tapped station row (``XMLBrowser.pm:846-900``).

        Perl descends to the leaf and renders *its* rows: the info rows of
        ``@mapAttributes`` (``Titel:``/``URL:``/``Bitrate:`` — :243-268, live
        2026-09-14 ``count`` 3, ``offset`` 0), and with ``xmlBrowseInterimCM:1``
        the play-control menu of ``_playlistControlContextMenu`` (:1811-1900,
        live ``count`` 7) whose favourites row offers "In Favoriten speichern" →
        ``['jivefavorites','add']`` when the stream is not a favourite yet
        (Perl's ``findUrl`` lookup, :1871-1884).  ``base``/``window`` are absent
        — exactly like the live answers.
        """
        from lyrion.web import favorites_menu, menus

        index = str(item_id).rsplit(".", 1)[-1]
        if any(str(a).startswith("xmlbrowserPlayControl:") for a in rest):
            # A tap on a defeated row: Perl answers the same play-control menu
            # but with ``noFavorites => 1`` (XMLBrowser.pm:822-838), i.e. the
            # three playlist rows only.
            return favorites_menu.play_control_context_menu(item_id, menu=feed)
        if any(str(a) == "xmlBrowseInterimCM:1" for a in rest):
            return menus.interim_context_menu(
                item_id, menu=feed, name=station.title, url=station.url,
                icon=station.favicon, item_index=index,
                in_favorites=await self._radio_in_favorites(station.url),
                bitrate=station.bitrate)
        return menus.leaf_info_menu(station.title, station.url,
                                    bitrate=station.bitrate)

    @staticmethod
    async def _radio_in_favorites(url: str) -> bool:
        """Perl ``findUrl`` (``XMLBrowser.pm:1871``) over the whole tree."""
        if not url:
            return False
        try:
            from lyrion.music.favorites import get_favorites_manager
            flat: list = []

            def _collect(nodes) -> None:
                for node in nodes or []:
                    flat.append(node)
                    _collect(node.get("children"))

            _collect(await get_favorites_manager().list_tree())
            return any(str(n.get("url") or "") == url for n in flat)
        except Exception:  # noqa: BLE001
            return False

    async def _radio_presets_menu(self, rest: list, tagged: dict, start: int,
                                  qty: int, use_play_control: bool) -> dict:
        """Perl's ``Eigene Voreinstellungen`` node = our own saved stations.

        Perl's ``presets`` feed is the TuneIn preset list of the user's account
        (``TuneIn.pm:85`` ``PRESETS_URL``, unshifted as tag ``presets`` at
        :156-164).  This port has no TuneIn account, so the user's own stations
        — the favourites tree — are what the node shows, walked exactly like the
        favourites feed (``_fav_items_loop``: folders become drill-down rows,
        streams touch-to-play rows carrying ``presetParams``).  Every generated
        command carries ``menu:presets`` so the client's taps stay inside this
        feed (``OPMLBased.pm:210-213``).  Without a single favourite Perl's own
        answer for an empty feed is kept ("Leer", ``XMLBrowser.pm:837-846`` —
        measured live for ``presets``).
        """
        from lyrion.web import favorites_menu

        fm = None
        try:
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
        except Exception:  # noqa: BLE001
            fm = None
        parent = None
        parent_path = _new_fav_sid()
        item_id = str(tagged.get("item_id") or "")
        if item_id and fm is not None and "." in item_id:
            resolved = await fm.resolve_path(item_id)
            if resolved is not None:
                parent, parent_path = resolved, item_id
        loop: list = []
        if fm is not None:
            loop = await self._fav_items_loop(fm, parent, parent_path, False,
                                              True, use_play_control,
                                              menu="presets")
        return favorites_menu.render_menu(
            "presets", loop, playcontrol_params=tagged,
            use_play_control=use_play_control, offset=start,
            empty_placeholder=True)

    async def _json_radio_playlist(self, feed: str, rest: list,
                                   pid: str | None) -> dict:
        """``<feed> playlist <play|add|insert> <params…>`` — start a station.

        Perl registers ``[<tag>,'playlist','_method']`` on the same
        ``XMLBrowser::cliQuery`` (``OPMLBased.pm:122-125``); ``_cliQuery_done``
        marks such a request ``$isPlaylistCmd`` (:291-293), walks the item path
        and hands the row's URL to the player (:778-790; the ``play``/``playlist``
        attribute branch at :389-405).  The controllers send the row's own
        ``params`` (``base.actions.play`` has ``itemsParams => 'params'``), i.e.
        ``['local','playlist','play','menu:local','item_id:<sid>.0.0']``.

        Perl answers ``setStatusDone()`` without a result body (:1512) — an empty
        map here.  ``add``/``insert`` land on the same play: Perl's insert path
        only differs in *where* in the playlist the stream goes
        (``Slim/Control/Commands.pm`` playlist handling), the stream URL is the
        same.
        """
        from lyrion.web import radiobrowser

        if not pid:
            return {}
        item_id = next((str(a)[8:] for a in rest
                        if str(a).startswith("item_id:")), "")
        if not item_id:
            return {}
        if feed == "presets":
            # the preset rows are favourites (`_radio_presets_menu`)
            from lyrion.music.favorites import get_favorites_manager
            fm = get_favorites_manager()
            fav_id = await fm.resolve_path(item_id)
            if fav_id is None:
                return {}
            await fm.play(pid, fav_id)
            return {}
        root, indices, crumb_search = radiobrowser.parse_item_id(item_id)
        if root is None or root.feed != feed:
            root = self._radio_node(feed, crumb_search)
        if root is None:
            return {}
        _node, station, _index = await radiobrowser.resolve(root, indices)
        if station is None:
            return {}
        from lyrion.player.manager import PlayerManager

        ok = await PlayerManager().play_url(pid, station.url, station.title)
        logger.info("radio stream %s on %s: %s (%s)", feed, pid,
                    station.title, "ok" if ok else "failed")
        return {}

    async def _json_radiosearch(self, cmd: str, args: list[str],
                                pid: str | None = None) -> dict:
        """``radiosearch <start> <count> term:<text>`` — the Radio search.

        Perl has no ``radiosearch`` dispatch: its radio search is the
        ``type = search`` entry of the Radio menu, tapped as
        ``['search','items','_index','_quantity','menu:search',
        'search:<term>']`` (``OPMLBased.pm:221-246``, live Perl 9.1.1 →
        ``{"offset":0,"title":"Suchergebnisse: rock","base":{…},
        "item_loop":[…],"count":66,"window":{"windowStyle":"home_menu"}}``).
        Controllers (Squeezer/OrangeSqueeze) address the same list through this
        additive alias, so it answers with that very envelope — including
        ``base.actions.play`` (``nextWindow: nowPlaying``) and ``presetParams``
        per row.  The former handler returned ``type: audio`` rows without a
        ``base``, i.e. rows a controller could neither start nor save.
        """
        nums = [int(str(s)) for s in args if str(s).lstrip("-").isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 20
        term = next((str(a)[5:] for a in args if str(a).startswith("term:")), "")
        if not term:
            term = next((str(a)[7:] for a in args
                         if str(a).startswith("search:")), "")
        return await self._json_radio_feed(
            "search", [str(start), str(count), "menu:search",
                       f"search:{term}"], pid)

    async def _json_apps(self, args: list[str]) -> dict:
        """``apps [<index> <quantity>]`` — OPML-based app menus.

        Perl builds the dispatch per ``is_app`` plugin as
        ``['apps','_index','_quantity']`` (``Slim/Plugin/OPMLBased.pm:26-28``,
        :125-132) and answers with the item form ``cmd``/``name``/``type``/
        ``icon``/``weight`` (:252-258, weight default 1000 :34), the loop name
        ``appss_loop`` and ``count`` last (``Slim/Control/Queries.pm:5384``,
        :5443). Live Perl 9.1.1, read-only 2026-09-13::

            apps 0 5 -> {"count":1,"appss_loop":[{"type":"xmlbrowser",
                        "cmd":"sounds","weight":90,"name":"Sounds & Effekte",
                        "icon":"plugins/Sounds/html/images/icon.png"}]}

        This port ships no ``is_app``/OPML plugin (no equivalent of
        ``Plugins/Sounds``), so the list is empty — but the Perl form
        (``count`` + ``appss_loop``) is kept so clients find the loop key.
        """
        start = int(str(args[0])) if args and str(args[0]).isdigit() else 0
        qty = int(str(args[1])) if len(args) > 1 and str(args[1]).isdigit() else 0
        apps: list[dict] = []
        count = len(apps)
        page = apps[start:start + qty] if (qty > 0 and start <= count - 1) else \
            (apps if qty <= 0 else [])
        return {"count": count, "appss_loop": page}

    async def _json_musicfolder(self, args: list[str]) -> dict:
        """``musicfolder [<index> <quantity>] [folder_id:|url:] [tags:]``.

        Shared primitive ``lyrion.media.folders.musicfolder_result`` —
        Perl ``mediafolderQuery``, of which ``musicfolderQuery`` is a thin
        alias (``Slim/Control/Queries.pm:2161-2167`` → :2169-2507). It
        resolves the media dirs (``Slim/Utils/Misc.pm:727-756``), lists the
        folder through ``readDirectory`` (Misc.pm:973-1043) and emits
        ``count`` + ``folder_loop`` with item keys ``id``/``filename``/
        ``type`` (:2384-2470, count last :2507).

        Live (read-only) 2026-09-12/13, Perl 9.1.1: ``musicfolder 0 100`` →
        ``{"count":303,"folder_loop":[{"id":…,"filename":"Accept","type":
        "folder"}, …]}``.  ``item_loop``/``loop_loop``/``offset`` are our
        additive aliases (Jive/Material read ``item_loop``); ``folder_loop``
        is always present like Perl.
        """
        from lyrion.media.folders import musicfolder_result

        index = str(args[0]) if args and str(args[0]).lstrip("-").isdigit() else "0"
        quantity = (str(args[1])
                    if len(args) > 1 and str(args[1]).lstrip("-").isdigit()
                    else "0")
        folder_id = next((str(a)[10:] for a in args
                          if str(a).startswith("folder_id:")), None)
        url = next((str(a)[4:] for a in args if str(a).startswith("url:")), None)
        tags = next((str(a)[5:] for a in args if str(a).startswith("tags:")), "")
        # A numeric ``folder_id`` is one of our virtual folder ids
        # (:func:`_folder_numeric_id`): ``media/folders.resolve_folder_id``
        # would read it as a ``tracks.id`` and find nothing, so it is
        # inverted here into the directory path the filesystem lister wants.
        if url is None and folder_id and folder_id.strip().isdigit():
            resolved = _folder_dir_by_id(folder_id.strip(), _bmf_music_root())
            if resolved:
                folder_id = resolved
        res = musicfolder_result(index, quantity, folder_id=folder_id, url=url,
                                 tags=tags)
        loop = res.get("folder_loop")
        if loop is not None:
            # Perl's ``id`` is the ``tracks`` row of the directory (:2429) —
            # a positive integer, created on demand (:2263-2268).  The
            # primitive hands it out already (``media/folders._folder_id``);
            # this loop is the safety net for a folder row that still carries
            # a URL (unwritable library DB) — it tries the row once more and
            # keeps the token when that is impossible.
            for row in loop:
                num = _folder_row_numeric_id(row)
                if num is not None:
                    row["id"] = num
            res["item_loop"] = loop
            res["loop_loop"] = loop
            try:
                res["offset"] = int(index)
            except (TypeError, ValueError):
                res["offset"] = 0
        return res

    async def _info_feed(self, cmd: str, pm, pid: str | None,
                         args: list) -> dict:
        """Answer ``<feed>info items <index> <quantity> <params…>`` — the
        long-press context menu (MENU-03's ``more`` action target).

        Perl: ``Slim/Menu/AlbumInfo.pm:32-37`` etc. dispatch
        ``['<feed>info', 'items', '_index', '_quantity']`` and route into the
        shared OPML menu builder (``Slim/Menu/Base.pm``).  Our port answers
        the track/album/artist/genre feeds from the verified context-menu
        builder (``contextmenu.py``) and the year feed from
        ``menus.year_info_menu`` — both shapes copied from live Perl
        answers.

        Only the ``items`` sub-command is served; a missing feed context
        (no id token) yields ``{}`` like Perl's bad dispatch.
        """
        from lyrion.web import menus

        if not args or str(args[0]) != "items":
            return {}
        index = args[1] if len(args) > 1 else "0"
        quantity = args[2] if len(args) > 2 else "20"
        rest = list(args[3:])
        if cmd == "yearinfo":
            year = next((str(a)[5:] for a in rest
                         if str(a).startswith("year:")), None)
            if not year:
                return {}
            try:
                qty = int(str(quantity))
            except (TypeError, ValueError):
                qty = 20
            try:
                idx = int(str(index))
            except (TypeError, ValueError):
                idx = 0
            return menus.year_info_menu(year, idx, qty)
        entity = _INFO_FEED_MENUS.get(cmd)
        if entity is None:
            return {}
        from lyrion.web.contextmenu import handle_contextmenu

        tokens = [t for t in rest if not str(t).startswith("menu:")]
        return await handle_contextmenu(
            self, pm, pid, [index, quantity, f"menu:{entity}"] + tokens)

    async def _json_browselibrary(self, cmd: str, args: list[str],
                                  pid: str | None = None) -> dict:
        """browselibrary items <start> <count> mode:<albums|artists|genres|
        years|bmf|search> — the My-Music children navigation.

        SqueezePlay/SqueezeClient open My Music through this command. The
        response must be the OpenSqueeze library shape (count + loop_loop,
        items with a string id, name, type playlist/audio/folder, image,
        isaudio, hasitems) — the same shape the favorites list uses that the
        controllers render and navigate. Each item also carries the go/play
        actions so clients that drive navigation from actions work too.
        """
        # Perl's ``<feed> playlist <verb>`` action — the target of the bmf
        # feed's OWN base actions (``browselibrary playlist add|insert|play``,
        # live Perl 9.1.1 ``browselibrary items 0 3 menu:1 mode:bmf``).
        # ``Slim/Control/XMLBrowser.pm`` routes it to the feed's playlist
        # command; this port has one such handler — the ``playlist`` command —
        # which already resolves ``folder_id`` (Commands.pm:1887 allowed cmds
        # load|insert|add|delete, :1354-1359 items).  Without this branch the
        # request fell through to the ITEMS listing: a folder's "play" action
        # answered the album list instead of loading the folder.
        if args and str(args[0]) == "playlist":
            sub = str(args[1]) if len(args) > 1 else ""
            if sub in ("add", "insert", "play", "load"):
                # „Musikordner“ (mode:bmf): the tap on an AUDIO row arrives
                # here too — Perl gives every bmf audio row
                # ``goAction: 'play'`` (XMLBrowser.pm:1259-1267) and replaces
                # the all-audio window's ``base.actions.go`` with its ``play``
                # (:1429-1430), so the client sends
                # ``browselibrary playlist play <from> <qty> …row params…``
                # with the row's ``item_id``/``touchToPlay``.  Perl resolves
                # that row against its cached feed and plays it
                # (:331-405, :667-702).  This must run BEFORE the generic
                # ``playlist`` command: the client inserts the window's
                # from/qty between the command and the params
                # (SlimBrowserApplet.lua:756-767), which the generic handler
                # would read as the playlist index — and its ``folder_id``
                # expansion would load the whole folder instead of the
                # tapped track.
                if _is_bmf_tap(args):
                    await self._bmf_playlist(pid, sub,
                                             [str(a) for a in args[2:]])
                    return {}
                await self._json_control(None, pid, "playlist",
                                         [sub] + [str(a) for a in args[2:]])
            return {}

        nums = [int(s) for s in args if str(s).isdigit()]
        start = nums[0] if nums else 0
        count = nums[1] if len(nums) > 1 else 512
        mode = next((str(a)[5:] for a in args if str(a).startswith("mode:")),
                    "albums")
        search = next((str(a)[7:] for a in args if str(a).startswith("search:")),
                      "")
        # „Musikordner“ drill token: SqueezePlay sends the tapped row's
        # item params back (Perl: folder_id; older shapes: url / item_id).
        # Accept all of them for bmf so a folder tap really descends;
        # a purely numeric item_id is a track id, not a folder path.
        if mode in ("bmf", "musicfolder") and not search:
            for _k in ("folder_id", "url"):
                _tok = next((str(a)[len(_k) + 1:] for a in args
                             if str(a).startswith(_k + ":")
                             and str(a)[len(_k) + 1:].strip()), "")
                if _tok:
                    search = _tok
                    break
        if mode in ("bmf", "musicfolder") and not search:
            # Perl's canonical bmf drill: the tapped row's own ``params``
            # (``item_id`` = the folder's index path inside the feed) rides
            # back on the request and Perl resolves it against its cached
            # feed.  We resolve the index path by walking the folder tree
            # (``_bmf_index_dir``); a token that is no index path stays the
            # legacy path/url token the older clients sent.
            _tok = next((str(a)[len("item_id:") :] for a in args
                         if str(a).startswith("item_id:")
                         and str(a)[len("item_id:"):].strip()), "")
            if _tok:
                search = _bmf_index_dir(_tok, _bmf_music_root()) or _tok
        # Drill-down ids SqueezePlay merges from the parent item's
        # commonParams (album_id:45, artist_id:…, year:…, genre_id:…).
        filters: dict = {}
        for a in args:
            s = str(a)
            for key in ("album_id", "artist_id", "genre_id", "year",
                        "playlist_id"):
                if s.startswith(f"{key}:") and s[len(key) + 1:].strip():
                    filters[key] = s[len(key) + 1:].strip()
        # Play-Control context menu (MENU-02): a SqueezePlay tap on a row
        # sends `useContextMenu:1` + `xmlbrowserPlayControl:<itemIndex>`
        # (merged from the row's playControlParams). Perl answers with the
        # Add / Play-next / Play (+ Play-all) menu for that row whenever the
        # browser is in MENU mode and the xmlbrowserPlayControl token is
        # present — the value is used numerically, so an empty or
        # non-numeric token still taps item 0, and useContextMenu is not
        # required (Slim/Control/XMLBrowser.pm:805 `if ($menuMode &&
        # defined $xmlbrowserPlayControl)`).
        play_ctl = next((str(a)[22:] for a in args
                         if str(a).startswith("xmlbrowserPlayControl:")),
                        None)
        # Perl's menu mode is *any* ``menu`` parameter, not the literal
        # ``menu:1``: ``Slim/Control/XMLBrowser.pm:313`` ``my $menuMode =
        # defined $menu;`` — and the browsable feeds name *themselves* in
        # ``base.actions.go`` (live Perl ``mode:bmf``:
        # ``params {mode: bmf, menu: browselibrary}``, ``BrowseLibrary.pm:
        # 2044-2046``), so the tap Squeeze Client/SqueezePlay builds from
        # that base sends ``menu:browselibrary`` back.  Answering *that* in
        # the modeless ``loop_loop`` shape (no ``item_loop``, no ``offset``)
        # made Squeeze Client's pager page on forever: its
        # ``serverHasMoreData`` is ``items.size + offset < count``
        # (``BasePagingListFragment.kt:125`` → ``model/ListResponse.kt``)
        # and an item-less answer with ``offset`` defaulting to 0 never
        # reached the folder's child count ("Musikordner zeigt nur die
        # oberste Ebene, darunter ist nichts").
        is_menu = any(str(a) == "menu" or str(a).startswith("menu:")
                      for a in args)
        # ── mode:search / mode:playlists — Perl's own feeds ──────────────
        # Both are *modeless* BrowseLibrary feeds whose item set does not
        # depend on the library rows: ``_search`` answers the five-row search
        # menu (``Slim/Menu/BrowseLibrary.pm:1031-1070``), ``_playlists`` the
        # saved-playlist list (:2154-2222).  The port answered ``mode:search``
        # with a title LIKE-search (0 rows without ``search:``) and fell back
        # to the ALBUM list for ``mode:playlists`` — live Perl 9.1.1
        # (read-only 2026-09-14) returns 5 ``type: search`` rows and, for an
        # empty playlist store, XMLBrowser's single "Leer"/``Empty``
        # placeholder row (``XMLBrowser.pm:841-846``).
        if mode in ("search", "playlists"):
            from lyrion.web import menus as _menus

            if mode == "search":
                # A *search* itself: Perl's row URLs are the target feeds
                # (``BrowseLibrary.pm:1036-1064`` ``url => $browseLibraryModeMap
                # {'artists'|'albums'|'works'|'tracks'|'playlists'}``).  The
                # client merges ``item_id`` (= the row index) and
                # ``search:<text>`` from the row's ``go`` params, and
                # XMLBrowser walks THAT feed with the query
                # (``XMLBrowser.pm:389-398``/``:497-498``).  Without this
                # routing the port would answer the search *menu* again.
                _item = next((str(a)[8:] for a in args
                              if str(a).startswith("item_id:")), "")
                _query = search.strip()
                if _item.isdigit() and _query and \
                        _query != "__TAGGEDINPUT__" and \
                        int(_item) < len(_menus.SEARCH_ENTRIES):
                    mode = _menus.SEARCH_ENTRIES[int(_item)][2]
                    search = _query
                else:
                    if is_menu:
                        items = _menus.search_menu_items()
                        return {
                            "offset": start,
                            "title": _menus.menu_title("SEARCH"),
                            "count": len(items),
                            "window": _menus.window_style_for_items(items),
                            "item_loop": items,
                            "base": {"actions": self._browselibrary_menu_actions(
                                "search", filters, start, count, False)},
                        }
                    items = _menus.search_menu_flat_items()
                    return {"title": _menus.menu_title("SEARCH"),
                            "count": len(items), "loop_loop": items}

            if mode == "playlists":
                playlists = self._saved_playlists()
                if is_menu:
                    items = _menus.saved_playlist_menu_items(playlists)
                    if not items:
                        items = [_menus.empty_placeholder_item()]
                    return {
                        "offset": start,
                        "count": len(items),
                        "window": _menus.window_style_for_items(items),
                        "item_loop": items,
                        "base": {"actions": self._browselibrary_menu_actions(
                            "playlists", filters, start, count, False)},
                    }
                if not playlists:
                    return {"count": 1,
                            "loop_loop": [_menus.empty_placeholder_flat_item(
                                f"{_new_fav_sid()}.0")]}
                loop = []
                for row in playlists:
                    pid = str(row["id"])
                    name = row["name"]
                    loop.append({
                        "id": pid, "name": name, "text": name,
                        "title": name, "type": "playlist",
                        "isaudio": 1, "hasitems": 1,
                        "actions": {
                            "go": {"player": 0,
                                   "cmd": ["browselibrary", "items"],
                                   "params": {
                                       "mode": _menus.PLAYLIST_TRACKS_MODE,
                                       "playlist_id": pid}},
                            "play": {"player": 0, "cmd": ["playlistcontrol"],
                                     "params": {"cmd": "load",
                                                "playlist_id": pid}},
                        },
                    })
                return {"count": len(loop), "loop_loop": loop}
        # A browse must ANSWER (see _LIBRARY_QUERY_TIMEOUT): the fetch is
        # bounded, a starved/timed-out library read degrades to the
        # Perl-shaped empty answer (BrowseLibrary: count 0 + empty loop)
        # instead of holding the /cometd POST open — that was the Squeezer
        # "Meine Musik / Alben dreht sich endlos" symptom.
        try:
            rows, total, plural, kind = await asyncio.wait_for(
                self._library_rows(mode, start, count, search, filters or None),
                timeout=_LIBRARY_QUERY_TIMEOUT)
        except Exception:  # noqa: BLE001 — incl. asyncio.TimeoutError
            logger.warning("browselibrary %s: library read failed/timed out",
                           mode)
            return ({"count": 0, "item_loop": [], "offset": start}
                    if is_menu else {"count": 0, "loop_loop": []})

        # Perl echoes the WINDOW's own drill token into its base params: live
        # Perl 9.1.1 ``browselibrary items 0 2 menu:1 mode:bmf
        # folder_id:204573`` → ``base.actions.play.params {mode, folder_id,
        # menu}`` (XMLBrowser.pm:899-901 merges ``$feed->{'query'}``), while
        # the WURZEL window has no drill token and carries no ``folder_id``
        # (live ``… mode:bmf`` → ``{mode, menu}``).  A row's own ``folder_id``
        # still wins in the client's merge, so the drill is unaffected.
        window_folder_id = ""
        if kind == "folder" and search:
            _wm_root = _bmf_music_root()
            _wm_dir = _bmf_resolve_dir(search, _wm_root)
            if _wm_dir:
                window_folder_id = _bmf_window_folder_id(_wm_dir)

        # menu:1 → SqueezePlay/Jive MENU window (Perl BrowseLibrary shape:
        # window.style + base.actions + text/type/commonParams items), NOT
        # the OpenSqueeze loop_loop. SqueezePlay's My-Music renders these
        # and drills via base.actions.go + each item's commonParams — a
        # numeric 'window' or a missing base made the list crash / taps
        # navigate nowhere ("Alben leer, keine Lieder").
        if is_menu:
            # Perl serves the tap menu for every audio feed (tracks,
            # albums, artists, genres, years — live probes); folder/search
            # items are not audio and keep the plain list.
            if play_ctl is not None and kind in ("tracks", "albums",
                                                 "artists", "genres", "years"):
                return self._playcontrol_context_menu(rows, start,
                                                      _playctl_index(play_ctl),
                                                      kind, filters)
            from lyrion.web import menus as _menus
            # Perl's window size is the FEED's total, never a recursive one:
            # ``my $count = $subFeed->{'total'};; $count ||= defined $items ?
            # scalar @$items : 0;`` (``Slim/Control/XMLBrowser.pm:792-793``)
            # for the bmf feed = the direct children of the browsed directory
            # (``Queries.pm:2361`` ``$count = scalar @$items`` after
            # ``readDirectory``, ``Slim/Utils/Misc.pm:973-1043``).
            total = int(total or len(rows))
            if not total:
                # Bug 7024 (``XMLBrowser.pm:841-846``): an empty menu answers
                # the ``Empty`` placeholder row instead of an empty list, and
                # reports it as its single item.  Live Perl 9.1.1
                # ``browselibrary items 0 1 menu:1 mode:bmf
                # folder_id:99999999`` → ``{count: 1, offset: 0, item_loop:
                # [{'text': 'Leer', 'style': 'itemNoAction', 'action': 'none',
                # 'type': 'text'}], base: …, window: {text_list}}``; this port
                # answered ``{count: 0, item_loop: []}``.
                empty = [_menus.empty_placeholder_item()]
                return {
                    "base": {"actions": self._browselibrary_menu_actions(
                        kind, filters, start, count, False, False,
                        window_folder_id)},
                    "count": 1,
                    "offset": start,
                    "window": _menus.window_style_for_items(empty),
                    "item_loop": empty,
                }
            if start >= total:
                # A window past the feed's end: Perl's ``normalize()`` marks it
                # invalid (``Slim/Control/Request.pm:1805-1839`` ``if ($from >
                # $lastidx) { return ($valid, 0, 0) }``), so the builder emits
                # neither ``item_loop`` nor ``base``/``title`` and adds only
                # ``count``, ``offset`` and ``window`` (``XMLBrowser.pm:851``,
                # ``:1420``, ``:1450``).  Live Perl 9.1.1 ``browselibrary items
                # 303 100 menu:1 mode:bmf`` → keys ``count``/``offset``/
                # ``window``.  (Perl's ``count`` *value* decays to 1 out there
                # — its cached-feed re-fetch answers the ``Empty`` list — while
                # this port keeps the feed's child count; the client only
                # needs an item-less window plus the offset to stop paging.)
                # An empty ``item_loop`` + a defaulted ``offset: 0`` instead
                # kept Squeeze Client paging forever (see ``is_menu`` above).
                return {"count": total, "offset": start,
                        "window": {"windowStyle": "text_list"}}
            menu = self._browselibrary_menu_items(kind, rows, mode, search,
                                                  start)
            # Perl's $presetFavSet: _jivePresetBase runs only when an item
            # carried presetParams (XMLBrowser.pm:1131-1135,1427).
            preset_fav_set = any("presetParams" in it for it in menu)
            # Perl's $allTouchToPlay (XMLBrowser.pm:801): a window whose rows
            # are ALL touch-to-play audio rows gets its base ``go`` replaced
            # by its ``play`` (:1429-1430) — the Accept folder of the live
            # Perl answers exactly that for ``menu:1 mode:bmf``.
            all_touch_to_play = bool(menu) and all(
                it.get("goAction") == "play" and it.get("type") == "audio"
                for it in menu)
            # The windowStyle is an item property, not a per-mode constant
            # (XMLBrowser.pm:1104-1127,1434-1441) — see menus.window_style_
            # for_items.  Live Perl 9.1.1 (2026-09-14): albums icon_list,
            # artists home_menu, genres/years/tracks/bmf text_list; this port
            # answered icon_list for every one of them.
            return {
                "base": {"actions": self._browselibrary_menu_actions(
                    kind, filters, start, count, preset_fav_set,
                    all_touch_to_play, window_folder_id)},
                "count": total,
                "offset": start,
                "window": _menus.window_style_for_items(menu),
                "item_loop": menu,
            }

        loop = []
        for r in rows:
            if kind == "folder" and r.get("type") == "audio":
                # A file inside the browsed „Musikordner“ directory — Perl's
                # bmf lists files next to folders. It is a leaf, so the tap
                # plays the track instead of descending.
                loop.append({
                    "id": str(r["id"]),
                    "name": r["name"],
                    "text": r["name"],
                    "title": r["name"],
                    "type": "audio",
                    "isaudio": 1,
                    "hasitems": 0,
                    "actions": {
                        "go": {"player": 0, "cmd": ["songinfo"],
                               "params": {"track_id": r["id"]}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"track_id": r["id"]}},
                    },
                })
                continue
            if kind == "albums":
                ident, name, image = str(r["id"]), r["title"] or "", \
                    (f"/music/{r['id']}/cover.jpg" if r.get("artwork") else "")
                go = ["titles"]
                go_params = {"album_id": r["id"]}
                play_params = {"album_id": r["id"]}
                icon = image or "html/images/albums.png"
            elif kind == "artists":
                ident, name = str(r["id"]), r["name"] or ""
                go = ["albums"]
                go_params = {"artist_id": r["id"]}
                play_params = {"artist_id": r["id"]}
                icon = "html/images/artists.png"
            elif kind == "genres":
                ident, name = str(r["id"]), r["genre"] or ""
                go = ["albums"]
                go_params = {"genre": name}
                play_params = {"genre": name}
                icon = "html/images/genres.png"
            elif kind == "years":
                ident, name = str(r["year"]), str(r["year"])
                go = ["albums"]
                go_params = {"year": r["year"]}
                play_params = {"year": r["year"]}
                icon = "html/images/years.png"
            elif kind == "search":
                ident, name = str(r["id"]), r.get("name") or ""
                go = ["songinfo"]
                go_params = {"track_id": r["id"]}
                play_params = {"track_id": r["id"]}
                icon = "html/images/search.png"
            elif kind == "tracks":
                # Song list (album/artist/year drill, and the ``mode:search``
                # row ``item_id:3``): one row per track.  Perl's flat
                # renderer (``XMLBrowser.pm:1378-1406``) writes id/name/type/
                # image/isaudio/hasitems, ``hasAudio`` (:1743-1751) is true
                # for a ``type: audio`` item and ``hasitems`` stays 0 for an
                # audio row with no ``items`` array — the same fields our
                # MENU shape uses for a track.  Without this branch the port's
                # flat ``mode:tracks`` answer skipped every row (empty list).
                ident, name = str(r["id"]), r["title"] or ""
                item = {
                    "id": ident, "name": name, "text": name, "title": name,
                    "type": "audio", "isaudio": 1, "hasitems": 0,
                    "actions": {
                        "go": {"player": 0, "cmd": ["songinfo"],
                               "params": {"track_id": r["id"]}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"track_id": r["id"]}},
                    },
                }
                loop.append(item)
                continue
            elif kind == "folder":
                ident, name = str(r["id"]), r["name"]
                go = ["browselibrary", "items"]
                # 'search:' is this server's original drill token,
                # 'folder_id:' the Perl one — emit both, accept both.
                go_params = {"mode": "bmf", "search": ident,
                             "folder_id": ident}
                play_params = {"folder_id": ident}
                icon = "html/images/musicfolder.png"
            else:
                continue
            item = {
                "id": ident,
                "name": name,
                "text": name,
                "title": name,
                "type": "playlist" if kind != "folder" else "folder",
                "image": icon,
                "isaudio": 1,
                "hasitems": 1,
            }
            item["actions"] = {
                "go": {"player": 0, "cmd": go, "params": go_params},
                "play": {"player": 0, "cmd": ["playlist", "play"],
                         "params": play_params},
            }
            loop.append(item)
        # ONE loop, Perl's flat XMLBrowser name: ``$menuMode ? 'item_loop' :
        # 'loop_loop'`` (XMLBrowser.pm:846) — the non-menu feed answers
        # ``loop_loop`` alone. Live Perl: ``browselibrary items 0 3
        # mode:albums`` → ``{"count":…, "loop_loop":[…]}``; the old triple
        # (loop_loop+item_loop+<plural>) tripled every page for nothing and
        # was the same defect that made the Squeeze-Client album probe
        # 22 MB instead of Perl's 1 MB.
        return {"count": total, "loop_loop": loop}

    @staticmethod
    def _browselibrary_menu_items(kind: str, rows: list, mode: str,
                                  search: str = "", start: int = 0) -> list:
        """Build the SqueezePlay/Jive MENU shape for browselibrary items
        (request token menu:1) — Perl Slim::Menu::BrowseLibrary parity:
        text/type/commonParams instead of the OpenSqueeze loop_loop shape.
        SqueezePlay drills into an entry through commonParams.<id>, so a
        missing menu shape makes taps navigate nowhere ("no songs").

        ``start`` is the absolute index of the first row (paging), used for
        the track rows' ``xmlbrowserPlayControl`` / ``play_index``
        (Perl :1003 ``$itemIndex = $start - 1``)."""
        out: list[dict] = []
        ids = []
        for r in rows:
            if kind == "albums" and r.get("id") is not None:
                ids.append(r["id"])
        artist_line: dict = {}
        if kind == "albums" and ids:
            try:
                import sqlite3
                db = sqlite3.connect(
                    f"file:{_library_db_path()}?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                marks = ",".join("?" * len(ids))
                # The album's artist line comes from the ALBUM's contributors
                # (albums_contributors — the album→artist relation Perl's
                # BrowseLibrary items use), NOT from aggregating every track's
                # contributors. The track-level join planned as
                # "SEARCH tc USING INDEX idx_tc_role (role=?)" and scanned all
                # 60k role rows for 200 albums: 2281 ms per page vs 1 ms here
                # (measured 2026-09-12). That made every album page ~2.4 s and
                # delayed playback by ~20 s (jive pages the list and opens the
                # stream afterwards).
                art = db.execute(
                    "SELECT ac.album AS aid, COUNT(DISTINCT c.id) AS n, "
                    "MIN(c.name) AS name FROM albums_contributors ac "
                    "JOIN contributors c ON c.id = ac.contributor "
                    "WHERE ac.role = 1 AND "
                    f"ac.album IN ({marks}) "
                    "GROUP BY ac.album", ids).fetchall()
                for row in art:
                    if row["n"] == 1:
                        artist_line[row["aid"]] = row["name"] or ""
                    elif row["n"] > 1:
                        artist_line[row["aid"]] = "Diverse Interpreten"
                db.close()
            except Exception:  # noqa: BLE001
                pass
        # Track rows carry presetParams (the preset/favorite base actions
        # read presetParams), which need the file URL. Looked up separately
        # and defensively: minimal/test DBs may expose tracks without url.
        # The bmf file rows need the same URL (Perl's ``_favoritesParams``,
        # XMLBrowser.pm:1131-1136/1892) — one batch query for both feeds.
        track_urls: dict = {}
        if kind in ("tracks", "folder"):
            tids = [r["id"] for r in rows
                    if isinstance(r.get("id"), int)]
            if tids:
                try:
                    marks = ",".join("?" * len(tids))
                    for row in _db_query(
                            f"SELECT id, url FROM tracks WHERE id IN ({marks})",
                            tuple(tids)):
                        track_urls[row["id"]] = row["url"]
                except Exception:  # noqa: BLE001
                    # _db_query closes its connection in a finally block, so
                    # a missing `url` column cannot leak a sqlite handle.
                    pass
        for pos, r in enumerate(rows):
            item: dict = {"type": "playlist"}
            if kind == "albums":
                title = r["title"] or ""
                artist = artist_line.get(r["id"], "")
                item["text"] = f"{title}\n{artist}" if artist else title
                item["commonParams"] = {"album_id": str(r["id"])}
                if r.get("artwork"):
                    # Jive builds '/music/' .. iconId .. '/cover' .. size
                    # (SlimServer.lua:1189 fetchArtwork) from these fields —
                    # `icon-id` must be the id OUR /music/<id>/cover endpoint
                    # accepts (the album id), and `icon` stays in Perl's
                    # relative form. Without them SqueezePlay never requests
                    # artwork and every album row shows the generic disc.
                    item["icon-id"] = str(r["id"])
                    item["icon"] = f"music/{r['id']}/cover"
                # presetParams (MENU-05) — live Perl albums item:
                # {favorites_url:"db:album.title=-&contributor.name=blamstrain",
                #  favorites_type:"playlist", favorites_title:"-",
                #  icon:"music/<id>/cover"} (icon only when artwork exists).
                # favorites_url is Perl's db: query (BrowseLibrary.pm:1597,
                # XMLBrowser.pm:1892 _favoritesParams); the % escapes come
                # from Perl's _tagsToParams (uri_escape).
                from urllib.parse import quote as _q
                fav_url = f"db:album.title={_q(title)}"
                if artist:
                    fav_url += f"&contributor.name={_q(artist)}"
                preset: dict = {"favorites_url": fav_url,
                                "favorites_type": "playlist",
                                "favorites_title": title}
                if r.get("artwork"):
                    preset["icon"] = f"music/{r['id']}/cover"
                item["presetParams"] = preset
            elif kind == "artists":
                item["text"] = r["name"] or ""
                item["commonParams"] = {"artist_id": str(r["id"])}
                item["icon"] = "html/images/artists.png"
                # Live Perl artists item: {icon:"html/images/artists.png",
                # favorites_url:"db:contributor.name=%3F",
                # favorites_title:"?", favorites_type:"playlist"}.
                from urllib.parse import quote as _q
                item["presetParams"] = {
                    "favorites_url": f"db:contributor.name={_q(r['name'] or '')}",
                    "favorites_type": "playlist",
                    "favorites_title": r["name"] or "",
                    "icon": "html/images/artists.png",
                }
            elif kind == "genres":
                item["text"] = r["genre"] or ""
                item["commonParams"] = {"genre_id": str(r["id"])}
                # No presetParams for genres — live Perl genres base.actions
                # carry no set-preset-* (no item has favorites_url), so
                # Perl's $presetFavSet stays 0 (XMLBrowser.pm:1131-1135).
            elif kind == "years":
                item["text"] = str(r["year"])
                item["commonParams"] = {"year": int(r["year"])}
                # Live Perl years item: {favorites_url:"db:year.id=2026",
                # favorites_type:"playlist", favorites_title:2026} — the
                # title is the NUMBER (Perl numifies it).
                item["presetParams"] = {
                    "favorites_url": f"db:year.id={int(r['year'])}",
                    "favorites_type": "playlist",
                    "favorites_title": int(r["year"]),
                }
            elif kind == "tracks":
                # Album/artist drill target: one row per song. Perl gives
                # every audio row goAction=playControl + playControlParams so
                # a tap opens the play-control context menu for THAT row
                # instead of re-opening the list (XMLBrowser.pm:1270-1271).
                item["type"] = "audio"
                item["text"] = r["title"] or ""
                item["goAction"] = "playControl"
                item["playControlParams"] = {
                    "xmlbrowserPlayControl": str(start + pos)}
                item["playallParams"] = {"play_index": start + pos}
                item["commonParams"] = {"track_id": int(r["id"])}
                url = track_urls.get(r["id"])
                # Perl always ships presetParams on audio items (the
                # base.actions.set-preset-* entries read them); emit at
                # least the title/type so the item shape stays usable when
                # the library has no (or an empty) file URL.
                preset = {"favorites_type": "audio",
                          "favorites_title": r["title"] or ""}
                if url:
                    preset["favorites_url"] = str(url)
                item["presetParams"] = preset
            elif kind == "folder" and r.get("type") == "audio":
                # A file in the browsed folder (Perl bmf lists files too): an
                # audio leaf carrying the track, not a drill target.
                # ``id`` is normally the ``tracks.id`` — but a child Perl would
                # have created a row for while listing (``Queries.pm:2263-2268``
                # ``objectForUrl({create => 1, playlist => isPlaylist($url)})``),
                # e.g. a playlist file the scan never touched, keeps its file
                # URL (``media/folders._child_item`` does the same; Perl plays
                # it through the ``tmp://`` volatile URL, ``BrowseLibrary.pm:
                # 2125-2138``).  The numeric ``track_id`` fields must survive
                # that token.
                #
                # Perl's row for such a file (live Perl 9.1.1, read-only,
                # ``browselibrary items 0 2 menu:1 mode:bmf folder_id:204573``
                # — the Accept folder)::
                #
                #   {"type": "audio", "style": "itemplay",
                #    "goAction": "play", "text": "<file name>",
                #    "textkey": "0",
                #    "params": {"item_id": "27235260.0",
                #               "touchToPlay": "27235260.0",
                #               "isContextMenu": 1},
                #    "presetParams": {...}, "icon"/"icon-id": ...,
                #    "actions": {"more": {"player": 0,
                #                         "cmd": ["trackinfo", "items"],
                #                         "params": {"track_id": 123166,
                #                                    "menu": 1},
                #                         "window": {"isContextMenu": 1}}}}
                #
                # Every one of those fields is load-bearing for the TAP:
                # ``BrowseLibrary.pm:2087-2096`` marks a track child
                # ``playall = 1``, so ``XMLBrowser.pm:1259-1267`` takes the
                # touch-to-play branch and sets ``goAction``/``style`` plus
                # ``params.touchToPlay``; Jive rewrites the window action
                # ``go`` into ``item.goAction`` (``SlimBrowserApplet.lua:
                # 1774-1853``) and — when it falls back to a base action —
                # ABORTS with ``EVENT_UNUSED`` if the item has no entry under
                # the action's ``itemsParams`` (:2003-2026 "No params entry in
                # item, no action taken").  Our old row had neither
                # ``goAction`` nor ``params``, so the tap never built a
                # request at all: "Audiodateien erscheinen, aber Antippen
                # spielt nicht ab".
                text = r["name"] or ""
                item["type"] = "audio"
                item["text"] = text
                item["textkey"] = text[:1].upper()
                item["style"] = "itemplay"
                item["goAction"] = "play"
                try:
                    ident: Any = int(r["id"])
                except (TypeError, ValueError):
                    ident = str(r["id"])
                # ``item_id``/``touchToPlay`` are Perl's own keys (the row's
                # index inside the feed, ``XMLBrowser.pm:1142``/``:1261``);
                # ``track_id`` is this port's resolvable token for the same
                # row (``_bmf_tap_track_ids``) — the client merges the whole
                # map into the base ``play``/``add``/``add-hold`` action.
                row_index = str(start + pos)
                item["params"] = {"item_id": row_index,
                                  "touchToPlay": row_index,
                                  "isContextMenu": 1}
                if isinstance(ident, int):
                    item["params"]["track_id"] = ident
                else:
                    # Row-less file: the file URL is all we have.  The tap
                    # resolves nothing for it (no ``tracks.id``) — the honest
                    # deviation from Perl's ``tmp://`` volatile URL.
                    item["params"]["url"] = str(ident)
                url = track_urls.get(r["id"]) if isinstance(ident, int) else None
                if url:
                    item["presetParams"] = {"favorites_type": "audio",
                                            "favorites_title": text,
                                            "favorites_url": str(url)}
                # Album-Artwork des Titels — Perl ``BrowseLibrary.pm:2097-2100``
                # (``image = 'music/' . coverid . '/cover'``,
                # ``artwork_track_id = coverid``) → ``XMLBrowser.pm:1160-1167``
                # (``icon``, ``icon-id``) → Jive baut daraus
                # ``/music/<icon-id>/cover_<size>_<m|f>.jpg``
                # (``SlimServer.lua:1189``).  Fehlten die Felder, blieb jede
                # Zeile in SqueezePlay ohne Cover und das Fenster auf
                # ``text_list`` (``XMLBrowser.pm:1434-1441``) — Squeeze
                # Client/Squeezer holen ihr Cover aus anderen Feldern.
                # Nur wenn ein Album wirklich Artwork hat (Perls ``if
                # $_->{coverid}``).  Der ``mode:tracks``-Feed der Albumabteilung
                # bleibt ohne Bildfeld: Perl setzt ``image``/``artwork_track_id``
                # dort nur im ``if ($name2)``-Zweig (``BrowseLibrary.pm:
                # 1898-1905``) und diese Zeilen tragen kein ``name2`` — live
                # belegt (``mode:tracks album_id:11018`` → kein ``icon``).
                aid = r.get("artwork_id")
                if aid:
                    item["icon"] = f"music/{aid}/cover"
                    item["icon-id"] = str(aid)
                    if "presetParams" in item:
                        # perl ``_favoritesParams`` (XMLBrowser.pm:1943-1944):
                        # ``icon = favorites_icon || image || icon || cover``
                        item["presetParams"]["icon"] = f"music/{aid}/cover"
                item["actions"] = {
                    "more": {"player": 0, "cmd": ["trackinfo", "items"],
                             "params": {"menu": 1, "track_id": ident},
                             "window": {"isContextMenu": 1}},
                }
            elif kind == "folder":
                text = r["name"] or ""
                ident = str(r["id"])
                item["text"] = text
                item["textkey"] = text[:1].upper()
                # A JSON *number*: the controllers parse folder ids as
                # integers (Perl hands out a ``tracks.id`` there) — a string
                # is what made Squeeze Client drop the tap.
                try:
                    item["id"] = int(ident)
                except (TypeError, ValueError):
                    item["id"] = ident
                # Two drill vocabularies on purpose: 'folder_id' is the
                # numeric folder id the controllers parse (Perl's own shape —
                # a directory is a ``tracks`` row there, Queries.pm:2311),
                # 'url' keeps the absolute path for older taps; the bmf
                # branch accepts both (api._bmf_resolve_dir).
                #
                # ``params`` is PERL's key for this feed: live Perl 9.1.1
                # bmf rows carry exactly ``"params": {"item_id": "0",
                # "isContextMenu": 1}`` and ``base.actions.go`` names
                # ``itemsParams: "params"`` (menus.base_actions), so the tap
                # sends ``browselibrary items … menu:browselibrary mode:bmf
                # item_id:0 isContextMenu:1`` and drills.  We add folder_id/
                # url INSIDE that same map: they are the tokens this server
                # can resolve without Perl's browse-session feed cache
                # (:func:`_bmf_index_dir` handles a bare item_id), and a
                # client that drops unknown keys still descends.
                _fpath = str(r.get("path") or ident)
                item["params"] = {"item_id": str(start + pos),
                                  "isContextMenu": 1,
                                  "folder_id": ident, "url": _fpath}
                # ``commonParams`` is this port's older spelling of the same
                # map (Perl uses it for the albums/artists/years feeds, NOT
                # for bmf); kept so in-tree readers that still merge it keep
                # working.
                item["commonParams"] = {"folder_id": ident, "url": _fpath}
                # Perl bmf folder item (BrowseLibrary): add/add-hold/play
                # carry the folder id, so they load the whole folder.
                item["actions"] = {
                    "add": {"player": 0, "cmd": ["playlistcontrol"],
                            "params": {"cmd": "add", "menu": 1,
                                       "folder_id": ident}},
                    "add-hold": {"player": 0, "cmd": ["playlistcontrol"],
                                 "params": {"cmd": "insert", "menu": 1,
                                            "folder_id": ident}},
                    "play": {"player": 0, "cmd": ["playlistcontrol"],
                             "params": {"cmd": "load", "menu": 1,
                                        "folder_id": ident},
                             "nextWindow": "nowPlaying"},
                }
                # No icon on a folder row: Perl's bmf feed sets only
                # type=playlist, url, passthrough and itemActions for a
                # folder (BrowseLibrary.pm:2051-2083) — live Perl 9.1.1 bmf
                # rows carry text/textkey/params/actions/type and no
                # icon/icon-id (read-only probe 2026-09-14).  Only a track
                # row with a coverid gets ``image`` (:2097-2100), which is
                # what makes $hasImage set at all in this feed.  An invented
                # musicfolder.png here is what forced windowStyle home_menu
                # (XMLBrowser.pm:1434-1441) instead of Perl's text_list.
            else:
                continue
            # Perl ships a textkey with every list row (albums:
            # ``substr($titleSort, 0, 1)`` Queries.pm:5322; artists :1401;
            # genres/albums via XMLBrowser.pm:1373) — the live 9.1.1 albums
            # page carries ``"textkey":"-"`` on its first row. Squeezer stores
            # it as ``JiveItem.textkey`` (JiveItem.java:249) and drives its
            # A-Z fast scroller from it (JiveItemListActivity.java:420-421);
            # without the field the scroller popup stays hidden.
            if "textkey" not in item:
                if kind == "albums":
                    item["textkey"] = (r["title"] or "")[:1]
                elif kind == "artists":
                    item["textkey"] = (r["name"] or "")[:1]
                elif kind == "genres":
                    item["textkey"] = (r["genre"] or "")[:1]
                elif kind == "years":
                    item["textkey"] = str(r["year"])[:1]
            out.append(item)
        return out

    @staticmethod
    def _saved_playlists() -> list[dict]:
        """The saved playlists of this library (Perl's ``playlists`` query).

        Perl's ``playlistsQuery`` (``Slim/Control/Queries.pm``, dispatch
        ``['playlists','_index','_quantity']`` Request.pm:576) lists every
        row of the ``playlists`` table; the BrowseLibrary ``_playlists`` feed
        (``Slim/Menu/BrowseLibrary.pm:2154-2222``) turns each row into an
        item.  The port's library DB carries the same table
        (``playlists.id`` / ``playlist`` / ``name`` / ``pl_type`` /
        ``disabled``); a disabled playlist (Perl: ``disabled``) is left out.

        Live Perl 9.1.1 (read-only 2026-09-14) has an **empty** playlist
        store and therefore answers XMLBrowser's ``Empty`` placeholder row
        (``:841-846``) — its "Leer" row is a property of that server's data,
        not of the feed.
        """
        try:
            rows = _db_query(
                "SELECT id, COALESCE(NULLIF(name, ''), playlist) AS name "
                "FROM playlists WHERE COALESCE(disabled, 0) = 0 "
                "ORDER BY name COLLATE NOCASE")
        except Exception as exc:  # noqa: BLE001 — no playlists table/db
            logger.debug("playlists read failed: %s", exc)
            return []
        return [{"id": r["id"], "name": r["name"] or ""} for r in rows]

    @staticmethod
    def _browselibrary_menu_actions(kind: str, filters: dict | None = None,
                                    start: int = 0,
                                    count: int = 1,
                                    preset_fav_set: bool = False,
                                    all_touch_to_play: bool = False,
                                    folder_id: str = "") -> dict:
        """Perl base.actions for a browselibrary menu window — SqueezePlay
        uses 'go' to drill (album→mode:tracks, artist→mode:albums, …),
        'play'/'add' to load the commonParams item into the playlist,
        'add-hold' to insert it (MENU-04), 'more' to open its context menu
        (MENU-03) and 'set-preset-0..9' to store it as a preset (MENU-05).
        For a TRACK list the plain go must NOT drill (tracks are leaves):
        Perl marks it context-only (window.isContextMenu), otherwise every
        single tap on a song re-opens the same list (infinite recursion).

        ``filters``/``start``/``count`` are echoed into the context action's
        params, like Perl's ``$request->getParamsCopy()``
        (Slim/Control/XMLBrowser.pm:978): the tap's follow-up request repeats
        them merged with the row's playControlParams, and without the drill
        filter it would list the whole library instead of the tapped album.

        ``preset_fav_set`` mirrors Perl's ``$presetFavSet``
        (XMLBrowser.pm:1131-1135,1427) — the caller sets it when at least one
        item of the window carried ``presetParams`` (live Perl:
        albums/artists/years/tracks yes, genres no).  The shapes live in
        ``lyrion/web/menus.py``.

        ``all_touch_to_play`` mirrors Perl's ``$allTouchToPlay``
        (XMLBrowser.pm:1429-1430) — an all-audio window's ``go`` becomes its
        ``play``, which is what makes a „Musikordner“ file tap send
        ``browselibrary playlist play`` instead of nothing.

        ``folder_id`` is the browsed window's own directory id (Perl echoes
        it into the base params, XMLBrowser.pm:899-901)."""
        from lyrion.web import menus

        return menus.base_actions(kind, filters, start, count, preset_fav_set,
                                  all_touch_to_play, folder_id)

    async def _bmf_playlist(self, pid: str | None, sub: str,
                            rest: list[str], pm=None) -> None:
        """Play/add/insert a tapped „Musikordner“ row (Perl bmf feed).

        Perl's own path for the tap of an audio row:

        * ``BrowseLibrary.pm:2087-2096`` marks a track child ``playall = 1``;
        * ``XMLBrowser.pm:1259-1267`` therefore takes the touch-to-play
          branch and stamps ``goAction: 'play'`` + ``params.touchToPlay``;
        * ``XMLBrowser.pm:1429-1430`` replaces the all-audio window's
          ``go`` with its ``play``, so the client issues
          ``browselibrary playlist play`` (:943-966 base actions);
        * the tapped row is resolved from ``item_id`` (:331-405) and a
          single audio row is played through ``playlist play <url>``
          (:667-702), while a folder row loads its whole track list.

        ``sub`` is the feed's playlist verb (``play``/``load`` from
        ``base.actions.play``, ``add``, ``insert`` from the row's
        ``playlistcontrol`` actions).  ``rest`` still contains the window's
        from/qty the client puts before the params
        (``SlimBrowserApplet.lua:756-767``) — only the ``key:value`` tokens
        are read.  ``pm`` is injectable for tests, like ``_json_control``'s.
        """
        from lyrion.player.manager import PlayerManager

        pm = pm or PlayerManager()
        if not pid:
            players = pm.get_all_players()
            pid = players[0].mac if players else None
        if not pid:
            return
        player = pm.get_player(pid)
        if player is None:
            return
        tagged: dict = {}
        for a in rest:
            s = str(a)
            if ":" in s:
                k, _, v = s.partition(":")
                tagged[k] = v
        ids = _bmf_tap_tracks(tagged)
        if not ids:
            # Perl plays a row-less file through its ``tmp://`` volatile URL
            # (BrowseLibrary.pm:2125-2138); this port refuses DB writes, so
            # such a row cannot be resolved to a stream.  Nothing is sent —
            # never a wrong track.
            logger.debug("bmf tap: no resolvable track for %s", tagged)
            return
        if sub == "add":
            for tid in ids:
                if tid not in player.playlist:
                    player.playlist.append(tid)
            player.playlist_total = len(player.playlist)
            player.last_activity = time.time()
            return
        if sub == "insert":
            # Perl inserts directly behind the currently playing song
            # (Playlist.pm addTracks/_insert_done:992-1050).
            playlist = list(player.playlist or [])
            pos = int(player.playlist_position or 0) + 1
            pos = max(0, min(pos, len(playlist)))
            playlist[pos:pos] = ids
            player.playlist = playlist
            player.playlist_total = len(playlist)
            player.last_activity = time.time()
            return
        # play/load: Perl replaces the queue (stopAndClear + addTracks,
        # Commands.pm:1941/:1694-1696) and starts with the first loaded item.
        player.playlist = list(ids)
        player.playlist_total = len(ids)
        player.playlist_position = 0
        player.last_activity = time.time()
        await self._play_playlist_item(pm, player, 0)

    @staticmethod
    def _playcontrol_context_menu(rows: list, start: int,
                                  index: int, kind: str = "tracks",
                                  filters: dict | None = None) -> dict:
        """Answer a SqueezePlay tap on an audio row with the play-control
        context menu (MENU-02) — Perl ``_playlistControlContextMenu``
        (Slim/Control/XMLBrowser.pm:1811) reached via the
        ``xmlbrowserPlayControl`` branch (:805-830).

        ``index`` is the absolute row index from the request
        (``$xmlbrowserPlayControl - $subFeed->{'offset'}``), so a paged
        request addresses the right row; an index outside the fetched
        window (negative, e.g. ``-1``) yields Perl's empty menu.

        Texts/styles/actions mirror the real Perl responses
        (tests/fixtures/perl_browselibrary_playcontrol_*.json):

        * one-item window (``items 0 1``) → Add to end / Play next / Play;
        * window with more than one item → the third text switches to
          "Diesen Titel wiedergeben" and a fourth "Alle Titel wiedergeben"
          entry is appended. Perl's condition is
          ``playalbum && defined subItemId && @{$subFeed->{items}} > 1 &&
          (subFeed/item playall)`` (:1839-1844), i.e. the *window size*, not
          the request's total ``count``.

        The per-mode target params come from the feed item action
        variables (:1623 ``_makePlayAction``; live probes: ``mode:albums`` →
        album_id + performance, ``mode:artists`` → artist_id + menu_mode,
        ``mode:genres`` → genre_id + role_id, ``mode:years`` → year,
        ``mode:tracks`` → track_id).
        """
        window = {"windowStyle": "text_list"}
        pos = index - start
        if pos < 0 or pos >= len(rows):
            # Perl only adds item_loop inside the in-range branch
            # (XMLBrowser.pm:813-830); out of range it returns just
            # window/offset/count (live probe + fixture: keys are exactly
            # ['count', 'offset', 'window']). Clients read item_loop via
            # ``.get``, so omitting the key is safe.
            return {"window": window, "offset": 0, "count": 0}
        row = rows[pos]
        if kind == "tracks":
            target: dict = {"track_id": str(row["id"])}
        elif kind == "artists":
            target = {"artist_id": str(row["id"]), "menu_mode": "artists"}
        elif kind == "genres":
            target = {"genre_id": str(row["id"]), "role_id": "ALBUMARTIST"}
        elif kind == "years":
            target = {"year": int(row["year"])}
        else:  # albums
            target = {"album_id": str(row["id"]), "performance": ""}

        def entry(cmd: str, next_window: str) -> dict:
            params = {"cmd": cmd, "menu": 1}
            params.update(target)
            return {"player": 0, "cmd": ["playlistcontrol"],
                    "params": params, "nextWindow": next_window}

        # Play-all needs >1 item in the current window (Perl); it replays
        # the request's drill filter and the tapped absolute index
        # (probe: params {cmd: load, menu: 1, play_index: 5,
        # sort: "albumtrack", album_id: "45"}).
        play_all = kind == "tracks" and len(rows) > 1
        item_loop = [
            {"text": "Am Ende hinzufügen", "style": "item_add",
             "actions": {"go": entry("add", "parentNoRefresh")}},
            {"text": "Als nächstes wiedergeben", "style": "itemNoAction",
             "actions": {"go": entry("insert", "parentNoRefresh")}},
            {"text": "Diesen Titel wiedergeben" if play_all else "Wiedergabe",
             "style": "item_play",
             "actions": {"go": entry("load", "nowPlaying")}},
        ]
        if play_all:
            all_params: dict = {"cmd": "load", "menu": 1, "play_index": index,
                                "sort": "albumtrack"}
            all_params.update(filters or {})
            item_loop.append({
                "text": "Alle Titel wiedergeben", "style": "itemNoAction",
                "actions": {"go": {"player": 0, "cmd": ["playlistcontrol"],
                                   "params": all_params,
                                   "nextWindow": "nowPlaying"}}})
        return {"window": window, "offset": 0, "count": len(item_loop),
                "item_loop": item_loop}

    async def _library_rows(self, mode: str, start: int, count: int,
                            search: str = "", filters: dict | None = None):
        """Return (rows, total, plural, kind) for a browselibrary mode.

        ``filters`` carries the drill-down ids SqueezePlay merges from the
        item's commonParams (album_id/artist_id/year/genre_id/…)."""
        filters = filters or {}
        f_artist = filters.get("artist_id")
        f_album = filters.get("album_id")
        f_genre = filters.get("genre_id")
        f_year = filters.get("year")
        f_playlist = filters.get("playlist_id")

        def q(sql, *p):
            return _db_query(sql, p)

        def total_of(sql, *p):
            rows = _db_query(sql, p)
            # _db_query returns list[dict]; grab the first row's first value.
            return list(rows[0].values())[0] if rows else 0

        if mode in ("playlistTracks", "playlisttracks"):
            # Perl ``_playlistTracks`` (''Slim/Menu/BrowseLibrary.pm:2226-2261``)
            # runs the ``playlists tracks`` query with
            # ``playlist_id:<id>`` and lists the songs of that saved
            # playlist.  The port stores them in ``playlist_items``
            # (playlist/track/position), so the drill is a join — without
            # this branch the row's ``go`` target fell back to the album
            # list (the old ``mode:playlists`` symptom).
            if not f_playlist:
                return [], 0, "tracks_loop", "tracks"
            rows = q("SELECT t.id, t.title, t.year FROM tracks t "
                     "JOIN playlist_items pi ON pi.track = t.id "
                     "WHERE pi.playlist = ? ORDER BY pi.position "
                     "LIMIT ? OFFSET ?", f_playlist, count, start)
            total = total_of("SELECT COUNT(*) FROM playlist_items "
                             "WHERE playlist = ?", f_playlist)
            return rows, total, "tracks_loop", "tracks"

        if mode == "tracks" or mode in ("songs", "titles"):
            # Album/artist/year drill → the track list (Perl mode:tracks).
            where, params = [], []
            # Perl's songs/titles WHERE: ``tracks.audio = 1 AND
            # tracks.content_type NOT IN ("cpl", "src", "ssp", "dir")``
            # (``Slim/Control/Queries.pm:4843``) — a directory row is not a
            # song.
            where.append(dir_rows.songs_clause("t"))
            if f_album:
                where.append("t.id IN (SELECT track FROM tracks_albums "
                             "WHERE album = ?)")
                params.append(f_album)
            if f_artist:
                where.append("t.id IN (SELECT track FROM tracks_contributors "
                             "WHERE contributor = ? AND role = 1)")
                params.append(f_artist)
            if f_year:
                where.append("t.year = ?")
                params.append(f_year)
            if f_genre:
                genre_text = ""
                if str(f_genre).isdigit():
                    g = _db_query("SELECT DISTINCT genre FROM tracks "
                                  "WHERE genre != '' ORDER BY genre COLLATE "
                                  "NOCASE LIMIT 1 OFFSET ?", (int(f_genre),))
                    genre_text = g[0]["genre"] if g else ""
                if genre_text:
                    where.append("t.genre = ?")
                    params.append(genre_text)
            if search:
                where.append("t.title LIKE ?")
                params.append(f"%{search}%")
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            order = (" ORDER BY t.tracknum" if f_album
                     else " ORDER BY t.title COLLATE NOCASE")
            rows = q("SELECT DISTINCT t.id, t.title, t.year FROM tracks t"
                     + where_sql + order + " LIMIT ? OFFSET ?",
                     *(params + [count, start]))
            total = total_of(
                "SELECT COUNT(DISTINCT t.id) FROM tracks t" + where_sql,
                *params)
            return rows, total, "tracks_loop", "tracks"
        if mode == "albums":
            where, params = [], []
            if f_artist:
                where.append("al.id IN (SELECT album FROM tracks_albums "
                             "WHERE track IN (SELECT track FROM "
                             "tracks_contributors WHERE contributor = ? "
                             "AND role = 1))")
                params.append(f_artist)
            if f_year:
                where.append("al.year = ?")
                params.append(f_year)
            if f_genre:
                genre_text = ""
                if str(f_genre).isdigit():
                    g = _db_query("SELECT DISTINCT genre FROM tracks "
                                  "WHERE genre != '' ORDER BY genre COLLATE "
                                  "NOCASE LIMIT 1 OFFSET ?", (int(f_genre),))
                    genre_text = g[0]["genre"] if g else ""
                if genre_text:
                    where.append("al.id IN (SELECT album FROM tracks_albums "
                                 "WHERE track IN (SELECT id FROM tracks "
                                 "WHERE genre = ?))")
                    params.append(genre_text)
            if search:
                where.append("al.title LIKE ?")
                params.append(f"%{search}%")
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            rows = q("SELECT DISTINCT al.id, al.title, al.artwork "
                     "FROM albums al" + where_sql +
                     " ORDER BY al.title LIMIT ? OFFSET ?",
                     *(params + [count, start]))
            total = total_of(
                "SELECT COUNT(DISTINCT al.id) FROM albums al" + where_sql,
                *params)
            return rows, total, "albums_loop", "albums"
        if mode == "artists":
            # ``search:`` filters the contributor name (Perl's browse-library
            # ``_artists``, ``Slim/Menu/BrowseLibrary.pm:1091-1137`` passes
            # ``$search`` into the DB search; live Perl 9.1.1, read-only
            # 2026-09-14: ``browselibrary items 0 5 mode:artists menu:1
            # search:radio`` → count 11171 → **22**, while this port answered
            # the unfiltered 10604 — the ``artists`` feed ignored ``search:``).
            artist_where, artist_params = "", ()
            if search:
                artist_where = " WHERE c.name LIKE ?"
                artist_params = (f"%{search}%",)
            rows = q("SELECT DISTINCT c.id, c.name FROM contributors c "
                     "JOIN tracks_contributors tc ON tc.contributor = c.id "
                     "AND tc.role = 1" + artist_where +
                     " ORDER BY c.name LIMIT ? OFFSET ?",
                     *(artist_params + (count, start)))
            total = total_of("SELECT COUNT(DISTINCT c.id) FROM contributors c "
                             "JOIN tracks_contributors tc ON tc.contributor = c.id "
                             "AND tc.role = 1" + artist_where, *artist_params)
            return rows, total, "artists_loop", "artists"
        if mode == "genres":
            # LIB-10: the importer now fills the genres table (id/name/
            # namesort/namespell), so the real numeric genre id that Perl
            # sends is available (``genres_loop`` items carry it; live Perl
            # 9.1.1 ``genres 0 2`` → ids 1727/1728; SQL
            # Queries.pm:1910-1911 ORDER BY namesort). Degradation only for a
            # database that has not been rescanned since the genres table was
            # added (table empty): fall back to the old stable DISTINCT-text
            # index, which _genre_id_to_text and the drill filters understand.
            #
            # ``search:`` filters the genre name — Perl's ``_genres``
            # (``Slim/Menu/BrowseLibrary.pm``, live 2026-09-14:
            # ``browselibrary items 0 5 mode:genres menu:1 search:rock`` →
            # count 762 → **91**; this port answered the unfiltered 713).
            genre_where, genre_params = "", ()
            if search:
                genre_where = " WHERE name LIKE ?"
                genre_params = (f"%{search}%",)
            rows, total = [], 0
            try:
                rows = q("SELECT id, name AS genre FROM genres" + genre_where +
                         " ORDER BY sortkey LIMIT ? OFFSET ?",
                         *(genre_params + (count, start)))
                total = total_of("SELECT COUNT(*) FROM genres" + genre_where,
                                 *genre_params)
            except Exception as exc:  # noqa: BLE001 — ältere DBs ohne Tabelle
                logger.debug("genres table unavailable, falling back: %s", exc)
            if not rows and not total:
                text_where, text_params = " WHERE genre != ''", ()
                if search:
                    text_where += " AND genre LIKE ?"
                    text_params = (f"%{search}%",)
                rows = q("SELECT genre, "
                         "ROW_NUMBER() OVER (ORDER BY genre COLLATE NOCASE) - 1 "
                         "AS id FROM (SELECT DISTINCT genre FROM tracks"
                         + text_where + ") "
                         "ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                         *(text_params + (count, start)))
                total = total_of("SELECT COUNT(DISTINCT genre) FROM tracks"
                                 + text_where, *text_params)
            return rows, total, "genres_loop", "genres"
        if mode == "years":
            rows = q("SELECT DISTINCT year FROM tracks WHERE year > 0 "
                     "ORDER BY year DESC LIMIT ? OFFSET ?", count, start)
            total = total_of("SELECT COUNT(DISTINCT year) FROM tracks "
                             "WHERE year > 0")
            return rows, total, "years_loop", "years"
        if mode in ("bmf", "musicfolder"):
            # „Musikordner“: the tree comes from the library rows — the
            # directory rows the scan/migration wrote plus the ancestor
            # directories of the track URLs. Without a root (no pref, no
            # multi-track library) the folder stays empty instead of
            # showing the first URL component ("home"/"run" bug).
            root = _bmf_music_root()
            if not root:
                return [], 0, "musicfolder_loop", "folder"
            directory = _bmf_resolve_dir(search, root) if search else root
            if not directory:
                # Unresolvable numeric folder id: Perl answers {"count": 0}
                # (live probe, see _bmf_resolve_dir).
                return [], 0, "musicfolder_loop", "folder"
            rows, total = _bmf_children(directory, start, count)
            return rows, total, "musicfolder_loop", "folder"
        if mode == "search":
            # My Music → Suchen: search track titles (falling back to an
            # empty list instead of dumping every album).
            if not search:
                return [], 0, "search_loop", "search"
            like = f"%{search}%"
            # Same songs filter as everywhere else (Queries.pm:4843).
            _sw = f"WHERE t.title LIKE ? AND {dir_rows.songs_clause('t')}"
            rows = q("SELECT DISTINCT t.id, t.title FROM tracks t "
                     + _sw + " ORDER BY t.title COLLATE NOCASE "
                     "LIMIT ? OFFSET ?", like, count, start)
            total = total_of(
                "SELECT COUNT(DISTINCT t.id) FROM tracks t " + _sw, like)
            rows = [{"id": r["id"], "name": r["title"] or ""} for r in rows]
            return rows, total, "search_loop", "search"
        # fallback: albums
        return await self._library_rows("albums", start, count)

    async def _json_years(self, start: int, count: int) -> dict:
        """years — a distinct release-year list (My Music → Jahrgang)."""
        try:
            rows = _db_query(
                "SELECT DISTINCT year FROM tracks WHERE year > 0 "
                "ORDER BY year DESC LIMIT ? OFFSET ?",
                (count, start))
            loop = [{"id": str(r["year"]), "text": str(r["year"]),
                     "name": str(r["year"]), "type": "outline",
                     "actions": {"go": {"player": 0, "cmd": ["albums"],
                                        "params": {"year": r["year"]}},
                                 "do": {"player": 0, "cmd": ["albums"],
                                        "params": {"year": r["year"]}}}}
                    for r in rows]
            total = _db_query("SELECT COUNT(DISTINCT year) FROM tracks "
                              "WHERE year > 0")[0][0] if rows else 0
            return self._browse_response(loop, total, "years_loop")
        except Exception:  # noqa: BLE001
            return self._browse_response([])

    #: Die fünf Entitäten der ``infoTotalQuery``-Dispatch-Einträge
    #: (``Slim/Control/Request.pm:501-505``).
    INFO_TOTAL_ENTITIES = ("albums", "artists", "genres", "songs", "duration")

    async def _json_info_total(self, args: list[str]) -> dict:
        """``info total <entity> ?`` — Perl ``infoTotalQuery``.

        Nur die exakt registrierte Form ``["total", <entity>, "?"]`` antwortet
        (``Request.pm:501-505``); sonst ist der Request nicht dispatchable und
        Perl liefert gar kein Result. Der Schlüssel ist immer ``_<entity>``
        (``Queries.pm:2039-2051``); gezählt wird wie im CLI-Pfad
        (``control/cli_commands.py:3378-3412``): songs = tracks, albums = albums,
        artists = DISTINCT contributors mit role 1 (``Schema.pm:3315``,
        ``tracks_contributors``), genres = nicht-leere ``tracks.genre``,
        duration = ``SUM(tracks.duration)`` (Perls ``totalTime`` summiert die
        Sekunden, ``Schema.pm:2190``).
        """
        if len(args) != 3 or str(args[0]) != "total" or str(args[2]) != "?":
            return {}
        entity = str(args[1])
        if entity not in self.INFO_TOTAL_ENTITIES:
            return {}
        value: Any = 0
        try:
            # Perl's ``totals`` are the counts of the *titles* query
            # (``Slim/Schema.pm:3305-3320`` → ``Queries.pm:2036-2049``), whose
            # base WHERE is ``tracks.audio = 1 AND tracks.content_type NOT IN
            # ("cpl", "src", "ssp", "dir")`` (``Queries.pm:4843``), and
            # ``totalTime`` sums ``secs`` over ``tracks.audio = 1``
            # (``Slim/Schema.pm:2186-2190``) — a directory row is in neither.
            if entity == "songs":
                rows = _db_query(
                    "SELECT COUNT(*) AS n FROM tracks WHERE "
                    f"{dir_rows.songs_clause('tracks')}")
                value = int(rows[0]["n"]) if rows else 0
            elif entity == "duration":
                rows = _db_query(
                    "SELECT COALESCE(SUM(duration),0) AS n FROM tracks WHERE "
                    f"{dir_rows.duration_clause('tracks')}")
                value = rows[0]["n"] if rows else 0
            elif entity == "albums":
                rows = _db_query("SELECT COUNT(*) AS n FROM albums")
                value = int(rows[0]["n"]) if rows else 0
            elif entity == "artists":
                rows = _db_query(
                    "SELECT COUNT(DISTINCT c.id) AS n FROM contributors c "
                    "JOIN tracks_contributors tc ON tc.contributor = c.id "
                    "AND tc.role = 1"
                )
                value = int(rows[0]["n"]) if rows else 0
            else:  # genres
                rows = _db_query(
                    "SELECT COUNT(DISTINCT genre) AS n FROM tracks "
                    "WHERE genre != ''")
                value = int(rows[0]["n"]) if rows else 0
        except Exception as exc:  # noqa: BLE001 — wie der CLI-Pfad: 0
            logger.debug("info total %s failed: %s", entity, exc)
            value = 0
        return {f"_{entity}": value}

    async def _json_browse(self, cmd: str, args: list[str]) -> dict:
        """Browse library tables (albums/artists/songs/genres) as JSON."""
        # browse <target> [<start> <count>] — the home menu items carry
        # actions.go/do.cmd = ["browse", <id>]; map the target onto the
        # library queries / favorites / radios so menu navigation works.
        if cmd == "browse" and args:
            target = str(args[0]).lower()
            rest = args[1:]
            if target in ("artists", "albums", "songs", "titles", "genres"):
                return await self._json_browse(target, rest)
            if target == "favorites":
                try:
                    from lyrion.music.favorites import get_favorites_manager
                    loop = await self._fav_items_loop(
                        get_favorites_manager(), None, "0", False)
                    return self._browse_response(loop)
                except Exception:
                    return self._browse_response([])
            if target == "radios":
                # Jive's browse wrapper for the Radio node — same Perl form
                # as the bare 'radios' query (radioss_loop).
                return await self._json_radios("radios", rest)
            if target == "apps":
                return await self._json_apps(rest)
            if target in ("musicfolder", "bmf", "folder"):
                return await self._json_musicfolder(rest)
            return self._browse_response([])

        start = int(args[0]) if args and str(args[0]).isdigit() else 0
        count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 50
        # P3-2: filters (genre_id/album_id/track_id/artist_id/year/search)
        # + tags: code (t=title a=artist l=album d=duration u=url g=genre y=year)
        filters: dict[str, str] = {}
        for a in args:
            s = str(a)
            if ":" in s:
                k, _, v = s.partition(":")
                if k in ("genre_id", "genre", "album_id", "track_id", "artist_id",
                         "year", "search", "tags"):
                    filters[k] = v
        tags = filters.pop("tags", "")
        plural: str | None = None  # category-specific loop name (LMS ref)
        try:
            import sqlite3
            db = sqlite3.connect(
                f"file:{_library_db_path()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row

            # genre_id:<n> — Perl restricts ``genres.id`` (Queries.pm:1832-1836)
            # and reaches the track tables over ``JOIN genre_track``
            # (:1828/1843-1854).  We resolve the id through the ``genres``
            # table (LIB-10, filled by the rescan importer) and hand the NAME
            # to the track-text filters of artists/albums below; on a legacy
            # library the id stays the index into the DISTINCT track-genre
            # list (documented divergence, no rescan in this build).
            if filters.get("genre_id"):
                name = _genre_id_to_text(filters["genre_id"])
                if name:
                    filters["genre"] = name

            def _conds(name_col: str) -> tuple[str, tuple]:
                c: list[str] = []
                p: list = []
                if filters.get("search"):
                    c.append(f"{name_col} LIKE ?")
                    p.append(f"%{filters['search']}%")
                if filters.get("year"):
                    c.append("year = ?")
                    p.append(filters["year"])
                if filters.get("genre"):
                    c.append("genre LIKE ?")
                    p.append(f"%{filters['genre']}%")
                return (" WHERE " + " AND ".join(c)) if c else "", tuple(p)

            if cmd == "artists":
                # Contributors have no role column; the role lives in
                # tracks_contributors.role (1 = artist).
                where, params = _conds("c.name")
                joins = " JOIN tracks_contributors tc ON tc.contributor = c.id AND tc.role = 1"
                extra_joins = ""
                if filters.get("album_id") or filters.get("year") or filters.get("genre"):
                    extra_joins += " JOIN tracks t ON t.id = tc.track"
                    extra_joins += " JOIN tracks_albums ta ON ta.track = t.id" \
                        if filters.get("album_id") else ""
                rows = db.execute(
                    "SELECT DISTINCT c.id, c.name FROM contributors c"
                    + joins + extra_joins + where +
                    " ORDER BY c.name LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                loop = []
                for r in rows:
                    name = r["name"] or ""
                    from urllib.parse import quote as _qa
                    item = {
                        "id": r["id"], "artist": name,
                        # Perl parity: favorites_url in artists_loop.
                        "favorites_url": f"db:contributor.name={_qa(name)}",
                    }
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["albums"],
                               "params": {"artist_id": r["id"], "menu": "tracks"}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"artist_id": r["id"]}},
                    }
                    loop.append(item)
                total = db.execute(
                    "SELECT COUNT(DISTINCT c.id) FROM contributors c"
                    + joins + extra_joins + where, params).fetchone()[0]
                plural = "artists_loop"
            elif cmd == "albums":
                where, params = _conds("al.title")
                joins = ""
                if filters.get("artist_id") or filters.get("genre"):
                    joins += " JOIN tracks_albums ta ON ta.album = al.id" \
                             " JOIN tracks t ON t.id = ta.track"
                if filters.get("artist_id") and str(filters["artist_id"]).isdigit():
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                    # The JOIN alone is not enough — add the actual
                    # contributor predicate, else every artist's albums leak.
                    cond = "tc.contributor = ?"
                    where = ((" WHERE " + cond) if not where else where + " AND " + cond)
                    params = params + (int(filters["artist_id"]),)
                rows = db.execute(
                    "SELECT DISTINCT al.id, al.title, al.year, al.artwork FROM albums al"
                    + joins + where +
                    " ORDER BY al.title LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                loop = []
                for r in rows:
                    # Perl parity (albums without tags:): id, album,
                    # performance, favorites_url, favorites_title. The
                    # Jive actions stay — controllers need them.
                    title = r["title"] or ""
                    from urllib.parse import quote as _q
                    fav_url = f"db:album.title={_q(title)}"
                    item = {
                        "id": r["id"], "album": title,
                        "performance": "",
                        "favorites_url": fav_url,
                        "favorites_title": title,
                        "year": r["year"] or 0,
                    }
                    # Jive actions: go opens the album's tracks, play
                    # plays the whole album.
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["titles"],
                               "params": {"album_id": r["id"], "menu": "songinfo"}},
                        "play": {"player": 0, "cmd": ["playlist", "play"],
                                 "params": {"album_id": r["id"]}},
                    }
                    if r["artwork"]:
                        item["coverid"] = r["id"]
                        item["coverart"] = 1
                        item["artwork_url"] = f"/music/{r['id']}/cover.jpg"
                    loop.append(item)
                total = db.execute(
                    "SELECT COUNT(DISTINCT al.id) FROM albums al" + joins + where,
                    params).fetchone()[0]
                plural = "albums_loop"
            elif cmd == "songs" or cmd == "titles":
                # tracks_albums ta is joined in the base query below — do
                # NOT add a second JOIN tracks_albums ta here (alias clash,
                # which made the album drill return count 0 / no tracks).
                _w: list[str] = []
                _p: list = []
                # Perl's titles/songs WHERE (Queries.pm:4843) — a directory
                # row is not a song.
                _w.append(dir_rows.songs_clause("t"))
                if filters.get("search"):
                    _w.append("t.title LIKE ?"); _p.append(f"%{filters['search']}%")
                joins = ""
                if filters.get("artist_id") and str(filters["artist_id"]).isdigit():
                    joins += " JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1"
                    _w.append("tc.contributor = ?"); _p.append(int(filters["artist_id"]))
                if filters.get("album_id") and str(filters["album_id"]).isdigit():
                    _w.append("ta.album = ?"); _p.append(int(filters["album_id"]))
                where = (" WHERE " + " AND ".join(_w)) if _w else ""
                params = tuple(_p)
                rows = db.execute(
                    "SELECT DISTINCT t.id, t.title, t.url, t.duration, "
                    "al.id AS album_id, al.artwork AS album_artwork "
                    "FROM tracks t"
                    " LEFT JOIN tracks_albums ta ON ta.track = t.id"
                    " LEFT JOIN albums al ON al.id = ta.album"
                    + joins + where +
                    " ORDER BY t.title LIMIT ? OFFSET ?",
                    params + (count, start)).fetchall()
                # Enrich with artist/album (songinfo tags g/a/l/d) for the
                # Web UI columns and the controller stream info.
                enrich = await self._load_tracks([r["id"] for r in rows])
                loop = []
                for r in rows:
                    info = enrich.get(r["id"], {})
                    entry = {
                        "id": r["id"], "title": r["title"] or "", "url": r["url"] or "",
                        "duration": r["duration"] or 0,
                        "artist": info.get("artist", ""),
                        "album": info.get("album", ""),
                        "genre": info.get("genre", ""),
                        # Jive actions: go opens songinfo, play plays the track.
                        "actions": {
                            "go": {"player": 0, "cmd": ["songinfo"],
                                   "params": {"track_id": r["id"]}},
                            "play": {"player": 0, "cmd": ["playlist", "play"],
                                     "params": {"track_id": r["id"]}},
                        },
                    }
                    if r["album_artwork"]:
                        entry["coverid"] = r["album_id"]
                        entry["coverart"] = 1
                        entry["artwork_url"] = f"/music/{r['album_id']}/cover.jpg"
                    loop.append(entry)
                total = db.execute(
                    "SELECT COUNT(DISTINCT t.id) FROM tracks t"
                    " LEFT JOIN tracks_albums ta ON ta.track = t.id"
                    + joins + where,
                    params).fetchone()[0]
                plural = "titles_loop"
            elif cmd == "genres":
                # ── Perl ``genresQuery`` (Slim/Control/Queries.pm:1781-1985) ──
                # SQL :1910-1911 ``SELECT DISTINCT(genres.id), genres.name,
                # genres.namesort FROM genres … ORDER BY genres.namesort``:
                # the ids are the REAL ``genres`` rows, not an offset.  Search
                # matches ``genres.namesearch LIKE`` with searchStringSplit word
                # prefixes (:1814-1823, Text.pm:209-236), ``genre_id:a,b``
                # restricts ``genres.id IN (…)`` (:1832-1836) and the ORDER is
                # the namesort collation (:1911).  count is ``COUNT(1) FROM
                # ( $sql )`` WITHOUT the LIMIT (:1919-1925) → the total, never
                # the window.  Loop ``genres_loop`` (:1945) with exactly
                # id/genre/favorites_url (:1971-1973) + ``textkey`` for
                # ``tags:s`` (:1974); an empty name goes out as JSON ``null``
                # with the bare ``db:genre.name=`` url.  Live 2026-09-12
                # ``genres 0 2`` → ``{"result":{"count":762,"genres_loop":
                # [{"genre":null,"favorites_url":"db:genre.name=","id":1727},
                #  {"genre":null,"favorites_url":"db:genre.name=","id":1728}]}}``.
                # NOT ported here: ``tags:Z`` (indexList :1901-1908,
                # _createIndexList :6875-6915) and the album_id/year/work_id/
                # library_id joins (:1857-1887) — no client of ours sends them
                # for ``genres``.
                from urllib.parse import quote as _qg
                real_ids = _genres_table_populated(db)
                if tags == "CC":
                    # ``unless $tags eq 'CC'`` skips ORDER BY + the loop
                    # (:1911/:1943) but still returns the count (:1919-1925).
                    # Live ``genres 0 2 tags:CC`` → ``{"count":762}``.
                    total = db.execute(
                        "SELECT COUNT(*) FROM genres" if real_ids else
                        "SELECT COUNT(DISTINCT genre) FROM tracks "
                        "WHERE genre != ''").fetchone()[0]
                    return {"count": total}
                gwhere, gparams = "", ()
                if real_ids:
                    gc: list[str] = []
                    gp: list = []
                    if filters.get("search"):
                        from lyrion.control.queries import search_string_split
                        patterns = search_string_split(filters["search"])
                        # Queries.pm:1817 — the split tokens are ONE condition
                        # joined with OR ('(' . join(' OR ', …) . ')').
                        if patterns:
                            gc.append("(" + " OR ".join(
                                "g.namespell LIKE ?" for _ in patterns) + ")")
                            gp += patterns
                    gids = [g.strip() for g in
                            str(filters.get("genre_id", "")).split(",")
                            if g.strip()]                     # :1833 split ','
                    if gids:
                        gc.append("g.id IN (" + ", ".join("?" * len(gids)) + ")")
                        gp += gids                                    # :1834
                    gwhere = (" WHERE " + " AND ".join(gc)) if gc else ""
                    gparams = tuple(gp)
                    total = db.execute(
                        "SELECT COUNT(*) FROM genres g" + gwhere,
                        gparams).fetchone()[0]
                else:
                    # Degradation (LIB-10): libraries imported BEFORE the
                    # genres importer ran have an empty ``genres`` table and
                    # are only fixed by the next rescan (never triggered here).
                    # Until then the DISTINCT track-genre text list is the
                    # source — the SAME pre-LIB-10 behaviour, so the genre
                    # list never goes blank; ``id`` is then the OFFSET into
                    # that list (documented divergence: no real ids).
                    lwhere, lparams = _conds("genre")
                    if lwhere:
                        lwhere = lwhere.replace(
                            " WHERE ", " WHERE genre != '' AND ", 1)
                    else:
                        lwhere = " WHERE genre != ''"
                    gwhere, gparams = lwhere, lparams
                    total = db.execute(
                        "SELECT COUNT(DISTINCT genre) FROM tracks" + gwhere,
                        gparams).fetchone()[0]

                # Window/validity — Perl ``normalize`` (Request.pm:1805-1839):
                # a missing quantity with an index means "all remaining"
                # (:1815); quantity 0 or an index past the last hit ⇒ no loop,
                # count only (:1816-1822).  Live: ``genres``, ``genres 0 0``
                # and ``genres 762 2`` all answer ``{"count":762}``; ``genres 0``
                # answers the full loop.
                idx_given = bool(args) and str(args[0]).isdigit()
                qty_given = len(args) > 1 and str(args[1]).isdigit()
                if not idx_given:
                    return {"count": total}
                if total <= 0 or start > total - 1 or (qty_given and count <= 0):
                    return {"count": total}
                limit = count if qty_given else total - start

                if real_ids:
                    rows = db.execute(
                        "SELECT g.id AS id, g.name AS name, "
                        "g.sortkey AS namesort FROM genres g" + gwhere +
                        " ORDER BY g.sortkey COLLATE NOCASE LIMIT ? OFFSET ?",
                        gparams + (limit, start)).fetchall()
                    entries = [(r["id"], r["name"], r["namesort"]) for r in rows]
                else:
                    rows = db.execute(
                        "SELECT DISTINCT genre AS name FROM tracks" + gwhere +
                        " ORDER BY genre COLLATE NOCASE LIMIT ? OFFSET ?",
                        gparams + (limit, start)).fetchall()
                    entries = [(start + i, r["name"], r["name"])
                               for i, r in enumerate(rows)]

                loop = []
                for gid, gname, gsort in entries:
                    # Queries.pm:1971-1973 in source order (Perl's JSON object
                    # order is hash-random — live shows ``id`` sometimes last).
                    item = {
                        "id": gid,
                        # NULL name stays JSON null (live); never invent a
                        # ``(None)``/'' placeholder.
                        "genre": gname or None,
                        # :1973 uri_escape_utf8 — '/', is %2F, '.' stays
                        # (live: db:genre.name=Alternative%20%2F%20Indie…).
                        "favorites_url": "db:genre.name=" +
                                         _qg(gname or "", safe=""),
                    }
                    if "s" in tags:                       # :1974
                        item["textkey"] = (gsort or "")[:1]
                    # Additive fields Perl's genresQuery does not send: the feed
                    # layer sets ``name``/``type`` on the loop item and drills
                    # with genre_id:<id> (Slim/Menu/BrowseLibrary.pm:1305-1313,
                    # :1318); ``text``/``title``/``type``/``hasitems`` are what
                    # the Android controllers read (SqueezeClient/Squeezer — see
                    # _browse_response docstring; unknown keys are ignored).
                    item["name"] = gname or ""
                    item["actions"] = {
                        "go": {"player": 0, "cmd": ["artists"],
                               "params": {"genre_id": gid, "menu": "albums"}},
                    }
                    loop.append(item)
                # The response-level extras (``offset``/``loop_loop``/
                # ``item_loop``) come from ``_browse_response`` for those same
                # clients — Perl sends only ``count`` + ``genres_loop`` here.
                plural = "genres_loop"
            elif cmd == "musicfolder":
                # mediafolderQuery/musicfolderQuery (Queries.pm:2161-2507) —
                # the shared primitive lyrion.media.folders reads the media
                # dirs (Misc.pm:727-756) and emits count + folder_loop with
                # id/filename/type. The old handler derived the listing from
                # tracks.url and answered count=1 (live Perl: 303).
                db.close()
                return await self._json_musicfolder(args)
            elif cmd == "songinfo":
                tid = filters.get("track_id") or (args[0] if args and str(args[0]).isdigit() else "")
                if not str(tid).isdigit():
                    return self._browse_response([])
                r = db.execute(
                    "SELECT t.id, t.title, t.url, t.duration, t.year, t.tracknum, "
                    "t.genre, t.filesize, t.bitrate, t.samplerate, t.channels, "
                    "t.content_type AS ctype, t.modtime, t.remote, "
                    "COALESCE(t.compilation,0) AS compilation FROM tracks t "
                    "WHERE t.id = ? LIMIT 1",
                    (int(tid),)).fetchone()
                if r is None:
                    return self._browse_response([])
                # Perl parity: songinfo_loop = ONE item PER FIELD, in the exact
                # LMS order (id, title, artist, work, duration, album_id,
                # filesize, genre, coverart, album, modificationTime, type,
                # genre_id, bitrate, artist_id, tracknum, remote, year,
                # compilation, addedTime). Empty fields are omitted. Perl
                # returns ONLY songinfo_loop (no count / loop_loop / item_loop).
                fields: list[tuple] = [("id", r["id"]), ("title", r["title"] or "")]
                a = db.execute(
                    "SELECT c.id, c.name FROM contributors c JOIN tracks_contributors tc "
                    "ON tc.contributor = c.id AND tc.role = 1 WHERE tc.track = ? "
                    "ORDER BY c.name LIMIT 1", (r["id"],)).fetchone()
                if a:
                    fields.append(("artist", a["name"]))
                # work: composer field (rare) — omit if absent
                if r["duration"] is not None:
                    fields.append(("duration", round(float(r["duration"]), 3)))
                al = db.execute(
                    "SELECT al.id, al.title, al.artwork, al.artwork_front "
                    "FROM albums al JOIN tracks_albums ta "
                    "ON ta.album = al.id WHERE ta.track = ? LIMIT 1",
                    (r["id"],)).fetchone()
                # Artwork ids — Perl's songinfo DEFAULT tag set contains c/j:
                # live `songinfo 0 10 track_id:155361` (no tags) answers
                # `{"coverid": "067cd484"}` and `{"coverart": "1"}`, while
                # `tags:gald` omits both (tagMap c/J/j, Queries.pm:5713-5725).
                # The id published here is the one our /music/<id>/cover.jpg
                # route accepts (see _artwork_id in the status builder): this
                # port's importer leaves the schema's tracks.cover empty and
                # the album id is what every other cover field carries.
                has_art = bool(al and (al["artwork"] or al["artwork_front"]))
                if has_art and ((not tags) or "c" in tags):
                    fields.append(("coverid", str(al["id"])))
                if al:
                    fields.append(("album_id", str(al["id"])))
                if r["filesize"]:
                    fields.append(("filesize", str(r["filesize"])))
                if r["genre"]:
                    fields.append(("genre", r["genre"]))
                # Artwork (see above): Perl answers artwork_track_id for tag J
                # and always the coverart flag in its default set.
                if has_art and "J" in tags:
                    # Perl answers artwork_track_id only for tag J (its
                    # default set has c/j but not J — live no-tags songinfo
                    # carries coverid + coverart, no artwork_track_id).
                    fields.append(("artwork_track_id", str(al["id"])))
                if (not tags) or "j" in tags:
                    fields.append(("coverart", "1" if has_art else "0"))
                if al:
                    fields.append(("album", al["title"]))
                if r["modtime"]:
                    fields.append(("modificationTime", str(r["modtime"])))
                type_code = {
                    "audio/flac": "flc", "audio/x-flac": "flc",
                    "audio/mpeg": "mp3", "audio/mp3": "mp3",
                    "audio/wav": "wav", "audio/x-wav": "wav",
                }.get((r["ctype"] or "").lower(), "")
                if type_code:
                    fields.append(("type", type_code))
                g_id = db.execute(
                    "SELECT id FROM genres WHERE name = ? LIMIT 1",
                    (r["genre"],)).fetchone() if r["genre"] else None
                if g_id:
                    fields.append(("genre_id", str(g_id["id"])))
                if r["bitrate"]:
                    fields.append(("bitrate", str(int(r["bitrate"]))))
                if a:
                    fields.append(("artist_id", str(a["id"])))
                if r["tracknum"]:
                    fields.append(("tracknum", str(r["tracknum"])))
                fields.append(("remote", "1" if r["remote"] else "0"))
                if r["year"]:
                    fields.append(("year", str(r["year"])))
                if r["compilation"]:
                    fields.append(("compilation", "1"))
                if r["modtime"]:
                    fields.append(("addedTime", str(r["modtime"])))
                loop = [{k: v} for k, v in fields]
                # Perl returns ONLY songinfo_loop — no count / loop_loop.
                return {"songinfo_loop": loop}
            else:
                db.close()
                return self._browse_response([])
            db.close()
            return self._browse_response(loop, total, plural)
        except Exception:
            return self._browse_response([])


class WebAPIHandler:
    """HTTP request handler that routes to JSON-RPC or the web UI.

    Routes:
      POST /jsonrpc.js  → JSONRPCAPI.handle_request()
      GET  /api/v1/*    → REST passthrough (subclass for details)
      GET  /            → Serve index.html from html/
    """

    def __init__(self, jsonrpc: Optional[JSONRPCAPI] = None) -> None:
        self.jsonrpc = jsonrpc or JSONRPCAPI()
        self._static_dir = None

    def set_static_dir(self, path: str) -> None:
        """Set the directory for static file serving."""
        from pathlib import Path
        self._static_dir = Path(path)

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict, bytes]:
        """Handle an incoming HTTP request.

        Returns:
            (status_code, headers_dict, body_bytes)
        """
        from pathlib import Path

        if path == "/jsonrpc.js" or path == "/api/jsonrpc":
            return await self._handle_jsonrpc(method, body)

        if path.startswith("/api/v1/"):
            return await self._handle_rest(method, path, body)

        if path == "/" or path.startswith("/html/") or path.startswith("/material"):
            return self._serve_static(path)

        # Skin assets (Classic/EN/Logic/…): the original LMS serves every
        # file under html/<skin>/ for GET requests. Try the static dir as a
        # last resort before answering 404 — unknown API paths still 404
        # because _serve_static only returns 200 for existing files.
        if method == "GET":
            return self._serve_static(path)

        # Fallback: 404
        return (
            404,
            {"Content-Type": "application/json"},
            b'{"error": "Not found"}',
        )

    async def _handle_jsonrpc(self, method: str, body: bytes) -> tuple[int, dict, bytes]:
        if method != "POST":
            return 405, {"Content-Type": "application/json"}, b'{"error": "Method not allowed"}'
        response = await self.jsonrpc.handle_request(body)
        return 200, {"Content-Type": "application/json"}, response

    async def _handle_rest(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict, bytes]:
        route = path[8:]  # strip "/api/v1/"
        if route == "status":
            result = await self.jsonrpc._server_status()
            return 200, {"Content-Type": "application/json"}, _json_dumps(result)
        if route == "players":
            result = await self.jsonrpc._player_list()
            return 200, {"Content-Type": "application/json"}, _json_dumps(result)
        return 404, {"Content-Type": "application/json"}, b'{"error": "Not found"}'

    def _serve_static(self, path: str) -> tuple[int, dict, bytes]:
        if self._static_dir is None:
            return 404, {}, b"Static dir not configured"

        from pathlib import Path

        from lyrion.web.app import _STATIC_SIZE_RE, _resize_skin_image, \
            _static_path_variants

        if path == "/":
            path = "/index.html"
        elif path == "/material" or path == "/material/":
            # Material Skin (Jive controller UI) SPA entry
            path = "/material/index.html"

        # Resolve the real path and enforce it stays inside the static root.
        # A naive string-prefix check on the unresolved path is bypassable
        # (e.g. "html/../secret.txt" literally starts with "html/").
        base = self._static_dir.resolve()

        def _resolve(rel: str) -> Path | None:
            try:
                resolved = (self._static_dir / rel).resolve()
            except OSError:
                return None
            if not resolved.is_relative_to(base) or not resolved.is_file():
                return None
            return resolved

        file_path: Path | None = None
        for rel in _static_path_variants(path):
            file_path = _resolve(rel)
            if file_path is not None:
                break

        if file_path is None:
            # Jive asks for LMS size-encoded static images
            # ('/html/images/albums_40x40_m.png', 'genres_40x40_m.png', …)
            # after its 'artworkspec add 40x40_m squeezeplayskin'. We ship
            # only the base files, so a missing sized name must fall back to
            # the unsized one — otherwise those list icons 404 (live client
            # log: '_getArtworkThumbSink(/html/images/genres_40x40_m.png)
            # error: HTTP/1.1 404 Not Found').
            #
            # Live Perl RESIZES for that request (curl 2026-09-14:
            # /html/images/genres_40x40_m.png → 200 image/png 1514 B,
            # radio_40x40_m.png → 200, 1961 B, 40x40 RGBA PNG); shipping the
            # 512x512 original answered 200 too but sent ~15× the bytes, so
            # the sized request is scaled here and the original is only the
            # fallback when Pillow cannot read the file.
            for rel in _static_path_variants(path):
                m = _STATIC_SIZE_RE.match(Path(rel).name)
                if not m:
                    continue
                unsized = _resolve(str(Path(rel).with_name(
                    m.group("base") + m.group("ext"))))
                if unsized is None:
                    continue
                try:
                    raw = unsized.read_bytes()
                except OSError:
                    continue
                resized = _resize_skin_image(
                    raw, (int(m.group("w")), int(m.group("h"))))
                return (200,
                        {"Content-Type": "image/png",
                         "Cache-Control": "max-age=604800"},
                        resized if resized is not None else raw)
            return 404, {}, b"Not found"

        import mimetypes
        mime, _ = mimetypes.guess_type(str(file_path))
        try:
            content = file_path.read_bytes()
            headers = {"Content-Type": mime or "application/octet-stream"}
            # HTML without cache so UI updates are picked up immediately
            if mime == "text/html":
                headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            # Perl answers skin images with a one-week cache (live:
            # Cache-Control: max-age=604800) — Jive re-fetches them per list
            # otherwise.
            elif (mime or "").startswith("image/") or mime in (
                    "image/svg+xml",):
                headers["Cache-Control"] = "max-age=604800"
            return 200, headers, content
        except Exception as e:
            return 500, {}, str(e).encode()
