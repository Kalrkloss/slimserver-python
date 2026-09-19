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


def test_embedded_tag_image_blocks_the_online_search(scanner_env, monkeypatch):
    """Tag-Bild zuerst (Perl ``_readCoverArtTags``, :480-516)."""
    music, audio, patch = scanner_env
    patch({"TIT2": "Song", "TPE1": "Bonfire", "TALB": "Point Blank"},
          pictures=[{"type": 3, "data": jpeg_bytes(300)}])   # FLAC-/Vorbis-Bild

    stub = _RecordingService()
    monkeypatch.setattr(art_online, "_service", stub)
    result = _run(MediaScanner(config=ScanConfig(base_path=music)).scan_single_file(audio))

    assert result is not None and result.has_embedded_artwork is True
    assert result.artwork_path is None
    assert stub.requests == [], "Tag-Bild vorhanden — keine Online-Suche"


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
    """Der Sortierschlüssel („bonfire“) findet den Treffer vom Tag („Bonfire“)."""
    service = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    written = service.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="e104643d", width=500, height=500)

    for artist in ("Bonfire", "bonfire", "BONFIRE", "etwas anderes"):
        result = service.read_cached_album("Point Blank", artist, 1989)
        assert result is not None, f"Artist-Variante {artist!r} fand nichts"
        assert result.path == written
    assert service.read_cached_album("Anderes Album", "Bonfire", 1989) is None


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
