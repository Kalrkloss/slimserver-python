"""
Request dispatcher for Lyrion Music Server.

Unified request handling pipeline from all sources:
CLI, HTTP (JSON/SqueezeOS), and Slimproto.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
)

if TYPE_CHECKING:
    from lyrion.control.cli import CLIContext
    from lyrion.control.queries import QueryHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request source
# ---------------------------------------------------------------------------


class RequestSource(str, Enum):
    """Origin of a request."""

    CLI = "cli"
    HTTP = "http"
    SLIMPROTO = "slimproto"
    JIVE = "jive"
    INTERNAL = "internal"


# ---------------------------------------------------------------------------
# Request object
# ---------------------------------------------------------------------------


@dataclass
class Request:
    """
    Represents a single request processed by the dispatcher.

    Attributes:
        source: Where the request originated.
        command: Command name (e.g. "play", "playlist add").
        args: Positional arguments.
        player_id: Target player MAC, if any.
        client_id: Originating client session.
        priority: Request priority (lower = higher priority).
        created_at: Unix timestamp when created.
        cb: Optional callback when request completes.
        cb_data: Opaque data passed to callback.
    """

    source: RequestSource = RequestSource.INTERNAL
    command: str = ""
    args: tuple[str, ...] = field(default_factory=tuple)
    player_id: Optional[str] = None
    client_id: str = ""
    priority: int = 50
    created_at: float = field(default_factory=time.time)
    cb: Optional[Callable[["Request", list[str]], None]] = None
    cb_data: Any = None

    # For asyncio.Lock coordination
    _future: Optional[asyncio.Future[list[str]]] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Permission checker protocol
# ---------------------------------------------------------------------------


class PermissionChecker(Protocol):
    """Protocol for permission checking callbacks."""

    def __call__(
        self,
        request: Request,
        player_id: Optional[str],
    ) -> bool:
        ...


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------


class RequestDispatcher:
    """
    Central dispatcher for all Lyrion server requests.

    Responsibilities:
    - Route commands to the appropriate handler (player, library, prefs)
    - Rate-limiting and request queuing
    - Permission checking
    - Async execution with callbacks

    Usage::

        dispatcher = RequestDispatcher()
        await dispatcher.start()
        # ...
        result = await dispatcher.submit(
            Request(source=RequestSource.CLI, command="play", args=("123",))
        )
        await dispatcher.stop()
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        rate_limit: float = 0.0,
    ) -> None:
        """
        Args:
            max_concurrent: Maximum simultaneous player requests.
            rate_limit: Minimum seconds between requests per player.
        """
        self._max_concurrent = max_concurrent
        self._rate_limit = rate_limit
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Registered handlers
        self._command_handlers: dict[str, Callable[..., Awaitable[list[str]]]] = {}
        self._query_handlers: dict[str, Callable[..., Awaitable[list[str]]]] = {}

        # Request queues
        self._high_priority_queue: asyncio.PriorityQueue[
            tuple[int, float, Request]
        ] = asyncio.PriorityQueue()
        self._normal_priority_queue: asyncio.PriorityQueue[
            tuple[int, float, Request]
        ] = asyncio.PriorityQueue()

        # Concurrency limiters per player
        self._player_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(max_concurrent)
        )
        # Rate limiting per player
        self._player_last_run: dict[str, float] = {}
        self._rate_limit_lock = asyncio.Lock()

        # Global concurrency limiter
        self._global_sem = asyncio.Semaphore(max_concurrent * 4)

        # Permission checker
        self._permission_checker: Optional[PermissionChecker] = None

        # Default player
        self._default_player: Optional[str] = None

        # Worker tasks
        self._workers: list[asyncio.Task[None]] = []

        # Event subscriptions: player -> list of callbacks
        self._subscriptions: dict[str, List[Callable[[dict], None]]] = defaultdict(
            list
        )

        # DB layer reference (set after startup)
        self._db = None

        # Player registry (mac -> info dict)
        self._players: dict[str, dict] = {}

        logger.info(
            "RequestDispatcher init: max_concurrent=%d rate_limit=%.2f",
            max_concurrent,
            rate_limit,
        )

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatcher and its worker goroutines."""
        if self._running:
            return
        self._running = True
        self._shutdown_event.clear()

        # Start worker coroutines
        num_workers = 4
        for i in range(num_workers):
            t = asyncio.create_task(self._worker_loop(i))
            self._workers.append(t)
        logger.info("RequestDispatcher started with %d workers", num_workers)

    async def stop(self) -> None:
        """Gracefully stop the dispatcher."""
        if not self._running:
            return
        self._running = False
        self._shutdown_event.set()
        # Drain queues
        while not self._high_priority_queue.empty():
            try:
                self._high_priority_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._normal_priority_queue.empty():
            try:
                self._normal_priority_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("RequestDispatcher stopped")

    # -----------------------------------------------------------------------
    # Configuration
    # -----------------------------------------------------------------------

    def set_permission_checker(
        self, checker: PermissionChecker
    ) -> None:
        """Set a permission checker callback."""
        self._permission_checker = checker

    def set_default_player(self, player_id: str) -> None:
        """Set the server's default player."""
        self._default_player = player_id

    def get_default_player(self) -> Optional[str]:
        """Return the server's default player ID."""
        return self._default_player

    def set_db(self, db: Any) -> None:
        """Set the database layer reference."""
        self._db = db

    # -----------------------------------------------------------------------
    # Player registry
    # -----------------------------------------------------------------------

    def register_player(self, player_id: str, info: dict) -> None:
        """Register a connected player."""
        self._players[player_id] = info
        logger.debug("Player registered: %s", player_id)

    def unregister_player(self, player_id: str) -> None:
        """Remove a player."""
        self._players.pop(player_id, None)
        logger.debug("Player unregistered: %s", player_id)

    async def list_players(self) -> List[dict]:
        """Return list of all known players."""
        # Source of truth is the PlayerManager singleton (filled by the
        # slimproto networking layer). Fall back to the local registry.
        try:
            from lyrion.player import PlayerManager
            pm_players = PlayerManager().get_all_players()
            if pm_players:
                return [p.to_dict() if hasattr(p, "to_dict") else p for p in pm_players]
        except Exception:
            pass
        return list(self._players.values())

    async def count_players(self) -> int:
        """Return number of connected players."""
        try:
            from lyrion.player import PlayerManager
            return PlayerManager().get_connected_count()
        except Exception:
            return len(self._players)

    # -----------------------------------------------------------------------
    # Command registration
    # -----------------------------------------------------------------------

    def register_command(
        self,
        name: str,
        handler: Callable[..., Awaitable[list[str]]],
    ) -> None:
        """Register a command handler."""
        self._command_handlers[name] = handler
        logger.debug("Registered command handler: %s", name)

    def register_query(
        self,
        name: str,
        handler: Callable[..., Awaitable[list[str]]],
    ) -> None:
        """Register a query handler."""
        self._query_handlers[name] = handler
        logger.debug("Registered query handler: %s", name)

    # -----------------------------------------------------------------------
    # Submit requests
    # -----------------------------------------------------------------------

    async def submit(self, request: Request) -> list[str]:
        """
        Submit a request for processing and await its result.

        Returns:
            Response lines.
        """
        if not self._running:
            return ["server shutting down"]

        # Permission check
        if self._permission_checker:
            if not self._permission_checker(request, request.player_id):
                return ["permission denied"]

        # Set up future for result delivery
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request._future = future

        # Enqueue
        if request.priority < 50:
            await self._high_priority_queue.put(
                (request.priority, request.created_at, request)
            )
        else:
            await self._normal_priority_queue.put(
                (request.priority, request.created_at, request)
            )

        # Wait for result
        try:
            return await future
        except asyncio.CancelledError:
            return ["request cancelled"]

    def submit_background(
        self,
        request: Request,
    ) -> None:
        """Submit a request without waiting for result."""
        if self._running:
            asyncio.create_task(self.submit(request))

    # -----------------------------------------------------------------------
    # Convenience submit helpers
    # -----------------------------------------------------------------------

    async def submit_cli(
        self,
        command: str,
        args: Optional[tuple[str, ...]] = None,
        client_id: str = "",
    ) -> list[str]:
        """Submit a CLI command as an internal request."""
        request = Request(
            source=RequestSource.CLI,
            command=command,
            args=args or (),
            client_id=client_id,
        )
        return await self.submit(request)

    async def player_command(
        self,
        player_id: str,
        command: str,
        args: Optional[list[str]] = None,
    ) -> list[str]:
        """Send a player-level command."""
        request = Request(
            source=RequestSource.INTERNAL,
            command=command,
            args=tuple(args) if args else (),
            player_id=player_id,
            priority=30,  # player commands are higher priority
        )
        return await self.submit(request)

    # -----------------------------------------------------------------------
    # Query helpers (delegate to query handler)
    # -----------------------------------------------------------------------

    async def query_artists(
        self,
        offset: int,
        limit: int,
        filters: dict,
    ) -> list[str]:
        """Query artists."""
        handler = self._query_handlers.get("artists")
        if handler:
            return await handler(offset, limit, filters)
        return ["artists 0", ""]

    async def query_albums(
        self,
        offset: int,
        limit: int,
        filters: dict,
        tag_str: str,
    ) -> list[str]:
        """Query albums."""
        handler = self._query_handlers.get("albums")
        if handler:
            return await handler(offset, limit, filters, tag_str)
        return ["albums 0", ""]

    async def query_tracks(
        self,
        offset: int,
        limit: int,
        filters: dict,
        tag_str: str,
    ) -> list[str]:
        """Query tracks."""
        handler = self._query_handlers.get("tracks")
        if handler:
            return await handler(offset, limit, filters, tag_str)
        return ["tracks 0", ""]

    async def query_genres(
        self,
        offset: int,
        limit: int,
        filters: dict,
    ) -> list[str]:
        """Query genres."""
        handler = self._query_handlers.get("genres")
        if handler:
            return await handler(offset, limit, filters)
        return ["genres 0", ""]

    async def query_years(
        self,
        offset: int,
        limit: int,
    ) -> list[str]:
        """Query years."""
        handler = self._query_handlers.get("years")
        if handler:
            return await handler(offset, limit)
        return ["years 0", ""]

    async def query_playlists(
        self,
        offset: int,
        limit: int,
        filters: dict,
    ) -> list[str]:
        """Query playlists."""
        handler = self._query_handlers.get("playlists")
        if handler:
            return await handler(offset, limit, filters)
        return ["playlists 0", ""]

    async def query_playlist_tracks(
        self,
        playlist_id: str,
        offset: int,
        limit: int,
        tag_str: str,
    ) -> list[str]:
        """Query tracks in a playlist."""
        handler = self._query_handlers.get("playlisttracks")
        if handler:
            return await handler(playlist_id, offset, limit, tag_str)
        return ["playlisttracks 0", ""]

    async def query_new_music(
        self,
        offset: int,
        limit: int,
    ) -> list[str]:
        """Query new music."""
        handler = self._query_handlers.get("newmusic")
        if handler:
            return await handler(offset, limit)
        return ["newmusic 0", ""]

    # -----------------------------------------------------------------------
    # Library / admin helpers
    # -----------------------------------------------------------------------

    async def search_library(
        self,
        search_type: str,
        query: str,
        offset: int,
        limit: int,
        tags: str = "",
    ) -> list[str]:
        """Search the media library."""
        handler = self._query_handlers.get(f"search:{search_type}")
        if handler:
            return await handler(query, offset, limit, tags)
        return []

    async def library_stats(
        self,
        args: list[str],
    ) -> list[str]:
        """Return library statistics."""
        if self._db:
            try:
                stats = await self._db.get_stats()
                total_songs = stats.get("songs", 0)
                total_artists = stats.get("artists", 0)
                total_albums = stats.get("albums", 0)
                total_genres = stats.get("genres", 0)
                return [
                    f"info total duration: {stats.get('total_duration', 0)}",
                    f"info genres: {total_genres}",
                    f"info songs: {total_songs}",
                    f"info albums: {total_albums}",
                    f"info artists: {total_artists}",
                    f"info ratings: 0",
                    "",
                ]
            except Exception as exc:
                logger.warning("library_stats error: %s", exc)
        return [
            "info total duration: 0",
            "info genres: 0",
            "info songs: 0",
            "info albums: 0",
            "info artists: 0",
            "",
        ]

    async def trigger_rescan(self, mode: str = "normal") -> None:
        """Trigger a media library rescan."""
        import asyncio as _asyncio
        _asyncio.create_task(self._run_rescan(mode))
        logger.info("Rescan triggered: mode=%s", mode)

    async def _run_rescan(self, mode: str) -> None:
        """Background task: run the media importer/scanner."""
        try:
            from lyrion.media.importer import MusicImporter, ImportConfig
            config = ImportConfig()
            importer = MusicImporter(config=config)
            stats = await importer.import_music()
            logger.info(
                "Rescan complete: imported=%d updated=%d skipped=%d errors=%d",
                stats.imported_files, stats.updated_files,
                stats.skipped_files, stats.error_files,
            )
        except Exception as exc:
            logger.error("Rescan failed: %s", exc, exc_info=True)

    async def wipe_cache(self) -> None:
        """Clear server caches."""
        logger.info("Cache wiped")
        # TODO: clear artwork cache, etc.

    async def rescan_progress(self) -> list[str]:
        """Return current rescan progress."""
        # TODO: integrate with scanner
        return ["rescanprogress: 0 done 0 0"]

    async def get_set_preference(
        self,
        key: str,
        value: Optional[str] = None,
    ) -> list[str]:
        """Get or set a server preference."""
        # TODO: integrate with prefs system
        return [f"pref {key}: {value or ''}"]

    # -----------------------------------------------------------------------
    # Subscription system
    # -----------------------------------------------------------------------

    def subscribe_player(
        self,
        player_id: str,
        callback: Callable[[dict], None],
    ) -> None:
        """Subscribe to status events for a player."""
        self._subscriptions[player_id].append(callback)

    def unsubscribe_player(
        self,
        player_id: str,
        callback: Callable[[dict], None],
    ) -> None:
        """Unsubscribe from player events."""
        if player_id in self._subscriptions:
            try:
                self._subscriptions[player_id].remove(callback)
            except ValueError:
                pass

    def broadcast_player_event(
        self,
        player_id: str,
        event: dict,
    ) -> None:
        """Broadcast an event to all subscribers of a player."""
        for callback in self._subscriptions.get(player_id, []):
            try:
                callback(event)
            except Exception as exc:
                logger.warning(
                    "Player event callback raised: %s", exc
                )

    # -----------------------------------------------------------------------
    # Worker loop
    # -----------------------------------------------------------------------

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker coroutine that processes requests from queues."""
        logger.debug("Worker %d started", worker_id)
        while self._running:
            request: Optional[Request] = None
            try:
                # Try high-priority first, then normal
                try:
                    _, _, request = self._high_priority_queue.get_nowait()
                except asyncio.QueueEmpty:
                    try:
                        _, _, request = await asyncio.wait_for(
                            self._normal_priority_queue.get(),
                            timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        continue

                if request is None:
                    continue

                # Rate limiting
                if self._rate_limit > 0 and request.player_id:
                    async with self._rate_limit_lock:
                        last = self._player_last_run.get(request.player_id, 0)
                        elapsed = time.time() - last
                        if elapsed < self._rate_limit:
                            await asyncio.sleep(self._rate_limit - elapsed)
                        self._player_last_run[request.player_id] = time.time()

                # Execute request
                result = await self._execute_request(request)

                # Deliver result
                if request._future and not request._future.done():
                    request._future.set_result(result)

                # Fire callback
                if request.cb:
                    try:
                        request.cb(request, result)
                    except Exception as exc:
                        logger.warning("Request callback raised: %s", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Worker %d exception: %s", worker_id, exc)
                if request and request._future and not request._future.done():
                    request._future.set_result([f"internal error: {exc}"])

        logger.debug("Worker %d stopped", worker_id)

    # -----------------------------------------------------------------------
    # Request execution
    # -----------------------------------------------------------------------

    async def _execute_request(self, request: Request) -> list[str]:
        """Execute a single request and return its result lines."""
        command = request.command

        # Try exact match
        if command in self._command_handlers:
            handler = self._command_handlers[command]
            try:
                return await handler(request, *request.args)
            except TypeError:
                # Try positional args form
                pass

        # Try compound command parts
        parts = command.split()
        for i in range(len(parts), 0, -1):
            prefix = " ".join(parts[:i])
            if prefix in self._command_handlers:
                handler = self._command_handlers[prefix]
                sub_args = list(request.args)
                return await handler(request, *sub_args)

        # Built-in commands
        return await self._builtin_command(request)

    async def _builtin_command(
        self,
        request: Request,
    ) -> list[str]:
        """Handle built-in dispatcher commands."""
        cmd = request.command.lower()
        args = request.args

        if cmd in ("play", "pause", "stop", "prev", "next", "power"):
            # Player transport commands — handled via slimproto
            return [f"{cmd} sent to player {request.player_id}"]

        elif cmd == "volume":
            return [f"volume: 50"]  # TODO: real volume

        elif cmd.startswith("playlist "):
            sub = cmd[9:]
            return await self._handle_playlist(request.player_id, sub, args)

        elif cmd == "status":
            return await self._player_status(request.player_id, args)

        else:
            return [f"unknown command: {cmd}"]

    async def _player_status(
        self,
        player_id: Optional[str],
        args: tuple[str, ...],
    ) -> list[str]:
        """Return status for a player."""
        if not player_id:
            return ["player: no player selected"]

        # TODO: real status from player state
        return [
            f"playerid: {player_id}",
            "mode: stop",
            "power: 1",
            "volume: 50",
            "rate: 1",
            "time: 0",
            "duration: 0",
            "can_seek: 0",
            "",
        ]

    async def _handle_playlist(
        self,
        player_id: Optional[str],
        sub: str,
        args: tuple[str, ...],
    ) -> list[str]:
        """Handle playlist subcommands."""
        # TODO: delegate to playlist manager
        return [f"playlist {sub}: ok"]


__all__ = [
    "RequestDispatcher",
    "Request",
    "RequestSource",
]
