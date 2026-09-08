"""Alarm JSON-RPC/CLI API tests — Perl alarmCommand parity.

Regression (gap analysis, 2026-09): the web UI's delete button
(['alarm', '<idx>', 'delete']) was a silent no-op (the bare 'delete'
token was ignored and the alarm re-saved); Material's Perl-form
['alarm', 'delete', 'id:<n>'] hit 'invalid alarm index'; 'add' was
unimplemented; and a 'fr:<hier-id>' wake source crashed on int('0.1').
"""

import asyncio

import pytest

import lyrion.alarms as alarms_mod
from lyrion.alarms import Alarm, AlarmManager, _alarm_from_parts
from lyrion.web.api import JSONRPCAPI


@pytest.fixture
def alarm_env(tmp_path, monkeypatch):
    """Isolated AlarmManager singleton + DB-less api under tmp prefs dir."""
    prefs = tmp_path / "prefs"
    prefs.mkdir(exist_ok=True)
    monkeypatch.setattr("lyrion.alarms.PREFS_DIR", prefs)
    AlarmManager._instance = None
    yield
    AlarmManager._instance = None


def _seed(mac: str, idx: int, **fields) -> None:
    a = Alarm(index=idx)
    for k, v in fields.items():
        setattr(a, k, v)
    AlarmManager().set(mac, idx, a)


def test_alarm_delete_index_form_removes(alarm_env):
    """Own web UI: ['alarm', '<idx>', 'delete'] must actually delete."""
    _seed("", 0, enabled=True, time="07:30", wake="url:http://x/y")

    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(None, ["0", "delete"])

    asyncio.run(run())
    assert AlarmManager().get("", 0) is None, "alarm must be removed"


def test_alarm_delete_id_form_removes(alarm_env):
    """Material: ['alarm', 'delete', 'id:<n>'] (Perl alarmCommand form)."""
    _seed("", 3, enabled=True, time="06:00")

    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(None, ["delete", "id:3"])

    asyncio.run(run())
    assert AlarmManager().get("", 3) is None, "alarm must be removed"


def test_alarm_add_creates_next_index(alarm_env):
    """Material: ['alarm', 'add', 'time:HHMM', 'dow:0,2,4', 'url:...']."""
    async def run():
        api = JSONRPCAPI()
        item = await api._json_alarm(
            None, ["add", "time:0730", "dow:0,2,4", "enabled:1",
                   "volume:70", "url:http://radio.example/stream"])
        return item

    item = asyncio.run(run())
    assert item["hour"] == 7 and item["minute"] == 30
    # dow 0=Mo,2=We,4=Fr → bitmask 1+4+16 = 21
    assert item["day"] == 21, f"dow mapping wrong: {item['day']}"
    assert item["volume"] == 70
    assert item["url"] == "http://radio.example/stream"
    alarms = AlarmManager().alarms_for("")
    assert len(alarms) == 1
    assert 0 in alarms


def test_alarm_update_merges_existing(alarm_env):
    _seed("", 0, enabled=True, time="07:00", days="1111111", volume=50,
          wake="url:http://old/")

    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(None, ["update", "id:0", "time:0800"])

    item = asyncio.run(run())
    a = AlarmManager().get("", 0)
    assert a.time == "08:00"
    assert a.volume == 50, "untouched fields must survive an update"
    assert a.wake == "url:http://old/"
    assert item["hour"] == 8


def test_time_hhmm_digits_and_url0_clear(alarm_env):
    """Perl sends time:HHMM digits; url:0 clears the wake source."""
    async def run():
        api = JSONRPCAPI()
        item = await api._json_alarm(None, ["add", "time:0600", "url:0"])
        return item

    item = asyncio.run(run())
    assert item["hour"] == 6 and item["minute"] == 0
    assert item["url"] == "" and item["wake"] == ""


def test_fr_hierarchical_wake_resolves(alarm_env, monkeypatch):
    """fr:0.2 (folder favorite) must resolve via resolve_path, not int()."""
    class _Favs:
        async def resolve_path(self, path):
            return 9 if path == "0.2" else None

        async def get(self, fav_id):
            if fav_id == 9:
                return {"url": "http://fav.example/live"}
            return None

    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: _Favs())

    async def run():
        sched = alarms_mod.AlarmScheduler()
        return await sched._favorite_url("0.2")

    assert asyncio.run(run()) == "http://fav.example/live"


def test_fr_plain_db_id_still_works(alarm_env, monkeypatch):
    class _Favs:
        async def resolve_path(self, path):
            return None

        async def get(self, fav_id):
            if fav_id == 5:
                return {"url": "http://fav.example/5"}
            return None

    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: _Favs())

    async def run():
        sched = alarms_mod.AlarmScheduler()
        return await sched._favorite_url("5")

    assert asyncio.run(run()) == "http://fav.example/5"
