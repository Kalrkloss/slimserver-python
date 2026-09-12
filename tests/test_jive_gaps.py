"""Jive-Restlücken: snooze/stop, Alarm-shufflemode, dow-Index, Preset-Playback, Recent-Searches.

Perl-Referenz (alles ``/tmp/lms-ref``):

* ``jiveAlarmCommand``: ``Slim/Control/Jive.pm:2459-2488`` — ``snooze`` (:2466)
  → ``$alarm->snooze()`` (:2475), ``stop``/``continueAudio`` (:2467-2468) →
  ``$alarm->stop($continueAudio)`` (:2477), Alarm aus
  ``Slim::Utils::Alarm->getCurrentAlarm`` (:2471).
* Snooze/Stop: ``Slim/Utils/Alarm.pm`` — ``snooze``:734-797 (``_active``
  :741, ``alarmSnoozeSeconds`` :750, ``_snoozeActive`` + Timer :781-787),
  ``stop``:862-936 (``currentAlarm`` löschen :872-874, ``_active``/``_snoozeActive``
  :875-876, no-op :868), ``getCurrentAlarm``:1241-1246, ``fireAlarm`` setzt
  ``currentAlarm`` :578-579; Pref-Default ``alarmSnoozeSeconds => 540``
  (``Slim/Player/Client.pm:44``).
* ``shufflemode``: ``Slim/Utils/Alarm.pm:258-275`` (0/1/2), gespeichert als
  ``_shufflemode`` (:1085) und wieder geladen (:1360); gesetzt vom
  ``alarm``-Kommando (``Slim/Control/Commands.pm:188``) und gelesen vom
  Jive-Menü (``Jive.pm:795`` ``radio => ($currentShuffleMode == N) + 0``,
  :804-870) sowie von ``alarmsQuery`` (``Slim/Control/Queries.pm:239``).
* ``dow``/``dowAdd``/``dowDel``: 0 = Sonntag … 6 = Samstag
  (``Slim/Utils/Alarm.pm:116-118`` "0=Sun 6=Sat", :183-197 ``sub day``;
  ``Slim/Control/Commands.pm:78`` Tag-Liste, :193-207 Anwendung;
  ``Jive.pm:939-963`` schickt dieselbe Zahl als ``dowAdd``/``dowDel``).
  Live-Probe Perl 9.1.1 (read-only, 2026-09-12): ein Mo-Fr-Alarm auf
  ``24:0a:c4:29:77:90`` liefert ``dow:"1,2,3,4,5"``.
* Preset-Wiedergabe über die Jive-Taste: ``Slim/Control/Commands.pm:263-291``
  (``buttonCommand`` → ``Slim::Hardware::IR::executeButton``) +
  ``Slim/Buttons/Common.pm:825-877`` (``playPreset``: 0 → 10 :828-830,
  ``type =~ /audio|playlist/`` :834, XMLBrowser-Fall :838-850,
  sonst ``playlist play <url>`` :851-859, Popup ``PRESET`` :860-868);
  Button-Code ``playPreset_<n>`` (:908). ``isRemoteURL``:
  ``Slim/Music/Info.pm:1115-1124``.
* Recent Searches: ``Jive.pm:39`` (``@recentSearches``), ``cacheSearch``
  :2711-2719 (nur mit ``text`` + ``actions.go.cmd``, ``unshift``),
  ``recentSearchMenu`` :2721-2766 (nur bei ``scalar(...) == 1``),
  ``jiveRecentSearchQuery`` :2768-2797; gefüllt vom Such-/Feed-Code
  (``Slim/Control/XMLBrowser.pm:1455`` ``cachesearch``-Param,
  Eintrag :1475-1489, Aufruf :1491).
"""

from __future__ import annotations

import asyncio

import pytest

import lyrion.alarms as alarms_mod
from lyrion.alarms import Alarm, AlarmManager, AlarmScheduler, _alarm_from_parts
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import (
    _JIVE_RECENT_SEARCHES,
    _jive_alarm_days_string,
    _jive_cache_search,
    _jive_recent_search_menu,
    JSONRPCAPI,
)

MAC = "1C:87:2C:47:FC:36"
JIVE = dict(
    mac=MAC,
    name="Küche",
    ip="192.168.1.225",
    port=48512,
    model="squeezeplay",
    model_name="SB Player",
    connected=True,
    power=True,
)


@pytest.fixture(autouse=True)
def alarm_env(tmp_path, monkeypatch):
    """Isolierter AlarmManager (JSON unter tmp) + leerer Such-Cache."""
    prefs = tmp_path / "prefs"
    prefs.mkdir(exist_ok=True)
    monkeypatch.setattr("lyrion.alarms.PREFS_DIR", prefs)
    AlarmManager._instance = None
    _JIVE_RECENT_SEARCHES.clear()
    yield
    AlarmManager._instance = None
    _JIVE_RECENT_SEARCHES.clear()


def _player(mac: str = MAC, **kw) -> PlayerState:
    base = dict(JIVE)
    base.update(mac=mac)
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


def _req(command: list, *players: PlayerState):
    """``JSONRPCAPI()._slim_request(MAC, [cmd, …])`` mit Test-Playern."""
    _pm(*players)
    return asyncio.run(JSONRPCAPI()._slim_request(MAC, command))


def _req_api(command: list, *players: PlayerState):
    _pm(*players)
    api = JSONRPCAPI()
    return api, asyncio.run(api._slim_request(MAC, command))


def _seed(mac: str, idx: int, **fields) -> None:
    a = Alarm(index=idx)
    for k, v in fields.items():
        setattr(a, k, v)
    AlarmManager().set(mac, idx, a)


# ── 1. jivealarm snooze/stop (Jive.pm:2459-2488, Alarm.pm:734-936) ────────


def test_jivealarm_snooze_snoozes_the_sounding_alarm():
    _seed(MAC, 2, enabled=True, time="07:00")
    mgr = AlarmManager()
    mgr.set_current(MAC, 2)                       # Alarm klingelt (Alarm.pm:578-579)
    _, res = _req_api(["jivealarm", "snooze:1"], _player())
    assert res == {}                              # setStatusDone ohne Result
    assert mgr.is_snoozing(MAC)
    assert mgr.current(MAC) is not None           # bleibt der aktuelle Alarm


def test_jivealarm_snooze_uses_alarm_snooze_seconds_pref(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_snooze(self, mac, seconds):
        calls.append((mac, seconds))
        return seconds

    monkeypatch.setattr(AlarmManager, "snooze", fake_snooze)
    _seed(MAC, 0, enabled=True, time="07:00")
    AlarmManager().set_current(MAC, 0)

    # Default aus Client.pm:44 = 540 Sekunden
    _req_api(["jivealarm", "snooze:1"], _player())
    assert calls == [(MAC, 540)]

    # Client-Pref schlägt den Default
    _req_api(["jivealarm", "snooze:1"],
             _player(playerprefs={"alarmSnoozeSeconds": "60"}))
    assert calls[-1] == (MAC, 60)


def test_jivealarm_snooze_without_sounding_alarm_is_a_noop():
    _seed(MAC, 0, enabled=True, time="07:00")
    mgr = AlarmManager()
    _, res = _req_api(["jivealarm", "snooze:1"], _player())
    assert res == {}
    assert not mgr.is_snoozing(MAC)               # Alarm.pm:741 return unless _active


def test_jivealarm_stop_clears_current_alarm_and_snooze():
    _seed(MAC, 1, enabled=True, time="07:00")
    mgr = AlarmManager()
    mgr.set_current(MAC, 1)
    mgr.snooze(MAC, 540)
    assert mgr.is_snoozing(MAC)

    _, res = _req_api(["jivealarm", "stop:1", "continueAudio:1"], _player())
    assert res == {}
    assert mgr.current(MAC) is None               # Alarm.pm:872-874
    assert not mgr.is_snoozing(MAC)               # Alarm.pm:875-876


def test_jivealarm_stop_passes_continue_audio_through(monkeypatch):
    seen: list[bool] = []

    def fake_stop(self, mac, continue_audio=False):
        seen.append(continue_audio)
        return True

    monkeypatch.setattr(AlarmManager, "stop", fake_stop)
    _seed(MAC, 0, enabled=True, time="07:00")
    _req_api(["jivealarm", "stop:1", "continueAudio:1"], _player())
    _req_api(["jivealarm", "stop:1"], _player())
    assert seen == [True, False]                  # Jive.pm:2468 truthy → 1


def test_jivealarm_stop_without_sounding_alarm_is_a_noop():
    mgr = AlarmManager()
    _, res = _req_api(["jivealarm", "stop:1"], _player())
    assert res == {}
    assert not mgr.stop(MAC)                      # Alarm.pm:868 return unless _active


def test_alarm_scheduler_marks_the_ringing_alarm(monkeypatch):
    """``fireAlarm`` setzt ``currentAlarm`` (Alarm.pm:578-579)."""

    async def fake_play_url(self, player_id, url, title=""):
        return True

    monkeypatch.setattr(PlayerManager, "play_url", fake_play_url)
    _seed(MAC, 3, enabled=True, time="07:00", repeat=True,
          wake="url:http://alarm.example/wake")
    _pm(_player())
    mgr = AlarmManager()
    alarm = mgr.get(MAC, 3)

    async def run():
        AlarmScheduler().fire(MAC, alarm)
        await asyncio.sleep(0.01)         # die _wake-Task anlaufen lassen

    asyncio.run(run())
    assert mgr.current(MAC) is not None
    assert mgr.current(MAC).index == 3


# ── 2. Alarm-shufflemode (Alarm.pm:258-275/1085/1360) ─────────────────────


def test_alarm_update_stores_shufflemode():
    _seed(MAC, 0, enabled=True, time="07:00", wake="url:http://old/")

    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(MAC, ["update", "id:0", "shufflemode:2"])

    item = asyncio.run(run())
    a = AlarmManager().get(MAC, 0)
    assert a.shufflemode == 2                     # Commands.pm:188
    assert item["shufflemode"] == 2               # Queries.pm:239
    assert a.wake == "url:http://old/"            # unberührte Felder bleiben


def test_alarm_shufflemode_is_persisted():
    _seed(MAC, 0, enabled=True, time="07:00", shufflemode=1)
    AlarmManager._instance = None                 # neu laden wie nach Neustart
    assert AlarmManager().get(MAC, 0).shufflemode == 1


def test_alarm_shufflemode_accepts_the_web_ui_string_form():
    a = _alarm_from_parts(0, {"shufflemode": "1"})
    assert a.shufflemode == 1
    assert _alarm_from_parts(0, {"shufflemode": "9"}).shufflemode == 2
    assert _alarm_from_parts(0, {"shufflemode": "x"}).shufflemode == 0


def test_alarm_shufflemode_survives_an_unrelated_update():
    _seed(MAC, 0, enabled=True, time="07:00", shufflemode=2)

    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(MAC, ["update", "id:0", "time:0800"])

    asyncio.run(run())
    assert AlarmManager().get(MAC, 0).shufflemode == 2


def test_jiveupdatealarm_menu_radios_the_stored_shufflemode():
    _seed(MAC, 2, enabled=True, time="06:15", days="1111100", shufflemode=1)
    res = _req(["jiveupdatealarm", "0", "100", "id:2"])
    shuffle = res["item_loop"][4]
    assert shuffle["text"] == "Shuffle"
    # Jive.pm:795/799/815/831: radio => ($currentShuffleMode == N) + 0
    assert [i["radio"] for i in shuffle["item_loop"]] == [0, 1, 0]


def test_jiveupdatealarm_menu_default_shufflemode_is_off():
    _seed(MAC, 2, enabled=True, time="06:15", days="1111100")
    res = _req(["jiveupdatealarm", "0", "100", "id:2"])
    assert [i["radio"] for i in res["item_loop"][4]["item_loop"]] == [1, 0, 0]


# ── 3. dow-Index: Perl zählt 0 = Sonntag (Alarm.pm:116-118) ───────────────


def test_alarm_dow_is_sunday_first_like_perl():
    # Live-Probe: ein Mo-Fr-Alarm liefert dow "1,2,3,4,5" (Perl 9.1.1).
    a = _alarm_from_parts(0, {"dow": "1,2,3,4,5"})
    assert a.days == "1111100"                    # unsere Montag-first-Ablage


def test_alarm_dowadd_dowdel_use_perl_day_numbers():
    # Perl wendet dowAdd/dowDel auf den GELADENEN Alarm an (Commands.pm:156/
    # 200-207) → Basis-Tagesmaske kommt vom ``base``-Alarm.
    a = _alarm_from_parts(0, {"dowAdd": "0"},
                          base=Alarm(index=0, days="0000000"))
    assert a.days == "0000001"                    # Sonntag dazu
    a = _alarm_from_parts(0, {"dowDel": "1"},
                          base=Alarm(index=0, days="1111100"))
    assert a.days == "0111100"                    # Montag entfernt
    a = _alarm_from_parts(0, {"dow": "1,2,3,4,5", "dowDel": "1"})
    assert a.days == "0111100"


def test_alarm_dow_roundtrip_with_the_jive_days_string():
    _seed(MAC, 4, enabled=True, time="06:00", days="1111100")   # Mo-Fr
    alarm = AlarmManager().get(MAC, 4)
    assert _jive_alarm_days_string(alarm) == "1,2,3,4,5"        # Jive.pm:660-663
    # was der Client zurückschickt, ergibt wieder dieselbe Ablage
    assert _alarm_from_parts(4, {"dow": _jive_alarm_days_string(alarm)}).days \
        == "1111100"


def test_jiveupdatealarmdays_sends_perl_day_numbers():
    _seed(MAC, 4, enabled=True, time="06:00", days="1111100")
    res = _req(["jiveupdatealarmdays", "0", "100", "id:4"])
    # Sonntag ist Perl-Tag 0, Samstag 6 (Jive.pm:939-963)
    assert res["item_loop"][0]["actions"]["on"]["params"] == {
        "id": "4", "dowAdd": "0"}
    assert res["item_loop"][0]["actions"]["off"]["params"] == {
        "id": "4", "dowDel": "0"}
    assert res["item_loop"][6]["actions"]["on"]["params"] == {
        "id": "4", "dowAdd": "6"}


def test_jiveupdatealarmdays_roundtrip_applies_the_button():
    _seed(MAC, 4, enabled=True, time="06:00", days="0000000")
    res = _req(["jiveupdatealarmdays", "0", "100", "id:4"])
    sunday_on = res["item_loop"][0]["actions"]["on"]["params"]
    # Der Client schickt genau diese params an ['alarm','update'] (Jive.pm:950-955)
    async def run():
        api = JSONRPCAPI()
        return await api._json_alarm(
            MAC, ["update", f"id:{sunday_on['id']}",
                  f"dowAdd:{sunday_on['dowAdd']}"])

    asyncio.run(run())
    assert AlarmManager().get(MAC, 4).days == "0000001"          # Sonntag


# ── 4. Preset-/Playlist-Wiedergabe (Common.pm:825-877) ────────────────────


class _PlayRecorder:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, player_id, url, title=""):
        self.calls.append((player_id, url, title))
        return True


def _presets(*entries) -> list:
    slots: list = [None] * 10
    for i, e in enumerate(entries):
        slots[i] = e
    return slots


def test_playpreset_plays_the_stored_url(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    player = _player(playerprefs={
        "presets": _presets({"URL": "http://radio.example/live",
                             "text": "Mein Sender", "type": "audio"})})
    pm = _pm(player)
    api = JSONRPCAPI()
    asyncio.run(api._json_control(pm, MAC, "button", ["playPreset_1"]))
    assert rec.calls == [(MAC, "http://radio.example/live", "Mein Sender")]
    # Common.pm:860-868: Popup popupplay mit PRESET <n> + Titel
    assert api._popup == {"jive": {
        "type": "popupplay", "text": ["Preset #1", "Mein Sender"]}}


def test_playpreset_zero_is_slot_ten(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    presets = _presets(*([None] * 9))
    presets[9] = {"URL": "http://radio.example/x", "text": "Zehn", "type": "audio"}
    api = JSONRPCAPI()
    asyncio.run(api._json_control(_pm(_player(playerprefs={"presets": presets})),
                                  MAC, "button", ["playPreset_0"]))
    assert rec.calls == [(MAC, "http://radio.example/x", "Zehn")]
    assert api._popup["jive"]["text"][0] == "Preset #10"


def test_playpreset_empty_slot_does_nothing(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    api = JSONRPCAPI()
    asyncio.run(api._json_control(_pm(_player()), MAC, "button", ["playPreset_3"]))
    assert rec.calls == []
    assert getattr(api, "_popup", None) is None


def test_playpreset_ignores_non_audio_entries(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    player = _player(playerprefs={
        "presets": _presets({"URL": "http://x/y", "text": "T", "type": "text"})})
    asyncio.run(JSONRPCAPI()._json_control(
        _pm(player), MAC, "button", ["playPreset_1"]))
    assert rec.calls == []                        # Common.pm:834 /audio|playlist/


def test_playpreset_playlist_with_remote_url_is_not_wired(monkeypatch):
    """Common.pm:838-850 → XMLBrowser::playItem; unser Port hat keinen
    XMLBrowser-Player ⇒ bewusst kein Start (UNKLAR)."""
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    player = _player(playerprefs={
        "presets": _presets({"URL": "http://x/list.m3u", "text": "Liste",
                             "type": "playlist"})})
    asyncio.run(JSONRPCAPI()._json_control(
        _pm(player), MAC, "button", ["playPreset_1"]))
    assert rec.calls == []


def test_playpreset_local_playlist_is_played(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    player = _player(playerprefs={
        "presets": _presets({"URL": "/playlists/liste.m3u", "text": "Lokal",
                             "type": "playlist"})})
    asyncio.run(JSONRPCAPI()._json_control(
        _pm(player), MAC, "button", ["playPreset_1"]))
    assert rec.calls == [(MAC, "/playlists/liste.m3u", "Lokal")]


def test_other_button_codes_stay_a_noop(monkeypatch):
    rec = _PlayRecorder()
    monkeypatch.setattr(PlayerManager, "play_url", rec)
    api = JSONRPCAPI()
    asyncio.run(api._json_control(_pm(_player()), MAC, "button", ["rew"]))
    assert rec.calls == []


def test_is_remote_url_matches_perl(monkeypatch):
    from lyrion.web.api import _is_remote_url
    assert _is_remote_url("http://x/y") is True
    assert _is_remote_url("https://x/y") is True
    assert _is_remote_url("/playlists/a.m3u") is False
    assert _is_remote_url("file:///tmp/a.mp3") is False


# ── 5. Recent Searches (Jive.pm:39/2711-2719/2721-2766/2768-2797) ─────────


def _search_entry(text: str) -> dict:
    return {
        "text": text,
        "actions": {"go": {"player": 0, "cmd": ["search", "items"],
                           "params": {"search": text}}},
    }


def test_recent_searches_query_is_empty_without_cached_searches():
    assert _req(["jiverecentsearches"]) == {
        "count": "1",
        "offset": 0,
        "item_loop": [{"text": "Empty", "style": "itemNoAction",
                       "action": "none"}],
    }


def test_recent_searches_query_returns_cached_entries():
    _jive_cache_search(_search_entry("Radio"))
    _jive_cache_search(_search_entry("Jazz"))
    res = _req(["jiverecentsearches"])
    assert res["count"] == 2                      # Jive.pm:2786
    assert res["offset"] == 0
    assert [i["text"] for i in res["item_loop"]] == ["Jazz", "Radio"]


def test_cache_search_requires_text_and_a_go_command():
    _jive_cache_search({"text": "ohne cmd", "actions": {"go": {}}})
    _jive_cache_search({"actions": {"go": {"cmd": ["search", "items"]}}})
    _jive_cache_search("kein dict")
    assert _JIVE_RECENT_SEARCHES == []            # Jive.pm:2715


def test_recent_search_menu_needs_exactly_one_entry():
    # Perl prüft echt auf ``scalar(@recentSearches) == 1`` (Jive.pm:2728)
    assert _jive_recent_search_menu() == []
    _jive_cache_search(_search_entry("Eins"))
    menu = _jive_recent_search_menu()
    assert [i["id"] for i in menu] == ["homeSearchRecent", "myMusicSearchRecent"]
    assert [i["node"] for i in menu] == ["home", "myMusicSearch"]
    assert [i["weight"] for i in menu] == [111, 50]
    assert all(i["actions"]["go"] == {"cmd": ["jiverecentsearches"]}
               for i in menu)
    _jive_cache_search(_search_entry("Zwei"))
    assert _jive_recent_search_menu() == []


def test_home_menu_shows_recent_searches_when_cached():
    api = JSONRPCAPI()
    ids = [i["id"] for i in api._home_menu()]
    assert "homeSearchRecent" not in ids
    _jive_cache_search(_search_entry("Eins"))
    ids = [i["id"] for i in api._home_menu()]
    assert "homeSearchRecent" in ids
    assert "myMusicSearchRecent" in ids
