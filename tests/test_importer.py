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
