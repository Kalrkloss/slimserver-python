"""Favorites order = Perl's OPML document order (``position``), not title sort.

Perl's Favorites plugin loads ``favorites.opml`` with ``XML::Simple``
(``Slim/Plugin/Favorites/Opml.pm:74-76``: ``XMLin(…, forcearray =>
['outline','body'], SuppressEmpty => undef)``) and walks the outline arrays in
document order — the Jive menu and the ``favorites items`` answer therefore
follow the OPML order; Perl never sorts by title and has no "folders first"
rule.

Live Perl 9.1.1 (read-only, 2026-09-14, player ``ca:c8:c7:26:6d:38``,
``favorites items 0 100``)::

    Chill, Nachrichten, Lokal, Dub, Trance, Rock,
    1Mix Radio EDM Stream, 1Mix Radio EDM Stream

with the sub-trees in document order too (``Chill`` →
``Hirschmilch Chillout, Absolut relax (Easy Listening), Hirschmilch
Electronic, SUNSHINE LIVE, SUNSHINE LIVE - Chillout, 1.FM - Ambient
Psychill``).

The same OPML imported into this port's DB keeps that order in the ``id``
column (rows are inserted in document order) — while ``position`` stayed 0
for every pre-existing row of the legacy import, so the ORDER BY has to be
``position, id``.  This port used to answer folders-first/alphabetically:
``Chill, Dub, Lokal, Nachrichten, Rock, Trance, …``.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base, Favorite
from lyrion.music.favorites import FavoritesManager

#: The live Perl root order (== OPML document order == insertion/id order).
ROOT_ORDER = ["Chill", "Nachrichten", "Lokal", "Dub", "Trance", "Rock",
              "1Mix Radio EDM Stream", "1Mix Radio EDM Stream"]
CHILL_ORDER = ["Hirschmilch Chillout", "Absolut relax (Easy Listening)",
               "Hirschmilch Electronic", "SUNSHINE LIVE",
               "SUNSHINE LIVE - Chillout", "1.FM - Ambient Psychill"]


@pytest.fixture
def fav_db(tmp_path, monkeypatch):
    """Throwaway DB in OPML document order; every ``position`` stays 0.

    That is exactly the state of the live DB (``position`` was never filled
    by the legacy import), so the ordering must not depend on it alone.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lyrion.db'}")
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
        finally:
            await session.close()

    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", _session_ctx)
    # never merge the developer's real favorites.opml into this fixture
    monkeypatch.setattr("lyrion.music.favorites._opml_path", lambda: None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)

    async def _seed():
        async with _session_ctx() as session:
            # Insertion order = OPML document order (id order on the live DB).
            rows = [
                Favorite(title="Chill", url=None, parent_id=None, position=0),
                Favorite(title="Nachrichten", url=None, parent_id=None, position=0),
                Favorite(title="Lokal", url=None, parent_id=None, position=0),
                Favorite(title="Dub", url=None, parent_id=None, position=0),
                Favorite(title="Trance", url=None, parent_id=None, position=0),
                Favorite(title="Rock", url=None, parent_id=None, position=0),
                Favorite(title="1Mix Radio EDM Stream", url="http://a/1",
                         parent_id=None, position=0),
                Favorite(title="1Mix Radio EDM Stream", url="http://a/2",
                         parent_id=None, position=0),
            ]
            session.add_all(rows)
            await session.flush()
            chill = rows[0]
            for i, name in enumerate(CHILL_ORDER):
                session.add(Favorite(title=name, url=f"http://c/{i}",
                                     parent_id=chill.id, position=0))
            await session.commit()

    asyncio.run(_seed())
    yield
    asyncio.run(engine.dispose())


def _titles(items):
    return [i["title"] for i in items]


def test_root_order_is_the_opml_document_order(fav_db):
    """Perl's root order (live) — NOT folders-first/alphabetical."""
    items = asyncio.run(FavoritesManager().list_items())
    assert _titles(items) == ROOT_ORDER
    # the folders are not grouped in front: 'Dub' (a folder) follows 'Lokal'
    # in document order, and the streams are last only because the OPML has
    # them last.
    assert _titles(items)[:6] == ["Chill", "Nachrichten", "Lokal", "Dub",
                                  "Trance", "Rock"]


def test_sub_order_is_the_opml_document_order(fav_db):
    """Live Perl ``item_id:<sid>.0`` drill (the Chill folder)."""
    async def run():
        mgr = FavoritesManager()
        root = await mgr.list_items()
        chill = next(i for i in root if i["title"] == "Chill")
        return await mgr.list_items(chill["id"])

    assert _titles(asyncio.run(run())) == CHILL_ORDER


def test_list_tree_uses_the_same_order(fav_db):
    """`list_tree` must not disagree with `list_items` inside one process."""
    tree = asyncio.run(FavoritesManager().list_tree())
    assert _titles(tree) == ROOT_ORDER
    chill = next(i for i in tree if i["title"] == "Chill")
    assert [c["title"] for c in chill["children"]] == CHILL_ORDER


def test_a_real_position_column_wins_over_id(tmp_path, monkeypatch):
    """``position`` is Perl's document order, so it outranks the id fallback.

    (For rows whose position was filled — e.g. by the OPML merge — the
    stored order is the OPML order even when the ids disagree.)
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'p.db'}")
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
        finally:
            await session.close()

    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", _session_ctx)
    monkeypatch.setattr("lyrion.music.favorites._opml_path", lambda: None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)

    async def _seed():
        async with _session_ctx() as session:
            session.add_all([
                Favorite(title="Zzz", url=None, parent_id=None, position=0),
                Favorite(title="Aaa", url=None, parent_id=None, position=1),
            ])
            await session.commit()

    asyncio.run(_seed())
    items = asyncio.run(FavoritesManager().list_items())
    assert _titles(items) == ["Zzz", "Aaa"], (
        "position (OPML order) must win — not the title, not the id")
    asyncio.run(engine.dispose())
