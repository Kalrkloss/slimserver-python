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
    # Shuffle mode used for the alarm playlist while the alarm sounds
    # (``Slim/Utils/Alarm.pm:258-275``: 0 = off, 1 = by song, 2 = by album;
    # persisted as ``_shufflemode`` in the client prefs, Alarm.pm:1085/1360).
    shufflemode: int = 0
    # Wake source: 'url:<stream>' or 'track:<id>' or '' for default none.
    wake: str = ""

    def day_int(self) -> int:
        """Day mask as an int with Monday = bit 0.

        Our OWN representation (Perl has no alarm day mask): the Perl web UI
        and CLI always address days through ``$alarm->day(0..6)`` with
        ``0 = Sunday .. 6 = Saturday`` (``Slim/Utils/Alarm.pm:116-118,183-197``).
        """
        n = 0
        for i, ch in enumerate(self.days[:7]):
            if ch == "1":
                n |= 1 << i
        return n


def _perl_day_to_index(day: int) -> int:
    """Perl alarm day (0 = Sunday .. 6 = Saturday) → index in ``Alarm.days``.

    ``Alarm.days`` is Monday-first (bit 0 = Monday, ``day_int``); Perl counts
    ``0 = Sunday .. 6 = Saturday``
    (``Slim/Utils/Alarm.pm:116-118`` "0=Sun 6=Sat", ``:183-197`` ``sub day``,
    ``Slim/Control/Commands.pm:193-198`` applies the ``dow`` tag through it).
    """
    return 6 if day == 0 else day - 1


def _default_alarm(index: int = 0) -> Alarm:
    return Alarm(index=index)


class AlarmManager:
    """Singleton store of per-player alarms, persisted as JSON."""

    __slots__ = ("_db_path", "_data", "_lock", "_init_done", "_alarm_loop",
                 "_current", "_snooze")

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
        # Transient (NOT persisted, Perl keeps them on the client object): the
        # alarm currently sounding (``$client->alarmData->{currentAlarm}``,
        # Alarm.pm:578-579/872-874/1241-1246) and the snooze expiry per player
        # (``_snoozeActive`` + the stopSnooze timer, Alarm.pm:781-784/810-847).
        self._current: dict[str, int] = {}
        self._snooze: dict[str, float] = {}

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
                          "duration", "repeat", "shufflemode", "wake"):
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
            if self._current.get(mac) == index:
                self._current.pop(mac, None)
                self._snooze.pop(mac, None)

    # ---- the alarm currently sounding (snooze/stop, jiveAlarmCommand) ----

    def set_current(self, mac: str, index: int) -> None:
        """Mark an alarm as currently sounding.

        Perl sets ``$client->alarmData->{currentAlarm}`` when the alarm starts
        (``Slim/Utils/Alarm.pm:578-579``, called from ``fireAlarm``), and
        ``getCurrentAlarm`` (:1241-1246) is exactly what ``jiveAlarmCommand``
        (``Slim/Control/Jive.pm:2471``) snoozes/stops.
        """
        self.load()
        with self._lock:
            self._current[mac] = index
            self._snooze.pop(mac, None)

    def current(self, mac: str) -> "Alarm | None":
        """``Slim::Utils::Alarm->getCurrentAlarm($client)`` (Alarm.pm:1241-1246)."""
        idx = self._current.get(mac)
        return None if idx is None else self.get(mac, idx)

    def is_snoozing(self, mac: str) -> bool:
        """Perl ``$alarm->{_snoozeActive}`` (Alarm.pm:747-784)."""
        until = self._snooze.get(mac)
        return until is not None and until > time.time()

    def snooze(self, mac: str, seconds: int) -> int | None:
        """``$alarm->snooze()`` (Alarm.pm:734-797).

        No-op unless an alarm is currently sounding (:741). The snooze length
        comes from the client pref ``alarmSnoozeSeconds`` (Client.pm:44), the
        caller reads it and passes it in. Returns the snooze length used, or
        ``None`` when nothing was sounding.

        Perl additionally pauses/stops the wake source (:768-779) and re-sounds
        after the timer (:784) — the rescheduling itself is out of scope here
        (our ``AlarmScheduler`` has no re-arm hook).
        """
        with self._lock:
            if mac not in self._current:
                return None           # Perl: return unless $self->{_active}
            self._snooze[mac] = time.time() + max(0, int(seconds))
            return int(seconds)

    def stop(self, mac: str, continue_audio: bool = False) -> bool:
        """``$alarm->stop($continueAudio)`` (Alarm.pm:862-936).

        Clears the current alarm and the snooze state (:872-876); no-op when no
        alarm is sounding (:868). ``continue_audio`` only controls Perl's
        restore of volume/shuffle/power (:890-922) — our port never touched
        those while the alarm sounded, so there is nothing to restore.
        Returns whether an alarm was stopped.
        """
        with self._lock:
            if mac not in self._current:
                return False
            self._current.pop(mac, None)
            self._snooze.pop(mac, None)
            return True


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
        """Resolve a favorite id to its URL (for a 'fr:' wake source).

        Accepts a plain DB id ('5') and an LMS hierarchical id ('0.2.1' —
        favorites nested in folders) via FavoritesManager.resolve_path.
        """
        try:
            from lyrion.music.favorites import get_favorites_manager

            mgr = get_favorites_manager()
            fav = str(fav_id).strip()
            if not fav.isdigit():
                # Hierarchical id ('0.x…') → DB id; None if not found.
                resolved = await mgr.resolve_path(fav)
                if resolved is None:
                    logger.info("alarm: favorite path %r not found", fav_id)
                    return None
                fav = str(resolved)
            favorite = await mgr.get(int(fav))
            if favorite:
                return favorite.get("url") or favorite.get("type")
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
        # Perl marks the ringing alarm on the client: ``$self->{_active} = 1;
        # $client->alarmData->{currentAlarm} = $self;`` (Alarm.pm:578-579).
        # jiveAlarmCommand's snooze/stop act on exactly this alarm.
        AlarmManager().set_current(mac, alarm.index)

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


def _alarm_from_parts(index: int, parts: dict[str, str],
                      base: "Alarm | None" = None) -> Alarm:
    """Build an Alarm from CLI key:value parts (validating + defaulting).

    ``base`` is the alarm being updated (Perl loads the existing alarm and
    applies only the given tags, ``Slim/Control/Commands.pm:156`` + :167-207);
    ``dowAdd``/``dowDel`` are incremental and therefore operate on the base's
    day mask.
    """
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
        t = parts["time"].strip()
        if ":" in t and len(t) >= 4:
            a.time = t.zfill(5)
        elif t.isdigit() and len(t) in (3, 4):
            # Perl CLI/JSON form: time:HHMM (or HMM) digits only
            a.time = f"{int(t[:-2]):02d}:{t[-2:]}"
    if "hour" in parts or "minute" in parts:
        hh = int(parts.get("hour", a.time.split(":")[0]))
        mm = int(parts.get("minute", a.time.split(":")[1]))
        a.time = f"{hh:02d}:{mm:02d}"
    if "dow" in parts:
        # Perl ``dow`` tag: comma-separated day numbers, 0 = Sunday .. 6 =
        # Saturday (``Slim/Utils/Alarm.pm:116-118`` "0=Sun 6=Sat", applied by
        # ``Slim/Control/Commands.pm:193-198`` ``$alarm->day($_, $set)``).
        # NOTE: our ``Alarm.days`` is Monday-first, so each Perl day is mapped
        # through ``_perl_day_to_index``. The old Monday-first reading of
        # ``dow`` was wrong (live Perl 9.1.1: an Mo-Fr alarm reports
        # ``dow:1,2,3,4,5``).
        try:
            vals = [int(x) for x in parts["dow"].split(",") if x.strip() != ""]
            days = ["0"] * 7
            for v in vals:
                if 0 <= v <= 6:
                    days[_perl_day_to_index(v)] = "1"
            a.days = "".join(days)
        except ValueError:
            pass
    if "dowAdd" in parts or "dowDel" in parts:
        # ``dowAdd``/``dowDel`` carry ONE Perl day (Commands.pm:200-207
        # ``$alarm->day($params->{dowAdd}, 1)``), same 0 = Sunday numbering.
        # Perl applies them to the LOADED alarm's day mask (:156/:194-197), so
        # the base's days are the starting point (default: all days on, like
        # ``Alarm.pm:118``).
        try:
            base_days = getattr(base, "days", None) if base is not None else None
            days = list((base_days or a.days).ljust(7, "0")[:7])
            for x in str(parts.get("dowAdd", "")).split(","):
                if x.strip().isdigit() and 0 <= int(x) <= 6:
                    days[_perl_day_to_index(int(x))] = "1"
            for x in str(parts.get("dowDel", "")).split(","):
                if x.strip().isdigit() and 0 <= int(x) <= 6:
                    days[_perl_day_to_index(int(x))] = "0"
            a.days = "".join(days)
        except ValueError:
            pass
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
    if "shufflemode" in parts:
        # ``$alarm->shufflemode($params->{shufflemode})`` (Commands.pm:188);
        # Perl accepts 0/1/2 (Alarm.pm:258-275). The Jive menu sends the raw
        # number (Jive.pm:804-870), the web UI the string.
        try:
            a.shufflemode = max(0, min(2, int(str(parts["shufflemode"]).strip())))
        except ValueError:
            pass
    # wake source — 'url:'/track:' wrap with their prefix; a 'wake:' value
    # already carries it (don't double-prefix). url:0 clears the wake
    # (Perl: '0' as url means "current playlist"/no source — Jive sends it
    # when the user clears the wake source).
    for w in ("url", "track"):
        if w in parts and parts[w]:
            a.wake = "" if parts[w] == "0" else f"{w}:{parts[w]}"
    if "wake" in parts and parts["wake"]:
        if parts["wake"] == "0":
            a.wake = ""
        else:
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
        f" shufflemode:{int(a.shufflemode)}"
        f" wake:{a.wake}"
    )