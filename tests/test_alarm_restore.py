"""Alarm-Restlücken: Snooze-Neuarmung, Snooze-Dauer, Restore-Semantik.

Perl-Referenz (alles ``/tmp/lms-ref``):

* ``fireAlarm`` merkt sich VOR dem Klingeln (``Slim/Utils/Alarm.pm``):
  Power ``:593`` (vor ``['power', 1]`` ``:594``), Lautstärke ``:610-611``
  (``_originalVolume``) + die Alarm-Lautstärke ``:615-616``
  (``_activeVolume``), Shuffle ``:627-628`` (``_originalShuffleMode``);
  die Alarm-Lautstärke wird gesetzt ``:619-623``, der Alarm-Shuffle
  ``:629-632``.
* ``stop($continueAudio)`` (``Alarm.pm:862-936``): No-op ``:868``,
  ``currentAlarm`` ``:872-874``, ``_active``/``_snoozeActive`` ``:875-876``;
  Restore NUR wenn ``!$continueAudio`` ``:890`` und alles in Timern +1 s
  (``:896`` Analog-Out, ``:905`` Volume/Shuffle/Power) — Volume nur wenn
  nicht spielend UND unverändert ``:908-912``, Shuffle ``:914-916``,
  Power ``:918-920``.
* ``snooze()`` (``Alarm.pm:734-797``): No-op ``:741``, kein zweites Snooze
  ``:747-749``, Dauer = Client-Pref ``alarmSnoozeSeconds`` ``:750``
  (``Slim/Player/Client.pm:44`` Default 540), Quelle pausieren
  (Remote-URL: stoppen) ``:768-779``, ``_snoozeActive = 1`` ``:781``,
  Re-Sound-Timer ``:784``.
* ``stopSnooze(1)`` (``Alarm.pm:810-847``): No-op ``:818``,
  ``_snoozeActive = 0`` ``:823``, Unpause ``:825-828``; der Alarm bleibt
  ``_active``/``currentAlarm`` (kein Clear) — er klingelt nach dem Snooze
  erneut.
* ``jiveAlarmCommand`` (``Slim/Control/Jive.pm:2459-2488``) ruft genau
  diese beiden Subs.
"""

from __future__ import annotations

import asyncio

import pytest

import lyrion.alarms as alarms_mod
from lyrion.alarms import (
    DEFAULT_SNOOZE_SECONDS,
    Alarm,
    AlarmManager,
    AlarmScheduler,
)
from lyrion.player.state import PlayerState

MAC = "1C:87:2C:47:FC:36"


class _FakePlayerManager:
    """Aufzeichnender PlayerManager-Ersatz (nur die von alarms.py genutzten Calls).

    Signaturen wie ``lyrion/player/manager.py``: ``get_player``:515,
    ``set_power``:576 (sync), ``set_volume``:650, ``pause_player``:977,
    ``stop_player``, ``play_url``:894, ``play_track``:831 (async).
    """

    def __init__(self, player: PlayerState | None = None) -> None:
        self.player = player
        self.calls: list[tuple] = []

    def get_player(self, mac: str):
        if self.player is not None and self.player.mac == mac:
            return self.player
        return None

    def set_power(self, mac: str, on: bool) -> None:
        self.calls.append(("power", bool(on)))
        if self.player is not None:
            self.player.power = bool(on)

    async def set_volume(self, mac: str, volume: int) -> bool:
        self.calls.append(("volume", int(volume)))
        if self.player is not None:
            self.player.volume = int(volume)
        return True

    async def pause_player(self, mac: str, pause: bool) -> bool:
        self.calls.append(("pause", bool(pause)))
        if self.player is not None:
            self.player.mode = "pause" if pause else "play"
        return True

    async def stop_player(self, mac: str) -> bool:
        self.calls.append(("stop",))
        if self.player is not None:
            self.player.mode = "stop"
        return True

    async def play_url(self, player_id: str, url: str, title: str = "") -> bool:
        self.calls.append(("play_url", url))
        if self.player is not None:
            self.player.mode = "play"
        return True

    async def play_track(self, player_id: str, track_id) -> bool:
        self.calls.append(("play_track", track_id))
        if self.player is not None:
            self.player.mode = "play"
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolierter AlarmManager + injizierbarer PlayerManager-Ersatz."""
    prefs = tmp_path / "prefs"
    prefs.mkdir(exist_ok=True)
    monkeypatch.setattr("lyrion.alarms.PREFS_DIR", prefs)
    # Der 1-s-Restore-Timer (Alarm.pm:896/905) darf Tests nicht bremsen.
    monkeypatch.setattr(alarms_mod, "RESTORE_DELAY_SECONDS", 0.0)
    AlarmManager._instance = None

    def install(player: PlayerState | None = None) -> _FakePlayerManager:
        fake = _FakePlayerManager(player)
        monkeypatch.setattr("lyrion.player.manager.PlayerManager", lambda: fake)
        return fake

    yield install
    AlarmManager._instance = None


def _player(**kw) -> PlayerState:
    return PlayerState(
        mac=kw.pop("mac", MAC),
        name=kw.pop("name", "Küche"),
        ip=kw.pop("ip", "192.168.1.225"),
        port=kw.pop("port", 48512),
        **{"connected": True, **kw},
    )


def _seed_alarm(mgr: AlarmManager, index: int = 0) -> Alarm | None:
    """Der klingelnde Alarm muss auch im Store liegen (``current`` → ``get``)."""
    if mgr.get(MAC, index) is None:
        mgr.set(MAC, index, Alarm(index=index, enabled=True, time="07:00"))
    return mgr.get(MAC, index)


def _fire(alarm: Alarm, *, wait: float = 0.01) -> None:
    """``AlarmScheduler.fire`` samt ``_wake``-Task im eigenen Eventloop."""
    async def run():
        AlarmScheduler().fire(MAC, alarm)
        await asyncio.sleep(wait)

    asyncio.run(run())


# ── 1. Was wird VOR dem Klingeln gemerkt (Alarm.pm:593/610-616/627-628) ───


def test_fire_saves_pre_alarm_state(env):
    player = _player(power=True, volume=30, shuffle=2, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  shufflemode=1, wake="url:http://alarm.example/wake")

    _fire(alarm)

    saved = mgr.saved_state(MAC)
    assert saved is not None
    assert saved.original_power is True          # Alarm.pm:593
    assert saved.original_volume == 30           # Alarm.pm:610-611
    assert saved.active_volume == 70             # Alarm.pm:615-616
    assert saved.original_shuffle == 2           # Alarm.pm:627-628
    # … und was der Alarm daraus macht
    assert player.power is True                  # Alarm.pm:594
    assert player.volume == 70                   # Alarm.pm:619-623
    assert player.shuffle == 1                   # Alarm.pm:629-632


def test_fire_captures_original_power_before_switching_it_on(env):
    """``_originalPower`` wird VOR ``['power', 1]`` gelesen (Alarm.pm:593-594)."""
    player = _player(power=False, volume=20, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=-1,
                  wake="url:http://alarm.example/wake")

    _fire(alarm)

    saved = mgr.saved_state(MAC)
    assert saved is not None and saved.original_power is False
    assert player.power is True
    # volume -1 = „unverändert lassen“ -> active_volume ist der Ist-Wert
    assert saved.active_volume == 20


def test_fire_without_player_saves_nothing(env):
    fake = env(None)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00")

    _fire(alarm)

    assert fake.calls == []
    assert mgr.saved_state(MAC) is None


# ── 2. Restore beim Stop (Alarm.pm:890-920) ───────────────────────────────


def test_stop_restores_volume_shuffle_and_power(env):
    player = _player(power=True, volume=30, shuffle=2, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  shufflemode=1, wake="url:http://alarm.example/wake")
    _fire(alarm)
    # Ende des Alarms: Musik gestoppt, Lautstärke unverändert am Alarm-Pegel
    player.mode = "stop"

    async def run():
        assert mgr.stop(MAC) is True
        await asyncio.sleep(0.01)             # Restore-Task
    asyncio.run(run())

    assert player.volume == 30                    # Alarm.pm:911
    assert player.shuffle == 2                    # Alarm.pm:916
    assert player.power is True                   # Alarm.pm:920


def test_stop_restores_power_off_when_player_was_off(env):
    player = _player(power=False, volume=30, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  wake="url:http://alarm.example/wake")
    _fire(alarm)
    assert player.power is True

    async def run():
        mgr.stop(MAC)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert player.power is False                  # Alarm.pm:918-920


def test_stop_with_continue_audio_leaves_the_player_alone(env):
    """``if (!$continueAudio)`` (Alarm.pm:890)."""
    player = _player(power=True, volume=30, shuffle=2, mode="play")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  shufflemode=1, wake="url:http://alarm.example/wake")
    _fire(alarm)
    player.volume = 70
    player.shuffle = 1

    async def run():
        assert mgr.stop(MAC, continue_audio=True) is True
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert player.volume == 70 and player.shuffle == 1 and player.power is True
    assert mgr.current(MAC) is None               # UI trotzdem beendet :872-876


def test_stop_keeps_volume_the_user_changed_during_the_alarm(env):
    """Volume nur zurück, wenn es noch dem Alarm-Pegel entspricht (:909)."""
    player = _player(power=True, volume=30, shuffle=2, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  shufflemode=1, wake="url:http://alarm.example/wake")
    _fire(alarm)
    player.mode = "stop"
    player.volume = 55                            # Nutzer hat gedreht

    async def run():
        mgr.stop(MAC)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert player.volume == 55                    # bleibt
    assert player.shuffle == 2                    # Shuffle trotzdem zurück :916


def test_stop_keeps_volume_while_the_alarm_still_plays(env):
    """``!$client->isPlaying`` (Alarm.pm:909)."""
    player = _player(power=True, volume=30, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  shufflemode=1, wake="url:http://alarm.example/wake")
    _fire(alarm)
    player.mode = "play"                          # klingelt noch

    async def run():
        mgr.stop(MAC)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert player.volume == 70                    # nicht zurückgesetzt


def test_stop_clears_current_snooze_and_saved_state(env):
    player = _player(volume=30, mode="stop")
    env(player)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  wake="url:http://alarm.example/wake")
    _fire(alarm)
    mgr.snooze(MAC, 540)
    assert mgr.is_snoozing(MAC)

    async def run():
        mgr.stop(MAC)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert mgr.current(MAC) is None               # Alarm.pm:872-874
    assert not mgr.is_snoozing(MAC)               # Alarm.pm:875-876
    assert mgr.saved_state(MAC) is None


def test_stop_without_a_sounding_alarm_is_a_noop(env):
    player = _player(volume=30)
    env(player)
    assert AlarmManager().stop(MAC) is False      # Alarm.pm:868
    assert player.volume == 30


def test_restore_waits_for_the_perl_delay(env, monkeypatch):
    """Perl stellt erst nach 1 s zurück (Alarm.pm:896/905)."""
    player = _player(volume=30, mode="stop")
    env(player)
    monkeypatch.setattr(alarms_mod, "RESTORE_DELAY_SECONDS", 0.05)
    mgr = AlarmManager()
    alarm = Alarm(index=0, enabled=True, time="07:00", volume=70,
                  wake="url:http://alarm.example/wake")
    _fire(alarm)
    player.mode = "stop"

    async def run():
        mgr.stop(MAC)
        await asyncio.sleep(0.01)
        assert player.volume == 70                # noch nicht
        await asyncio.sleep(0.1)
        assert player.volume == 30                # jetzt
    asyncio.run(run())


# ── 3. Snooze: Dauer + Neuarmung (Alarm.pm:734-847) ───────────────────────


def test_default_snooze_seconds_matches_the_perl_client_pref():
    """``alarmSnoozeSeconds => 540`` (Client.pm:44, „9 minutes“)."""
    assert DEFAULT_SNOOZE_SECONDS == 540


def _snooze(mgr: AlarmManager, seconds: int):
    async def run():
        result = mgr.snooze(MAC, seconds)
        await asyncio.sleep(0.01)
        return result

    return asyncio.run(run())


def test_snooze_pauses_the_wake_source(env):
    """``$client->execute(['pause', 1])`` (Alarm.pm:775-778)."""
    player = _player(power=True, volume=70, mode="play")
    fake = env(player)
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)

    assert _snooze(mgr, 540) == 540

    assert ("pause", True) in fake.calls
    assert mgr.is_snoozing(MAC)                  # Alarm.pm:781
    assert player.mode == "pause"


def test_snooze_stops_a_remote_stream_instead_of_pausing(env):
    """Remote-URLs werden gestoppt, damit Radio in Echtzeit bleibt (:768-772)."""
    player = _player(power=True, volume=70, mode="play", remote=1)
    fake = env(player)
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)

    _snooze(mgr, 540)

    assert ("stop",) in fake.calls
    assert ("pause", True) not in fake.calls     # else-Zweig :773-779


def test_snooze_is_a_noop_without_a_sounding_alarm(env):
    env(_player())
    mgr = AlarmManager()
    assert _snooze(mgr, 540) is None             # Alarm.pm:741
    assert not mgr.is_snoozing(MAC)


def test_snooze_does_not_arm_a_second_time(env):
    """``if ($self->{_snoozeActive}) { … } else { … }`` (Alarm.pm:747-749)."""
    env(_player(mode="stop"))
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)

    assert _snooze(mgr, 540) == 540
    assert _snooze(mgr, 60) == 540               # Dauer des LAUFENDEN Snooze
    assert mgr.is_snoozing(MAC)


def test_snooze_expiry_resumes_the_alarm(env):
    """``stopSnooze`` nach ``snoozeSeconds`` (Alarm.pm:784 → :810-847)."""
    player = _player(power=True, volume=70, mode="play")
    fake = env(player)
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)
    _snooze(mgr, 0)                              # Ablauf sofort
    fake.calls.clear()

    async def run():
        AlarmScheduler()._check_once(mgr)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert ("pause", False) in fake.calls        # Alarm.pm:825-828
    assert mgr.current(MAC) is not None          # bleibt aktiv
    assert not mgr.is_snoozing(MAC)              # Alarm.pm:823


def test_snooze_expiry_resumes_only_once(env):
    player = _player(power=True, volume=70, mode="play")
    fake = env(player)
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)
    _snooze(mgr, 0)
    fake.calls.clear()

    async def run():
        sched = AlarmScheduler()
        sched._check_once(mgr)
        await asyncio.sleep(0.01)
        sched._check_once(mgr)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert [c for c in fake.calls if c == ("pause", False)] == [("pause", False)]


def test_running_snooze_is_not_resumed_by_a_poll(env):
    player = _player(power=True, volume=70, mode="play")
    fake = env(player)
    mgr = AlarmManager()
    mgr.set_current(MAC, 0)
    _seed_alarm(mgr)
    _snooze(mgr, 540)
    fake.calls.clear()

    async def run():
        AlarmScheduler()._check_once(mgr)
        await asyncio.sleep(0.01)
    asyncio.run(run())

    assert ("pause", False) not in fake.calls
    assert mgr.is_snoozing(MAC)
