"""Tests for album identity and retag link cleanup in the importer.

Regression: album identity was (sort-title, year) — different artists'
same-title/same-year albums merged into one, and differing track years
split a single compilation. Retagging also left stale album/contributor
links.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from lyrion.database.schema import Base, Album, tracks_albums, tracks_contributors
from lyrion.media.importer import MusicImporter


def _info(title="T", artist="Unknown Artist", album="Unknown Album", year=0):
    return SimpleNamespace(title=title, artist=artist, album=album, year=year, duration=10000)


async def _import(importer, session, entries):
    for batch in entries:
        await importer._import_batch(session, batch)
    await session.commit()


def _engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


def test_two_artists_same_album_name_year_do_not_merge():
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        importer = MusicImporter()
        async with Session() as session:
            await _import(importer, session, [[
                (Path("/m/A/a.mp3"), _info(artist="Artist A", album="Greatest Hits", year=2000)),
                (Path("/m/B/b.mp3"), _info(artist="Artist B", album="Greatest Hits", year=2000)),
            ]])
            albums = (await session.execute(select(Album))).scalars().all()
            return [a.title for a in albums]
        await engine.dispose()

    titles = asyncio.run(run())
    assert len(titles) == 2, f"expected 2 albums, got {titles}"


def test_different_track_years_do_not_split_album():
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        importer = MusicImporter()
        async with Session() as session:
            await _import(importer, session, [[
                (Path("/m/C/1.mp3"), _info(artist="Artist C", album="Same Album", year=1999)),
                (Path("/m/C/2.mp3"), _info(artist="Artist C", album="Same Album", year=2000)),
            ]])
            albums = (await session.execute(select(Album))).scalars().all()
            return [a.title for a in albums]
        await engine.dispose()

    titles = asyncio.run(run())
    assert titles == ["Same Album"], f"year must not split an album: {titles}"


def test_retag_removes_stale_links():
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        importer = MusicImporter()
        path = Path("/m/X/t.mp3")
        async with Session() as session:
            await _import(importer, session, [[
                (path, _info(artist="Old Artist", album="Old Album")),
            ]])
            await _import(importer, session, [[
                (path, _info(artist="New Artist", album="New Album")),
            ]])
            # the track must now link to exactly one album + one contributor
            ta = (await session.execute(
                select(tracks_albums.c.album).where(tracks_albums.c.track == 1))).all()
            tc = (await session.execute(
                select(tracks_contributors.c.contributor).where(
                    tracks_contributors.c.track == 1))).all()
            return [r[0] for r in ta], [r[0] for r in tc]
        await engine.dispose()

    ta, tc = asyncio.run(run())
    assert len(ta) == 1, f"stale album links left: {ta}"
    assert len(tc) == 1, f"stale contributor links left: {tc}"


def test_folder_adoption_merges_into_existing_va_album():
    """Ordner-Adoption darf den UNIQUE-Index (titlesort, albumartist_sort) nicht reißen.

    Live-Regression (exit=1 des Scanprozesses nach ~1.900 Titeln):
    ``sqlite3.IntegrityError: UNIQUE constraint failed: albums.titlesort,
    albums.albumartist_sort`` beim ``UPDATE albums SET albumartist_sort=…``.
    Wird ein Ordner-Album zur Compilation (Perl
    ``Slim/Schema.pm:1399-1415`` ``mergeSingleVAAlbum``, :2207-2295), gibt es
    die Various-Artists-Zeile desselben Titels oft schon (Ordner eines anderen
    Termins derselben Charts-Reihe) — dann müssen die beiden Zeilen
    zusammengelegt werden, statt die zweite Identität zu erzeugen.
    """
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        importer = MusicImporter()
        async with Session() as session:
            # 1) Various-Artists-Zeile des Titels (anderer Ordner desselben
            #    Samplers, eigenes Jahr — sonst greift die (titlesort, year)-
            #    Wiederverwendung und der Konflikt entsteht gar nicht).
            await _import(importer, session, [[
                (Path("/m/A/1.mp3"),
                 _info(artist="Various Artists", album="Charts", year=2023)),
            ]])
            # 2) Ordner-Zeile: erster Track dieses Ordners prägt die Identität.
            await _import(importer, session, [[
                (Path("/m/B/1.mp3"),
                 _info(artist="Alice", album="Charts", year=2024)),
            ]])
            # 3) zweiter Track des Ordners, anderer Interpret -> Compilation-Übernahme.
            await _import(importer, session, [[
                (Path("/m/B/2.mp3"),
                 _info(artist="Bob", album="Charts", year=2024)),
            ]])
            albums = (await session.execute(select(Album))).scalars().all()
            rows = [(a.id, a.albumartist_sort, a.compilation) for a in albums]
            links = sorted(r[0] for r in (await session.execute(
                select(tracks_albums.c.album))).all())
            return rows, links
        await engine.dispose()

    rows, links = asyncio.run(run())
    assert len(rows) == 1, f"eine Identität = eine Albumzeile, bekam {rows}"
    _id, artist_sort, compilation = rows[0]
    assert artist_sort == "various artists", rows
    assert compilation == 1, rows
    # Alle drei Tracks hängen an der zusammengelegten Zeile (keine Waise).
    assert links == [_id, _id, _id], links


def test_folder_adoption_keeps_single_artist_album_together():
    """Gegenprobe: gleicher Interpret im Ordner -> eine Zeile, kein Merge."""
    async def run():
        engine = _engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        importer = MusicImporter()
        async with Session() as session:
            await _import(importer, session, [[
                (Path("/m/C/1.mp3"), _info(artist="Alice", album="Solo")),
                (Path("/m/C/2.mp3"), _info(artist="Alice", album="Solo")),
                (Path("/m/C/3.mp3"), _info(artist="Alice", album="Solo")),
            ]])
            albums = (await session.execute(select(Album))).scalars().all()
            return [(a.id, a.albumartist_sort, a.compilation) for a in albums]
        await engine.dispose()

    rows = asyncio.run(run())
    assert rows == [(1, "alice", 0)], rows
