"""Alarm clock manager for players (LMS 'alarm'/'alarms' API).

Each player can hold up to 16 alarms (``index`` 0..15). An alarm stores the
usual LMS clock fields (enabled, days-of-week mask, time, volume, fade-in
duration, play length, repeat) plus an optional wake source (stream URL or a
DB track id). Alarms are persisted per player as JSON under the LMS Prefs
directory and are evaluated by :class:`AlarmScheduler` every minute: when a
matching alarm fires, the player is powered on and plays its wake source.

This implements the real functionality behind the 'alarm'/'alarms' CLI and
JSON-RPC shapes (previously only empty placeholders existed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_ALARMS = 16
# Default Prefs dir mirrors config.py / utils.prefs.
PREFS_DIR = Path.home() / ".lyrion" / "Lyrion" / "Prefs"


@dataclass
class Alarm:
    """A single alarm clock (LMS fields + wake source)."""

    index: int = 0
    enabled: bool = False
    # 7-char day mask, Monday first, '1' = fires on that day. e.g. "1111111".
    days: str = "1111111"
    # ISO 'HH:MM' 24h.
    time: str = "07:00"
    # Player volume on wake; -1 = leave as-is.
    volume: int = -1
    # Fade-in length in seconds.
    fade: int = 0
    # Play length in minutes; 0 = until stopped (or repeat).
    duration: int = 0
    # Daily / per-day repeat (0 = single-shot).
    repeat: bool = False
    # Wake source: 'url:<stream>' or 'track:<id>' or '' for default none.
    wake: str = ""

    def day_int(self) -> int:
        """Day mask as an int with Monday = bit 0."""
        n = 0
        for i, ch in enumerate(self.days[:7]):
            if ch == "1":
                n |= 1 << i
        return n


def _default_alarm(index: int = 0) -> Alarm:
    return Alarm(index=index)


class AlarmManager:
    """Singleton store of per-player alarms, persisted as JSON."""

    __slots__ = ("_db_path", "_data", "_lock", "_init_done", "_alarm_loop")

    _instance: "AlarmManager | None" = None

    def __new__(cls, db_path: Path | str | None = None) -> "AlarmManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: Path | str | None = None) -> None:
        if getattr(self, "_init_done", False):
            return
        self._db_path = Path(db_path) if db_path else PREFS_DIR / "alarms.json"
        self._lock = threading.RLock()
        self._init_done = False
        self._data: dict[str, dict[str, dict]] = {}

    def load(self) -> None:
        """Load persisted alarms (idempotent)."""
        with self._lock:
            if self._init_done:
                return
            if self._db_path.exists():
                try:
                    self._data = json.loads(self._db_path.read_text("utf-8"))
                except Exception as exc:  # pragma: no cover
                    logger.warning("alarm load failed: %s", exc)
                    self._data = {}
            else:
                self._data = {}
            self._init_done = True

    def _save(self) -> None:
        with self._lock:
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                self._db_path.write_text(json.dumps(self._data, indent=2), "utf-8")
            except Exception as exc:  # pragma: no cover
                logger.warning("alarm persist failed: %s", exc)

    # ---- accessors ----------------------------------------------------

    def alarms_for(self, mac: str) -> dict[int, Alarm]:
        """Return {index: Alarm} for a player."""
        self.load()
        with self._lock:
            raw = self._data.get(mac, {})
            out: dict[int, Alarm] = {}
            for k, v in raw.items():
                try:
                    idx = int(k)
                except (TypeError, ValueError):
                    continue
                a = Alarm(index=idx)
                for f in ("enabled", "days", "time", "volume", "fade",
                          "duration", "repeat", "wake"):
                    if f in v and isinstance(v[f], (str, int, bool)):
                        setattr(a, f, v[f])
                out[idx] = a
            return out

    def get(self, mac: str, index: int) -> Alarm | None:
        return self.alarms_for(mac).get(index)

    def set(self, mac: str, index: int, alarm: Alarm) -> None:
        """Store an alarm for a player and persist."""
        self.load()
        with self._lock:
            self._data.setdefault(mac, {})[str(index)] = asdict(alarm)
            self._save()

    def delete(self, mac: str, index: int) -> None:
        """Remove an alarm for a player and persist."""
        self.load()
        with self._lock:
            slot = self._data.get(mac, {})
            if slot.pop(str(index), None) is not None:
                self._save()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class AlarmScheduler:
    """Fires due alarms once per minute: powers on the player and plays the
    wake source. Standalone (no event loop owned here) — call ``run()`` as an
    asyncio task."""

    _PAD = 1.0  # allow a couple seconds of scheduling slop

    def __init__(self, polling_interval: float = 20.0) -> None:
        self._polling_interval = polling_interval
        self._last_fired: set[tuple[str, int, str]] = set()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Background loop: check alarms, fire those due."""
        mgr = AlarmManager()
        while True:
            try:
                self._check_once(mgr)
            except Exception as exc:  # pragma: no cover
                logger.warning("alarm scheduler error: %s", exc)
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(self._polling_interval)

    def _check_once(self, mgr: AlarmManager) -> None:
        from datetime import datetime

        now = datetime.now()
        now_min = now.strftime("%H:%M")
        weekday = now.weekday()  # 0 = Monday (matches day mask bit 0)

        for raw_mac, alarms in list(self._snapshot_alarms(mgr).items()):
            for alarm in alarms:
                if not alarm.enabled:
                    continue
                if alarm.time != now_min:
                    continue
                # Day mask applies?
                if not self._day_matches(alarm, weekday):
                    continue
                key = (raw_mac, alarm.index, now.strftime("%Y%m%d%H%M"))
                if key in self._last_fired:
                    continue
                self._last_fired.add(key)
                self.fire(raw_mac, alarm)

    def _snapshot_alarms(self, mgr: AlarmManager) -> dict[str, list[Alarm]]:
        out: dict[str, list[Alarm]] = {}
        for player in self._players():
            mac = player.mac
            alarms = mgr.alarms_for(mac)
            if alarms:
                out[mac] = sorted(alarms.values(), key=lambda a: a.index)
        return out

    def _day_matches(self, alarm: Alarm, weekday: int) -> bool:
        days = alarm.days[:7]
        if len(days) < 7:
            return False
        return days[weekday] == "1"

    def _players(self) -> list:
        from lyrion.player.manager import PlayerManager

        return list(PlayerManager().players.values()) if hasattr(
            PlayerManager(), "players") else []

    async def _favorite_url(self, fav_id: str) -> str | None:
        """Resolve a favorite id to its URL (for a 'fr:' wake source)."""
        try:
            from lyrion.music.favorites import get_favorites_manager

            fav = await get_favorites_manager().get(int(fav_id))
            if fav:
                return fav.get("url") or fav.get("type")
        except Exception as exc:  # pragma: no cover
            logger.warning("alarm: cannot resolve favorite %s: %s", fav_id, exc)
        return None

    def fire(self, mac: str, alarm: Alarm) -> None:
        """Power on the player and start the wake source."""
        from lyrion.player.manager import PlayerManager

        pm = PlayerManager()
        player = pm.get_player(mac)
        if player is None:
            logger.info("alarm: player not found for %s", mac)
            return

        logger.info("alarm firing on %s at %s (wake=%s)", mac, alarm.time, alarm.wake)

        async def _wake() -> None:
            try:
                # Power on (synchronous; set_power is not async).
                pm.set_power(mac, True)
                # Apply wake volume if requested.
                if alarm.volume >= 0:
                    await pm.set_volume(mac, alarm.volume)
                # Start the wake source.
                if alarm.wake.startswith("url:"):
                    await pm.play_url(mac, alarm.wake[4:], title=alarm.time)
                elif alarm.wake.startswith("track:"):
                    try:
                        await pm.play_track(mac, int(alarm.wake[6:]))
                    except ValueError:
                        logger.warning("alarm: bad wake track %r", alarm.wake)
                elif alarm.wake.startswith("fr:"):
                    # Wake with a favorite: resolve fav_id → its URL, then play.
                    fav_id = alarm.wake[3:]
                    furl = await self._favorite_url(fav_id)
                    if furl:
                        await pm.play_url(mac, furl, title=alarm.time)
                    else:
                        logger.info("alarm: favorite %r not found for %s", fav_id, mac)
                else:
                    logger.info("alarm: no wake source configured for %s", mac)
                # A non-repeat alarm should switch itself off after firing.
                if not alarm.repeat:
                    alarm.enabled = False
                    AlarmManager().set(mac, alarm.index, alarm)
            except Exception as exc:  # pragma: no cover
                logger.warning("alarm wake failed for %s: %s", mac, exc)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_wake())
        except RuntimeError:  # pragma: no cover - no running loop
            asyncio.run(_wake())


def _alarm_from_parts(index: int, parts: dict[str, str]) -> Alarm:
    """Build an Alarm from CLI key:value parts (validating + defaulting)."""
    a = _default_alarm(index)
    if "enabled" in parts:
        a.enabled = parts["enabled"] not in ("0", "false", "")
    if "days" in parts:
        d = parts["days"]
        # accept '1111111' or '01xxxxx' style; keep 7 chars
        a.days = (d[:7].ljust(7, "1")) if d else "1111111"
    if "day" in parts:
        # integer bitmask (1=Mo .. 64=So), e.g. from JSON clients
        try:
            mask = int(parts["day"])
            a.days = "".join("1" if (mask >> i) & 1 else "0" for i in range(7))
        except ValueError:
            pass
    if "time" in parts:
        t = parts["time"]
        if ":" in t and len(t) >= 4:
            a.time = t.zfill(5)
    if "hour" in parts or "minute" in parts:
        hh = int(parts.get("hour", a.time.split(":")[0]))
        mm = int(parts.get("minute", a.time.split(":")[1]))
        a.time = f"{hh:02d}:{mm:02d}"
    if "volume" in parts:
        try:
            a.volume = int(parts["volume"])
        except ValueError:
            pass
    if "fade" in parts:
        try:
            a.fade = int(parts["fade"])
        except ValueError:
            pass
    if "duration" in parts:
        try:
            a.duration = int(parts["duration"])
        except ValueError:
            pass
    if "repeat" in parts:
        a.repeat = parts["repeat"] not in ("0", "false", "")
    # wake source — 'url:'/track:' wrap with their prefix; a 'wake:' value
    # already carries it (don't double-prefix).
    for w in ("url", "track"):
        if w in parts and parts[w]:
            a.wake = f"{w}:{parts[w]}"
    if "wake" in parts and parts["wake"]:
        a.wake = parts["wake"] if parts["wake"].startswith(("url:", "track:", "fr:")) \
            else f"url:{parts['wake']}"
    return a


def alarm_query_string(index: int, a: Alarm | None) -> str:
    """Build the CLI/JSON reply line for a single alarm."""
    if a is None:
        a = _default_alarm(index)
    return (
        f"alarm index:{index}"
        f" enabled:{1 if a.enabled else 0}"
        f" time:{a.time}"
        f" days:{a.days}"
        f" volume:{a.volume}"
        f" fade:{a.fade}"
        f" duration:{a.duration}"
        f" repeat:{1 if a.repeat else 0}"
        f" wake:{a.wake}"
    )