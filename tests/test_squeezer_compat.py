"""Squeezer-Kompatibilität (Android-Client ``uk.org.ngo.squeezer``).

Drei gemeldete Symptome gegen den Dev-Server, je Symptom ein Test mit den
Belegstellen aus dem Squeezer-Quellbaum
(``/tmp/squeezer-src/android-squeezer-develop``) UND dem Perl-LMS
(read-only ``/tmp/lms-ref``):

1. **Unknown artist/album beim Radio** — Squeezer liest die Now-Playing-Daten
   aus ``item_loop[0]`` des ``status``-Antwortsatzes:

       CometClient.java:419-426  item_data = messageData.get("item_loop");
                                 currentSong = new CurrentTrack(record)
       CurrentTrack.java:33      songInfo.title = getStringOrEmpty(record, "track")
       Song.java:63-75           title/tracknum/artists/album/album_id/… aus
                                 "title"/"artist"/"album"/"albumartist"/"track"
       JiveItem.java:248/498-508 name/text2 aus ""text"" per ``indexOf('\\n')``
       JiveItem.java:83          SONG_TAGS = "ABdejJKlrStTuxy" (status-Tags)

   Perl baut dieses Item in ``_addJiveSong`` (Queries.pm:5561-5661):
   ``track``/``album``/``artist`` stehen IMMER als Strings (:5637-5651),
   ``text`` = ``title . "\\n" . join(' - ', artist, album)`` (:5620), und für
   einen Remote-Stream wird die Sender-/Eintrags-Titel als ``album``
   eingesetzt, sobald er sich vom Track-Titel unterscheidet
   (:5611-5615 ``$album = $remote_title``; ``remote_title`` = ``'N'``-Tag,
   _songData :5968-5981). ``current_title`` ist der ROHE ICY-StreamTitle
   (:4089-4090 ``Slim::Music::Info::getCurrentTitle``, Info.pm:556-581).
   Live Perl 9.1.1 (Player ``00:00:00:00:00:00``, 1.FM-Stream, 2026-09-13)::

       status - 1 tags:ABdejJKlrStTuxy
         current_title: "Goabert - pulchra somnium"
         remoteMeta: {id, title:"pulchra somnium", artist:"Goabert",
                      duration:"0", coverart:"0", bitrate:"256kb/s CBR",
                      url:"http://strm112.1.fm/ambientpsy_mobile_mp3", ...}
         playlist_loop[0]: {duration:"0", "playlist index":0, url, remote:1,
                            coverart:"0", artist:"Goabert",
                            title:"pulchra somnium", year:"0", …}
       status - 1 menu:menu useContextMenu:1
         current_title: null
         count: 3, offset: "-", base.actions.more: {itemsParams:"params",
              params:{menu:"track", context:"playlist"}, player:0,
              cmd:["contextmenu"], window:{isContextMenu:1}}
         item_loop[0]: {"artist":"Goabert", "style":"itemplay",
                        "trackType":"radio", "track":"pulchra somnium",
                        "album":"1.FM - Ambient Psychill",
                        "text":"pulchra somnium\\nGoabert - 1.FM - Ambient Psychill",
                        "params":{"track_id":…, "playlist_index":0},
                        "icon":"html/images/favorites.png"}

2. **Absturz beim Öffnen des Vollbild-Playerstatus** — Squeezer holt im
   Vollbild-Layout über ``song.moreAction`` den Kontext-Menü-Feed:

       NowPlayingFragment.java:805   requireService().pluginItems(song.moreAction, …)
                                     (OHNE Null-Check)
       SqueezeService.java:1434      mDelegate.requestItems(...).cmd(action.action.cmd)
       JiveItem.java:280             moreAction = extractAction("more", baseActions, …)
       JiveItem.java:585-596         baseActions aus `record`/`messageData["base"]`,
                                     itemsParams verweist auf das Item-``params``
                                     (extractJsonAction :629-632)

   Ohne ``base`` bleibt ``moreAction`` ``null`` → NPE. Perl liefert ``base``
   im menuMode immer (Queries.pm:4322-4343): mit ``useContextMenu`` den
   ``more``-Eintrag aus ``_contextMenuBase('track')`` (:6229-6245) plus
   ``context => 'playlist'`` (:4327), sonst ``go => ['trackinfo','items']``
   (:4329-4341). ``count``/``offset`` sind dagegen NUR im menuMode vorhanden
   (:4320 ``count = $songCount + 2``, :4408/:4433 ``offset``) — im
   Nicht-Menü-Status also nicht vermisst; Squeezer liest für ``status`` ohnehin
   ``playlist_tracks`` als Count (CometClient.java:616).

3. **Endloses Laden bei „Meine Musik / Alben"** — die exakte Browse-Anfrage
   steht im Dev-Log (13.09. 17:34:16, Client 192.168.240.112/Squeezer):

       Cometd POST /cometd: [{"clientId":"lyrion-1","data":{"request":
         ["1C:87:2C:47:FC:36",["browselibrary","items","0","512","mode:albums",
          "useContextMenu:1","menu:1"]],"response":"/lyrion-1/slim/request/2"},
         "channel":"/slim/request","id":"17"}]

   (Payload-Bau: ``browseLibraryCommand`` CustomJiveItemHandling.java:56-62,
   Paging: CometClient.java:712-714 ``page(start, itemsPerResponse)``,
   PageSize 512 aus res/values/constants.xml:20, Empfang
   ItemListener.parseMessage CometClient.java:543-591 mit ``count`` =
   Loop-Länge.) Nach diesem POST fehlt jede Access-Log-Zeile für den Client —
   die /cometd-Antwort wurde nie geschrieben, die App wartet ewig.

   Der Antwort-Shape ist Perl-gleich (Live-Perl ``browselibrary items 0 512
   mode:albums menu:1 useContextMenu:1`` → ``count``/``offset``/``window``
   ``{windowStyle:"icon_list"}``/``base.actions.go`` (itemsParams
   ``commonParams``, mode tracks)/``item_loop`` mit ``type:"playlist"``,
   ``text: "<album>\\n<artist>"``, ``commonParams.album_id``,
   ``presetParams``, ``textkey`` — Queries.pm:5322 ``substr($titleSort,0,1)``
   + XMLBrowser.pm:1373). ``textkey`` speist Squeezers A-Z-Scroller
   (JiveItem.java:249, JiveItemListActivity.java:420-421). Der Request-Pfad
   ist jetzt mit ``asyncio.wait_for`` begrenzt (Muster des radios-Fixes
   d6653d91c) und degradiert zur Perl-Form (count 0) statt die Verbindung
   offen zu halten.
"""

import asyncio
import sqlite3

from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI

MAC = "1c:87:2c:47:fc:36"
STREAM_URL = "http://hirschmilch.de:7000/chillout.mp3"
#: Der ICY-StreamTitle, wie ihn der Player hält (Perl: getCurrentTitle).
ICY = "Dense (chillgressive tunes) - Chill On! #862 - 2026-09-13"

#: ``_db``-Cache (siehe Docstring) — je tmp_path genau ein DB-Aufbau.
_DB_CACHE: dict[str, str] = {}


def _db(tmp_path, remote_media=True):
    """Minimal-Bibliothek: Alben + Album-Contributors (Browse) + Radio-Row.

    ``_library_db_path`` wird pro Request mehrfach gelesen — die Datei wird
    daher pro ``tmp_path`` genau einmal gebaut (Cache), sonst scheitert der
    zweite ``CREATE TABLE``-Lauf.
    """
    key = str(tmp_path)
    if key in _DB_CACHE:
        return _DB_CACHE[key]
    path = tmp_path / "lyrion.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, title TEXT, url TEXT, duration REAL,
            year INTEGER, tracknum INTEGER, bitrate INTEGER, samplerate INTEGER,
            bitspersample INTEGER, genre TEXT, cover TEXT, remote INTEGER,
            disc INTEGER, filesize INTEGER, comment TEXT, lyrics TEXT,
            content_type TEXT, replay_gain REAL, replay_peak REAL
        );
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE albums_contributors (album INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE remote_media (id INTEGER PRIMARY KEY, url TEXT, name TEXT, bitrate INTEGER);
        INSERT INTO tracks (id, title, url, duration, genre, tracknum, remote, content_type)
        VALUES (11, 'First Song', 'file:///music/first.mp3', 180.0, 'Rock', 1, 0, 'mp3');
        INSERT INTO contributors (id, name) VALUES (7, 'The Artist');
        INSERT INTO tracks_contributors (track, contributor, role) VALUES (11, 7, 1);
        INSERT INTO albums (id, title, artwork) VALUES
            (45, 'The Album', '/music/art/cover.jpg'),
            (46, '#Alpha', NULL);
        INSERT INTO tracks_albums (track, album) VALUES (11, 45);
        INSERT INTO albums_contributors (album, contributor, role) VALUES (45, 7, 1);
        """
    )
    if remote_media:
        con.execute("INSERT INTO remote_media (id, url, name, bitrate) "
                    "VALUES (1, ?, 'Hirschmilch Chillout', 128000)",
                    (STREAM_URL,))
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


def _player(playlist, position, **kw):
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


# ----------------------------------------------------------------------
# Symptom 1 — Radio: artist/album/track für den NP-Item (Unknown artist/album)
# ----------------------------------------------------------------------
def test_radio_np_item_carries_track_artist_album_like_addjivesong(tmp_path,
                                                                   monkeypatch):
    """Perl ``_addJiveSong`` (Queries.pm:5561-5661) vs. Squeezer
    ``Song``/``CurrentTrack`` (Song.java:63-80, CurrentTrack.java:33,
    JiveItem.java:498-508): das ``item_loop[0]``-Item des Menü-Status muss
    ``track``/``artist``/``album`` und den mehrzeiligen ``text`` tragen — sonst
    zeigt die App beim hirschmilch.de-Stream keinen/interpreten Titel."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([STREAM_URL], 0, mode="play", elapsed=379.2,
                     current_url=STREAM_URL, current_title=ICY, remote=1,
                     stream_images={}, stream_titles={})

    res = _status(player, ["-", "1", "menu:menu", "useContextMenu:1"])
    item = res["item_loop"][0]

    # Perl :5637-5651: die drei NP-Felder stehen immer (Strings, leer wenn
    # unbekannt) — Squeezer liest genau sie (Song.java:63-75).
    assert item["track"] == "Chill On! #862 - 2026-09-13"
    assert item["artist"] == "Dense (chillgressive tunes)"
    # Perl :5611-5615: der Sender (unsere remote_media.name) wird als Album
    # eingesetzt, wenn er sich vom Track-Titel unterscheidet.
    assert item["album"] == "Hirschmilch Chillout"
    # Perl :5620: text = title . "\n" . "artist - album"; Squeezer splittet an
    # '\n' (JiveItem.java:498-508) → name = Track, text2 = "artist - album"
    # (CurrentTrack.java:54-60 rendert genau diese Zeile).
    assert item["text"] == (
        "Chill On! #862 - 2026-09-13\n"
        "Dense (chillgressive tunes) - Hirschmilch Chillout")
    # Perl :5576 ``$isRemote ? 'radio' : 'local'``.
    assert item["trackType"] == "radio"
    # Perl :4089-4090 getCurrentTitle: der ROHE ICY-Titel (live Perl:
    # "Goabert - pulchra somnium"), nicht der abgespaltene Track-Anteil.
    assert res["current_title"] == ICY
    # Perl remoteMeta (_songData): ICY-Paar getrennt + 'N'-remote_title.
    assert res["remoteMeta"]["title"] == "Chill On! #862 - 2026-09-13"
    assert res["remoteMeta"]["artist"] == "Dense (chillgressive tunes)"
    assert res["remoteMeta"]["remote_title"] == "Hirschmilch Chillout"


def test_local_np_item_text_is_two_lines_too(tmp_path, monkeypatch):
    """Derselbe Shape für einen lokalen Titel (Perl :5637-5651 gilt für alle
    Quellen) — Squeezer zeigt sonst für lokale Titel nur den Einzeiler."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11], 0, mode="play", elapsed=10.0)

    item = _status(player, ["-", "1", "menu:menu",
                            "useContextMenu:1"])["item_loop"][0]

    assert item["track"] == "First Song"
    assert item["artist"] == "The Artist"
    assert item["album"] == "The Album"
    assert item["trackType"] == "local"
    assert item["text"] == "First Song\nThe Artist - The Album"


def test_tags_status_keeps_the_plain_playlist_item(tmp_path, monkeypatch):
    """Der ``handleChangedSong``-Pfad (CometClient.java:436-438,
    ``status - 1 tags:ABdejJKlrStTuxy``, JiveItem.java:83) holt nur die
    Tags-Form (Perl ``_addSong`` / statusQuery:4425-4470): artist ja
    (Tag 'A'/'a'), aber kein NP-Zusatz — Perl-gleich, live gegen 9.1.1
    geprüft (playlist_loop[0] ohne track/album/text)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([STREAM_URL], 0, mode="play", elapsed=5.0,
                     current_url=STREAM_URL, current_title=ICY, remote=1,
                     stream_images={}, stream_titles={})

    res = _status(player, ["-", "1", "tags:ABdejJKlrStTuxy"])
    item = res["playlist_loop"][0]

    assert item["artist"] == "Dense (chillgressive tunes)"
    assert item["title"] == "hirschmilch.de"          # unser Host-Stand-in
    assert "base" not in res                          # kein menuMode


# ----------------------------------------------------------------------
# Symptom 2 — Absturz im Vollbild: base.actions.more (moreAction) fehlt
# ----------------------------------------------------------------------
def test_menu_status_carries_base_more_action(tmp_path, monkeypatch):
    """Squeezer: ``moreAction = extractAction("more", baseActions, …)``
    (JiveItem.java:280) über ``base.actions.more.itemsParams -> params``
    (:585-596/:629-632); NowPlayingFragment.java:805 ruft
    ``pluginItems(song.moreAction, …)`` OHNE Null-Check und
    SqueezeService.java:1434 dereferenziert ``action.action.cmd`` → ohne
    ``base`` ist das ein NPE beim Öffnen des Vollbild-Status.

    Perl liefert ``base`` im menuMode (Queries.pm:4322-4343,
    ``_contextMenuBase('track')`` :6229-6245 + context 'playlist' :4327)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11], 0, mode="play", elapsed=10.0)

    res = _status(player, ["-", "1", "menu:menu", "useContextMenu:1"])
    more = res["base"]["actions"]["more"]

    assert more["cmd"] == ["contextmenu"]
    assert more["itemsParams"] == "params"
    assert more["params"] == {"menu": "track", "context": "playlist"}
    assert more["player"] == 0
    assert more["window"] == {"isContextMenu": 1}
    # Squeezers Auflösung: das Item muss das ``params``-Ziel tragen
    # (Perl :5655-5659), daraus baut extractJsonAction die Action-Params.
    item = res["item_loop"][0]
    assert set(item["params"]) >= {"track_id", "playlist_index"}
    # count/offset sind Perl-only im menuMode (:4320/:4408).
    assert res["count"] == len(res["item_loop"])
    assert "offset" in res


def test_menu_status_without_use_context_menu_uses_go_action(tmp_path,
                                                              monkeypatch):
    """Perl :4329-4341: ohne ``useContextMenu`` ist base.actions.go der
    trackinfo-Feed (ebenfalls mit itemsParams 'params' — Squeezer braucht
    in beiden Fällen ein ``base``, sonst NPE)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    player = _player([11], 0, mode="play", elapsed=10.0)

    res = _status(player, ["-", "1", "menu:menu"])
    go = res["base"]["actions"]["go"]

    assert go["cmd"] == ["trackinfo", "items"]
    assert go["itemsParams"] == "params"
    assert go["params"]["context"] == "playlist"


# ----------------------------------------------------------------------
# Symptom 3 — „Meine Musik / Alben" lädt endlos
# ----------------------------------------------------------------------
def _browse(args):
    return asyncio.run(JSONRPCAPI()._json_browselibrary("browselibrary",
                                                        args))


def test_album_browse_menu_shape_matches_the_squeezer_request(tmp_path,
                                                              monkeypatch):
    """Die exakte Squeezer-Anfrage (Dev-Log 2026-09-13 17:34:16):
    ``browselibrary items 0 512 mode:albums useContextMenu:1 menu:1``
    (CustomJiveItemHandling.java:56-62 + CometClient.java:712-714, PageSize
    512). Antwort-Shape wie Perl/BrowseLibrary + XMLBrowser.pm:1373."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))

    res = _browse(["items", "0", "512", "mode:albums", "useContextMenu:1",
                   "menu:1"])

    # Squeezer ItemListener.parseMessage (CometClient.java:543-591) liest
    # ``count`` + ``item_loop`` und rendert danach die Liste.
    assert res["count"] == 2
    assert res["offset"] == 0
    assert res["window"]["windowStyle"] == "icon_list"
    assert [it["type"] for it in res["item_loop"]] == ["playlist", "playlist"]
    # Perl-Querschnitt (BrowseLibrary/Queries.pm:873-895): "<album>\n<artist>"
    # in ``text`` (ohne Artist-Zeile nur der Titel).
    assert [it["text"] for it in res["item_loop"]] == ["#Alpha",
                                                      "The Album\nThe Artist"]
    # Drill-Token, das Squeezer aus commonParams in den Tap-Request merged
    # (JiveItem.java:585-596 + :629-632).
    assert res["item_loop"][1]["commonParams"]["album_id"] == "45"
    # Perl ``textkey`` = substr(titleSort, 0, 1) (Queries.pm:5322,
    # XMLBrowser.pm:1373; live Perl-Albumseite: "textkey":"-") — Squeezers
    # A-Z-Scroller (JiveItem.java:249, JiveItemListActivity.java:420-421).
    assert [it["textkey"] for it in res["item_loop"]] == ["#", "T"]
    # base.actions.go: itemsParams der Item-Quelle (Perl live:
    # `{"itemsParams":"commonParams","params":{"menu":1,"mode":"tracks"}}`).
    go = res["base"]["actions"]["go"]
    assert go["cmd"] == ["browselibrary", "items"]
    assert go["itemsParams"] == "commonParams"
    assert go["params"]["mode"] == "tracks"


def test_album_browse_answers_when_the_library_read_hangs(tmp_path,
                                                          monkeypatch):
    """Der Live-Hänger (kein Antwort-Frame für den /cometd-POST des Browsers)
    darf sich nicht wiederholen: der Request-Pfad ist begrenzt und
    degradiert zur Perl-Form ``count 0`` statt die Verbindung offen zu halten
    (Muster asyncio.wait_for, radios-Fix d6653d91c)."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    monkeypatch.setattr(api_mod, "_LIBRARY_QUERY_TIMEOUT", 0.05)

    async def _hang(*_a, **_kw):
        await asyncio.sleep(5)
        raise AssertionError("die Bibliotheksabfrage darf nicht durchlaufen")

    monkeypatch.setattr(JSONRPCAPI, "_library_rows", _hang, raising=False)

    res = _browse(["items", "0", "512", "mode:albums", "useContextMenu:1",
                   "menu:1"])

    # Perl-Form einer leeren Browse-Antwort — die App bekommt einen Frame und
    # beendet den Spinner, statt auf eine nie kommende Antwort zu warten.
    assert res["count"] == 0
    assert res["item_loop"] == []


def test_album_browse_paging_does_not_hang(tmp_path, monkeypatch):
    """Squeezer blättert mit ``start = page * PageSize`` (CometClient.java:
    712-714): jede Seite muss antworten und ``offset`` mitführen."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))

    res = _browse(["items", "1", "1", "mode:albums", "useContextMenu:1",
                   "menu:1"])

    assert res["offset"] == 1
    assert res["count"] == 2
    assert [it["commonParams"]["album_id"] for it in res["item_loop"]] == ["45"]
