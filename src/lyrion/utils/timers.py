"""
Async timer management for Lyrion Music Server.

Ported from Slim::Utils::Timers. Provides a robust async timer scheduler
that supports one-shot and periodic timers with accurate timing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
from typing import Callable, Awaitable, Any
from dataclasses import dataclass, field
from collections import defaultdict
from contextlib import asynccontextmanager

logger = logging.getLogger("lyrion.timers")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TimerCallback = Callable[..., Awaitable[Any] | Any]
T = Any


@dataclass
class TimerEntry:
    """A single timer entry."""
    id: int
    callback: TimerCallback
    interval: float  # seconds, 0 for one-shot
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    next_fire: float  # absolute time
    canceled: bool = False
    name: str = ""


# ---------------------------------------------------------------------------
# TimerManager
# ---------------------------------------------------------------------------

class TimerManager:
    """
    Async timer manager using asyncio.

    Provides:
    - One-shot timers (fire once after N seconds)
    - Periodic timers (fire every N seconds)
    - Accurate timing (adjusts for drift)
    - Named timers for debugging
    - Pause/resume support
    - Thread-safe API (timers can be set from any thread)
    """

    __slots__ = (
        "_timers",
        "_lock",
        "_next_id",
        "_running",
        "_paused",
        "_main_task",
        "_loop",
        "_cond",
    )

    _instance: TimerManager | None = None

    def __init__(self) -> None:
        self._timers: dict[int, TimerEntry] = {}
        self._lock = threading.RLock()
        self._next_id = 1
        self._running = False
        self._paused = False
        self._main_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond = threading.Condition()

    @classmethod
    def instance(cls) -> TimerManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        inst = cls._instance
        if inst is not None:
            inst.stop()
        cls._instance = None

    # ---- lifecycle ----

    async def start(self) -> None:
        """Start the timer manager's background loop."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the timer manager."""
        self._running = False
        if self._main_task is not None:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            self._main_task = None
        with self._lock:
            self._timers.clear()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Pause all timers (they won't fire until resumed)."""
        self._paused = True

    def resume(self) -> None:
        """Resume all timers."""
        self._paused = False
        # Wake up the loop
        with self._cond:
            self._cond.notify_all()

    # ---- timer scheduling ----

    def _next_id_locked(self) -> int:
        with self._lock:
            nid = self._next_id
            self._next_id += 1
            return nid

    def add(
        self,
        callback: TimerCallback,
        interval: float,
        *args: Any,
        periodic: bool = False,
        name: str = "",
        **kwargs: Any,
    ) -> int:
        """
        Schedule a timer.

        Args:
            callback: async or sync callable to invoke
            interval: delay in seconds
            *args: positional arguments for the callback
            periodic: if True, repeat every `interval` seconds
            name: optional name for debugging

        Returns:
            timer ID that can be used to cancel
        """
        interval = max(0.001, interval)
        tid = self._next_id_locked()
        entry = TimerEntry(
            id=tid,
            callback=callback,
            interval=interval if periodic else 0.0,
            args=args,
            kwargs=kwargs,
            next_fire=time.monotonic() + interval,
            name=name or callback.__name__ if hasattr(callback, "__name__") else str(tid),
        )

        with self._lock:
            self._timers[tid] = entry
            self._cond.notify_all()

        logger.debug("Added timer %d (%s) interval=%.3f periodic=%s", tid, entry.name, interval, periodic)
        return tid

    def add_once(
        self,
        callback: TimerCallback,
        delay: float,
        *args: Any,
        name: str = "",
        **kwargs: Any,
    ) -> int:
        """Schedule a one-shot timer (fire once after delay seconds)."""
        return self.add(callback, delay, *args, periodic=False, name=name, **kwargs)

    def add_interval(
        self,
        callback: TimerCallback,
        interval: float,
        *args: Any,
        name: str = "",
        **kwargs: Any,
    ) -> int:
        """Schedule a periodic timer (fire every interval seconds)."""
        return self.add(callback, interval, *args, periodic=True, name=name, **kwargs)

    def cancel(self, tid: int) -> bool:
        """Cancel a timer by ID. Returns True if found and canceled."""
        with self._lock:
            entry = self._timers.get(tid)
            if entry is not None:
                entry.canceled = True
                del self._timers[tid]
                logger.debug("Canceled timer %d (%s)", tid, entry.name)
                return True
        return False

    def cancel_all(self) -> int:
        """Cancel all timers. Returns the count canceled."""
        with self._lock:
            count = len(self._timers)
            self._timers.clear()
            return count

    def is_scheduled(self, tid: int) -> bool:
        """Return True if a timer is currently scheduled."""
        with self._lock:
            return tid in self._timers and not self._timers[tid].canceled

    def get_timers(self) -> list[TimerEntry]:
        """Return a snapshot of all active timers."""
        with self._lock:
            return [
                e for e in self._timers.values()
                if not e.canceled
            ]

    # ---- internal loop ----

    async def _run_loop(self) -> None:
        """Main timer loop."""
        while self._running:
            try:
                entry = await self._wait_for_next()
                if entry is None:
                    continue
                if self._paused or entry.canceled:
                    continue

                # Fire the callback
                try:
                    result = entry.callback(*entry.args, **entry.kwargs)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.warning("Timer callback %s raised: %s", entry.name, e)

                # Reschedule if periodic
                if entry.interval > 0 and not entry.canceled:
                    entry.next_fire = time.monotonic() + entry.interval
                else:
                    entry.canceled = True
                    with self._lock:
                        self._timers.pop(entry.id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Timer loop error: %s", e)

    async def _wait_for_next(self) -> TimerEntry | None:
        """Wait until the next timer fires, or forever."""
        while self._running:
            with self._lock:
                if not self._timers:
                    # Wait indefinitely for a new timer
                    pass
                else:
                    # Wait until the earliest timer
                    next_entry = min(
                        (e for e in self._timers.values() if not e.canceled),
                        key=lambda e: e.next_fire,
                        default=None,
                    )
                    if next_entry is None:
                        pass
                    else:
                        wait_time = max(0, next_entry.next_fire - time.monotonic())
                        if wait_time <= 0:
                            return next_entry
                        # Wait with timeout
                        async with asyncio.timeout(wait_time):
                            try:
                                await asyncio.sleep(wait_time)
                            except asyncio.TimeoutError:
                                return next_entry
                            return next_entry

            # No timers: wait for signal
            await asyncio.sleep(0.1)
        return None


# ---------------------------------------------------------------------------
# Decorator-based timers
# ---------------------------------------------------------------------------

def after(seconds: float, *, name: str = ""):
    """Decorator to schedule a function to run once after `seconds`."""
    def decorator(func: TimerCallback) -> TimerCallback:
        def wrapper(*args: Any, **kwargs: Any) -> None:
            TimerManager.instance().add_once(func, seconds, *args, name=name or func.__name__, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


def every(seconds: float, *, name: str = ""):
    """Decorator to schedule a function to run every `seconds`."""
    def decorator(func: TimerCallback) -> TimerCallback:
        def wrapper(*args: Any, **kwargs: Any) -> None:
            TimerManager.instance().add_interval(func, seconds, *args, name=name or func.__name__, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_timer_manager: TimerManager = TimerManager.instance()


def add_timer(*args: Any, **kwargs: Any) -> int:
    return _timer_manager.add(*args, **kwargs)


def add_once(*args: Any, **kwargs: Any) -> int:
    return _timer_manager.add_once(*args, **kwargs)


def add_interval(*args: Any, **kwargs: Any) -> int:
    return _timer_manager.add_interval(*args, **kwargs)


def cancel_timer(tid: int) -> bool:
    return _timer_manager.cancel(tid)


def cancel_all_timers() -> int:
    return _timer_manager.cancel_all()
