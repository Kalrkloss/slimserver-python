"""Verdrahtung der Online-Albumcover-Suche (Scanner → Dienst → Web).

Belegt mit rohen Werten, was der Kollege-Agent als offene Verdrahtung gemeldet
hat (Commit ``8453909e8`` baute nur das Modul ``media/art_online.py``):

1. Reihenfolge des Scans wie Perl — Tag-Bild → Ordnerbild → **hier** online
   (``Slim/Music/Artwork.pm:387-400`` ``readCoverArt`` → ``:480-516``
   ``_readCoverArtTags`` → ``:517-637`` ``_readCoverArtFiles``).  Ohne Bild
   wird die Suche *angestossen* (``request_lookup``), nie abgewartet.
2. Perls Ordnerbild-Namen (``:523-524``) stehen in ``scanner.COVER_NAMES``.
3. Das Scan-Ende sammelt die angestossenen Suchen ein (Queue-Drain).
4. ``web/app.py`` liefert ein **gecachtes** Online-Cover vor dem Platzhalter
   aus — die gecachte Datei wird wirklich gelesen, kein Netz.
5. Die Einstellungen sind beim Start registriert (nicht nur lazy auf der Seite).
6. Album-Artist (TPE2/ALBUMARTIST) und Compilation (TCMP/COMPILATION) kommen
   aus ``_extract_tags`` beim Importer an (Album-Identität nach Perl).

Kein Live-Netz: Anbieter werden durch Fakes ersetzt; Bilddaten sind echte
JPEG-Bytes (Pillow), damit Format- und Grössenprüfung laufen.
"""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import aiosqlite
import pytest
from PIL import Image
from sqlalchemy import select

from lyrion.config import _PREF_DB_SCHEMA, get_config, get_prefs
from lyrion.database.sqlite_helper import close_db, db_session, init_db
from lyrion.database.schema import Album
from lyrion.media import art_online
from lyrion.media.art_online import (
    AlbumQuery,
    ArtOnlineService,
    ArtOnlineSettings,
    FetchResponse,
    ProviderError,
)
from lyrion.media.scanner import COVER_NAMES, MediaScanner, ScanConfig, ScanResult

# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def jpeg_bytes(edge: int = 500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (edge, edge), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeInfo:
    length = 1.0
    bitrate = 128000
    sample_rate = 44100
    channels = 2


class _FakeAudio:
    """Mutagen-Ersatz: nur ``tags``/``info``/``pictures`` werden gelesen."""

    def __init__(self, tags: dict, pictures: list | None = None) -> None:
        self.tags = tags
        self.info = _FakeInfo()
        self.pictures = pictures or []


class _RecordingService:
    """Dienst-Attrappe: merkt sich jede angeforderte Suche."""

    def __init__(self, enabled: bool = True) -> None:
        self.settings = ArtOnlineSettings(enabled=enabled, cache_dir=Path("/tmp"))
        self.requests: list[AlbumQuery] = []

    def request_lookup(self, query: AlbumQuery) -> bool:
        self.requests.append(query)
        return True


@pytest.fixture
def scanner_env(tmp_path, monkeypatch):
    """Ein Ordner mit einem „Song“; Mutagen wird durch Tags ersetzt."""
    import lyrion.media.scanner as scanner_mod

    music = tmp_path / "music"
    music.mkdir()
    audio = music / "song.mp3"
    audio.write_bytes(b"ID3\x03\x00\x00\x00")

    def _patch(tags: dict, pictures: list | None = None) -> None:
        monkeypatch.setattr(scanner_mod, "MutagenFile",
                            lambda path: _FakeAudio(tags, pictures))

    art_online.reset_service()
    yield music, audio, _patch
    art_online.reset_service()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Scan-Reihenfolge: Tag → Ordner → online
# ---------------------------------------------------------------------------


def test_folder_image_wins_and_no_online_search(scanner_env, monkeypatch):
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"})
    (music / "cover.jpg").write_bytes(jpeg_bytes(300))

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)

    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))

    assert result is not None and result.artwork_path == music / "cover.jpg"
    assert stub.requests == [], "Ordnerbild vorhanden — keine Online-Suche"
    # Gegenprobe der Namensliste (Perl :523-524)
    assert "cover.jpg" in COVER_NAMES and "Cover.png" in COVER_NAMES
    assert "thumb.jpg" in COVER_NAMES and "Folder.gif" in COVER_NAMES


def test_perl_uppercase_name_is_found(scanner_env, monkeypatch):
    """Perl prüft ``Cover.jpg``/``Thumb.jpg``/``folder.jpg`` einzeln (:523-532)."""
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"})
    (music / "Cover.jpg").write_bytes(jpeg_bytes(300))

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)
    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))

    assert result is not None and result.artwork_path == music / "Cover.jpg"
    assert stub.requests == []


def test_embedded_tag_image_blocks_the_online_search(scanner_env, monkeypatch, tmp_path):
    """Tag-Bild zuerst (Perl ``_readCoverArtTags``, :480-516).

    Perls Reihenfolge ist **nicht** „Ordnerbild zuerst": ``Slim/Schema.pm:
    1799-1813`` nimmt ``findStandaloneArtwork`` nur, wenn die Tags kein Bild
    lieferten (``Audio::Scan`` liefert ``COVER``/``COVER_LENGTH``), und
    ``albums.artwork`` hängt am Cover des Tracks (:1376-1381).  Das Tag-Bild
    wird deshalb zu einer Datei materialisiert
    (``artwork.write_embedded_artwork``) — nur dann kann die Cover-URL es
    ausliefern (``web/app.py`` liest ``Album.artwork`` als Pfad).
    """
    music, audio, patch = scanner_env
    cover_bytes = jpeg_bytes(300)
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"},
          pictures=[{"type": 3, "data": cover_bytes}])   # FLAC-/Vorbis-Bild

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)
    cache_dir = tmp_path / "artcache"
    result = _run(MediaScanner(config=ScanConfig(
        base_path=music, artwork_cache_dir=cache_dir)).scan_single_file(audio))

    assert result is not None and result.has_embedded_artwork is True
    assert stub.requests == [], "Tag-Bild vorhanden — keine Online-Suche"
    assert result.artwork_path is not None, "Tag-Bild muss materialisiert werden"
    assert result.artwork_path.parent == cache_dir
    assert result.artwork_path.suffix == ".jpg"
    assert result.artwork_path.read_bytes() == cover_bytes, "echte Bildbytes"
    with Image.open(result.artwork_path) as img:      # echtes Bild, lesbar
        assert (img.width, img.height) == (300, 300)


def test_tag_image_wins_over_the_folder_image(scanner_env, monkeypatch, tmp_path):
    """Perl ``Schema.pm:1799-1813``: Ordnerbild nur ohne Tag-Bild."""
    music, audio, patch = scanner_env
    tag_bytes = jpeg_bytes(300)
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"},
          pictures=[{"type": 3, "data": tag_bytes}])
    (music / "cover.jpg").write_bytes(jpeg_bytes(120))

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)
    result = _run(MediaScanner(config=ScanConfig(
        base_path=music, artwork_cache_dir=tmp_path / "artcache")).scan_single_file(audio))

    assert result is not None
    assert result.artwork_path is not None
    assert result.artwork_path.read_bytes() == tag_bytes
    assert result.artwork_path.parent != music
    assert stub.requests == []


def test_same_tag_image_is_not_rewritten(scanner_env, monkeypatch, tmp_path):
    """Inhalts-Hash als Dateiname: zwei Scans finden dieselbe Datei wieder."""
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"},
          pictures=[{"type": 3, "data": jpeg_bytes(300)}])
    config = ScanConfig(base_path=music, artwork_cache_dir=tmp_path / "artcache")
    scanner = MediaScanner(config=config)

    first = _run(scanner.scan_single_file(audio))
    assert first is not None and first.artwork_path is not None
    stamp = first.artwork_path.stat().st_mtime_ns
    second = _run(scanner.scan_single_file(audio))

    assert second is not None and second.artwork_path is not None
    assert first.artwork_path == second.artwork_path
    assert len(list(first.artwork_path.parent.iterdir())) == 1
    assert second.artwork_path.stat().st_mtime_ns == stamp, "nicht neu geschrieben"


def test_no_image_anywhere_starts_the_lookup_with_album_artist(scanner_env, monkeypatch):
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Track Artist", "TPE2": "Bonfire",
           "TALB": "Point Blank", "TDRC": "1989"})

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)
    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))

    assert result is not None and result.artwork_path is None
    assert len(stub.requests) == 1
    query = stub.requests[0]
    assert query.album == "Point Blank"
    # Album-Interpret (TPE2) schlägt den Track-Interpreten — Perl
    # ``ALBUMARTIST || ARTIST`` (``Slim/Schema.pm:3065``).
    assert query.artist == "Bonfire"
    assert query.year == 1989


def test_lookup_is_not_awaited_per_file(scanner_env, monkeypatch, tmp_path):
    """Der Scan wartet nie auf das Netz (Vorgabe: Netz blockiert den Scan nicht)."""
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"})

    class _SlowFetcher:
        async def get(self, url, params=None):
            await asyncio.sleep(30)                     # „Netz hängt“
            raise ProviderError("sollte nie erreicht werden")

        async def close(self):
            pass

    art_online.reset_service()
    art_online.get_service(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    art_online._service.fetcher = _SlowFetcher()

    import time

    start = time.monotonic()
    _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"scan_single_file wartete {elapsed:.1f}s aufs Netz"


# ---------------------------------------------------------------------------
# 2. Dienst: eine Anfrage je Album, Scan-Ende sammelt ein
# ---------------------------------------------------------------------------


def test_request_lookup_dedupes_the_same_album(tmp_path):
    async def run():
        service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
        service.fetcher = _RecordingFetcher()
        query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
        assert service.request_lookup(query) is True
        assert service.request_lookup(query) is True      # Duplikat
        assert service.queue_pending == 1, "ein Album nur einmal einreihen"
        await service.close()

    _run(run())


def test_drain_at_scan_end_writes_the_miss_row(tmp_path, monkeypatch):
    """``scan()`` endet erst, wenn die Queue leer ist (Nachweis: Cache-Zeile)."""
    music = tmp_path / "music"
    music.mkdir()
    audio = music / "song.mp3"
    audio.write_bytes(b"ID3\x03\x00\x00\x00")

    import lyrion.media.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda path: _FakeAudio(
        {"TIT2": "Song", "TPE1": "Niemand", "TALB": "Garantiert kein Treffer 4711"}))
    art_online.reset_service()
    service = art_online.get_service(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    service.fetcher = _RecordingFetcher()

    results, stats = _run(MediaScanner(config=ScanConfig(base_path=music)).scan())

    assert stats.processed_files == 1 and results
    hits = service.cache.stats_sync()
    assert hits.get("miss") == 1, f"erwartete eine Fehlschlag-Zeile, sah {hits}"
    art_online.reset_service()


class _RecordingFetcher:
    """Antwortet wie MusicBrainz „0 Treffer“ und protokolliert die URLs."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url, params=None):
        self.urls.append(url + ("?" + str(sorted((params or {}).items())) if params else ""))
        return FetchResponse(status=200, content=b'{"count": 0, "releases": []}',
                             headers={"Content-Type": "application/json"})

    async def close(self):
        pass


def test_transient_miss_is_retried_after_six_hours(tmp_path, monkeypatch):
    """Ein Rate-Limit (503) blockiert das Album nicht ``retry_days`` (30 Tage).

    Live belegt (2026-09-19): zwei Suchen 0,5 s auseinander → MusicBrainz
    ``503 (Rate-Limit)``.  Der Fehlschlag bekommt deshalb das Präfix
    ``vorübergehend:`` und gilt nur ``TRANSIENT_RETRY_SECONDS`` (6 h) — eine
    definitive Absage (0 Treffer) gilt weiter ``retry_days``.
    """
    class _Fetcher:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status, self.payload, self.calls = status, payload, 0

        async def get(self, url, params=None):
            self.calls += 1
            return FetchResponse(status=self.status, content=self.payload,
                                 headers={"Content-Type": "application/json"})

        async def close(self):
            pass

    def age_hours(service, query, hours: float) -> None:
        with service.cache._connect() as conn:
            conn.execute(
                "UPDATE artwork_online_cache SET created_at = ? WHERE album_key = ?",
                (int(time.time() - hours * 3600), query.key()))
            conn.commit()

    async def run(status: int, payload: bytes, hours: float) -> tuple[str, int, int]:
        service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / f"c{status}"))
        service.fetcher = _Fetcher(status, payload)
        query = AlbumQuery(album="Rate Limit Album", artist="Jemand", year=2001)
        assert await service.find_cover(query) is None
        entry = service.cache.read_sync(query.key())
        assert entry is not None and entry.status == "miss"
        first_calls = service.fetcher.calls
        age_hours(service, query, hours)
        await service.find_cover(query)
        return entry.reason, first_calls, service.fetcher.calls

    # 503 (Rate-Limit): Präfix gesetzt, nach 7 h wird erneut gefragt.
    reason, first, second = _run(run(503, b"{}", 7.0))
    assert reason.startswith(art_online.TRANSIENT_REASON_PREFIX), reason
    assert "503" in reason
    assert second > first, "vorübergehender Fehlschlag muss erneut versucht werden"

    # Definitive Absage (0 Treffer): kein Präfix, nach 7 h NICHT erneut fragen.
    reason, first, second = _run(run(200, b'{"count": 0, "releases": []}', 7.0))
    assert not reason.startswith(art_online.TRANSIENT_REASON_PREFIX), reason
    assert second == first, "definitiver Fehlschlag bleibt retry_days liegen"


# ---------------------------------------------------------------------------
# 3. Cache-Auslieferung über die Album-Zeile (app.py)
# ---------------------------------------------------------------------------


def test_read_cached_album_matches_the_sort_key_spelling(tmp_path):
    """Der Sortierschlüssel („bonfire“) findet den Treffer vom Tag („Bonfire“).

    **Fremder Interpret zählt nicht**: der Titel allein ist kein Schlüssel
    („Greatest Hits“ gibt es von Fleetwood Mac, Barry Manilow, Janis Joplin,
    ZZ Top, The Police und Bob Marley — UAS sucht immer mit Interpret,
    ``albumuniversal.xml:6-10``); lieber der Perl-Platzhalter
    (``Slim/Web/Graphics.pm:275-291``) als ein fremdes Bild.
    """
    service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="e104643d", width=500, height=500)

    for artist in ("Bonfire", "bonfire", "BONFIRE", "The Bonfire"):
        result = service.read_cached_album("Point Blank", artist, 1989)
        assert result is not None, f"Artist-Variante {artist!r} fand nichts"
        assert result.path == written
    assert service.read_cached_album("Anderes Album", "Bonfire", 1989) is None
    # Fremder Interpret zum selben Titel: kein Treffer (nicht raten).
    assert service.read_cached_album("Point Blank", "Accept", 1989) is None
    assert service.read_cached_album("Point Blank", "", 1989) is None


def test_cover_route_serves_the_cached_online_cover(tmp_path, monkeypatch):
    """``_online_cover_for_album`` liest die gecachte Datei (echte DB + Cache)."""
    from lyrion.web import app as app_mod

    data = jpeg_bytes(500)
    cache_dir = tmp_path / "artwork-online"
    service = ArtOnlineService(ArtOnlineSettings(cache_dir=cache_dir))
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, data, provider="coverartarchive", mime="image/jpeg",
        source_url="https://coverartarchive.org/x", mbid="e104643d",
        width=500, height=500)

    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                album = Album(titlesort="point blank", title="Point Blank",
                              albumartist_sort="bonfire", year=1989, artwork=None)
                session.add(album)
                await session.commit()
                album_id = int(album.id)
            monkeypatch.setattr(art_online, "_service", service)
            got, mime = await app_mod._online_cover_for_album(album_id, None)
            small, small_mime = await app_mod._online_cover_for_album(album_id, (40, 40))
            return album_id, got, mime, small, small_mime
        finally:
            await close_db()

    album_id, got, mime, small, small_mime = _run(run())
    assert got == data and mime == "image/jpeg", f"Album {album_id}: {len(got or b'')} Bytes"
    assert written.read_bytes() == data
    with Image.open(io.BytesIO(small)) as img:            # Grössenvariante
        assert (img.width, img.height) == (40, 40)
    assert small_mime == "image/jpeg"


def test_cover_route_without_cache_returns_nothing(tmp_path, monkeypatch):
    """Negativfall: kein Treffer → ``(None, None)`` → Platzhalter (kein Netz)."""
    from lyrion.web import app as app_mod

    service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "leer"))
    service.fetcher = _RecordingFetcher()

    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                album = Album(titlesort="x", title="Ohne Cover", year=2020)
                session.add(album)
                await session.commit()
                album_id = int(album.id)
            monkeypatch.setattr(art_online, "_service", service)
            got, mime = await app_mod._online_cover_for_album(album_id, None)
            return got, mime, await app_mod._online_cover_for_album(999999, None)
        finally:
            await close_db()

    got, mime, missing = _run(run())
    assert (got, mime) == (None, None)
    assert missing == (None, None)
    # Kein einziger HTTP-Aufruf im Auslieferungspfad.
    assert service.fetcher.urls == []


# ---------------------------------------------------------------------------
# 3b. Album-Zeile ↔ Treffer: ``albums.artwork`` aus dem Online-Cover
# ---------------------------------------------------------------------------


def _empty_album_cover_cache(tmp_path):
    """Dienst mit leerem Plattencache (keine Netzanfrage in diesen Tests)."""
    service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "online"))
    service.fetcher = _RecordingFetcher()
    return service


def test_album_row_takes_the_new_online_cover(tmp_path, monkeypatch):
    """``store_online_cover`` schreibt ``albums.artwork`` (Perl ``Schema.pm:1376-1381``).

    Perl hängt das Album-Cover an den Cover-Schlüssel des Tracks
    (``$albumHash->{artwork} = $trackColumns->{coverid}``) und trägt ihn nach,
    sobald ein Track ein Bild bekommt
    (``Slim/Utils/Scanner/Local.pm:1086-1091``).  Hier ist der ``on_cover``-
    Rückruf des Dienstes die Stelle (``media/art_online.py:1244``).
    """
    from lyrion.media.importer import store_online_cover
    from lyrion.media.art_online import AlbumQuery, CoverResult

    service = _empty_album_cover_cache(tmp_path)
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="e104643d", width=500, height=500)
    cover = CoverResult(path=written, provider="coverartarchive",
                        mime="image/jpeg", width=500, height=500)
    folder_cover = str(tmp_path / "cover.jpg")

    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                session.add_all([
                    Album(titlesort="point blank", title="Point Blank",
                          albumartist_sort="bonfire", year=1989, artwork=None),
                    # gleicher Titel, gleiches Jahr, anderer Album-Interpret
                    Album(titlesort="point blank", title="Point Blank",
                          albumartist_sort="jemand anderes", year=1989,
                          artwork=None),
                    # anderer Titel
                    Album(titlesort="anderes album", title="Anderes Album",
                          albumartist_sort="bonfire", year=1989, artwork=None),
                    # hat schon ein Ordnerbild → darf nicht überschrieben werden
                    Album(titlesort="cover album", title="Cover Album",
                          albumartist_sort="bonfire", year=1989,
                          artwork=folder_cover),
                ])
                await session.commit()

            set_rows = await store_online_cover(query, cover)

            async with db_session() as session:
                rows = {(str(a.titlesort), str(a.albumartist_sort)): a.artwork
                        for a in (await session.execute(select(Album))).scalars()}
            return set_rows, rows

        finally:
            await close_db()

    set_rows, rows = _run(run())
    assert set_rows == 1, f"nur die passende Album-Zeile, war {set_rows} ({rows})"
    assert rows[("point blank", "bonfire")] == str(written), \
        "Titel+Artist+Jahr → Cover"
    assert rows[("point blank", "jemand anderes")] is None, \
        "gleicher Titel, anderer Album-Interpret bleibt ohne Cover"
    assert rows[("anderes album", "bonfire")] is None, "anderer Titel bleibt ohne Cover"
    assert rows[("cover album", "bonfire")] == folder_cover, \
        "Album mit Ordnerbild wird nicht überschrieben"


def test_album_row_without_matching_artist_stays_empty(tmp_path):
    """Album-Artist ist Teil der Zuordnung — nicht jeder gleiche Titel zählt."""
    from lyrion.media.importer import store_online_cover
    from lyrion.media.art_online import AlbumQuery, CoverResult

    service = _empty_album_cover_cache(tmp_path)
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="e104643d", width=500, height=500)

    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                album = Album(titlesort="point blank", title="Point Blank",
                              albumartist_sort="jemand anderes", year=1989,
                              artwork=None)
                session.add(album)
                await session.commit()
                album_id = int(album.id)
            rows = await store_online_cover(
                query, CoverResult(path=written, provider="coverartarchive"))
            async with db_session() as session:
                stored = await session.get(Album, album_id)
                return rows, (stored.artwork if stored is not None else "?")

        finally:
            await close_db()

    set_rows, artwork = _run(run())
    assert (set_rows, artwork) == (0, None)


def test_importer_reads_the_online_cache_for_empty_album_rows(tmp_path, monkeypatch):
    """``online_cover_path`` liefert den Plattencache-Treffer — ohne Netz.

    Reihenfolge bleibt Tag-Bild → Ordnerbild → online: nur wenn der Importer
    kein Bild hatte, greift dieser Weg (``media/importer.py`` ``_online_cover``).
    """
    from lyrion.media.importer import online_cover_path, wire_online_artwork

    service = _empty_album_cover_cache(tmp_path)
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="e104643d", width=500, height=500)

    monkeypatch.setattr(art_online, "_service", service)
    assert wire_online_artwork(service) is True
    assert service.on_cover is not None
    assert wire_online_artwork(service) is False, "idempotent"
    assert online_cover_path("Point Blank", "bonfire", 1989) == written
    assert online_cover_path("Point Blank", "Bonfire", 1989) == written
    assert online_cover_path("Ganz anderes Album", "Bonfire", 1989) is None
    assert getattr(service.fetcher, "urls") == [], \
        "kein Netzzugriff beim Nachschlagen"


def test_importer_has_no_online_service_without_one(tmp_path, monkeypatch):
    """Ohne Dienst wird nichts gesucht und kein Dienst angelegt."""
    from lyrion.media.importer import online_cover_path

    monkeypatch.setattr(art_online, "_service", None)
    assert online_cover_path("Point Blank", "Bonfire", 1989) is None
    assert art_online.current_service() is None


# ---------------------------------------------------------------------------
# 5b. ``--localfile``-Konfiguration ↔ GUI-Einstellung (dieselbe Wirkung)
# ---------------------------------------------------------------------------


def test_artwork_online_settings_follow_the_localfile_conf(tmp_path):
    """``artworkOnlineSearch=0`` aus der ``.conf``-Datei schaltet die Suche ab.

    Perl liest Server-Prefs aus ``prefs.db`` (``--prefsfile``/``--prefsdir``,
    ``Slim/Utils/Prefs.pm:60-92``); die Konfigurationsdatei dieses Ports
    (``--localfile``) hat in ``LyrionConfig.get`` Vorrang.  Ohne die
    ``.conf``-Ebene im Prefs-Store las der Scan-Prozess die
    ``artworkOnline*``-Werte nur aus dem ``prefhash`` — der Dateiwert blieb
    wirkungslos (live belegt 2026-09-19).  Gegenprobe: ein ``prefhash``-Wert
    „an“ schlägt die Datei NICHT.
    """
    from lyrion.config import parse_conf
    from lyrion.web import settings as settings_module

    store = get_prefs()
    before = dict(store._conf_overrides)
    cache_before = dict(store._cache)
    conf = parse_conf(
        "artworkOnlineSearch = 0\n"
        "artworkOnlineCacheDir = /tmp/online-covers\n"
        "artworkOnlineRetryDays = 7\n")
    try:
        store.set_conf_overrides(conf.get("", {}))
        # GUI-Wert (prefhash) steht auf „an“ — die Datei gewinnt.
        store._cache["artworkOnlineSearch"] = "1"
        settings = settings_module.load_art_online_settings()
        from_file = (settings.enabled, str(settings.cache_dir), settings.retry_days)

        # Ohne Konfigurationsdatei gilt wieder der prefhash-Wert.
        store.set_conf_overrides({})
        from_prefs = settings_module.load_art_online_settings().enabled
    finally:
        store.clear_conf_overrides()
        store.set_conf_overrides(before)
        store._cache.clear()
        store._cache.update(cache_before)

    assert from_file == (False, "/tmp/online-covers", 7), \
        f"Konfigurationsdatei nicht wirksam: {from_file}"
    assert from_prefs is True, "ohne Datei gilt der prefhash-Wert"


def test_conf_layer_sits_below_the_cli_override():
    """Rangfolge wie in ``LyrionConfig.get``: CLI > ``.conf`` > Prefs-DB."""
    store = get_prefs()
    before_conf = dict(store._conf_overrides)
    before_cli = dict(store._cli_overrides)
    cache_before = dict(store._cache)
    before_value = store.get("artworkOnlineTimeout")
    try:
        store._cache["artworkOnlineTimeout"] = "30"
        store.set_conf_overrides({"artworkOnlineTimeout": "8"})
        assert store.get("artworkOnlineTimeout") == "8"
        store.set_cli_override("artworkOnlineTimeout", 3)
        assert store.get("artworkOnlineTimeout") == 3
    finally:
        store._cli_overrides.clear()
        store._cli_overrides.update(before_cli)
        store.clear_conf_overrides()
        store.set_conf_overrides(before_conf)
        store._cache.clear()
        store._cache.update(cache_before)
    # Der Store ist wieder im Ausgangszustand (für die folgenden Tests).
    assert store.get("artworkOnlineTimeout") == before_value


# ---------------------------------------------------------------------------
# 4. Album-Artist / Compilation aus den Tags
# ---------------------------------------------------------------------------


def test_extract_tags_reads_album_artist_and_compilation(scanner_env):
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Track Artist", "TPE2": "Various Artists",
           "TCMP": "1", "TALB": "Back From The Grave Vol 8", "TDRC": "2016"})

    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))
    assert result is not None
    assert result.album_artist == "Various Artists"
    assert result.compilation is True
    assert result.to_dict()["album_artist"] == "Various Artists"
    assert result.to_dict()["compilation"] is True


def test_extract_tags_without_album_artist_stays_empty(scanner_env):
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Track Artist", "TALB": "Album", "TDRC": "2016"})

    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))
    assert result is not None and result.album_artist == ""
    assert result.compilation is False and result.has_embedded_artwork is False


def test_album_identity_sees_the_tags_from_the_scan(scanner_env, tmp_path):
    """Gegenprobe am Importer: mit Tags Sampler-Schlüssel, ohne Tags Ordnerregel."""
    from lyrion.media.importer import _album_artist_tag, album_identity

    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Track Artist", "TPE2": "Various Artists",
           "TCMP": "1", "TALB": "Back From The Grave Vol 8", "TDRC": "2016"})
    tagged: ScanResult = _run(
        MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))
    assert _album_artist_tag(tagged) == "Various Artists"
    titlesort, artist_key, has_album_artist, _disc = album_identity(tagged, audio)
    assert artist_key == "various artists" and has_album_artist is True

    patch({"TIT2": "Song", "TPE1": "Track Artist", "TALB": "Back From The Grave Vol 8"})
    plain = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))
    assert _album_artist_tag(plain) == ""
    _t, plain_key, has_plain_artist, _d = album_identity(plain, audio)
    assert has_plain_artist is False, "ohne Tag greift die Ordner-Regel"
    assert plain_key == "track artist"


# ---------------------------------------------------------------------------
# 5. Einstellungen beim Start registrieren
# ---------------------------------------------------------------------------


@pytest.fixture
def _prefs_store():
    """Globalen Prefs-Store auf In-Memory-DB hängen (Muster ``test_art_online``)."""
    store = get_prefs()
    opened = False
    if getattr(store, "_db", None) is None:
        async def _open() -> None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
            await store._db.executescript(_PREF_DB_SCHEMA)

        _run(_open())
        opened = True
    yield store
    db = getattr(store, "_db", None)
    if opened and db is not None:
        try:
            _run(db.close())
        except Exception:  # noqa: BLE001
            pass
        store._db = None


def test_artwork_online_prefs_are_registered_at_startup(_prefs_store):
    """``LyrionConfig._register_known_prefs`` registriert ``artworkOnline*``.

    Der Prefs-Store ist ein globales Singleton: die Registrierung schreibt
    ``prefmeta``-Zeilen und den Meta-Cache für *alle* bekannten Prefs (u. a.
    ``mediadirs`` als ``list``).  Damit dieser Test andere Module nicht
    verändert, werden beide Caches vorher/nachher gesichert.
    """
    store = _prefs_store
    meta_before = dict(store._meta_cache)
    cache_before = dict(store._cache)

    async def run():
        await store.init()
        await get_config()._register_known_prefs()
        rows = await (await store._db.execute(
            "SELECT name FROM prefmeta WHERE name LIKE 'artworkOnline%'")).fetchall()
        return {str(r[0]) for r in rows}

    try:
        names = _run(run())
    finally:
        store._meta_cache.clear()
        store._meta_cache.update(meta_before)
        store._cache.clear()
        store._cache.update(cache_before)

    assert set(art_online.PREF_DEFAULTS) <= names, f"nicht registriert: {names}"
