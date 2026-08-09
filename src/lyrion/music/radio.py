"""
Internet radio: station management + public directory search.

Search uses Radio Browser (https://www.radio-browser.info) — a large,
open, community-maintained directory with a public REST API that requires
no token or API key. Station data is persisted in the `remote_media`
SQLAlchemy table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RadioStation:
    """A single radio station (from directory or user-added)."""

    name: str
    url: str
    id: Optional[int] = None          # DB id (persisted stations)
    genre: str = ""
    country: str = ""
    language: str = ""
    artwork_url: str = ""
    bitrate: int = 0
    codec: str = ""
    homepage: str = ""
    source: str = "manual"            # "manual" | "radio-browser"

    def to_cli_lines(self) -> list[str]:
        """Format for CLI output (one station, multi-line)."""
        lines = [
            f"radio id: {self.id if self.id is not None else '-'}",
            f"  name: {self.name}",
            f"  url: {self.url}",
        ]
        if self.genre:
            lines.append(f"  genre: {self.genre}")
        if self.country:
            lines.append(f"  country: {self.country}")
        if self.bitrate:
            lines.append(f"  bitrate: {self.bitrate}")
        if self.codec:
            lines.append(f"  codec: {self.codec}")
        return lines


# ---------------------------------------------------------------------------
# Public directory client (Radio Browser — no token required)
# ---------------------------------------------------------------------------

# Leading junk stripped from station names (from Kalrkloss/tray_radio catalog.py)
_LEADING_JUNK = " \t+-#*=._~|>"


def _clean_name(raw: str) -> str:
    """Strip leading junk characters from a station name."""
    return raw.lstrip(_LEADING_JUNK).strip()


def _is_pls_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith((".pls", ".m3u", ".m3u8", ".xspf"))


class RadioBrowserClient:
    """Client for the public Radio Browser directory API.

    Endpoints (all JSON, no auth, no token):
      GET /json/servers                          — discover healthy mirrors
      GET /json/stations/search?name=&tag=&country=&limit=&offset=
      GET /json/stations/bycountry/<CC>
      GET /json/stations/topvote/<n>
      GET /json/url/<uuid>                       — click tracking

    Mirrors are discovered via /json/servers (random pick, like tray_radio),
    with a hardcoded fallback list. Station names are cleaned and .pls/.m3u
    stream URLs are resolved to the real stream endpoint where possible.
    """

    # Fallback mirrors if discovery fails (from tray_radio catalog.py)
    FALLBACK_BASE_URLS: list[str] = [
        "https://de1.api.radio-browser.info",
        "https://de2.api.radio-browser.info",
        "https://fr1.api.radio-browser.info",
        "https://at1.api.radio-browser.info",
        "https://nl1.api.radio-browser.info",
    ]
    TIMEOUT = 15.0

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._base_url = base_url
        self._last_mirror: Optional[str] = None

    # -- public API ---------------------------------------------------------

    async def search(
        self,
        name: str = "",
        tag: str = "",
        country: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[RadioStation]:
        """Search stations by name and/or tag and/or country."""
        params: dict[str, str] = {
            "limit": str(limit),
            "offset": str(offset),
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
        }
        if name:
            params["name"] = name
        if tag:
            params["tag"] = tag
        if country:
            params["country"] = country
        data = await self._get("/json/stations/search", params)
        return [await self._parse(d) for d in data]

    async def by_country(self, country_code: str, limit: int = 50) -> list[RadioStation]:
        """List stations for an ISO-3166 country code (e.g. 'DE', 'AT')."""
        data = await self._get(f"/json/stations/bycountry/{country_code.upper()}", {
            "limit": str(limit),
            "hidebroken": "true",
        })
        return [await self._parse(d) for d in data]

    async def top(self, limit: int = 50) -> list[RadioStation]:
        """Most voted stations."""
        data = await self._get(f"/json/stations/topvote/{limit}", {
            "hidebroken": "true",
        })
        return [await self._parse(d) for d in data]

    async def click_station(self, uuid: str) -> None:
        """Report a click so Radio Browser can rank the station (best effort)."""
        try:
            await self._get(f"/json/url/{uuid}", {})
        except Exception:  # noqa: BLE001
            pass

    # -- internals ----------------------------------------------------------

    @staticmethod
    async def _resolve_stream_url(url: str) -> str:
        """Resolve .pls/.m3u/.xspf URLs to the real stream endpoint."""
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Lyrion-Music-Server/9.2.0"})
                text = resp.text
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                low = ln.lower()
                if low.startswith("file1="):
                    return ln.split("=", 1)[1].strip()
                if low.startswith(("http://", "https://")) and not low.endswith((".pls", ".m3u", ".xspf")):
                    return ln
            if lines and lines[0].lower().startswith(("http://", "https://")):
                return lines[0]
        except Exception:  # noqa: BLE001
            pass
        return url

    async def _parse(self, d: dict) -> RadioStation:
        """Map a Radio Browser station dict to RadioStation (async: resolves .pls)."""
        url = d.get("url", "") or ""
        url_resolved = d.get("url_resolved", "") or ""
        if not url_resolved and _is_pls_url(url):
            url_resolved = await self._resolve_stream_url(url)
        return RadioStation(
            name=_clean_name(d.get("name") or d.get("stationuuid") or "Unknown"),
            url=url_resolved or url,
            genre=(d.get("tags") or "")[:255] or "",
            country=(d.get("country") or "")[:255] or "",
            language=(d.get("language") or "")[:255] or "",
            artwork_url=d.get("favicon", "") or "",
            bitrate=int(d.get("bitrate") or 0),
            codec=d.get("codec", "") or "",
            homepage=d.get("homepage", "") or "",
            source="radio-browser",
        )

    async def _get(self, path: str, params: dict) -> list[dict]:
        """GET with mirror discovery + failover."""
        async with httpx.AsyncClient(timeout=self.TIMEOUT, follow_redirects=True) as client:
            if not self._base_url and not self._last_mirror:
                discovered = await self._discover_server(client)
                if discovered:
                    self._last_mirror = discovered
                else:
                    logger.warning("Radio Browser server discovery failed, using fallbacks")

            urls: list[str]
            if self._base_url:
                urls = [self._base_url]
            elif self._last_mirror:
                urls = [self._last_mirror] + [
                    u for u in self.FALLBACK_BASE_URLS if u != self._last_mirror
                ]
            else:
                urls = list(self.FALLBACK_BASE_URLS)

            last_err: Optional[Exception] = None
            for base in urls:
                try:
                    resp = await client.get(
                        base + path,
                        params=params,
                        headers={"User-Agent": "Lyrion-Music-Server/9.2.0"},
                    )
                    resp.raise_for_status()
                    self._last_mirror = base
                    return resp.json()
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.warning("Radio Browser mirror %s failed: %s", base, e)
        raise RuntimeError(f"All Radio Browser mirrors failed: {last_err}")

    @staticmethod
    async def _discover_server(client: httpx.AsyncClient) -> Optional[str]:
        """Pick a random healthy mirror from /json/servers (like tray_radio)."""
        import random

        try:
            resp = await client.get(
                "https://api.radio-browser.info/json/servers", timeout=5
            )
            if resp.is_success:
                servers = resp.json()
                if servers:
                    random.shuffle(servers)
                    return f"https://{servers[0]}"
        except Exception:  # noqa: BLE001
            pass
        # Fallback: probe the known mirrors
        for fb in RadioBrowserClient.FALLBACK_BASE_URLS:
            try:
                resp = await client.get(f"{fb}/json/servers", timeout=5)
                if resp.is_success:
                    return fb
            except Exception:  # noqa: BLE001
                continue
        return None


# ---------------------------------------------------------------------------
# Manager (persistence + playback commands)
# ---------------------------------------------------------------------------


class RadioManager:
    """Singleton managing radio stations, persisted in `remote_media`.

    Duplicate URLs are rejected on add (case-insensitive URL match).
    """

    _instance: Optional[RadioManager] = None

    def __new__(cls) -> RadioManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.directory = RadioBrowserClient()
        logger.info("RadioManager initialized")

    # -- persistence --------------------------------------------------------

    async def add_station(
        self,
        name: str,
        url: str,
        *,
        genre: str = "",
        country: str = "",
        language: str = "",
        artwork_url: str = "",
        bitrate: int = 0,
        codec: str = "",
        homepage: str = "",
    ) -> RadioStation:
        """Add a station (returns existing one if the URL is already known)."""
        from sqlalchemy import select

        from lyrion.database.schema import RemoteMedia
        from lyrion.database.sqlite_helper import db_session

        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"Invalid stream URL: {url}")

        async with db_session() as session:
            existing = (
                await session.execute(
                    select(RemoteMedia).where(RemoteMedia.url == url)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._to_station(existing)

            row = RemoteMedia(
                url=url,
                name=name.strip() or url,
                genre=genre or None,
                artwork_url=artwork_url or None,
                bitrate=bitrate or None,
                content_type="audio/mpeg",
                stream_format=codec or None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            station = self._to_station(row)
            logger.info("Added radio station: %s (%s)", station.name, station.url)
            return station

    async def remove_station(self, station_id: int) -> bool:
        """Remove a station by id. Returns True if something was deleted."""
        from sqlalchemy import delete, select

        from lyrion.database.schema import RemoteMedia
        from lyrion.database.sqlite_helper import db_session

        async with db_session() as session:
            row = (
                await session.execute(
                    select(RemoteMedia).where(RemoteMedia.id == station_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.execute(
                delete(RemoteMedia).where(RemoteMedia.id == station_id)
            )
            await session.commit()
            logger.info("Removed radio station id=%d (%s)", station_id, row.name)
            return True

    async def list_stations(self) -> list[RadioStation]:
        """All persisted stations, ordered by name."""
        from sqlalchemy import select

        from lyrion.database.schema import RemoteMedia
        from lyrion.database.sqlite_helper import db_session

        async with db_session() as session:
            rows = (
                await session.execute(
                    select(RemoteMedia).order_by(RemoteMedia.name)
                )
            ).scalars().all()
            return [self._to_station(r) for r in rows]

    async def get_station(self, station_id: int) -> Optional[RadioStation]:
        from sqlalchemy import select

        from lyrion.database.schema import RemoteMedia
        from lyrion.database.sqlite_helper import db_session

        async with db_session() as session:
            row = (
                await session.execute(
                    select(RemoteMedia).where(RemoteMedia.id == station_id)
                )
            ).scalar_one_or_none()
            return self._to_station(row) if row else None

    # -- playback -----------------------------------------------------------

    async def play_station(
        self,
        player_id: str,
        station_id: Optional[int] = None,
        url: Optional[str] = None,
    ) -> Optional[RadioStation]:
        """Point a player at a station (sends a real strm frame so
        Squeezelite connects to the station URL and plays audio)."""
        station: Optional[RadioStation] = None
        if station_id is not None:
            station = await self.get_station(station_id)
        elif url:
            station = await self.add_station(url, url)

        if station is None:
            return None

        try:
            from lyrion.player import PlayerManager
            pm = PlayerManager()
            ok = await pm.play_url(player_id, station.url, station.name)
            if not ok:
                logger.warning("play_station: could not start stream for %s", player_id)
                return None
            player = pm.get_player(player_id)
            if player is not None:
                player.display_state = {
                    "artist": "Internet Radio",
                    "title": station.name,
                    "url": station.url,
                    "genre": station.genre,
                }
            logger.info("Player %s -> radio station: %s", player_id, station.name)
        except Exception as exc:
            logger.warning("Could not set player state for %s: %s", player_id, exc)
        return station

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _to_station(row) -> RadioStation:
        return RadioStation(
            id=row.id,
            name=row.name,
            url=row.url,
            genre=row.genre or "",
            artwork_url=row.artwork_url or "",
            bitrate=row.bitrate or 0,
            codec=row.stream_format or "",
            source="manual",
        )


def get_radio_manager() -> RadioManager:
    """Return the RadioManager singleton."""
    return RadioManager()
