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
import re
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

#: Perl's browse-session handle: ``getSID`` accepts any token *starting* with
#: 8 hex digits (``Slim/Control/XMLBrowser.pm:1739-1741``), created by
#: ``createUUID`` as ``substr(sha1_hex(time . $$ . hostname), 0, 8)``
#: (``Slim/Utils/Misc.pm:1557-1560``).
_SESSION_SID_RE = re.compile(r"^[a-f0-9]{8}$", re.IGNORECASE)


def _is_session_root(token: object) -> bool:
    """``XMLBrowser::getSID`` — the browse-session prefix of an item id."""
    return bool(_SESSION_SID_RE.match(str(token or "")))


#: ``Slim/Plugin/Favorites/OpmlFavorites.pm:87`` — the fallback every
#: icon-less favourite ends on.
FAVORITES_ICON = "html/images/favorites.png"

#: ``Slim/Player/Protocols/HTTP.pm:1138-1148`` ``getIcon`` — the icon the
#: HTTP protocol handler reports for a stream URL (``ProtocolHandlers::
#: iconForURL``, ``Slim/Player/ProtocolHandlers.pm:138-153``).
STREAM_ICON = "html/images/radio.png"


def favorite_icon(url: object, player: object = None) -> str:
    """``$favs->icon($url)`` — the icon of a favourites entry.

    ``Slim/Plugin/Favorites/OpmlFavorites.pm:83-88``::

        sub icon {
            my $class = shift;
            my $url = shift;

            return Slim::Player::ProtocolHandlers->iconForURL($url)
                || 'html/images/favorites.png';
        }

    ``iconForURL`` asks the URL's protocol handler (``ProtocolHandlers.pm:
    138-153``); an http(s) stream answers ``HTTP.pm:1138-1148`` →
    ``'html/images/radio.png'``.  Everything else (no URL, a file URL, a
    folder) has no handler and gets the favourites icon.

    The handler's *own* logo wins over the generic placeholder — that is what
    ``ProtocolHandlers.pm:138-153`` answers first (``getMetadataFor``'s
    ``cover``, then ``$handler->getIcon($url)``; for the port's radio feeds
    the handler logo is the station image the feed row registered under the
    URL, the ``remote_image_$url`` analogue, ``XMLBrowser.pm:1043-1049``).
    Without a registered logo the placeholder of the handler's class stands:
    ``HTTP.pm:1148`` for a stream, ``OpmlFavorites.pm:87`` otherwise.
    """
    registered = ""
    try:
        from lyrion.web.api import _registered_stream_image
        registered = _registered_stream_image(player, str(url or ""))
    except Exception:  # noqa: BLE001 — ohne api bleibt der Platzhalter
        registered = ""
    if registered:
        return registered
    if str(url or "").lower().startswith(("http://", "https://")):
        return STREAM_ICON
    return FAVORITES_ICON



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
            # Perl never leaves ``icon`` undefined on a favourite: the OPML
            # value wins, a missing one is derived (``OpmlFavorites.pm:133-136``).
            "icon": fav.icon or favorite_icon(fav.url),
            "type": "folder" if fav.url is None else "stream",
            "parent_id": fav.parent_id,
            "position": fav.position,
        }
        if include_children and getattr(fav, "children", None):
            item["children"] = [FavoritesManager._fav_to_dict(c) for c in fav.children]
        return item

    # ── queries ────────────────────────────────────────────────────────

    async def list_items(self, parent_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Return favorites under parent_id (None = root), in the OPML order.

        Perl keeps the document order of the ``favorites.opml`` outlines: the
        Favorites plugin loads the file with ``XML::Simple`` (``SuppressEmpty``
        / ``forcearray => ['outline','body']``, ``Slim/Plugin/Favorites/Opml.pm:74-76``),
        which preserves the outline arrays verbatim, and walks them as they
        came for the Jive menu — Perl never re-sorts favourites by title or by
        "folders first".  Live Perl 9.1.1 (2026-09-14, read-only
        ``favorites items 0 100``): ``Chill, Nachrichten, Lokal, Dub, Trance,
        Rock, <2 streams>`` — exactly the OPML/DB order, while this port
        answered ``Chill, Dub, Lokal, Nachrichten, Rock, Trance, …``
        (folders first, then title).

        ``position`` is that document order; ``id`` breaks the tie for rows
        an older import left at ``position = 0`` (they were inserted in
        document order, so id order *is* the OPML order for them)."""
        await ensure_opml_imported()
        async with self._db_session() as session:
            stmt = (
                select(Favorite)
                .where(Favorite.parent_id == parent_id)
                .order_by(Favorite.position, Favorite.id)
            )
            result = await session.execute(stmt)
            return [self._fav_to_dict(f) for f in result.scalars().all()]

    async def list_tree(self) -> list[dict[str, Any]]:
        """Return the full tree (root items with nested children).

        Same ordering as :meth:`list_items` (Perl's OPML document order); the
        previous ``position, title`` sort disagreed with it inside one process.
        """
        async with self._db_session() as session:
            stmt = (
                select(Favorite)
                .options(selectinload(Favorite.children))
                .where(Favorite.parent_id.is_(None))
                .order_by(Favorite.position, Favorite.id)
            )
            result = await session.execute(stmt)
            return [self._fav_to_dict(f, include_children=True) for f in result.scalars().all()]

    async def resolve_path(self, path: str) -> Optional[int]:
        """Resolve an LMS item id ('<sid>.3.1' / legacy '0.3.1') to a DB id.

        Perl's ``XMLBrowser`` roots every item id in the browse session's
        handle: ``my @crumbIndex = $sid ? ($sid) : ()`` plus one entry per
        drill-down (``Slim/Control/XMLBrowser.pm:341-353``/``:387-394``), so
        an id reads ``<8-hex-sid>.<index>[.<index>…]`` (``:1022``/``:1142``).
        On the way back ``_cliQuery_done`` splits on '.' and shifts the handle
        off when ``getSID`` recognises it (``:331-336``, ``:1739-1741``).
        ``'0'`` is our pre-session root and stays accepted so ids from older
        responses keep resolving.

        Each following number is the index into the sorted item list of its
        parent (same ordering as list_items: folders first, then streams,
        both alphabetical). Returns None if the path does not exist.
        """
        crumbs = [c for c in str(path).split(".") if c]
        # Perl `getSID($index[0])` (XMLBrowser.pm:336/1739-1741): the leading
        # crumb of a session id is the 8-hex browse handle, never an index.
        if crumbs and _is_session_root(crumbs[0]):
            crumbs = crumbs[1:]
        elif crumbs and crumbs[0] == "0":
            crumbs = crumbs[1:]
        if not crumbs:
            return None
        try:
            parts = [int(p) for p in crumbs]
        except ValueError:
            return None
        parent: Optional[int] = None
        for idx in parts:
            items = await self.list_items(parent)
            if idx < 0 or idx >= len(items):
                return None
            # list_items returns the hierarchical string as 'id' (e.g. "0.2")
            # and the real DB id as 'dbid'. Use 'dbid' so hierarchical paths
            # resolve to the actual Favorite row (playing a favorite by
            # item_id otherwise falls back to a stale/default stream).
            item = items[idx]
            real_id = item.get("dbid")
            if real_id is not None:
                parent = int(real_id)
                continue
            try:
                parent = int(item["id"])
            except (ValueError, TypeError, KeyError):
                return None
        return parent

    async def get(self, fav_id: int) -> Optional[dict[str, Any]]:
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            return self._fav_to_dict(fav) if fav else None

    async def find_url(self, url: str) -> Optional[int]:
        """DB id of the first favourite stream with this URL — Perl ``findUrl``.

        ``Slim/Utils/Favorites.pm`` ``findUrl`` walks the OPML outline and
        answers the entry whose ``URL`` matches; ``XMLBrowser`` uses it for the
        add-vs-delete decision (``Slim/Control/XMLBrowser.pm:1886-1893``) and
        ``Plugin.pm``'s ``cliDelete`` falls back to it when no index was sent
        (``Slim/Plugin/Favorites/Plugin.pm:946-952``::

            if (!defined $index || !defined $favs->entry($index)) {
                if ($url) { $favs->deleteUrl($url); }

        ).  Folders (``url`` is NULL) never match.
        """
        url = (url or "").strip()
        if not url:
            return None
        async with self._db_session() as session:
            result = await session.execute(
                select(Favorite.id).where(Favorite.url == url)
                .order_by(Favorite.id).limit(1)
            )
            found = result.scalar()
        return int(found) if found is not None else None

    # ── mutations ──────────────────────────────────────────────────────

    async def _next_position(
        self, session: Any, parent_id: Optional[int]
    ) -> int:
        stmt = select(func.coalesce(func.max(Favorite.position), -1)).where(
            Favorite.parent_id == parent_id
        )
        result = await session.execute(stmt)
        return int(result.scalar() or -1) + 1

    async def add(
        self, title: str, url: Optional[str] = None, parent_id: Optional[int] = None,
        icon: Optional[str] = None,
    ) -> Optional[int]:
        """Add a favorite. url=None creates a folder. Returns new id or None.

        ``icon`` is the entry's ``icon`` attribute; Perl stores
        ``icon || $favs->icon($url)`` (``Slim/Plugin/Favorites/Plugin.pm:855``)
        — the caller's value wins, otherwise it is derived exactly like Perl
        (an http stream → ``html/images/radio.png``, a folder →
        ``html/images/favorites.png``).
        """
        title = (title or "").strip()
        if not title:
            return None
        url_value = url.strip() if url else None
        icon_value = (str(icon or "").strip()) or favorite_icon(url_value)
        async with self._db_session() as session:
            if parent_id is not None:
                parent = await session.get(Favorite, parent_id)
                if parent is None:
                    return None
            fav = Favorite(
                title=title,
                url=url_value,
                icon=icon_value,
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
        """Play a favorite (stream) on a player.

        Perl registers the **row's logo** under the stream URL before the
        stream starts: rendering the feed caches ``remote_image_$url`` from the
        item's ``image``/``cover`` (``Slim/Control/XMLBrowser.pm:1043-1049``)::

            if ( $isPlayable && $item->{url} && $item->{url} =~ /^http/
                 && (my $cover = ($item->{image} || $item->{cover}))
                 && !Slim::Utils::Cache->new->get("remote_image_" . $item->{url}) ) {
                $cache->set("remote_image_" . $item->{url}, $cover, 86400);
            }

        and ``setRemoteMetadata`` caches it again for 30 days
        (``Slim/Music/Info.pm:485-489``).  The logo the status then reports is
        this favourite's ``icon`` — without the registration the receiver
        falls back to the generic radio placeholder.
        """
        async with self._db_session() as session:
            fav = await session.get(Favorite, fav_id)
            if fav is None or fav.url is None:
                return False
            url = fav.url
            title = fav.title
            icon = fav.icon or favorite_icon(fav.url)
        from lyrion.player import PlayerManager
        pm = PlayerManager()
        player = pm.get_player(player_id)
        if player is not None and icon:
            # Same helper the radio feed uses for its station logo
            # (``api._json_radio_feed`` → ``JSONRPCAPI._set_stream_image``).
            from lyrion.web.api import JSONRPCAPI
            JSONRPCAPI._set_stream_title(player, url, title)
            JSONRPCAPI._set_stream_image(player, url, icon)
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

    The outline's ``icon`` attribute travels with the entry — it is what Perl
    answers as the row's image (``Slim/Plugin/Favorites/OpmlFavorites.pm:
    83-88``/:133-136): the stored value wins, only a *missing* one is derived.
    An entry the DB already knows but whose ``icon`` is still empty (rows
    imported before the column existed) is filled from the OPML here, exactly
    like Perl's ``_urlindex`` fills it when it loads the file.
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
                    select(Favorite.id, Favorite.url, Favorite.title,
                           Favorite.parent_id, Favorite.icon)
                )).all()
            # url -> (db id, stored icon); (title, parent) -> same for folders
            known_urls = {r[1]: (r[0], r[4]) for r in rows if r[1]}
            known_folders = {(r[2], r[3]): (r[0], r[4]) for r in rows if not r[1]}
            added = 0
            reiconed = 0

            async def _merge(outline: ET.Element, parent_id: Optional[int]) -> None:
                nonlocal added, reiconed
                attrs = outline.attrib
                name = attrs.get("text") or attrs.get("title") or "???"
                url = (attrs.get("URL") or "").strip() or None
                icon = (attrs.get("icon") or "").strip() or None
                if url:
                    known = known_urls.get(url)
                    if known is not None:
                        await _backfill_icon(mgr, known, icon)
                        return
                    known_urls[url] = (None, icon)
                    new_id = await mgr.add(name, url, parent_id, icon=icon)
                    added += 1
                else:
                    key = (name, parent_id)
                    known = known_folders.get(key)
                    if known is not None:
                        # A known folder still gets its own icon filled *and*
                        # its children walked: the legacy import kept every
                        # row, only the icons are missing — descending is what
                        # reaches the nested entries (the logos live there).
                        await _backfill_icon(mgr, known, icon)
                        new_id = known[0] if known[0] is not None else parent_id
                    else:
                        known_folders[key] = (None, icon)
                        new_id = await mgr.add(name, url, parent_id, icon=icon)
                        added += 1
                for child in outline.findall("outline"):
                    await _merge(child, new_id)

            async def _backfill_icon(
                manager: FavoritesManager, known: tuple, icon: Optional[str]
            ) -> None:
                """Fill a stored-but-empty ``icon`` from the OPML — Perl's
                ``_urlindex`` rule (``OpmlFavorites.pm:134-136``): never
                overwrite an existing value."""
                nonlocal reiconed
                db_id, stored = known
                if db_id is None or stored or not icon:
                    return
                async with manager._db_session() as session:
                    await session.execute(
                        update(Favorite).where(Favorite.id == db_id)
                        .values(icon=icon)
                    )
                    await session.commit()
                reiconed += 1

            tree = ET.parse(opml)
            body = tree.getroot().find("body")
            if body is None:
                return
            for outline in body.findall("outline"):
                await _merge(outline, None)
            if added or reiconed:
                logger.info(
                    "Merged %d favorite(s) from %s into DB (%d icon(s) filled)",
                    added, opml, reiconed,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OPML favorites import failed: %s", exc)
