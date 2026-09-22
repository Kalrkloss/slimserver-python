"""Der icy-name des Streams als Zeilentitel (Perl ``remote_title``, Tag ``N``).

Symptom: fuer einen Stream OHNE Registrierung (kein Radio-Feed, kein
Favorit — also kein Name in ``remote_media``/``stream_titles``) blieb
``remote_title`` leer, waehrend Perl den Zeilentitel des Senders nennt.

Perl-Kette (alles zitiert, Clone ``/tmp/lms-ref`` = public/9.1):

1. **Der Player meldet die Antwort-Header der Quelle.** Bei einem DIREKTEN
   Stream verbindet sich der Player selbst mit dem Sender und schickt die
   Header als RESP-Frame; Perl parst sie in
   ``Squeezebox2::directHeaders`` (``Slim/Player/Squeezebox2.pm:478-601``) mit
   ``parseDirectHeaders`` (``Slim/Player/Protocols/HTTP.pm:698-819``), das den
   Namen des Senders liest::

       if ($header =~ /^(?:ic[ey]-name|x-audiocast-name):\\s*(.+)/i) {
           $title = Slim::Utils::Unicode::utf8decode_guess($1);
       }
                                                          (HTTP.pm:729-731)

2. **Der Name wird zum Titel der URL — und damit zur Zeile.** Am Ende von
   ``directHeaders`` (identisch in ``Protocols/HTTP.pm:851-861`` fuer den
   Proxy-Stream)::

       # Always prefer the title returned in the headers of a radio station
       if ( $title ) {
           Slim::Music::Info::setCurrentTitle( $url, $title );

           # Bug 7979, Only update the database title if this item doesn't
           # already have a title
           my $curTitle = Slim::Music::Info::title($url);
           if ( !$curTitle || $curTitle =~ /^(?:http|mms)/ ) {
               Slim::Music::Info::setTitle( $url, $title );
           }
       }
                                             (Squeezebox2.pm:592-601)

   ``setCurrentTitle`` legt den Namen unter der URL des laufenden Streams ab
   (``%currentTitles{$url}``, ``Info.pm:513-552``); ``setTitle`` schreibt den
   ``TITLE`` des RemoteTrack dieser URL (``Info.pm:290-304``) — genau der Wert,
   den ``_songData`` als Tag ``N`` ausgibt::

       if ($tag eq 'N') {
           if ($parentTrack) { $returnHash{remote_title} = $parentTrack->title; }
           elsif ( $isRemote && !$track->secs && $remoteMeta->{title} && !$remoteMeta->{album} ) {
               if (my $meta = $track->title) { $returnHash{remote_title} = $meta; }
           }
       }
                                                    (Queries.pm:5981-5988)

   Fuer eine URL ohne eigenen Titel ist ``Info::title($url)`` die URL selbst
   (``plainTitle``, ``Info.pm:669-673``: "derived from the file path or URL")
   — sie beginnt mit ``http`` und passiert damit die Bug-7979-Schranke. Deshalb
   antwortet live Perl 9.1.1 fuer einen unregistrierten Stream mit dem
   icy-name ("80s80s Digital Web", SWR3 -> "SWR3 MP3 128").
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lyrion.networking.protocol import SlimProtoClient
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import (
    JSONRPCAPI,
    _stream_entry_title,
    _stream_registered_name,
)

MAC = "1C:87:2C:47:FC:36"
MAC_CLEAN = "1C872C47FC36"

#: Der Stream, den der Player gerade von der Quelle holt.
STREAM_URL = "http://regiocast.streamabc.net/regc-80s80smweb.mp3?probe=noname2"
ICY_NAME = "80s80s Digital Web"          # live: icy-name dieses Senders
FEED_NAME = "1Mix Radio EDM Stream"      # registrierter Name (Feed-Zeile)

_DB_CACHE: dict[str, str] = {}


def _db(tmp_path) -> str:
    """Bibliotheks-DB ohne jede Registrierung fuer ``STREAM_URL``."""
    key = str(tmp_path)
    if key in _DB_CACHE:
        return _DB_CACHE[key]
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
            duration REAL, year INTEGER, tracknum INTEGER, bitrate INTEGER,
            samplerate INTEGER, bitspersample INTEGER, genre TEXT, cover TEXT,
            remote INTEGER, disc INTEGER, filesize INTEGER, comment TEXT,
            lyrics TEXT, content_type TEXT);
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER,
            role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE remote_media (id INTEGER PRIMARY KEY, url TEXT,
            name TEXT, bitrate INTEGER);
        """
    )
    con.commit()
    con.close()
    _DB_CACHE[key] = str(path)
    return str(path)


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _player(**kw) -> PlayerState:
    p = PlayerState(mac=MAC, name="Taverne", ip="127.0.0.1", port=1234)
    p.power = True
    p.mode = "play"
    for key, value in kw.items():
        setattr(p, key, value)
    return p


def _playing_stream(player: PlayerState, url: str = STREAM_URL,
                    baseline: str = "") -> PlayerState:
    """Der Player hat den Stream angefordert und wartet auf die Quell-Header."""
    player.playlist = [url]
    player.playlist_position = 0
    player.playlist_total = 1
    player.remote = 1
    player.current_url = url
    player.current_title = baseline or url
    player.stream_baseline_title = baseline or url
    player.strm_sent_at = 0.0            # kein strm-Handshake-Fenster mehr
    return player


def _client(player: PlayerState) -> SlimProtoClient:
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {player.mac: player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    client = SlimProtoClient.__new__(SlimProtoClient)
    client._player_writers = {MAC_CLEAN: _FakeWriter()}
    client._player_connections = {MAC_CLEAN: 1}
    client._resp_waiters = {}
    client.web_port = 9000
    client.server_ip = "127.0.0.1"
    return client


def _resp(*headers: str) -> bytes:
    return ("\r\n".join(headers) + "\r\n").encode("latin-1")


# ---------------------------------------------------------------------------
# 1. Header -> Titel der URL (Perl HTTP.pm:729-731 + Squeezebox2.pm:592-601)
# ---------------------------------------------------------------------------

def test_icy_name_becomes_the_title_of_an_unregistered_stream(tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _playing_stream(_player())
    client = _client(player)

    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         "icy-metaint: 8192",
                                         f"icy-name: {ICY_NAME}"))

    # ``setCurrentTitle($url, $title)`` — der laufende Stream hat jetzt einen
    # Titel (Info.pm:552); ``setTitle`` schreibt ihn als Titel der URL.
    assert player.current_title == ICY_NAME
    assert player.remote_meta["streamtitle"] == ICY_NAME
    assert player.remote_meta["url"] == STREAM_URL
    assert player.stream_meta_url == STREAM_URL
    assert player.stream_titles[STREAM_URL] == ICY_NAME
    assert player.stream_baseline_title == ICY_NAME
    # Der Zeilentitel, den ``_songData`` fuer Titel und Tag N liest
    # (Queries.pm:5972/5981-5988).
    assert _stream_registered_name(player, STREAM_URL) == ICY_NAME
    assert _stream_entry_title(player, STREAM_URL) == (ICY_NAME, "")


def test_x_audiocast_name_and_case_are_read_too():
    """``/^(?:ic[ey]-name|x-audiocast-name):\\s*(.+)/i`` — HTTP.pm:729."""
    for header in (f"x-audiocast-name: {ICY_NAME}",
                   f"ICY-NAME: {ICY_NAME}",
                   f"Ice-Name: {ICY_NAME}"):
        player = _playing_stream(_player())
        client = _client(player)
        client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK", header))
        assert player.stream_titles[STREAM_URL] == ICY_NAME, header


def test_utf8_guess_of_the_header_value():
    """``Slim::Utils::Unicode::utf8decode_guess($1)`` — HTTP.pm:731."""
    name = "Radio Köln – Studio"
    player = _playing_stream(_player())
    client = _client(player)
    client._handle_resp_frame(
        MAC, b"HTTP/1.0 200 OK\r\nicy-name: " + name.encode("utf-8") + b"\r\n")
    assert player.stream_titles[STREAM_URL] == name


def test_without_an_icy_name_nothing_changes():
    player = _playing_stream(_player())
    before = (player.current_title, dict(player.stream_titles),
              dict(player.remote_meta), player.stream_baseline_title)
    client = _client(player)

    async def _main():
        fut = asyncio.get_running_loop().create_future()
        client._resp_waiters[MAC_CLEAN] = fut
        client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                             "icy-metaint: 8192",
                                             "icy-br: 128"))
        return fut.result()

    # Der bestehende RESP-Pfad bleibt unveraendert (metaint -> cont-Waiter).
    assert asyncio.run(_main()) == 8192
    assert (player.current_title, dict(player.stream_titles),
            dict(player.remote_meta), player.stream_baseline_title) == before
    assert player.stream_source_ready is True


def test_no_header_title_without_a_current_stream_url():
    """Ohne offenen Stream gibt es keine URL, unter der der Name liegen
    koennte — Perl haengt ihn an ``$controller->streamUrl()``."""
    player = _player()                   # current_url leer
    client = _client(player)
    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))
    assert player.stream_titles == {}
    assert player.current_title == ""


# ---------------------------------------------------------------------------
# 2. Bug 7979: eine REGISTRIERUNG wird nicht ueberschrieben
# ---------------------------------------------------------------------------

def test_a_registered_name_survives_the_icy_name(tmp_path, monkeypatch):
    """``my $curTitle = Slim::Music::Info::title($url); if (!$curTitle ||
    $curTitle =~ /^(?:http|mms)/) { setTitle(...) }`` — Squeezebox2.pm:597-600.

    Der registrierte Name (Feed-Zeile, ``setRemoteMetadata``,
    ``Control/XMLBrowser.pm:693-700``) bleibt der Titel der URL, also auch der
    ``remote_title``; ``setCurrentTitle`` gilt trotzdem — der ANGEZEIGTE Titel
    ist der icy-name (``getCurrentTitle``, Info.pm:572-574).
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _playing_stream(_player(), baseline=FEED_NAME)
    player.stream_titles = {STREAM_URL: FEED_NAME}
    client = _client(player)

    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))

    assert player.stream_titles[STREAM_URL] == FEED_NAME   # nicht ueberschrieben
    assert player.stream_baseline_title == FEED_NAME
    assert player.current_title == ICY_NAME                # setCurrentTitle
    assert _stream_registered_name(player, STREAM_URL) == FEED_NAME
    # Der Status: der laufende Stream zeigt den icy-name (getCurrentTitle),
    # ``remote_title`` bleibt der registrierte Name (Tag N = $track->title).
    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]
    assert item["title"] == ICY_NAME
    assert item["remote_title"] == FEED_NAME


def test_a_db_registration_also_blocks_the_icy_name(tmp_path, monkeypatch):
    """``Info::title($url)`` kommt aus der DB (``remote_media.name``)."""
    path = _db(tmp_path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO remote_media (url, name, bitrate) VALUES (?,?,?)",
                (STREAM_URL, FEED_NAME, 128))
    con.commit()
    con.close()
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: path)
    player = _playing_stream(_player())
    client = _client(player)

    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))

    assert player.stream_titles.get(STREAM_URL) is None
    assert _stream_registered_name(player, STREAM_URL) == FEED_NAME


# ---------------------------------------------------------------------------
# 3. Der Status: remote_title fuer den unregistrierten Stream
# ---------------------------------------------------------------------------

class _PM:
    def __init__(self, player):
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _status(player, args):
    return asyncio.run(JSONRPCAPI()._json_player_status(_PM(player), MAC, args))


def test_status_reports_the_icy_name_as_the_line_title(tmp_path, monkeypatch):
    """Vorher (ohne icy-name): das Feld fehlte. Jetzt: der Zeilentitel.

    Perl: ``title`` = ``$remoteMeta->{title} || $track->title``
    (Queries.pm:5972) und Tag ``N`` = ``$track->title`` (:5981-5988).
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _playing_stream(_player(), baseline="")
    client = _client(player)
    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))

    res = _status(player, ["-", "1", "tags:galduKxN"])
    item = res["playlist_loop"][0]
    assert item["title"] == ICY_NAME
    assert item["remote_title"] == ICY_NAME
    assert res["remoteMeta"]["remote_title"] == ICY_NAME
    assert res["remoteMeta"]["title"] == ICY_NAME


def test_empty_stream_title_leaves_the_icy_name_standing(tmp_path,
                                                         monkeypatch):
    """``StreamTitle=''`` ⇒ kein Metadaten-Titel (``Info.pm:572-574`` liefert
    nur einen WAHREN Cache-Eintrag) ⇒ der icy-name bleibt der Zeilentitel."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _playing_stream(_player())
    client = _client(player)
    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))
    player.stream_source_ready = True
    player.strm_sent_at = 0.0

    client._handle_stmu_frame(MAC, b"StreamTitle='';\x00")

    assert player.current_title == ICY_NAME
    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]
    assert item["title"] == ICY_NAME
    assert item["remote_title"] == ICY_NAME


def test_a_real_stream_title_still_wins_over_the_icy_name(tmp_path,
                                                          monkeypatch):
    """Ein ``StreamTitle`` des Senders ersetzt den Namen als ANGEZEIGTEN Titel
    (``setCurrentTitle``, Info.pm:552) — ``remote_title`` bleibt der Name."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _playing_stream(_player())
    client = _client(player)
    client._handle_resp_frame(MAC, _resp("HTTP/1.0 200 OK",
                                         f"icy-name: {ICY_NAME}"))
    player.stream_source_ready = True
    player.strm_sent_at = 0.0

    async def _main():
        client._handle_stmu_frame(MAC, b"StreamTitle='The Cure - Lullaby';\x00")
        await asyncio.sleep(0)

    asyncio.run(_main())

    assert player.current_title == "The Cure - Lullaby"
    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]
    assert item["title"] == "Lullaby"
    assert item["artist"] == "The Cure"
    assert item["remote_title"] == ICY_NAME
