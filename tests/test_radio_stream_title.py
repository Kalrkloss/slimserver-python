"""Titelregel für Stream-Einträge: Sendername, ICY-Titel, URL als Rückfall.

Perl-Kette (wörtlich zitiert, Clone ``/tmp/lms-ref``):

1. **SenderN AME**: Startet ein Radio-Feed eine Zeile, registriert Perl den
   Namen der Zeile unter der URL — ``Slim::Music::Info::setRemoteMetadata(
   $url, {title => $subFeed->{name} || $subFeed->{title}, …})``
   (``Slim/Control/XMLBrowser.pm:693-700``) schreibt das ``TITLE``-Attribut
   des RemoteTrack dieser URL (``Slim/Music/Info.pm:395-478``).  Ab dann ist
   ``$track->title`` = der SENDENAME (nie die URL).
2. **ICY-Titel**: ``Slim/Player/Protocols/HTTP.pm:271-357`` (``parseMetadata``)
   legt jeden ``StreamTitle`` unter der URL des LAUFENDEN Streams ab
   (``setCurrentTitle`` → ``Info.pm:513-552``); ``Info.pm:556-583``
   (``getCurrentTitle``) antwortet diesen Wert, solange er WAHR ist, sonst den
   Standard-Titel der URL (= der Name aus 1.).
3. **Anzeige**: ``Slim/Control/Queries.pm:5972``
   ``$returnHash{'title'} = $remoteMeta->{title} || $track->title;`` mit
   ``$remoteMeta`` aus ``Protocols/HTTP.pm:1031-1085`` (``getMetadataFor``:
   ``getCurrentTitle``-Wert, bei ``Artist - Titel`` in Künstler/Titel
   getrennt, ``:1076-1083``).  Tag ``N`` (``remote_title``) = der NAME
   (``:5981-5986``), ``playlist name ?`` liefert genau den
   (``:2766-2767``).
4. **Rückfall**: Ist nichts registriert und sendet der Stream nichts, ist der
   Titel die URL selbst — live Perl 9.1.1 (read-only 2026-09-19) antwortet der
   Eintrag ``http://192.168.1.90/alarm.mp3`` mit genau dieser URL als
   ``title`` und OHNE ``remote_title`` (``Slim/Music/Info.pm:669-673``
   ``plainTitle``: "derived from the file path or URL").

Das gemeldete Symptom war der Verstoß gegen Schritt 1/3: der Port zeigte den
URL-HOST (``regiocast.streamabc.net``) statt des SenderNAMENS.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"
RADIO_URL = ("http://regiocast.streamabc.net/regc-80s80smweb2517500-mp3-192-"
             "1672667?sABC=6nn9pn5r%230%23r30o443r1929r059s085628511796n57%23")
NO_ICY_URL = "https://st01.sslstream.dlf.de/dlf/01/low/opus/stream.opus"
ICY = "The Cure - Lullaby"

_DB_CACHE: dict[str, str] = {}


def _db(tmp_path) -> str:
    """Bibliotheks-DB mit einer ``remote_media``-Zeile (Radio-Import)."""
    key = str(tmp_path)
    if key in _DB_CACHE:
        return _DB_CACHE[key]
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, url TEXT,
            duration REAL, remote INTEGER, content_type TEXT);
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


class _PM:
    def __init__(self, player):
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _player(playlist, position, **kw) -> PlayerState:
    p = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130", port=58044)
    p.power = True
    p.playlist = list(playlist)
    p.playlist_position = position
    p.playlist_total = len(p.playlist)
    for key, value in kw.items():
        setattr(p, key, value)
    return p


def _status(player, args):
    return asyncio.run(JSONRPCAPI()._json_player_status(_PM(player), MAC, args))


# ---------------------------------------------------------------------------
# 1./4. Name statt URL-Host — ohne ICY-Titel des Streams
# ---------------------------------------------------------------------------
def test_feed_station_without_icy_shows_the_station_name(tmp_path, monkeypatch):
    """Der radio-browser-Sender trägt seinen NAMEN, nicht den URL-Host.

    Symptom (User): „Im Player-Fenster wird bei Radios aus dem Radiobrowser
    momentan die URL angezeigt (z. B. regiocast.streamabc.net), das sollte
    aber der RadioNAME sein."  Genau der Host kam hier aus dem Item-Titel.
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([RADIO_URL], 0, mode="play", elapsed=12.0,
                     current_url=RADIO_URL, current_title="80s80s Radio",
                     stream_baseline_title="80s80s Radio", remote=1)

    res = _status(player, ["-", "1", "tags:galduKxN"])
    item = res["playlist_loop"][0]

    assert item["title"] == "80s80s Radio"          # Sendername (Feed-Zeile)
    assert item["url"] == RADIO_URL
    assert "regiocast" not in item["title"]
    # Perl Tag 'N' (Queries.pm:5981-5986) = `$track->title` = der Name.
    assert res["remoteMeta"]["remote_title"] == "80s80s Radio"


def test_stream_metadata_of_the_previous_stream_does_not_leak(tmp_path,
                                                              monkeypatch):
    """``%currentTitles`` hängt an der URL (Info.pm:552) — fremder Titel nie.

    Der Wechsel auf einen zweiten Sender lässt ``remote_meta`` des alten im
    Player stehen (Live-Lehre vom 2026-09-18); ``stream_meta_url`` zeigt auf
    den alten Stream, der neue Sender darf seinen Namen nicht verlieren.
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([NO_ICY_URL], 0, mode="play", elapsed=3.0,
                     current_url=NO_ICY_URL,
                     current_title="Deutschlandfunk | DLF | OPUS 24k",
                     stream_baseline_title="Deutschlandfunk | DLF | OPUS 24k",
                     stream_meta_url=RADIO_URL, remote=1,
                     remote_meta={"streamtitle": ICY, "title": "Lullaby",
                                  "artist": "The Cure", "url": RADIO_URL})

    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]

    assert item["title"] == "Deutschlandfunk | DLF | OPUS 24k"


# ---------------------------------------------------------------------------
# 2./3. ICY-Titel ersetzt den Namen — Name bleibt als remote_title
# ---------------------------------------------------------------------------
def test_icy_title_replaces_the_name_and_keeps_it_as_remote_title(
        tmp_path, monkeypatch):
    """ICY live: Titel = ICY-Anteil, ``remote_title`` = Sendername.

    Live Perl 9.1.1 (read-only 2026-09-19, Player 00:04:20:2B:88:C8):
    ``playlist_loop[0]`` = ``{… "title":"Tierra Azul (Nordlight Remix)",
    "artist":"Vibrasphere", "remote_title":"Hirschmilch Chillout" …}`` bei
    ``current_title: "Vibrasphere - Tierra Azul (Nordlight Remix)"``.
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([RADIO_URL], 0, mode="play", elapsed=300.0,
                     current_url=RADIO_URL, current_title=ICY,
                     stream_baseline_title="80s80s Radio",
                     stream_meta_url=RADIO_URL, remote=1,
                     remote_meta={"streamtitle": ICY, "title": "Lullaby",
                                  "artist": "The Cure", "url": RADIO_URL})

    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]

    assert item["title"] == "Lullaby"               # ICY-Anteil (Track)
    assert item["artist"] == "The Cure"             # ICY-Anteil (Künstler)
    assert item["album"] == ""


def test_menu_item_carries_the_name_as_remote_title(tmp_path, monkeypatch):
    """Menü-Form: ``remote_title`` = Sendername (Perl 'N', :5981-5986)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([RADIO_URL], 0, mode="play", elapsed=300.0,
                     current_url=RADIO_URL, current_title=ICY,
                     stream_baseline_title="80s80s Radio",
                     stream_meta_url=RADIO_URL, remote=1,
                     remote_meta={"streamtitle": ICY, "title": "Lullaby",
                                  "artist": "The Cure", "url": RADIO_URL})

    item = _status(player, ["-", "1", "menu:menu",
                            "useContextMenu:1"])["item_loop"][0]

    assert item["track"] == "Lullaby"
    assert item["artist"] == "The Cure"
    assert item["remote_title"] == "80s80s Radio"
    # Perl :5611-5616: der Name steht als Album-Zeile, wenn er sich vom
    # Track-Titel unterscheidet.
    assert item["album"] == "80s80s Radio"


# ---------------------------------------------------------------------------
# 2. Leerer StreamTitle: der Name bleibt stehen (Info.pm:556-583)
# ---------------------------------------------------------------------------
def test_empty_stream_title_keeps_the_station_name(tmp_path, monkeypatch):
    """``StreamTitle=''`` ⇒ kein Metadaten-Titel ⇒ Sendername bleibt.

    ``Info.pm:572-574`` liefert nur einen WAHREN ``%currentTitles``-Eintrag;
    ``networking/protocol.py:4104-4122`` setzt dafür die Baseline zurück
    (live: SUNSHINE LIVE sendet ``StreamTitle='';``).
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([RADIO_URL], 0, mode="play", elapsed=60.0,
                     current_url=RADIO_URL, current_title="80s80s Radio",
                     stream_baseline_title="80s80s Radio",
                     stream_meta_url="", remote=1, remote_meta={})

    item = _status(player, ["-", "1", "tags:galduKxN"])["playlist_loop"][0]

    assert item["title"] == "80s80s Radio"
    assert item.get("artist", "") == ""


# ---------------------------------------------------------------------------
# 4. Letzter Rückfall: die URL (Perl live: alarm.mp3)
# ---------------------------------------------------------------------------
def test_unknown_stream_falls_back_to_the_url(tmp_path, monkeypatch):
    """Nichts registriert, nichts gesendet ⇒ die URL (Perl: ``alarm.mp3``).

    Live Perl 9.1.1 (read-only 2026-09-19, Player ca:c8:c7:26:6d:38):
    ``playlist index:0 … title:http://192.168.1.90/alarm.mp3`` — und KEIN
    ``remote_title`` (``Info.pm:669-673`` ``plainTitle``).
    """
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    bare = "http://192.168.1.90/alarm.mp3"
    player = _player([bare], 0, mode="play", elapsed=1.0, current_url=bare,
                     current_title=bare, stream_baseline_title=bare, remote=1)

    res = _status(player, ["-", "1", "tags:galduKxN"])
    item = res["playlist_loop"][0]

    assert item["title"] == bare
    assert "remote_title" not in res["remoteMeta"]


# ---------------------------------------------------------------------------
# Die Regel als Einzelfunktion (mehrere Einträge, fremder Eintrag)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,name,icy,expected", [
    ("http://a.example/x.mp3", "Sender A", "", "Sender A"),
    ("http://b.example/x.mp3", "Sender B", ICY, "Lullaby"),
    ("http://c.example/x.mp3", "", "", "http://c.example/x.mp3"),
])
def test_entry_title_rule(url, name, icy, expected):
    """``$remoteMeta->{title} || $track->title`` (Queries.pm:5972)."""
    player = _player([url], 0, current_url=url, remote=1,
                     current_title=icy or name, stream_baseline_title=name or url,
                     stream_meta_url=url if icy else "",
                     remote_meta={"streamtitle": icy} if icy else {},
                     stream_titles={url: name} if name else {})
    title, artist = api_mod._stream_entry_title(player, url)
    assert title == expected
    assert artist == ("The Cure" if icy else "")


def test_foreign_queue_entry_keeps_the_registered_name():
    """Ein NICHT laufender Eintrag behält den registrierten Namen.

    Perl: ``getCurrentTitle($client, $url)`` → ``%currentTitles{$url}`` (leer)
    → ``standardTitle`` → ``$track->title`` = der Name (``Info.pm:572-583``).
    """
    other = "http://other.example/live.mp3"
    player = _player([other], 0, current_url="http://playing.example/x.mp3",
                     remote=1, current_title="Läuft", stream_titles={
                         other: "Anderer Sender"})
    assert api_mod._stream_entry_title(player, other) == ("Anderer Sender", "")
