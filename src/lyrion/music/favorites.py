"""
Favorites manager — radio streams and folders, mirroring LMS "Favorites".

Structure mirrors the original LMS favorites (OPML outlines): a favorite is
either a folder (url is None) or a stream (url set). Folders nest to any
depth; items are ordered by position within their parent.

The DB table is the source of truth. On first use, an existing
favorites.opml (written by the Perl LMS) is imported once so existing
favorites survive the migration.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from lyrion.database.schema import Favorite

logger = logging.getLogger("lyrion.music.favorites")

_OPML_CANDIDATES = (
    Path("/var/lib/squeezeboxserver/prefs/favorites.opml"),  # Debian LMS
    Path("/etc/squeezeboxserver/prefs/favorites.opml"),      # alt. Debian
)
_opml_lock = asyncio.Lock()
_opml_import_done = False


def _opml_path() -> Path | None:
    """Find favorites.opml: config prefs dir first (LYRION_SERVERDATA-aware),
    then the standard Perl-LMS locations."""
    try:
        from lyrion.config import get_config
        p = get_config().prefs_dir / "favorites.opml"
        if p.exists():
            return p
    except Exception:  # noqa: BLE001
        pass
    for c in _OPML_CANDIDATES:
        if c.exists():
            return c
    return None


class FavoritesManager:
    """CRUD + move/play for the favorites tree."""

    def __init__(self) -> None:
        from lyrion.database.sqlite_helper import db_session
        self._db_session = db_session

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _fav_to_dict(fav: Any, include_children: bool = False) -> dict[str, Any]:
        item = {
            "id": fav.id,
            "title": fav.title,
            "url": fav.url,
            "type": "folder" if fav.url is None else "stream",
            "parent_id": fav.parent_id,
            "position": fav.position,
        }
        if include_children and getattr(fav, "children", None):
            item["children"] = [FavoritesManager._fav_to_dict(c) for c in fav.children]
        return item

    # ── queries ────────────────────────────────────────────────────────

    async def list_items(self, parent_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Return favorites under parent_id (None = root), folders first
        (alphabetical), then streams (alphabetical)."""
        await ensure_opml_imported()
        async with self._db_session() as session:
            stmt = (
                select(Favorite)
                .where(Favorite.parent_id == parent_id)
                # Folders (url IS NULL) sort before streams; both groups
                # alphabetically (case-insensitive).
                .order_by(
                    Favorite.url.is_not(None),
                    func.lower(Favorite.title),
                )
            )
            result = await session.execute(stmt)
            return [self._fav_to_dict(f) for f in result.scalars().all()]

    async def list_tree(self) -> list[dict[str, Any]]:
        """Return the full tree (root items with nested children)."""
        async with self._db_session() as session:
            stmt = (
                select(Favorite)
                .options(selectinload(Favorite.children))
                .where(Favorite.parent_id.is_(None))
                .order_by(Favorite.position, Favorite.title)
            )
            result = await session.execute(stmt)
            return [self._fav_to_dict(f, include_children=True) for f in result.scalars().all()]

    async def resolve_path(self, path: str) -> Optional[int]:
        """Resolve an LMS hierarchical id ('0.3.1') to a DB favorite id.

        '0' is the virtual root; each following number is the index into
        the sorted item list of the parent (same ordering as list_items:
        folders first, then streams, both alphabetical). Returns None if
        the path does not exist.
        """
        try:
            parts = [int(p) for p in str(path).split(".") if p]
        except ValueError:
            return None
        if not parts or parts[0] != 0:
            return None
        parent: Optional[int] = None
        for idx in parts[1:]:
            items = await self.list_items(parent)
            if idx < 0 or idx >= len(items):
                return None
            parent = int(items[idx]["id"])
        return parent

    async def get(self, fav_id: int) -> Optional[dict[str, Any]]:
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            return self._fav_to_dict(fav) if fav else None

    # ── mutations ──────────────────────────────────────────────────────

    async def _next_position(
        self, session: Any, parent_id: Optional[int]
    ) -> int:
        from sqlalchemy import func

        stmt = select(func.coalesce(func.max(Favorite.position), -1)).where(
            Favorite.parent_id == parent_id
        )
        result = await session.execute(stmt)
        return int(result.scalar() or -1) + 1

    async def add(
        self, title: str, url: Optional[str] = None, parent_id: Optional[int] = None
    ) -> Optional[int]:
        """Add a favorite. url=None creates a folder. Returns new id or None."""
        title = (title or "").strip()
        if not title:
            return None
        async with self._db_session() as session:
            if parent_id is not None:
                parent = await session.get(Favorite, parent_id)
                if parent is None:
                    return None
            fav = Favorite(
                title=title,
                url=url.strip() if url else None,
                parent_id=parent_id,
                position=await self._next_position(session, parent_id),
            )
            session.add(fav)
            await session.commit()
            await session.refresh(fav)
            _notify_favorites_changed()
            return fav.id

    async def delete(self, fav_id: int) -> bool:
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            if fav is None:
                return False
            await session.delete(fav)  # children cascade
            await session.commit()
            _notify_favorites_changed()
            return True

    async def rename(self, fav_id: int, title: str, url: str | None = None) -> bool:
        title = (title or "").strip()
        if not title:
            return False
        values: dict = {"title": title}
        if url is not None:
            values["url"] = url.strip()
        async with self._db_session() as session:
            result = await session.execute(
                update(Favorite).where(Favorite.id == fav_id).values(**values)
            )
            await session.commit()
            if result.rowcount:
                _notify_favorites_changed()
            return bool(result.rowcount)

    async def move(
        self, fav_id: int, parent_id: Optional[int], position: Optional[int] = None
    ) -> bool:
        """Move a favorite to another parent (or root) and optionally set position."""
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            if fav is None:
                return False
            if parent_id is not None:
                new_parent = await session.get(Favorite, parent_id)
                if new_parent is None or new_parent.url is not None:
                    return False  # target must be a folder
                if parent_id == fav_id:
                    return False  # cannot move into itself
            fav.parent_id = parent_id
            if position is None:
                fav.position = await self._next_position(session, parent_id)
            else:
                fav.position = max(0, int(position))
            await session.commit()
            _notify_favorites_changed()
            return True

    async def play(self, player_id: str, fav_id: int) -> bool:
        """Play a favorite (stream) on a player."""
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            if fav is None or fav.url is None:
                return False
            url = fav.url
            title = fav.title
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        return await pm.play_url(player_id, url, title)


# Module-level singleton
_manager: Optional[FavoritesManager] = None


def get_favorites_manager() -> FavoritesManager:
    global _manager
    if _manager is None:
        _manager = FavoritesManager()
    return _manager


def _notify_favorites_changed() -> None:
    """Wake Cometd favorites subscribers ('changed' event).

    Called after favorite mutations; schedules the event push on the
    running event loop. SqueezeCtrl reloads the list on the event.
    """
    try:
        from lyrion.web.cometd import get_manager
        mgr = get_manager()
        if mgr is not None:
            import asyncio as _asyncio
            _asyncio.create_task(mgr.notify_favorites_changed())
    except Exception:  # noqa: BLE001
        pass


async def ensure_opml_imported() -> None:
    """Merge favorites.opml into the DB once per process.

    Adds OPML favorites (written by the Perl LMS) that are not yet in the
    DB (matched by URL for streams, by title+parent for folders), so
    existing DB favorites are kept and the user's OPML favorites survive
    the migration.
    """
    global _opml_import_done
    if _opml_import_done:
        return
    async with _opml_lock:
        if _opml_import_done:
            return
        _opml_import_done = True  # set before the work: add() re-enters
        try:
            opml = _opml_path()
            if opml is None:
                return
            mgr = FavoritesManager()
            async with mgr._db_session() as session:
                rows = (await session.execute(
                    select(Favorite.url, Favorite.title, Favorite.parent_id)
                )).all()
            known_urls = {r[0] for r in rows if r[0]}
            known_folders = {(r[1], r[2]) for r in rows if not r[0]}
            added = 0

            async def _merge(outline: ET.Element, parent_id: Optional[int]) -> None:
                nonlocal added
                attrs = outline.attrib
                name = attrs.get("text") or attrs.get("title") or "???"
                url = (attrs.get("URL") or "").strip() or None
                if url:
                    if url in known_urls:
                        return
                    known_urls.add(url)
                else:
                    key = (name, parent_id)
                    if key in known_folders:
                        return
                    known_folders.add(key)
                new_id = await mgr.add(name, url, parent_id)
                added += 1
                for child in outline.findall("outline"):
                    await _merge(child, new_id)

            tree = ET.parse(opml)
            body = tree.getroot().find("body")
            if body is None:
                return
            for outline in body.findall("outline"):
                await _merge(outline, None)
            if added:
                logger.info("Merged %d favorite(s) from %s into DB", added, opml)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OPML favorites import failed: %s", exc)
