"""Album-Identität nach Perl: Sampler vs. normales Album.

Perl bildet ein Album über den ALBUM-Interpreten — ``ALBUMARTIST || ARTIST ||
TRACKARTIST`` (Slim/Schema.pm:3065), für eine Zusammenstellung das
Various-Artists-Objekt (:1286-1288), dessen compilation-Flag in der Album-Suche
die Artist-Bedingung ERSETZT (:1178-1186, „a contributor match would fail").
Ohne DISC/DISCC/MUSICBRAINZ_ALBUM_ID sucht Perl zusätzlich über den Ordner
(``tracks.url LIKE "$basename%"``, :1198-1209); unterscheiden sich die
Track-Interpreten des so gefundenen Albums, wird es zur Compilation
(``mergeSingleVAAlbum``, :1399-1415 / :2207-2295).

Symptom vorher: jedes Sampler-Album (TALB + nur TPE1 pro Track) zerfiel in ein
Album je Track-Interpret. Live gemessen an der eigenen Bibliothek: der Titel
„german top 100 single charts“ lag in 178 Album-Zeilen (eine je Track-Artist),
Perl führt dieselbe Zusammenstellung als EIN Album mit dem Contributor
„Diverse Interpreten“.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from lyrion.database.schema import (
    Base, Album, Track, tracks_albums,
)
from lyrion.media.importer import (
    ImportConfig, MusicImporter, VA_ARTIST_KEY, album_identity,
)


def _info(title="T", artist="Unknown Artist", album="Unknown Album", year=0, **kw):
    data = dict(title=title, artist=artist, album=album, year=year, duration=10000)
    data.update(kw)
    return SimpleNamespace(**data)


def _engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


async def _albums_with_tracks(session) -> list[tuple]:
    """Roh: (id, title, albumartist_sort, compilation, [(track title, artist)])"""
    out = []
    for album in (await session.execute(select(Album))).scalars().all():
        rows = (await session.execute(text(
            "SELECT t.title, c.name FROM tracks_albums ta "
            "JOIN tracks t ON t.id = ta.track "
            "LEFT JOIN tracks_contributors tc ON tc.track = t.id AND tc.role = 1 "
            "LEFT JOIN contributors c ON c.id = tc.contributor "
            "WHERE ta.album = :a"), {"a": album.id})).all()
        rows = [r for r in rows if r[0] is not None]
        if rows:
            out.append((album.id, album.title, album.albumartist_sort,
                        album.compilation, sorted((r[0], r[1]) for r in rows)))
    return out


async def _run(entries, pre=None):
    engine = _engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    importer = MusicImporter()
    async with Session() as session:
        if pre is not None:
            await pre(session)
        for batch in entries:
            await importer._import_batch(session, batch)
        await session.commit()
        result = await _albums_with_tracks(session)
    await engine.dispose()
    return result


# ---------------------------------------------------------------- Schluessel

def test_album_identity_uses_album_artist_not_track_artist():
    """ALBUMARTIST gewinnt; der Track-Interpret bleibt außen vor."""
    info = _info(artist="Track Artist", album="Some Album",
                 album_artist="Album Artist")
    titlesort, artist_key, has_album_artist, disc = album_identity(
        info, Path("/m/A/1.mp3"))
    assert (titlesort, artist_key, has_album_artist, disc) == \
        ("some album", "album artist", True, None)


def test_album_identity_uses_various_artists_marker():
    """Compilation-Tag → Sampler-Schlüssel (Perl Schema.pm:1178-1186)."""
    info = _info(artist="Track Artist", album="Some Album", compilation=True)
    assert album_identity(info, Path("/m/A/1.mp3"))[1] == VA_ARTIST_KEY


def test_album_identity_maps_localized_various_artists():
    """„Diverse Interpreten“ ist Perls VA-Objekt (lokalisierter Name)."""
    info = _info(artist="Track Artist", album="Some Album",
                 album_artist="Diverse Interpreten")
    assert album_identity(info, Path("/m/A/1.mp3"))[1] == VA_ARTIST_KEY


# ------------------------------------------------------------------ Sampler

def test_sampler_tracks_in_one_folder_become_one_album():
    """Der Kern-Fall: verschiedene Track-Interpreten, ein Album, EIN Ordner."""
    rows = asyncio.run(_run([[
        (Path("/m/GSC/01.mp3"), _info("T1", "Artist A", "German Top 100")),
        (Path("/m/GSC/02.mp3"), _info("T2", "Artist B", "German Top 100")),
        (Path("/m/GSC/03.mp3"), _info("T3", "Artist C", "German Top 100")),
    ]]))
    assert len(rows) == 1, f"Sampler muss EIN Album sein: {rows}"
    _, title, artist_key, compilation, tracks = rows[0]
    assert title == "German Top 100"
    assert artist_key == VA_ARTIST_KEY, artist_key
    assert compilation == 1
    # Track-Interpreten bleiben am Track (Perl Contributor-Track-Rollen).
    assert [t[1] for t in tracks] == ["Artist A", "Artist B", "Artist C"]


def test_sampler_same_folder_with_album_artist_tag_stays_one_album():
    rows = asyncio.run(_run([[
        (Path("/m/S/01.mp3"), _info("T1", "Artist A", "Comp",
                                    album_artist="Various Artists")),
        (Path("/m/S/02.mp3"), _info("T2", "Artist B", "Comp",
                                    album_artist="Various Artists")),
    ]]))
    assert len(rows) == 1, rows
    assert rows[0][2] == VA_ARTIST_KEY
    assert rows[0][3] == 1  # VA als Album-Interpret ⇒ Compilation


def test_sampler_with_compilation_tag_is_one_album():
    rows = asyncio.run(_run([[
        (Path("/m/S/01.mp3"), _info("T1", "Artist A", "Comp", compilation=True)),
        (Path("/m/S/02.mp3"), _info("T2", "Artist B", "Comp", compilation=True)),
    ]]))
    assert len(rows) == 1, rows
    assert rows[0][2] == VA_ARTIST_KEY and rows[0][3] == 1


def test_sampler_split_across_two_batches_stays_one_album():
    """Der Ordner-Treffer läuft über die Batch-Grenze (DB-Suche)."""
    rows = asyncio.run(_run([
        [(Path("/m/GSC/01.mp3"), _info("T1", "Artist A", "Top 100"))],
        [(Path("/m/GSC/02.mp3"), _info("T2", "Artist B", "Top 100"))],
    ]))
    assert len(rows) == 1, rows
    assert len(rows[0][4]) == 2


def test_legacy_split_albums_converge_on_rescan():
    """Reste eines alten Laufs (ein Album je Track-Interpret) laufen zusammen."""
    async def pre(session):
        album_a = Album(titlesort="top 100", title="Top 100",
                        albumartist_sort="artist a", compilation=0)
        album_b = Album(titlesort="top 100", title="Top 100",
                        albumartist_sort="artist b", compilation=0)
        session.add_all([album_a, album_b])
        await session.flush()
        for n, (url, album) in enumerate((
                (Path("/m/GSC/01.mp3").as_uri(), album_a),
                (Path("/m/GSC/02.mp3").as_uri(), album_b)), start=1):
            track = Track(url=url, titlesort=f"t{n}", title=f"T{n}",
                          content_type="audio/mpeg", audio=1, video=0,
                          disabled=0, filesize=1, modtime=1)
            session.add(track)
            await session.flush()
            await session.execute(
                tracks_albums.insert().values(track=track.id, album=album.id))
        await session.flush()

    rows = asyncio.run(_run([[
        (Path("/m/GSC/01.mp3"), _info("T1", "Artist A", "Top 100")),
        (Path("/m/GSC/02.mp3"), _info("T2", "Artist B", "Top 100")),
    ]], pre=pre))
    assert len(rows) == 1, f"Alt-Split muss zusammenlaufen: {rows}"
    _, _, artist_key, compilation, tracks = rows[0]
    assert artist_key == VA_ARTIST_KEY and compilation == 1
    assert len(tracks) == 2


# ------------------------------------------------------- Regressionsschutz

def test_normal_album_keeps_track_artist_identity():
    """Ein-Interpret-Album bleibt unverändert (kein VA, ein Album)."""
    rows = asyncio.run(_run([[
        (Path("/m/A/1.mp3"), _info("T1", "Artist A", "Greatest Hits")),
        (Path("/m/A/2.mp3"), _info("T2", "Artist A", "Greatest Hits")),
    ]]))
    assert len(rows) == 1, rows
    assert rows[0][2] == "artist a" and rows[0][3] == 0


def test_same_album_name_different_artists_different_folders_stay_apart():
    """Gleichnamige Alben verschiedener Interpreten bleiben getrennt."""
    rows = asyncio.run(_run([[
        (Path("/m/A/1.mp3"), _info("T1", "Artist A", "Greatest Hits")),
        (Path("/m/B/1.mp3"), _info("T1", "Artist B", "Greatest Hits")),
    ]]))
    assert len(rows) == 2, rows
    assert {r[2] for r in rows} == {"artist a", "artist b"}
    assert all(r[3] == 0 for r in rows)


def test_same_album_name_different_album_artists_different_folders_stay_apart():
    """Auch über den ALBUMARTIST bleiben zwei Alben zwei Alben."""
    rows = asyncio.run(_run([[
        (Path("/m/A/1.mp3"), _info("T1", "X", "Live",
                                   album_artist="Band A")),
        (Path("/m/B/1.mp3"), _info("T1", "Y", "Live",
                                   album_artist="Band B")),
    ]]))
    assert len(rows) == 2, rows
    assert {r[2] for r in rows} == {"band a", "band b"}


def test_track_artist_albums_with_disc_numbers_keep_artist_identity():
    """Mit DISC-Tag entscheidet Perl über Titel+DISC+Contributor (:1126-1166):
    der Ordner-Treffer ist dann NICHT zulässig."""
    rows = asyncio.run(_run([[
        (Path("/m/C/1.mp3"), _info("T1", "Artist A", "Live", disc_number=1)),
        (Path("/m/C/2.mp3"), _info("T2", "Artist B", "Live", disc_number=1)),
    ]]))
    assert len(rows) == 2, rows


# -------------------------------------------------- Scan-Ende (Album->rescan)

@pytest.fixture
def _scan_lib_db(tmp_path, monkeypatch):
    """Globales ``db_session`` auf eine Wegwerf-DB + Sampler-Scanner."""
    db_path = tmp_path / "lyrion.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def _session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", _session_ctx)

    async def _fake_scan(self, file_path):
        # Sampler-Form: EIN Album-Titel, je Track ein anderer Interpret,
        # kein ALBUMARTIST, kein COMPILATION-Tag.
        return SimpleNamespace(
            title=file_path.stem,
            artist=f"Artist {file_path.stem}",
            album="Sampler Vol 1",
            compilation=False,
            mimetype="audio/mpeg",
            duration=180000,
        )

    monkeypatch.setattr(
        "lyrion.media.scanner.MediaScanner.scan_single_file", _fake_scan)

    yield db_path
    asyncio.run(engine.dispose())


def test_full_scan_of_a_sampler_yields_one_album_and_prunes_orphans(
        tmp_path, _scan_lib_db):
    """Ende-zu-Ende: Scan eines Sampler-Ordners → EIN Album am Ende.

    Deckt zusätzlich Perls ``Slim::Schema::Album->rescan``
    (Slim/Schema/Album.pm:383-406) ab: eine Album-Zeile ohne Track muss am
    Scan-Ende verschwinden, sonst bliebe der Split sichtbar.
    """
    db_path = _scan_lib_db
    music = tmp_path / "music"
    folder = music / "Sampler"
    folder.mkdir(parents=True)
    for i in range(3):
        (folder / f"t{i}.mp3").write_bytes(b"x")

    # Karteileiche: Album-Zeile ohne Track (Altzustand eines Splits)
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO albums (titlesort, title, albumartist_sort, "
                "disccount, disc, compilation, numtracks, numdiscs, "
                "artflow_flag, disabled) VALUES "
                "('sampler vol 1', 'Sampler Vol 1', 'artist t0', 1, 1, 0, 0, 1, 0, 0)")
    con.commit()
    con.close()

    async def run():
        importer = MusicImporter(ImportConfig(source_path=music))
        return await importer.import_music()

    stats = asyncio.run(run())
    assert stats.imported_files == 3

    con = sqlite3.connect(db_path)
    try:
        albums = con.execute(
            "SELECT id, title, albumartist_sort, compilation FROM albums").fetchall()
        links = con.execute("SELECT COUNT(*) FROM tracks_albums").fetchone()[0]
    finally:
        con.close()
    assert len(albums) == 1, f"Sampler muss EIN Album sein: {albums}"
    assert albums[0][2] == VA_ARTIST_KEY and albums[0][3] == 1, albums
    assert links == 3
