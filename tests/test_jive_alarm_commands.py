"""Jive-Alarm-/Sync-/Sleep-/Listen-Kommandos wie Perl (Slim/Control/Jive.pm).

Perl-Quellen (alles ``/tmp/lms-ref``):

* Dispatch-Tabelle: ``Slim/Control/Jive.pm:57-162`` (``alarmsettings``:64-65,
  ``jiveupdatealarm``:67-68, ``jiveupdatealarmdays``:70-71, ``syncsettings``
  :73-74, ``sleepsettings``:76-77, ``jivealarm``:95-96, ``jiveendoftracksleep``
  :98-99, ``jivefavorites``:101-102, ``jivepresets``:104-105, ``jivealarmvolume``
  :107-108, ``jivesync``:124-125, ``jiveplaylists``:127-128,
  ``jiverecentsearches``:130-131, ``firmwareupgrade``:135-136,
  ``jiveapplets``:138-139, ``jivewallpapers``:141-142, ``jivesounds``:144-145,
  ``jivepatches``:147-148).
* Handler: ``alarmSettingsQuery``:591-696, ``getCurrentAlarms``:658-693,
  ``alarmVolumeSettings``:695-708, ``alarmUpdateMenu``:700-923,
  ``alarmUpdateDays``:925-1005, ``jiveAlarmVolumeSlider``:1029-1062,
  ``syncSettingsQuery``:1064-1089, ``endOfTrackSleepCommand``:1091-1114,
  ``sleepSettingsQuery``:1116-1162, ``howManyPlayersToSyncWith``:1975-1991,
  ``getPlayersToSyncWith``:1993-2086, ``jiveSyncCommand``:2088-2131,
  ``sleepInXHash``:2282-2300, ``jivePlaylistsCommand``:2412-2457,
  ``jiveAlarmCommand``:2459-2488, ``jivePresetsMenu``:2490-2596,
  ``jiveFavoritesCommand``:2601-2690, ``_jiveNoResults``:2692-2699,
  ``jiveRecentSearchQuery``:2768-2797, ``extensionsQuery``:2830-2885.
* Menü-Loop: ``sliceAndShip`` (Jive.pm:1338-1357) + ``normalize``
  (Slim/Control/Request.pm:1805-1839).
* Alarme: ``Slim/Utils/Alarm.pm`` — ``day``:189-203, ``displayStr``:946-963,
  ``time``:285-305, ``getAlarms``:1269-1291, ``getAlarm``:1298-1306,
  ``getCurrentAlarm``:1241-1248, ``defaultVolume``:1542-1556;
  ``Slim/Control/Commands.pm:55-212`` (alarmCommand: id/dow/dowAdd/dowDel/
  enabled/repeat/time/playlisturl/url), ``Commands.pm:2955`` (currentSleepTime
  in Minuten).
* Client-Prefs: ``Slim/Player/Client.pm:42-45`` (alarmsEnabled 1,
  alarmDefaultVolume 50, alarmfadeseconds 1); ``setPreset`` Client.pm:1323-1340;
  ``syncedWithNames`` Client.pm:1398-1410; ``Slim/Player/Sync.pm:123-128``
  (isMaster).
* Firmware: ``Slim/Utils/Firmware.pm:288-310`` (url), ``:319-345`` (need_upgrade).

Ground Truth (read-only Proben gegen den Perl-LMS 9.1.1, 192.168.1.90:9000,
jsonrpc.js):

* ``alarmsettings 1 1`` auf einem Player mit einem Alarm →
  ``{"count":5,"offset":"1","item_loop":[{"text":"Wecker 1: 06:15 Mo Di Mi Do
  Fr","actions":{"go":{"player":0,"cmd":["jiveupdatealarm"],"params":
  {"id":"5a47b241","enabled":"1","playlist":"http://…","days":"1,2,3,4,5",
  "time":22500}}}}]}``.
* ``alarmsettings`` ohne Alarme → count 4: All-Alarms-choice
  (``choiceStrings`` [„Aus","Ein"], ``selectedIndex`` 2), Add-Alarm-input
  (``initialText`` 25200), Alarm-Volume-Item, Fade-Checkbox (checkbox 1).
* ``jiveupdatealarm … id:<id>`` → 8 Items (Enabled-Checkbox, Zeit-Input mit
  ``initialText`` = übergebener ``time``-Parameter, Tage, Weckton, Zufall-Loop
  mit ``count`` 3, Repeat/One-Time-Radios, Remove-Menü mit ``count`` 2).
* ``jiveupdatealarmdays … id:<id>`` → 7 Items von Sonntag bis Samstag, u. a.
  ``{"text":"Montag","checkbox":1,"onClick":"refreshGrandparent","actions":
  {"on":{"params":{"id":…,"dowAdd":"1"}},"off":{"params":{"id":…,"dowDel":"1"}}}}``.
* ``jivealarmvolume`` → ``{"offset":0,"count":1,"item_loop":[{"slider":1,"min":1,
  "max":100,"sliderIcons":"volume","initial":"50",…}]}`` (``offset`` ZAHL,
  ``initial`` STRING).
* ``syncsettings 0 100`` → count 3: Kopfzeile ``"Synchronisieren Küche mit:"``
  (``style`` itemNoAction) plus die Optionen, sortiert nach Namen, mit
  ``params`` ``{syncWith, syncWithString, unsyncWith: 0}``.
* ``sleepsettings 0 100`` (kein Timer, nicht spielend) → 5 Items
  („15 Minuten" … „90 Minuten"), je ``setSelectedIndex`` "1" (STRING) und
  ``cmd`` ``["sleep",<Sekunden>]``.
* ``jivepresets 0 2 key:1 title:Y url:Z type:audio`` auf einem Player mit
  Presets → je Slot ``{text:"Voreinstellung N festlegen",count:2,offset:0,
  isContextMenu:1,item_loop:[Abbrechen, "<Titel> ersetzen?"]}``; ``key`` im
  ``set_preset``-Params ist ein STRING, ``parser`` ist ``null`` ohne Angabe.
* ``jivefavorites delete title:X url:Y type:audio`` → ``{"offset":0,"count":2,
  "item_loop":[CANCEL, {"text":"Löschen X","nextWindow":"grandparent"}]}``;
  mit ``icon:``/``item_id:`` landen beide im ``params``.
* ``jiveplaylists delete title:X url:Y playlist_id:1`` → dieselbe Form mit
  ``cmd`` ``["playlists","delete"]``.
* ``jiverecentsearches`` → ``{"count":"1","offset":0,"item_loop":[{"text":
  "Leer","style":"itemNoAction","action":"none"}]}`` (``count`` STRING).
* ``firmwareupgrade`` → ``{"firmwareUpgrade":0}`` (ohne Firmware-File),
  ``jivewallpapers``/``jivesounds`` → ``{"count":0}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lyrion.alarms import Alarm, AlarmManager
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"           # unser Client (SqueezePlay, NoDisplay)
SCHLAFZIMMER = "24:0A:C4:29:77:90"  # Gruppen-Master im Sync-Test
RADIO = "00:04:20:2B:88:C8"         # Slave derselben Gruppe
TAVERNE = "00:00:00:00:00:00"       # freier Player

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


def _player(mac: str = MAC, name: str = "Küche", **kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(JIVE)
    base.update(mac=mac, name=name)
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


def _req(command: list, *players: PlayerState) -> Any:
    """``JSONRPCAPI()._slim_request(MAC, [cmd, …])`` mit Test-Playern."""
    _pm(*players)
    return asyncio.run(JSONRPCAPI()._slim_request(MAC, command))


def _req_api(command: list, *players: PlayerState) -> tuple[JSONRPCAPI, Any]:
    """Wie ``_req``, gibt zusätzlich die API (für ``_popup``)/den Player aus."""
    pm = _pm(*players)
    api = JSONRPCAPI()
    res = asyncio.run(api._slim_request(MAC, command))
    return api, res


@pytest.fixture(autouse=True)
def alarm_env(tmp_path, monkeypatch):
    """Isolierter AlarmManager (JSON unter tmp) wie tests/test_alarms.py."""
    prefs = tmp_path / "prefs"
    prefs.mkdir(exist_ok=True)
    monkeypatch.setattr("lyrion.alarms.PREFS_DIR", prefs)
    AlarmManager._instance = None
    yield
    AlarmManager._instance = None


def _seed(mac: str, idx: int, **fields: Any) -> None:
    a = Alarm(index=idx)
    for k, v in fields.items():
        setattr(a, k, v)
    AlarmManager().set(mac, idx, a)


# ── 1. alarmsettings (Jive.pm:591-696) ────────────────────────────────────


def test_alarmsettings_without_alarms_matches_perl_live():
    res = _req(["alarmsettings", "0", "100"])
    assert res["count"] == 4
    assert res["offset"] == "0"                 # sliceAndShip-Reichweite
    assert [i["text"] for i in res["item_loop"]] == [
        "All Alarms", "Add Alarm", "Alarm Volume", "Fade Alarms In",
    ]

    on_off = res["item_loop"][0]
    # Jive.pm:601-606: ucfirst(string('OFF'|'ON')), selectedIndex = Pref+1
    assert on_off["choiceStrings"] == ["Off", "On"]
    assert on_off["selectedIndex"] == 2         # Client.pm:42 alarmsEnabled 1
    assert on_off["actions"]["do"]["choices"] == [
        {"player": 0, "cmd": ["alarm", "disableall"]},
        {"player": 0, "cmd": ["alarm", "enableall"]},
    ]

    add = res["item_loop"][1]
    assert add["text"] == "Add Alarm"
    assert add["input"]["initialText"] == 25200          # 7:00, Jive.pm:626
    assert add["input"]["_inputStyle"] == "time"
    assert add["input"]["len"] == 1
    assert add["input"]["title"] == "Add Alarm"
    assert add["input"]["help"]["text"].startswith("Use the scroll wheel")
    assert add["actions"]["do"] == {
        "player": 0,
        "cmd": ["alarm", "add"],
        "params": {"time": "__TAGGEDINPUT__", "enabled": 1},
    }
    assert add["nextWindow"] == "refresh"

    volume = res["item_loop"][2]
    assert volume == {"text": "Alarm Volume",
                      "actions": {"go": {"player": 0, "cmd": ["jivealarmvolume"]}}}

    fade = res["item_loop"][3]
    assert fade["checkbox"] == 1                         # Client.pm:45
    assert fade["actions"]["on"] == {"player": 0, "cmd": ["jivealarm"],
                                     "params": {"fadein": 1}}
    assert fade["actions"]["off"] == {"player": 0, "cmd": ["jivealarm"],
                                      "params": {"fadein": 0}}


def test_alarmsettings_with_alarm_matches_perl_live():
    # Mo-Fr 06:00, eingeschaltet, Stream-URL (Live-Probe Radio).
    _seed(MAC, 3, enabled=True, time="06:00", days="1111100",
          wake="url:http://strm.example/ambientpsy")
    res = _req(["alarmsettings", "0", "100"])
    assert res["count"] == 5
    item = res["item_loop"][1]
    # Perl: "<ALARM_ALARM> <n>: <displayStr>"; displayStr iteriert 1..6, 0
    assert item["text"] == "Alarm 1: 06:00 Mo Tu We Th Fr"
    go = item["actions"]["go"]
    assert go["player"] == 0
    assert go["cmd"] == ["jiveupdatealarm"]
    assert go["params"]["id"] == "3"
    assert go["params"]["enabled"] == 1
    # Perl-Tagesliste (getCurrentAlarms, Jive.pm:660-663): 0=So … 6=Sa
    assert go["params"]["days"] == "1,2,3,4,5"
    assert go["params"]["time"] == 21600                 # Sekunden seit 0:00
    assert go["params"]["playlist"] == "http://strm.example/ambientpsy"


def test_alarmsettings_display_string_covers_disabled_and_daily_alarms():
    _seed(MAC, 0, enabled=False, time="07:30", days="1111111")
    res = _req(["alarmsettings", "0", "100"])
    # Alarm.pm:946-963: ausgeschaltet → ALARM_OFF davor; everyDay → keine Tage
    assert res["item_loop"][1]["text"] == "Alarm 1: Off (07:30)"

    _seed(MAC, 1, enabled=True, time="09:15", days="0000001")
    res = _req(["alarmsettings", "0", "100"])
    assert res["count"] == 6                     # 2 Alarme + 4 feste Items
    assert res["item_loop"][2]["text"] == "Alarm 2: 09:15 Su"
    assert res["item_loop"][2]["actions"]["go"]["params"]["days"] == "0"


def test_alarmsettings_skips_volume_for_fixed_volume_players():
    # Bug 9226 (Jive.pm:654-661): digitalVolumeControl == 0 → kein Volume-Item
    res = _req(["alarmsettings", "0", "100"],
               _player(digital_volume_control=False))
    assert res["count"] == 3
    assert [i["text"] for i in res["item_loop"]] == [
        "All Alarms", "Add Alarm", "Fade Alarms In",
    ]


def test_alarmsettings_reads_client_prefs():
    res = _req(["alarmsettings", "0", "100"],
               _player(playerprefs={"alarmsEnabled": "0",
                                    "alarmfadeseconds": "0"}))
    assert res["item_loop"][0]["selectedIndex"] == 1     # 0 + 1
    assert res["item_loop"][3]["checkbox"] == 0


def test_alarmsettings_runs_the_slicer():
    res = _req(["alarmsettings", "1", "1"])
    assert res["count"] == 4
    assert res["offset"] == "1"
    assert len(res["item_loop"]) == 1
    assert res["item_loop"][0]["text"] == "Add Alarm"


def test_alarmsettings_without_player_has_no_result():
    # Perl needClient = 1 → Status 103, kein Result (Jive.pm:64-65)
    pm = PlayerManager()
    pm.players = {}
    assert asyncio.run(JSONRPCAPI()._slim_request(MAC, ["alarmsettings", "0", "100"])) == {}


# ── 2. jiveupdatealarm (Jive.pm:700-923) ──────────────────────────────────


def test_jiveupdatealarm_menu_matches_perl_live():
    _seed(MAC, 2, enabled=True, time="06:15", days="1111100", repeat=True)
    res = _req(["jiveupdatealarm", "0", "100", "id:2", "time:22500",
                "enabled:1", "days:1,2,3,4,5", "playlist:0"])
    assert res["count"] == 8
    assert res["offset"] == "0"

    enabled, set_time, set_days, playlist, shuffle, repeat_on, repeat_off, remove = \
        res["item_loop"][:8]

    assert enabled["text"] == "Enabled"
    assert enabled["checkbox"] == 1
    assert enabled["onClick"] == "refreshOrigin"
    assert enabled["actions"]["on"]["params"] == {"id": "2", "enabled": 1}
    assert enabled["actions"]["off"]["params"] == {"id": "2", "enabled": 0}

    assert set_time["text"] == "Set Time"
    # Live: initialText ist der vom Client mitgeschickte time-Parameter
    assert set_time["input"]["initialText"] == "22500"
    assert set_time["input"]["_inputStyle"] == "time"
    assert set_time["actions"]["do"]["params"] == {"id": "2",
                                                   "time": "__TAGGEDINPUT__"}
    assert set_time["nextWindow"] == "parent"

    assert set_days["text"] == "Choose Days"
    assert set_days["actions"]["go"] == {
        "player": 0, "cmd": ["jiveupdatealarmdays"], "params": {"id": "2"}}

    assert playlist["text"] == "Alarm Sound"
    assert playlist["actions"]["go"] == {
        "player": 0, "cmd": ["alarm", "playlists"],
        "params": {"id": "2", "menu": 1}}

    assert shuffle["text"] == "Shuffle"
    assert shuffle["count"] == 3
    assert shuffle["offset"] == 0
    assert [i["text"] for i in shuffle["item_loop"]] == [
        "Don't Shuffle Playlist", "Shuffle by Song", "Shuffle by Album"]
    assert [i["actions"]["do"]["params"]["shufflemode"]
            for i in shuffle["item_loop"]] == [0, 1, 2]
    assert [i["nextWindow"] for i in shuffle["item_loop"]] == ["refresh"] * 3
    assert [i["onClick"] for i in shuffle["item_loop"]] == ["refreshOrigin"] * 3

    assert repeat_on["text"] == "Repeat Alarm"
    assert repeat_on["radio"] == 1
    assert repeat_on["actions"]["do"]["params"] == {"id": "2", "repeat": 1}
    assert repeat_off["text"] == "One Time Alarm"
    assert repeat_off["radio"] == 0
    assert repeat_off["actions"]["do"]["params"] == {"id": "2", "repeat": 0}

    assert remove["text"] == "Remove Alarm"
    assert remove["count"] == 2
    assert remove["offset"] == 0
    cancel, delete = remove["item_loop"]
    assert cancel["text"] == "Cancel"
    assert cancel["actions"]["go"] == {"player": 0, "cmd": ["jiveblankcommand"]}
    assert cancel["nextWindow"] == "parent"
    assert delete["text"] == "Remove Alarm"
    assert delete["actions"]["go"] == {
        "player": 0, "cmd": ["alarm", "delete"], "params": {"id": "2"}}
    assert delete["nextWindow"] == "grandparent"


def test_jiveupdatealarm_without_matching_alarm_has_no_result():
    # Perl: getAlarm → undef, ``$alarm->enabled()`` stirbt (Live: Verbindung
    # schließt ohne Antwort)
    assert _req(["jiveupdatealarm", "0", "100", "id:7"]) == {}


def test_jiveupdatealarm_honours_the_slicer():
    _seed(MAC, 0, enabled=True, time="06:00", days="1111111")
    res = _req(["jiveupdatealarm", "0", "2", "id:0"])
    assert res["count"] == 8
    assert res["offset"] == "0"
    assert len(res["item_loop"]) == 2


# ── 3. jiveupdatealarmdays (Jive.pm:925-1005) ─────────────────────────────


def test_jiveupdatealarmdays_matches_perl_live():
    _seed(MAC, 4, enabled=True, time="06:00", days="1111100")   # Mo-Fr
    res = _req(["jiveupdatealarmdays", "0", "100", "id:4"])
    assert res["count"] == 7
    assert res["offset"] == "0"
    # Perl zählt 0=Sonntag … 6=Samstag (Alarm.pm:189-203, ALARM_DAY0..6)
    assert [i["text"] for i in res["item_loop"]] == [
        "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday",
    ]
    # Perl zählt 0=Sonntag … 6=Samstag (Alarm.pm:116-118/189-203, ALARM_DAY0..6)
    # und schickt dieselbe Zahl als dowAdd/dowDel zurück (Jive.pm:939-963);
    # ``alarm update`` legt sie durch ``$alarm->day()`` (Commands.pm:200-207)
    # mit dieser Zählung ab.
    assert [i["checkbox"] for i in res["item_loop"]] == [0, 1, 1, 1, 1, 1, 0]
    assert [i["onClick"] for i in res["item_loop"]] == ["refreshGrandparent"] * 7
    # Sonntag = Perl-Tag 0, Montag = 1, Samstag = 6
    assert res["item_loop"][0]["actions"]["on"]["params"] == {
        "id": "4", "dowAdd": "0"}
    assert res["item_loop"][0]["actions"]["off"]["params"] == {
        "id": "4", "dowDel": "0"}
    assert res["item_loop"][1]["actions"]["on"]["params"] == {
        "id": "4", "dowAdd": "1"}
    assert res["item_loop"][6]["actions"]["on"]["params"] == {
        "id": "4", "dowAdd": "6"}
    assert all(i["actions"]["on"]["cmd"] == ["alarm", "update"]
               for i in res["item_loop"])


def test_jiveupdatealarmdays_without_matching_alarm_has_no_result():
    assert _req(["jiveupdatealarmdays", "0", "100", "id:0"]) == {}


# ── 4. jivealarmvolume (Jive.pm:1029-1062) ────────────────────────────────


def test_jivealarmvolume_matches_perl_live():
    res = _req(["jivealarmvolume"])
    assert res == {
        "offset": 0,                     # addResult-Zahl (kein String!)
        "count": 1,
        "item_loop": [{
            "slider": 1,
            "min": 1,
            "max": 100,
            "sliderIcons": "volume",
            "initial": "50",             # Pref-Getter → STRING, Client.pm:43
            "actions": {"do": {
                "player": 0,
                "cmd": ["alarm", "defaultvolume"],
                "params": {"valtag": "volume"},
            }},
        }],
    }


def test_jivealarmvolume_uses_the_client_pref():
    res = _req(["jivealarmvolume"], _player(playerprefs={"alarmDefaultVolume": "30"}))
    assert res["item_loop"][0]["initial"] == "30"


# ── 5. jivealarm (Jive.pm:2459-2488) und Sleep am Titelende (:1091-1114) ──


def test_jivealarm_stores_the_fadein_pref():
    player = _player()
    _, res = _req_api(["jivealarm", "fadein:0"], player)
    assert res == {}                                  # setStatusDone ohne Result
    assert player.playerprefs["alarmfadeseconds"] == "0"


def test_jivealarm_without_fadein_changes_nothing():
    player = _player()
    _, res = _req_api(["jivealarm"], player)
    assert res == {}
    assert player.playerprefs == {}


def test_end_of_track_sleep_sets_the_remaining_time():
    player = _player(mode="play", duration=240.0, elapsed=40.0)
    _, res = _req_api(["jiveendoftracksleep", "0", "100"], player)
    assert res == {}
    assert player.sleep_remaining == 200


def test_end_of_track_sleep_shows_a_popup_when_idle():
    player = _player(mode="stop", duration=0)
    api, res = _req_api(["jiveendoftracksleep", "0", "100"], player)
    assert res == {}
    assert player.sleep_remaining == 0
    # Jive.pm:1104-1111: showBriefly mit jive-Typ 'popupplay'
    assert api._popup == {"jive": {
        "type": "popupplay", "text": ["Nothing currently playing"]}}


# ── 6. syncsettings (Jive.pm:1064-1089, 1975-2086) ────────────────────────


def test_syncsettings_without_partners_returns_the_textarea():
    # Bug 16030 (Jive.pm:1076-1086)
    res = _req(["syncsettings", "0", "100"])
    assert res["count"] == 0
    assert res["window"]["textarea"].startswith(
        "Add one or more additional Squeezeboxes")


def test_syncsettings_matches_perl_live():
    master = _player(mac=SCHLAFZIMMER, name="Schlafzimmer",
                     sync_slaves=[RADIO])
    radio = _player(mac=RADIO, name="Squeezebox Radio",
                    sync_master=SCHLAFZIMMER)
    taverne = _player(mac=TAVERNE, name="Taverne")
    res = _req(["syncsettings", "0", "100"],
               _player(), master, radio, taverne)
    assert res["count"] == 3
    assert res["offset"] == "0"
    assert res["item_loop"][0] == {"text": "Sync Küche to:",
                                   "style": "itemNoAction"}
    # Optionen nach Namen sortiert (Jive.pm:2049), Gruppenmaster mit
    # Gruppenname aus syncedWithNames(1)
    assert [i["text"] for i in res["item_loop"][1:]] == [
        "Squeezebox Radio & Schlafzimmer", "Taverne"]
    assert [i["radio"] for i in res["item_loop"][1:]] == [0, 0]
    first = res["item_loop"][1]
    assert first["actions"]["do"]["cmd"] == ["jivesync"]
    assert first["actions"]["do"]["params"] == {
        "syncWith": SCHLAFZIMMER,
        "syncWithString": "Squeezebox Radio & Schlafzimmer",
        "unsyncWith": 0,
    }
    assert first["nextWindow"] == "refresh"


def test_syncsettings_marks_the_own_group_and_offers_no_sync():
    partner = _player(mac=TAVERNE, name="Taverne")
    me = _player(sync_slaves=[TAVERNE])
    res = _req(["syncsettings", "0", "100"], me, partner)
    assert res["count"] == 3
    own = res["item_loop"][1]
    assert own["text"] == "Taverne"          # syncedWithNames(0)
    assert own["radio"] == 1
    assert own["actions"]["do"]["params"]["syncWith"] == MAC
    assert own["actions"]["do"]["params"]["unsyncWith"] == "Taverne"
    last = res["item_loop"][2]
    assert last["text"] == "No Sync"
    assert last["radio"] == 0
    assert last["actions"]["do"]["params"] == {
        "syncWith": 0, "syncWithString": 0, "unsyncWith": "Taverne"}


# ── 7. jivesync (Jive.pm:2088-2131) ───────────────────────────────────────


def test_jivesync_syncs_the_other_player_to_us():
    partner = _player(mac=TAVERNE, name="Taverne")
    api, res = _req_api(
        ["jivesync", "syncWith:" + TAVERNE, "syncWithString:Taverne",
         "unsyncWith:0"], _player(), partner)
    assert res == {}
    pm = PlayerManager()
    assert pm.get_player(MAC).sync_slaves == [TAVERNE]
    assert pm.get_player(TAVERNE).sync_master == MAC
    assert api._popup == {"jive": {
        "type": "popupplay", "text": ["Syncing with: Taverne"]}}


def test_jivesync_unsyncs_with_the_given_partner():
    partner = _player(mac=TAVERNE, name="Taverne")
    me = _player(sync_slaves=[TAVERNE])
    api, res = _req_api(
        ["jivesync", "syncWith:0", "syncWithString:0", "unsyncWith:Taverne"],
        me, partner)
    assert res == {}
    pm = PlayerManager()
    assert pm.get_player(MAC).sync_slaves == []
    assert api._popup == {"jive": {
        "type": "popupplay", "text": ["Unsyncing from: Taverne"]}}


# ── 8. sleepsettings (Jive.pm:1116-1162, 2282-2300) ───────────────────────


def test_sleepsettings_without_timer_matches_perl_live():
    res = _req(["sleepsettings", "0", "100"])
    assert res["count"] == 5
    assert res["offset"] == "0"
    assert [i["text"] for i in res["item_loop"]] == [
        "15 minutes", "30 minutes", "45 minutes", "60 minutes", "90 minutes"]
    assert [i["actions"]["go"]["cmd"] for i in res["item_loop"]] == [
        ["sleep", 900], ["sleep", 1800], ["sleep", 2700], ["sleep", 3600],
        ["sleep", 5400]]
    # Perl-Literal '1' → STRING
    assert [i["setSelectedIndex"] for i in res["item_loop"]] == ["1"] * 5
    assert all(i["nextWindow"] == "refresh" for i in res["item_loop"])
    assert all(i["actions"]["go"]["player"] == 0 for i in res["item_loop"])


def test_sleepsettings_offers_end_of_song_while_playing():
    res = _req(["sleepsettings", "0", "100"],
               _player(mode="play", duration=180.0))
    assert res["count"] == 6
    item = res["item_loop"][0]
    assert item["text"] == "Sleep at end of song"
    assert item["actions"]["go"] == {"player": 0, "cmd": ["jiveendoftracksleep"]}
    assert item["nextWindow"] == "refresh"
    assert item["setSelectedIndex"] == 1        # Zahl, Jive.pm:1147


def test_sleepsettings_shows_the_running_timer():
    res = _req(["sleepsettings", "0", "100"],
               _player(sleep_remaining=900))
    assert res["count"] == 7
    assert res["item_loop"][0] == {"text": "Sleeping in 16 minutes",
                                   "style": "itemNoAction"}
    # sleepInXHash($client, $val, 0) → SLEEP_CANCEL, cmd ['sleep', 0]
    assert res["item_loop"][1]["text"] == "Cancel sleep"
    assert res["item_loop"][1]["actions"]["go"]["cmd"] == ["sleep", 0]


# ── 9. jivepresets (Jive.pm:2490-2596) ────────────────────────────────────


def test_jivepresets_unset_slots_offer_set_preset():
    res = _req(["jivepresets", "0", "100", "key:1", "title:Y", "url:Z",
                "type:audio"])
    assert res["count"] == 10
    assert res["offset"] == 0                   # addResult-Zahl
    first = res["item_loop"][0]
    assert first["text"] == "Set Preset 1"
    assert first["nextWindow"] == "presets"
    assert first["actions"] == {"go": {
        "player": 0,
        "cmd": ["jivefavorites", "set_preset"],
        "params": {"key": "1", "favorites_url": "Z", "favorites_title": "Y",
                   "favorites_type": "audio", "parser": None},
    }}
    assert res["item_loop"][9]["text"] == "Set Preset 10"
    assert res["item_loop"][9]["actions"]["go"]["params"]["key"] == "10"


def test_jivepresets_keeps_playlist_type_and_normalises_audio():
    playlist = _req(["jivepresets", "0", "100", "title:Y", "url:Z",
                     "type:playlist"])
    assert playlist["item_loop"][0]["actions"]["go"]["params"]["favorites_type"] \
        == "playlist"
    other = _req(["jivepresets", "0", "100", "title:Y", "url:Z", "type:opml"])
    assert other["item_loop"][0]["actions"]["go"]["params"]["favorites_type"] \
        == "audio"


def test_jivepresets_without_title_or_url_has_no_result():
    # Jive.pm:2524-2527: setStatusBadDispatch → keine Antwort
    assert _req(["jivepresets", "0", "100", "url:Z"]) == {}
    assert _req(["jivepresets", "0", "100", "title:Y"]) == {}


def test_jivepresets_set_slot_becomes_a_context_menu():
    _seed_preset = {"URL": "http://old/", "text": "Alter Sender",
                    "type": "audio"}
    presets: list[Any] = [None] * 10
    presets[0] = _seed_preset
    res = _req(["jivepresets", "0", "100", "title:Y", "url:Z"],
               _player(playerprefs={"presets": presets}))
    first = res["item_loop"][0]
    assert first["count"] == 2
    assert first["offset"] == 0
    assert first["isContextMenu"] == 1
    cancel, overwrite = first["item_loop"]
    assert cancel["text"] == "Cancel"
    assert cancel["nextWindow"] == "parent"
    assert overwrite["text"] == "Replace Alter Sender?"
    assert overwrite["nextWindow"] == "presets"
    # ungesetzte Slots bleiben einfache Einträge
    assert "isContextMenu" not in res["item_loop"][1]


# ── 10. jivefavorites (Jive.pm:2601-2690) ─────────────────────────────────


def test_jivefavorites_delete_menu_matches_perl_live():
    res = _req(["jivefavorites", "delete", "title:X", "url:Y", "type:audio"])
    assert res["offset"] == 0
    assert res["count"] == 2
    cancel, action = res["item_loop"]
    assert cancel == {"text": "Cancel",
                      "actions": {"go": {"player": 0,
                                         "cmd": ["jiveblankcommand"]}},
                      "nextWindow": "parent"}
    assert action["text"] == "Delete X"
    assert action["nextWindow"] == "grandparent"
    assert action["actions"]["go"] == {
        "player": 0,
        "cmd": ["favorites", "delete"],
        "params": {"title": "X", "url": "Y", "type": "audio", "parser": None},
    }


def test_jivefavorites_add_keeps_icon_and_item_id():
    res = _req(["jivefavorites", "add", "title:Y", "url:Z", "type:audio",
                "icon:http://i", "item_id:5"])
    params = res["item_loop"][1]["actions"]["go"]["params"]
    assert res["item_loop"][1]["text"] == "Add Y"
    assert res["item_loop"][1]["actions"]["go"]["cmd"] == ["favorites", "add"]
    assert params == {"title": "Y", "url": "Z", "type": "audio",
                      "parser": None, "icon": "http://i", "item_id": "5"}


def test_jivefavorites_set_preset_stores_it_and_answers_empty():
    player = _player()
    api, res = _req_api(["jivefavorites", "set_preset",
                         "key:2", "favorites_title:Y", "favorites_url:Z",
                         "favorites_type:audio"], player)
    assert res == {}
    presets = player.playerprefs["presets"]
    # Client.pm:1323-1340: Slot 1..10 → Index 0..9, Schlüssel URL/text/type
    assert presets[1] == {"URL": "Z", "text": "Y", "type": "audio"}
    assert presets[0] is None
    assert api._popup == {"jive": {"type": "popupplay",
                                   "text": ["Saving preset #2...", "Y"]}}


def test_jivefavorites_set_preset_without_key_uses_slot_10():
    # Jive.pm:2617-2619: $preset == 0 (undef) → 10
    player = _player()
    _req_api(["jivefavorites", "set_preset", "favorites_title:Y",
              "favorites_url:Z"], player)
    assert player.playerprefs["presets"][9] == {
        "URL": "Z", "text": "Y", "type": "audio"}


def test_jivefavorites_set_preset_without_url_has_no_result():
    player = _player()
    _, res = _req_api(["jivefavorites", "set_preset", "favorites_title:Y"],
                      player)
    assert res == {}
    assert "presets" not in player.playerprefs


# ── 11. jiveplaylists (Jive.pm:2412-2457) ─────────────────────────────────


def test_jiveplaylists_delete_menu_matches_perl_live():
    res = _req(["jiveplaylists", "delete", "title:X", "url:Y",
                "playlist_id:1"])
    assert res["offset"] == 0
    assert res["count"] == 2
    assert res["item_loop"][0]["text"] == "Cancel"
    action = res["item_loop"][1]
    assert action["text"] == "Delete X"
    assert action["nextWindow"] == "grandparent"
    assert action["actions"]["go"] == {
        "player": 0,
        "cmd": ["playlists", "delete"],
        "params": {"playlist_id": "1", "title": "X", "url": "Y"},
    }


# ── 12. jiverecentsearches (Jive.pm:2768-2797) ────────────────────────────


def test_jive_recent_searches_is_empty_like_perl_live():
    res = _req(["jiverecentsearches"])
    assert res == {
        "count": "1",                    # Perl-Literal STRING
        "offset": 0,
        "item_loop": [{"text": "Empty", "style": "itemNoAction",
                       "action": "none"}],
    }


# ── 13. firmwareupgrade / extension queries ───────────────────────────────


def test_firmware_upgrade_without_a_firmware_file():
    # Firmware.pm:288-310 liefert ohne Firmware-File keinen URL; need_upgrade
    # ist dann undef → 0 (Live ohne Download-Quelle: {"firmwareUpgrade":0}).
    assert _req(["firmwareupgrade"]) == {"firmwareUpgrade": 0}
    assert _req(["firmwareupgrade", "machine:jive",
                 "firmwareVersion:7.7.3 r16676"]) == {"firmwareUpgrade": 0}


@pytest.mark.parametrize("cmd", ["jiveapplets", "jivewallpapers",
                                 "jivesounds", "jivepatches"])
def test_extension_queries_without_providers(cmd):
    # extensionsQuery (Jive.pm:2830-2885): ohne Provider nur ``count 0``
    assert _req([cmd]) == {"count": 0}
