"""
Favorites manager — radio streams and folders, mirroring LMS "Favorites".

Structure mirrors the original LMS favorites (OPML outlines): a favorite is
either a folder (url is None) or a stream (url set). Folders nest to any
depth; items are ordered by position within their parent.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from lyrion.database.schema import Favorite

logger = logging.getLogger("lyrion.music.favorites")


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
        """Return favorites under parent_id (None = root), ordered by position."""
        async with self._db_session() as session:
            stmt = (
                select(Favorite)
                .where(Favorite.parent_id == parent_id)
                .order_by(Favorite.position, Favorite.title)
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
            return fav.id

    async def delete(self, fav_id: int) -> bool:
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            if fav is None:
                return False
            await session.delete(fav)  # children cascade
            await session.commit()
            return True

    async def rename(self, fav_id: int, title: str) -> bool:
        title = (title or "").strip()
        if not title:
            return False
        async with self._db_session() as session:
            result = await session.execute(
                update(Favorite).where(Favorite.id == fav_id).values(title=title)
            )
            await session.commit()
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
