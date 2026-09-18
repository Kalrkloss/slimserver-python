"""``base.window.windowStyle`` per browse mode — Perl's item-driven rule.

Perl decides the style in ``Slim/Control/XMLBrowser.pm`` from the ITEMS the
window contains, not from the browse mode::

    :1104-1106  $windowStyle = $item->{style} if $item->{style};   # feed style
    :1119-1123  if ($item->{'name2'}) { … $windowStyle = 'icon_list' if !$windowStyle; }
    :1124-1126  elsif (my $line2 = $item->{line2} || $item->{subtext}) {
                    $windowStyle = 'icon_list';
                    $itemText = ($item->{line1} || $nameOrTitle) . "\\n" . $line2; }
    :1160-1169  $item->{icon}/{image}/{artwork_track_id} → icon(-id) + $hasImage = 1
    :1434-1441  $windowStyle ? it : ($hasImage ? 'home_menu' : 'text_list')

Live Perl 9.1.1 (read-only, 2026-09-14, player ``ca:c8:c7:26:6d:38``,
``browselibrary items 0 5 menu:1 mode:<m>``):

    mode      windowStyle   item evidence
    --------  ------------  ---------------------------------------------
    albums    icon_list     text = "<album>\\n<artist>" (line2 rule)
    artists   home_menu     every row carries ``icon-id: html/images/artists.png``
    genres    text_list     text only, no image
    years     text_list     text only, no image
    tracks    text_list     audio rows, no image
    bmf       text_list     folder/file rows, no image
    playlists text_list     empty-library placeholder row ("Leer")

This port answered a hard-coded ``icon_list`` for every one of them
(``api.py`` ``_json_browselibrary``) — the deviation these tests pin.
"""

import asyncio
import sqlite3

from lyrion.web import api as api_mod
from lyrion.web import menus
from lyrion.web.api import JSONRPCAPI

#: Live Perl 9.1.1 answers (mode → windowStyle), 2026-09-14, read-only.
PERL_WINDOW = {
    "albums": "icon_list",
    "artists": "home_menu",
    "genres": "text_list",
    "years": "text_list",
    "tracks": "text_list",
    "bmf": "text_list",
}


#: ``_db``-cache — ``_library_db_path`` is read several times per request,
#: so the file must be built once per ``tmp_path`` (a second CREATE TABLE
#: would raise and the read would degrade to Perl's empty answer).
_DB_CACHE: dict[str, str] = {}


def _db(tmp_path):
    """Minimal library: albums (+artist line), artists, genres, years, files."""
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
            content_type TEXT, replay_gain REAL, replay_peak REAL,
            audio INTEGER DEFAULT 1
        );
        CREATE TABLE contributors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tracks_contributors (track INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, artwork TEXT);
        CREATE TABLE tracks_albums (track INTEGER, album INTEGER);
        CREATE TABLE albums_contributors (album INTEGER, contributor INTEGER, role INTEGER);
        CREATE TABLE remote_media (id INTEGER PRIMARY KEY, url TEXT, name TEXT, bitrate INTEGER);

        INSERT INTO tracks (id, title, url, duration, year, tracknum, genre, remote, content_type)
        VALUES (11, 'First Song', 'file:///srv/music/Rock/The Album/01.mp3',
                180.0, 1999, 1, 'Rock', 0, 'mp3'),
               (12, 'Second Song', 'file:///srv/music/Rock/The Album/02.mp3',
                200.0, 1999, 2, 'Rock', 0, 'mp3'),
               (13, 'Jazz Tune', 'file:///srv/music/Jazz/Other/03.flac',
                210.0, 2001, 1, 'Jazz', 0, 'flac');
        INSERT INTO contributors (id, name) VALUES (7, 'The Artist'), (8, 'Other Artist');
        INSERT INTO tracks_contributors (track, contributor, role)
        VALUES (11, 7, 1), (12, 7, 1), (13, 8, 1);
        INSERT INTO albums (id, title, artwork) VALUES
            (45, 'The Album', '/music/art/cover.jpg'),
            (46, '#Alpha', NULL);
        INSERT INTO tracks_albums (track, album) VALUES (11, 45), (12, 45), (13, 46);
        INSERT INTO albums_contributors (album, contributor, role) VALUES (45, 7, 1);
        """
    )
    con.commit()
    con.close()
    _DB_CACHE[key] = str(path)
    return _DB_CACHE[key]


def _browse(args):
    async def run():
        return await JSONRPCAPI()._json_browselibrary("browselibrary", args)

    return asyncio.run(run())


def _mode(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    monkeypatch.setattr(api_mod, "_bmf_musicdir_pref", lambda: "/srv/music")
    return _browse(["items", "0", "10", "menu:1", f"mode:{mode}"])


# ---------------------------------------------------------------------------
# the rule itself (XMLBrowser.pm:1104-1127,1434-1441)
# ---------------------------------------------------------------------------
def test_line2_second_line_forces_icon_list():
    """``name2``/``line2`` ⇒ icon_list (:1119-1126) — Perl renders
    ``text = name "\\n" line2``, so the newline in our rendered text is it."""
    assert menus.window_style_for_items([{"text": "Album\nArtist"}]) == {
        "windowStyle": "icon_list"}


def test_image_without_second_line_is_home_menu():
    """``$hasImage`` ⇒ home_menu (:1434-1438) — live artists/albums.png."""
    assert menus.window_style_for_items([
        {"text": "The Artist", "icon-id": "html/images/artists.png"}]) == {
        "windowStyle": "home_menu"}
    assert menus.window_style_for_items([
        {"text": "The Artist", "icon": "html/images/artists.png"}]) == {
        "windowStyle": "home_menu"}
    assert menus.window_style_for_items([
        {"text": "Song", "artwork_track_id": "45"}]) == {
        "windowStyle": "home_menu"}


def test_plain_text_rows_are_a_text_list():
    """Neither image nor second line ⇒ text_list (:1440-1441)."""
    assert menus.window_style_for_items([{"text": "Rock"}]) == {
        "windowStyle": "text_list"}
    assert menus.window_style_for_items([]) == {"windowStyle": "text_list"}


def test_second_line_beats_an_image():
    """The line2 branch is unconditional (:1124-1126) — live albums carry
    both an artwork icon-id and the artist line and answer icon_list."""
    assert menus.window_style_for_items([
        {"text": "Album\nArtist", "icon-id": "45", "icon": "music/45/cover"},
    ]) == {"windowStyle": "icon_list"}


# ---------------------------------------------------------------------------
# per mode, against the live Perl answers
# ---------------------------------------------------------------------------
def test_albums_window_is_icon_list(tmp_path, monkeypatch):
    res = _mode(tmp_path, monkeypatch, "albums")
    assert res["window"] == {"windowStyle": PERL_WINDOW["albums"]}
    # the artist line ("<album>\n<artist>") is the reason (XMLBrowser.pm:1124)
    assert any("\n" in str(it.get("text")) for it in res["item_loop"])
    # and it still wins over the artwork image on the same row (:1124-1126)
    with_art = [it for it in res["item_loop"] if "\n" in str(it.get("text"))
                and (it.get("icon-id") or it.get("icon"))]
    assert with_art, "the fixture album with a cover must carry both signals"


def test_artists_window_is_home_menu(tmp_path, monkeypatch):
    res = _mode(tmp_path, monkeypatch, "artists")
    assert res["window"] == {"windowStyle": PERL_WINDOW["artists"]}
    item = res["item_loop"][0]
    assert item.get("icon-id") or item.get("icon"), "artist rows carry the image"


def test_genres_window_is_text_list(tmp_path, monkeypatch):
    res = _mode(tmp_path, monkeypatch, "genres")
    assert res["window"] == {"windowStyle": PERL_WINDOW["genres"]}
    assert "icon" not in res["item_loop"][0]
    assert "icon-id" not in res["item_loop"][0]


def test_years_window_is_text_list(tmp_path, monkeypatch):
    res = _mode(tmp_path, monkeypatch, "years")
    assert res["window"] == {"windowStyle": PERL_WINDOW["years"]}


def test_tracks_window_is_text_list(tmp_path, monkeypatch):
    res = _mode(tmp_path, monkeypatch, "tracks")
    assert res["window"] == {"windowStyle": PERL_WINDOW["tracks"]}


def test_music_folder_window_is_text_list(tmp_path, monkeypatch):
    """Perl's bmf feed gives folder rows no icon at all
    (``BrowseLibrary.pm:2051-2083``; only a track row with a ``coverid`` gets
    ``image``, :2097-2100) — live Perl answers text_list.  An invented
    ``html/images/musicfolder.png`` on our rows forced home_menu."""
    res = _mode(tmp_path, monkeypatch, "bmf")
    assert res["window"] == {"windowStyle": PERL_WINDOW["bmf"]}
    for item in res["item_loop"]:
        assert "icon" not in item and "icon-id" not in item, (
            f"Perl's bmf rows carry no image: {sorted(item)}")


def test_playcontrol_context_menu_stays_text_list(tmp_path, monkeypatch):
    """The play-control window is text_list too (live Perl + fixtures
    ``perl_browselibrary_playcontrol_*.json``) — unaffected by the rule."""
    monkeypatch.setattr(api_mod, "_library_db_path", lambda: _db(tmp_path))
    res = _browse(["items", "0", "1", "mode:tracks", "album_id:45",
                   "useContextMenu:1", "menu:1",
                   "xmlbrowserPlayControl:0"])
    assert res["window"] == {"windowStyle": "text_list"}
