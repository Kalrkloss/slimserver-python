"""Jive-Settings-/Menü-Kommandos wie Perl (Slim/Control/Jive.pm).

Perl-Quellen (alles ``/tmp/lms-ref``):

* Dispatch-Tabelle der Kommandos: ``Slim/Control/Jive.pm:57-162``
  (``jivetonesettings``:75-76, ``jivefixedvolumesettings``:78-79,
  ``jivestereoxl``:81-82, ``jivelineout``:84-85, ``crossfadesettings``:87-88,
  ``replaygainsettings``:90-91, ``jiveplayerbrightnesssettings``:111-112,
  ``jiveplayertextsettings``:114-115, ``jivealbumsortsettings``:117-118,
  ``jivesetalbumsort``:120-121, ``date``:132-133, ``jiveblankcommand``:159-160,
  ``jivedummycommand``:93-94).
* Handler: ``toneSettingsQuery``:1259-1292, ``fixedVolumeSettingsQuery``:1221-1256,
  ``stereoXLQuery``:1164-1190, ``lineOutQuery``:1192-1218,
  ``crossfadeSettingsQuery``:1294-1315, ``replaygainSettingsQuery``:1317-1335,
  ``transitionHash``:2302-2316, ``replayGainHash``:2318-2332,
  ``playerBrightnessMenu``:1774-1838, ``playerTextMenu``:1840-1877,
  ``albumSortSettingsMenu``:339-369, ``jiveSetAlbumSort``:329-335,
  ``dateQuery``:2136-2181, ``jiveDummyCommand``:2799-2801.
* Slicing: ``sliceAndShip`` (Jive.pm:1338-1357) + ``normalize``
  (Slim/Control/Request.pm:1805-1839).
* Client-Prefs/Defaults: ``Slim/Player/Player.pm:37-44,63`` (bass 50, treble 50,
  pitch 100, digitalVolumeControl 1), ``Squeezebox2.pm:44,47`` (transitionType 0,
  replayGainMode 0), ``Boom.pm:38,41,57-58`` (analogOutMode -1, stereoxl minXL 0,
  minAutoBrightness 2, sensAutoBrightness 10), ``Display/Display.pm:68``
  (idleBrightness 1), ``Display/Graphics.pm:41-43`` (idle 2, powerOff 1, powerOn 4),
  ``Display/Boom.pm:118-120,126-131`` (Brightness 6, Fonts *_n),
  ``Squeezebox2.pm:136-140`` (activeFont/idleFont light/standard/full, _curr 1),
  ``SqueezeboxG.pm:45-50`` (small/medium/large/huge),
  ``Slim/Utils/Prefs.pm:271`` (jivealbumsort default 'album').
* Brightness-Optionen: ``Display/Display.pm:401-438`` (getBrightnessOptions)
  + ``Display/Boom.pm:180-192`` (brightnessMap mit Umgebungs-Index).

Ground Truth (read-only Proben gegen den Perl-LMS 9.x, 192.168.1.90:9000,
jsonrpc.js, Player ``ca:c8:c7:26:6d:38`` = SqueezePlay und
``00:04:20:2b:88:c8`` = Squeezebox Radio, beide ``displaytype: none``):

* ``jivetonesettings 0 100 cmd:bass`` →
  ``{"count":1,"offset":"0","item_loop":[{"slider":1,"min":-23,"max":23,
  "adjust":24,"initial":"50","actions":{"do":{"player":0,
  "cmd":["playerpref","bass"],"params":{"valtag":"value"}}}}]}``
  (``initial`` ist ein STRING, ``offset`` ist ein STRING).
* ``jivefixedvolumesettings 0 100`` → ``{"count":1,"offset":"0","item_loop":
  [{"text":"…","checkbox":0,"actions":{"on":{…"digitalVolumeControl",0},
  "off":{…"digitalVolumeControl",1}}}]}``.
* ``jivelineout``/``crossfadesettings``/``replaygainsettings``/``jivestereoxl``:
  Radio-Loops, ``radio`` = 1 beim aktuellen Wert; ``cmd``-Werte sind bei
  crossfade/replaygain STRINGS ("0".."4"), bei lineout/stereoxl ZAHLEN,
  bei den Helligkeits-Optionen STRINGS (Perl-Hash-Keys, Jive.pm:1809).
* ``jiveplayerbrightnesssettings`` → 3 Items („While Active" etc.), je
  ``count``=5/``offset``=0/``item_loop`` mit den Optionen ABSTEIGEND
  (4 „4 (Brightest)" … 0 „0 (Dark)"); ``powerOnBrightness``/``powerOffBrightness``
  sind bei diesen Playern undef → ``radio``=1 bei Option 0 (Perl: ``undef == 0``),
  ``idleBrightness`` = 1 → ``radio``=1 bei Option 1.
* ``jiveplayerbrightnesssettings 0 0``/``jivefixedvolumesettings`` (ohne Index)
  → ``{"count":N}`` — sliceAndShip liefert dann KEIN offset/item_loop.
* ``jivelineout 1 2`` → ``{"count":4,"offset":"1", …2 Items}``;
  ``jivelineout 5 2`` / ``… 1 0`` → ``{"count":4}``;
  ``jivelineout -1 2`` → ``offset`` 0 als ZAHL.
* ``jivealbumsortsettings`` ignoriert Index/Quantity (Handler slidet nicht):
  immer ``{"count":3,"offset":0,"item_loop":[album, artflow, artistalbum]}``,
  ``radio``=1 beim Server-Pref ``jivealbumsort``.
* ``jivesetalbumsort sortMe:album``/``jiveblankcommand``/``jivedummycommand`` →
  ``{}``.
* ``date`` → ``{"date":"0000-00-00T00:00:00+00:00","date_epoch":<now>}``,
  ``date … set:1700000000`` → ``date_epoch`` als String.
* ``jivestereoxl`` und ``jiveplayertextsettings`` antworten beim Perl-LMS für
  diese Player NICHT (Handler stirbt: ``$client->stereoxl()`` existiert nur in
  ``Boom.pm:260``; ``activeFont`` existiert nur bei Grafik-Displays) —
  unser Server liefert stattdessen das Menü mit Pref-Defaults.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite
import pytest

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"

# SqueezePlay = NoDisplay (displaytype 'none') — wie der Live-Player
# ca:c8:c7:26:6d:38, an dem die Perl-Shapes verifiziert wurden.
JIVE = dict(
    mac=MAC,
    name="Küche",
    ip="192.168.1.225",
    port=48512,
    model="squeezeplay",
    model_name="SB Player",
    connected=True,
    power=True,
    bass=0,
    treble=0,
    pitch=100,
    digital_volume_control=True,
)


def _player(**kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(JIVE)
    base.update(kw)
    return PlayerState(**base)


def _pm_with(player: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {player.mac: player}
    return pm


def _req(command: list[str], player: PlayerState | None = None) -> Any:
    """``JSONRPCAPI()._slim_request(MAC, [cmd, …])`` mit Test-Player."""
    if player is not None:
        _pm_with(player)
    return asyncio.run(JSONRPCAPI()._slim_request(MAC, command))


@pytest.fixture(scope="module", autouse=True)
def _close_server_prefs():
    """Schließt den globalen Prefs-Store am Modulende.

    ``_init_server_prefs()`` hängt eine aiosqlite-Verbindung an den GLOBALEN
    Prefs-Store. Dessen Worker-Thread lebt bis zum Prozessende weiter und
    blockiert den Interpreter-Shutdown (faulthandler: ``threading._shutdown``
    wartet auf ``aiosqlite/core.py:59 _connection_worker_thread``) — pytest
    meldet „38 passed", der Prozess beendet sich aber nicht mehr.
    """
    yield
    store = get_prefs()
    db = getattr(store, "_db", None)
    if db is not None:
        try:
            asyncio.run(db.close())
        except Exception:  # noqa: BLE001 — Aufräumen darf nicht scheitern
            pass
        store._db = None


def _init_server_prefs():
    """Prefs-Store des Servers mit In-Memory-DB (Muster: tests/test_prefs.py)."""
    store = get_prefs()
    if getattr(store, "_db", None) is None:
        async def _open() -> None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
            await store._db.executescript(_PREF_DB_SCHEMA)
        asyncio.run(_open())
    return store


# ── 1. Ton-Einstellungen (toneSettingsQuery, Jive.pm:1259-1292) ────────────


def test_tone_settings_slider_shape_like_perl():
    res = _req(["jivetonesettings", "0", "100", "cmd:bass"], _player(bass=0))
    assert res == {
        "count": 1,
        "offset": "0",
        "item_loop": [{
            "slider": 1,
            "min": -23,
            "max": 23,
            "adjust": 24,
            "initial": "0",           # Jive.pm:1274 $client->$tone() → Pref-String
            "actions": {"do": {
                "player": 0,
                "cmd": ["playerpref", "bass"],
                "params": {"valtag": "value"},   # Jive.pm:1281
            }},
        }],
    }


def test_tone_settings_reads_the_requested_tone():
    # Live: cmd:treble -> initial "50" (Player.pm:63), cmd:pitch -> "100"
    treble = _req(["jivetonesettings", "0", "100", "cmd:treble"], _player(treble=50))
    pitch = _req(["jivetonesettings", "0", "100", "cmd:pitch"], _player(pitch=100))
    assert treble["item_loop"][0]["initial"] == "50"
    assert treble["item_loop"][0]["actions"]["do"]["cmd"] == ["playerpref", "treble"]
    assert pitch["item_loop"][0]["initial"] == "100"
    assert pitch["item_loop"][0]["actions"]["do"]["cmd"] == ["playerpref", "pitch"]


def test_tone_settings_without_cmd_param_has_no_result():
    # Perl: $tone ist undef -> $client->undef() stirbt -> gar keine Antwort
    # (Live-Probe: "jivetonesettings cmd:bass" beendet die Verbindung ohne
    # Antwort, weil "cmd:bass" dann als _index konsumiert wird).
    assert _req(["jivetonesettings", "0", "100"], _player()) == {}


# ── 2. Feste Lautstärke (fixedVolumeSettingsQuery, Jive.pm:1221-1256) ─────


def test_fixed_volume_checkbox_off_when_digital_volume_control_enabled():
    res = _req(["jivefixedvolumesettings", "0", "100"], _player(digital_volume_control=True))
    assert res["count"] == 1
    assert res["offset"] == "0"
    item = res["item_loop"][0]
    # Jive.pm:1231-1233: currentSetting = 1 nur wenn digitalVolumeControl == 0
    assert item["checkbox"] == 0
    assert item["actions"]["on"]["cmd"] == ["playerpref", "digitalVolumeControl", 0]
    assert item["actions"]["off"]["cmd"] == ["playerpref", "digitalVolumeControl", 1]
    assert item["actions"]["on"]["player"] == 0
    assert set(item) == {"text", "checkbox", "actions"}


def test_fixed_volume_checkbox_checked_when_digital_volume_control_disabled():
    res = _req(["jivefixedvolumesettings", "0", "100"],
               _player(digital_volume_control=False))
    assert res["item_loop"][0]["checkbox"] == 1


def test_fixed_volume_reads_the_player_pref():
    # Perl liest $prefs->client($client)->get('digitalVolumeControl')
    # (Jive.pm:1229) — unser per-Player-Pref-Store ist playerprefs.
    res = _req(["jivefixedvolumesettings", "0", "100"],
               _player(playerprefs={"digitalVolumeControl": "0"}))
    assert res["item_loop"][0]["checkbox"] == 1


# ── 3. Stereo XL (stereoXLQuery, Jive.pm:1164-1190) ───────────────────────


def test_stereo_xl_menu_matches_perl_radio_loop():
    res = _req(["jivestereoxl", "0", "100"], _player())
    assert res["count"] == 4
    assert res["offset"] == "0"
    # Jive.pm:1170 qw/ CHOICE_OFF LOW MEDIUM HIGH / (Strings: strings.txt:1153,
    # 11846, 11790, 11863)
    assert [i["text"] for i in res["item_loop"]] == ["Off", "Low", "Medium", "High"]
    # Live: radio=1 bei Index 0, wenn das Pref nicht gesetzt ist
    assert [i["radio"] for i in res["item_loop"]] == [1, 0, 0, 0]
    # Jive.pm:1179: cmd => ['playerpref','stereoxl',$i] — Zahl, kein String
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "stereoxl", 0],
        ["playerpref", "stereoxl", 1],
        ["playerpref", "stereoxl", 2],
        ["playerpref", "stereoxl", 3],
    ]
    for item in res["item_loop"]:
        assert set(item) == {"text", "radio", "actions"}
        assert item["actions"]["do"]["player"] == 0


def test_stereo_xl_radio_follows_the_player_pref():
    # Boom.pm:41 'stereoxl' => minXL() (0); gesetzt wird es über den Pref
    res = _req(["jivestereoxl", "0", "100"],
               _player(playerprefs={"stereoxl": "2"}))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 1, 0]


# ── 4. Line-Out (lineOutQuery, Jive.pm:1192-1218) ─────────────────────────


def test_line_out_menu_matches_perl_radio_loop():
    res = _req(["jivelineout", "0", "100"], _player())
    assert res["count"] == 4
    # Jive.pm:1198 (Strings: strings.txt:20213, 20205, 20230, 20247)
    assert [i["text"] for i in res["item_loop"]] == [
        "Headphones", "Subwoofer", "Always On", "Always Off",
    ]
    # Perl: analogOutMode undef -> (undef == 0) ist wahr -> radio 1 bei 0
    assert [i["radio"] for i in res["item_loop"]] == [1, 0, 0, 0]
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "analogOutMode", 0],
        ["playerpref", "analogOutMode", 1],
        ["playerpref", "analogOutMode", 2],
        ["playerpref", "analogOutMode", 3],
    ]


def test_line_out_uses_the_player_pref():
    res = _req(["jivelineout", "0", "100"],
               _player(playerprefs={"analogOutMode": "3"}))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 0, 1]


def test_line_out_boom_default_is_minus_one():
    # Boom.pm:38 'analogOutMode' => -1 — dann ist kein Radio ausgewählt
    res = _req(["jivelineout", "0", "100"], _player(model="boom"))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 0, 0]


# ── 5. Crossfade (crossfadeSettingsQuery, Jive.pm:1294-1315/2302-2316) ────


def test_crossfade_settings_matches_perl():
    res = _req(["crossfadesettings", "0", "100"], _player())
    assert res["count"] == 5
    assert res["offset"] == "0"
    assert [i["text"] for i in res["item_loop"]] == [
        "None", "Crossfade", "Fade in", "Fade out", "Fade in and out",
    ]
    assert [i["radio"] for i in res["item_loop"]] == [1, 0, 0, 0, 0]
    # Jive.pm:2311 cmd => ['playerpref','transitionType',"$thisValue"] — STRING
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "transitionType", str(i)] for i in range(5)
    ]


def test_crossfade_settings_radio_follows_the_pref():
    res = _req(["crossfadesettings", "0", "100"],
               _player(playerprefs={"transitionType": "4"}))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 0, 0, 1]


# ── 6. ReplayGain (replaygainSettingsQuery, Jive.pm:1317-1335/2318-2332) ──


def test_replaygain_settings_matches_perl():
    res = _req(["replaygainsettings", "0", "100"], _player())
    assert res["count"] == 4
    assert res["offset"] == "0"
    assert [i["text"] for i in res["item_loop"]] == [
        "No Volume Adjustment", "Track Gain", "Album Gain", "Smart Gain",
    ]
    assert [i["radio"] for i in res["item_loop"]] == [1, 0, 0, 0]
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "replayGainMode", str(i)] for i in range(4)
    ]


def test_replaygain_settings_radio_follows_the_pref():
    res = _req(["replaygainsettings", "0", "100"],
               _player(playerprefs={"replayGainMode": "2"}))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 1, 0]


# ── 7. Helligkeit (playerBrightnessMenu, Jive.pm:1774-1838) ───────────────


def test_brightness_menu_three_pref_groups():
    res = _req(["jiveplayerbrightnesssettings", "0", "100"], _player())
    assert res["count"] == 3
    assert res["offset"] == "0"
    # Jive.pm:1782-1795 (Strings: strings.txt:4468, 4486, 4504)
    assert [i["text"] for i in res["item_loop"]] == [
        "While Active", "While Off", "Idle",
    ]
    for item in res["item_loop"]:
        assert set(item) == {"text", "count", "offset", "item_loop"}
        assert item["count"] == 5
        assert item["offset"] == 0            # Hash-Literal, Zahl


def test_brightness_menu_options_descending_like_perl():
    res = _req(["jiveplayerbrightnesssettings", "0", "100"], _player())
    # Jive.pm:1803 sort { $b <=> $a } keys %$hash — absteigend 4..0
    assert [r["text"] for r in res["item_loop"][0]["item_loop"]] == [
        "4 (Brightest)", "3", "2", "1 (Dimmest)", "0 (Dark)",
    ]
    assert [r["actions"]["do"]["cmd"] for r in res["item_loop"][0]["item_loop"]] == [
        ["playerpref", "powerOnBrightness", str(n)] for n in (4, 3, 2, 1, 0)
    ]
    for r in res["item_loop"][0]["item_loop"]:
        assert set(r) == {"text", "radio", "actions"}
        assert r["actions"]["do"]["player"] == 0


def test_brightness_menu_radio_defaults_of_a_nodisplay_player():
    # Live (SqueezePlay UND Radio, beide displaytype 'none'):
    # powerOn/powerOff sind undef -> radio 1 bei Option 0; idleBrightness 1
    # (Display.pm:68) -> radio 1 bei Option 1.
    res = _req(["jiveplayerbrightnesssettings", "0", "100"], _player())
    assert [i["radio"] for i in res["item_loop"][0]["item_loop"]] == [0, 0, 0, 0, 1]
    assert [i["radio"] for i in res["item_loop"][1]["item_loop"]] == [0, 0, 0, 0, 1]
    assert [i["radio"] for i in res["item_loop"][2]["item_loop"]] == [0, 0, 0, 1, 0]


def test_brightness_menu_uses_the_client_prefs():
    res = _req(["jiveplayerbrightnesssettings", "0", "100"],
               _player(playerprefs={"powerOnBrightness": "3",
                                    "powerOffBrightness": "1",
                                    "idleBrightness": "4"}))
    assert [i["radio"] for i in res["item_loop"][0]["item_loop"]] == [0, 1, 0, 0, 0]
    assert [i["radio"] for i in res["item_loop"][1]["item_loop"]] == [0, 0, 0, 1, 0]
    assert [i["radio"] for i in res["item_loop"][2]["item_loop"]] == [1, 0, 0, 0, 0]


def test_brightness_menu_graphics_defaults():
    # Graphics.pm:41-43 (Squeezebox2/3/Transporter, displaytype graphic-320x32):
    # powerOn 4, powerOff 1, idle 2 — die Optionsliste läuft absteigend 4..0
    res = _req(["jiveplayerbrightnesssettings", "0", "100"],
               _player(model="squeezebox2"))
    assert [i["radio"] for i in res["item_loop"][0]["item_loop"]] == [1, 0, 0, 0, 0]
    assert [i["radio"] for i in res["item_loop"][1]["item_loop"]] == [0, 0, 0, 1, 0]
    assert [i["radio"] for i in res["item_loop"][2]["item_loop"]] == [0, 0, 1, 0, 0]


def test_brightness_menu_boom_has_seven_options_and_sliders():
    # Display/Boom.pm:180-192 (brightnessMap) + Jive.pm:1826-1832: Boom bekommt
    # zusätzlich Minimal-/Sensitivitäts-Slider (Jive.pm:1706-1772).
    res = _req(["jiveplayerbrightnesssettings", "0", "100"], _player(model="boom"))
    assert res["count"] == 5
    first = res["item_loop"][0]
    # Boom.pm display: idleBrightness/powerOn/powerOff 6 (Display/Boom.pm:118-120)
    assert first["count"] == 7
    # Display.pm:425-437: Eintrag 6 ist der Umgebungs-Index (nur
    # BRIGHTNESS_AMBIENT ohne Nummer), Eintrag 5 wird zum „Brightest"-Eintrag
    assert [r["text"] for r in first["item_loop"]] == [
        "Automatic", "5 (Brightest)", "4", "3", "2", "1 (Dimmest)", "0 (Dark)",
    ]
    assert [r["radio"] for r in first["item_loop"]] == [1, 0, 0, 0, 0, 0, 0]

    mab = res["item_loop"][3]
    assert mab["text"] == "Minimal Brightness (Automatic)"
    assert mab["count"] == 1
    assert mab["offset"] == 0
    mab_slider = mab["item_loop"][0]
    assert mab_slider == {
        "slider": 1, "min": 1, "max": 5, "initial": 2,   # Boom.pm:57
        "actions": {"do": {"player": 0, "cmd": ["playerpref", "minAutoBrightness"],
                           "params": {"valtag": "value"}}},
    }
    sab = res["item_loop"][4]
    assert sab["text"] == "Brightness Sensitivity (Automatic)"
    assert sab["item_loop"][0] == {
        "slider": 1, "min": 1, "max": 20, "initial": 10,  # Boom.pm:58
        "actions": {"do": {"player": 0, "cmd": ["playerpref", "sensAutoBrightness"],
                           "params": {"valtag": "value"}}},
    }


# ── 8. Schriftgrößen (playerTextMenu, Jive.pm:1840-1877) ──────────────────


def test_text_menu_for_a_graphics_player():
    # Squeezebox2.pm:136-140 activeFont [light standard full], _curr 1
    res = _req(["jiveplayertextsettings", "activeFont", "0", "100"],
               _player(model="squeezebox2"))
    assert res["count"] == 3
    assert res["offset"] == "0"
    assert [i["text"] for i in res["item_loop"]] == ["Light", "Standard", "Full"]
    assert [i["radio"] for i in res["item_loop"]] == [0, 1, 0]
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "activeFont_curr", 0],
        ["playerpref", "activeFont_curr", 1],
        ["playerpref", "activeFont_curr", 2],
    ]
    for item in res["item_loop"]:
        assert set(item) == {"text", "radio", "actions"}
        assert item["actions"]["do"]["player"] == 0


def test_text_menu_idle_font_uses_its_own_curr_pref():
    res = _req(["jiveplayertextsettings", "idleFont", "0", "100"],
               _player(model="squeezebox2", playerprefs={"idleFont_curr": "2"}))
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 1]
    assert [i["actions"]["do"]["cmd"] for i in res["item_loop"]] == [
        ["playerpref", "idleFont_curr", n] for n in (0, 1, 2)
    ]


def test_text_menu_boom_fonts_and_idle_default():
    # Display/Boom.pm:126-131 (light_n/standard_n/full_n, idleFont_curr 2)
    res = _req(["jiveplayertextsettings", "idleFont", "0", "100"],
               _player(model="boom"))
    assert [i["text"] for i in res["item_loop"]] == [
        "Light Narrow", "Standard Narrow", "Full Narrow",
    ]
    assert [i["radio"] for i in res["item_loop"]] == [0, 0, 1]


def test_text_menu_squeezeboxg_fonts():
    # SqueezeboxG.pm:45-50 (small/medium/large/huge, _curr 1)
    res = _req(["jiveplayertextsettings", "activeFont", "0", "100"],
               _player(model="squeezebox"))
    assert [i["text"] for i in res["item_loop"]] == [
        "Small", "Medium", "Large", "Huge",
    ]
    assert [i["radio"] for i in res["item_loop"]] == [0, 1, 0, 0]


def test_text_menu_without_font_prefs_has_no_result():
    # SqueezePlay = NoDisplay: kein activeFont -> Perl stirbt (Live-Probe:
    # keine Antwort) -> unser Server antwortet mit leerem Result.
    assert _req(["jiveplayertextsettings", "activeFont", "0", "100"], _player()) == {}
    assert _req(["jiveplayertextsettings", "bogusFont", "0", "100"],
                _player(model="squeezebox2")) == {}


# ── 9. Album-Sortierung (Jive.pm:339-369 / 329-335) ──────────────────────


def test_album_sort_menu_matches_perl():
    res = _req(["jivealbumsortsettings", "0", "100"], _player())
    # Jive.pm:344-348 sort keys %sortMethods -> album, artflow, artistalbum
    assert res["count"] == 3
    assert res["offset"] == 0                 # addResult('offset', 0) — Zahl
    assert [i["text"] for i in res["item_loop"]] == [
        "Album", "Artist, Year, Album", "Artist, Album",
    ]
    assert [i["actions"]["do"]["params"]["sortMe"] for i in res["item_loop"]] == [
        "album", "artflow", "artistalbum",
    ]
    # Prefs.pm:271 jivealbumsort => 'album'
    assert [i["radio"] for i in res["item_loop"]] == [1, 0, 0]
    for item in res["item_loop"]:
        assert set(item) == {"text", "radio", "actions"}
        assert item["actions"]["do"]["cmd"] == ["jivesetalbumsort"]
        assert item["actions"]["do"]["player"] == 0


def test_album_sort_menu_ignores_index_and_quantity():
    # Live: "jivealbumsortsettings 3 1" -> trotzdem count 3, offset 0, alle Items
    res = _req(["jivealbumsortsettings", "3", "1"], _player())
    assert res["count"] == 3
    assert res["offset"] == 0
    assert len(res["item_loop"]) == 3


def test_set_album_sort_stores_the_server_pref():
    # Jive.pm:332-334 $prefs->set('jivealbumsort', $sort)
    store = _init_server_prefs()
    assert _req(["jivesetalbumsort", "sortMe:artflow"], _player()) == {}
    assert store.get("jivealbumsort") == "artflow"
    res = _req(["jivealbumsortsettings", "0", "100"], _player())
    assert [i["radio"] for i in res["item_loop"]] == [0, 1, 0]


# ── 10. Slicing (sliceAndShip, Jive.pm:1338-1357 / Request.pm:1805-1839) ──


def test_slice_index_and_quantity():
    res = _req(["jivelineout", "1", "2"], _player())
    assert res["count"] == 4
    assert res["offset"] == "1"
    assert [i["text"] for i in res["item_loop"]] == ["Subwoofer", "Always On"]


def test_slice_index_beyond_the_list_answers_count_only():
    # Live: "jivelineout 5 2" -> {"count":4} (kein offset/item_loop)
    assert _req(["jivelineout", "5", "2"], _player()) == {"count": 4}
    assert _req(["jivelineout", "4", "2"], _player()) == {"count": 4}
    # quantity 0 -> normalize ist ungültig (Live: "jivelineout 1 0")
    assert _req(["jivelineout", "1", "0"], _player()) == {"count": 4}


def test_slice_negative_index_is_clamped_to_zero():
    # Perl: if ($from < 0) { $from = 0 } -> offset ist dann die ZAHL 0
    res = _req(["jivelineout", "-1", "2"], _player())
    assert res["offset"] == 0
    assert len(res["item_loop"]) == 2


def test_missing_index_answers_count_only():
    # Live: "jivefixedvolumesettings" ohne Argumente -> {"count":1}
    assert _req(["jivefixedvolumesettings"], _player()) == {"count": 1}
    assert _req(["jiveplayerbrightnesssettings"], _player()) == {"count": 3}


# ── 11. date (dateQuery, Jive.pm:2136-2181) ──────────────────────────────


def test_date_query_returns_epoch_and_zero_date():
    res = _req(["date", "0", "100"], _player())
    assert set(res) == {"date", "date_epoch"}
    assert res["date"] == "0000-00-00T00:00:00+00:00"   # Jive.pm:2173
    assert isinstance(res["date_epoch"], int)           # Jive.pm:2161 time()
    assert abs(int(res["date_epoch"]) - time.time()) < 30


def test_date_query_set_param_is_echoed():
    # Live: "date 0 100 set:1700000000" -> date_epoch "1700000000" (String)
    res = _req(["date", "0", "100", "set:1700000000"], _player())
    assert res["date_epoch"] == "1700000000"
    # set:0 ist falsy -> time() (Live-Probe)
    res0 = _req(["date", "set:0"], _player())
    assert isinstance(res0["date_epoch"], int)


def test_date_query_needs_no_player():
    # dateQuery dispatch: needClient = 0 (Jive.pm:132-133)
    res = asyncio.run(JSONRPCAPI()._slim_request("", ["date", "0", "100"]))
    assert res["date"] == "0000-00-00T00:00:00+00:00"


# ── 12. Leere Kommandos (Jive.pm:93-94, 159-160, 2799-2801) ──────────────


def test_blank_and_dummy_commands_return_an_empty_result():
    # Live: jiveblankcommand/jivedummycommand -> {}
    assert _req(["jiveblankcommand", "0", "0"], _player()) == {}
    assert _req(["jivedummycommand", "0", "100"], _player()) == {}


# ── 13. Kein Player -> kein Result (dispatch needClient = 1) ─────────────


def test_queries_without_a_player_have_no_result():
    pm = PlayerManager()
    pm.players = {}
    for cmd in ("jivetonesettings", "jivefixedvolumesettings", "jivestereoxl",
                "jivelineout", "crossfadesettings", "replaygainsettings",
                "jiveplayerbrightnesssettings", "jiveplayertextsettings",
                "jivealbumsortsettings"):
        args = ["cmd:bass"] if cmd == "jivetonesettings" else (
            ["activeFont"] if cmd == "jiveplayertextsettings" else [])
        res = asyncio.run(
            JSONRPCAPI()._slim_request(MAC, [cmd, "0", "100"] + args))
        assert res == {}, f"{cmd} ohne Player muss leer antworten"
