"""Android-Controller-Kompatibilität: menustatus/displaystatus/getstring/…

Diese neun Kommandos beantwortete der JSON-Pfad gar nicht (Echo der CLI-Zeile)
oder mit einer falschen Form. Referenz ist Perl allein; die Dispatch-Einträge
stehen in ``Slim/Control/Request.pm``, die Handler in ``Queries.pm``.

Live gegengeprüft (nur lesend) gegen das Perl-LMS 9.1.1, 192.168.1.90:9000,
2026-09-13 — wörtliche ``result``-Werte des JSON-RPC::

    menustatus 0 2                       -> {}
    menustatus                           -> {}
    displaystatus 0 2   (mit Client)     -> {}
    displaystatus 0 2   (fremder Client) -> <Socket ohne Body>
    getstring SETUP_CHOOSE_LANGUAGE      -> {"SETUP_CHOOSE_LANGUAGE":""}
    getstring SETUP_CHOOSE_LANGUAGE ?    -> <Socket ohne Body>
    getstring PLAY                       -> {"PLAY":"Wiedergabe"}
    getstring PLAY,BROWSE,NOSUCH_TOKEN_XYZ
        -> {"PLAY":"Wiedergabe","BROWSE":"Durchsuchen","NOSUCH_TOKEN_XYZ":""}
    getstring NOTHING_CURRENTLY_PLAYING,CHOICE_OFF,LOW,SHUFFLE_OFF,SETUP_CHOOSE_LANGUAGE
        -> {"SHUFFLE_OFF":"Wiedergabeliste nicht mischen",
            "NOTHING_CURRENTLY_PLAYING":"Keine Wiedergabe",
            "SETUP_CHOOSE_LANGUAGE":"","CHOICE_OFF":"Aus","LOW":"Niedrig"}
    readdirectory 0 5                    -> {"count":0}
    readdirectory 0 5 folder:/tmp        -> {"count":118,"fsitems_loop":
                                            [{"isfolder":"1","name":…,
                                              "path":"/tmp/…"},…]}
    readdirectory 0 ? / readdirectory ?  -> <Socket ohne Body>
    lastscan ?                           -> <Socket ohne Body>
    lastscan                             -> <Socket ohne Body>
    debug ?                              -> <Socket ohne Body>
    debug scan ?                         -> {"_value":"ERROR"}
    irenable ?                           -> {"_irenable":1}
    linesperscreen ?                     -> {"_linesperscreen":0}
    gototime ?                           -> {"_time":0}
    favorites exists                     -> {"exists":0}
    favorites exists abc                 -> {"exists":0}
    favorites exists ?                   -> <Socket ohne Body>
    favorites exists 1                   -> <Socket ohne Body>
    libraries                            -> {}
    libraries ?                          -> <Socket ohne Body>
    libraries getid                      -> {"id":0}
    works                                -> {"count":0}
    works 0 5                            -> {"count":0}
    works ?                              -> <Socket ohne Body>
    artworkspec                          -> <Socket ohne Body>
    artworkspec ?                        -> <Socket ohne Body>
    displaystatus ?                      -> <Socket ohne Body>
    serverstatus 0 50                    -> {"lastscan":"1789309710",…}

„Socket ohne Body" heißt: Perl beendet den Request ohne Result (Status 102/103/
104 — ``isNotQuery``/``isNotCommand`` Request.pm:1749-1764 oder fehlender
Funktionszeiger im Dispatch-Eintrag :1044-1084), der Socket wird geschlossen
(JSONRPC.pm:497-517). Dieser Port antwortet dann mit dem leeren Dict — dieselbe
Konvention wie bei ``mixer``/``info`` in :mod:`lyrion.web.api`.

Perl-Fundstellen je Kommando:

* ``menustatus`` — ``addDispatch(['menustatus','_data','_action'], …,
  sub { warn "menustatus query\\n" })`` ``Jive.pm:150-152``: es gibt keinen
  Query-Handler, das Result bleibt leer. Echte Menüs gehen nur als
  Notification ``['menustatus', $items, 'add', $id]`` (:1972) über cometd
  (``Slim/Web/Cometd.pm:390-492``).
* ``displaystatus`` — ``Request.pm:495`` ``[1,1,1]``, ``Queries.pm:1630-1757``:
  ohne gespeicherte Anzeige ``setStatusDone`` ohne Result (:1757), sonst
  ``type`` (:1666) + ``display``{text,duration} (:1673-1687).
* ``getstring`` — ``Request.pm:500`` ``['getstring','_tokens']`` (kein ``?``),
  ``Queries.pm:1988-2016``: je Token ein Schlüssel mit dem String bzw. ``''``.
* ``readdirectory`` — ``Request.pm:497`` ``[0,1,1]``, ``Queries.pm:3093-3213``:
  ``count`` (:3165) + ``fsitems_loop``{path,name,isfolder} (:3201-3210),
  Ordner vor Dateien (:3183-3193), Paging über ``Request::normalize``
  (Request.pm:1805-1839).
* ``lastscan`` — in ``Request.pm`` gibt es KEINEN Dispatch-Eintrag; den Wert
  liefert ``serverstatusQuery`` (``Queries.pm:3731``,
  ``Import->lastScanTime`` Import.pm:290-300). Squeezer liest ihn aus
  serverstatus (``CometClient.java:355``).
* ``debug`` — ``Request.pm:489`` ``['debug','_debugflag','?']``,
  ``Queries.pm:1517-1549``: ``{"_value": <Level>}`` (:1538-1544), ungültiges
  Flag → bad params (:1526-1535).
* ``irenable`` — ``Request.pm:507`` ``['irenable','?']``, ``Queries.pm:2058-2072``;
  Client-Default 1 (``Slim/Player/Client.pm:201``).
* ``linesperscreen`` — ``Request.pm:511``, ``Queries.pm:2100-2112``;
  ``Player::Player::linesPerScreen`` (``Slim/Player/Player.pm:168``).
* ``gototime`` — ``Request.pm:669-670`` dispatcht auf ``timeQuery``
  (``Queries.pm:4786-4800``), Ergebnisschlüssel ist ``_time``.
* ``favorites exists`` — ``Slim/Plugin/Favorites/Plugin.pm:82`` +
  ``cliExists`` :786-815; ``OpmlFavorites::findUrl`` :372-390.
* ``libraries`` — ``Request.pm:509-510``, ``Queries.pm:2074-2097``.
* ``works`` — ``Request.pm:633``, ``Queries.pm:5062-…``.
* ``artworkspec`` — nur ``['artworkspec','add','_spec','_name']``
  (``Request.pm:482``); ``Commands.pm:224-256``.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web import api as api_mod
from lyrion.web.api import JSONRPCAPI, _fs_page, _read_directory

MAC = "1C:87:2C:47:FC:36"
PLAYER = dict(
    mac=MAC,
    name="Küche",
    ip="192.168.1.225",
    port=48512,
    model="squeezeplay",
    model_name="SB Player",
    connected=True,
    power=True,
)


def _player(**kw) -> PlayerState:
    base = dict(PLAYER)
    base.update(kw)
    return PlayerState(**base)


def _pm(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in (players or (_player(),))}
    return pm


def _req(command: list, player: str = MAC) -> object:
    _pm()
    return asyncio.run(JSONRPCAPI()._slim_request(player, list(command)))


# ── menustatus (Jive.pm:150-152) ─────────────────────────────────────────


def test_menustatus_query_is_empty_like_perl():
    """Perls menustatus-Dispatch ist ein Stub → ``{}``, kein Item-Array.

    Live: ``menustatus 0 2`` → ``{"result":{}}`` (mit und ohne Client).
    """
    assert _req(["menustatus", "0", "2"]) == {}
    assert _req(["menustatus", "0", "2"], player="") == {}
    assert _req(["menustatus", "0", "2"], player="aa:bb:cc:dd:ee:ff") == {}


def test_menustatus_without_arguments_keeps_the_cometd_seed_payload():
    """Dokumentierte Abweichung: ``["menustatus"]`` bleibt das Jive-Array.

    Squeezer abonniert ``/<cid>/slim/menustatus/<mac>``; das Cometd-Layer
    führt dafür ``["menustatus"]`` erneut aus (``cometd.py:333-336``) und
    liefert das Ergebnis als Event-Daten. ``parseMenuStatus`` liest
    ``data[1]`` als Item-Array und ``data[2]`` als Direktive
    (``CometClient.java:492-505``). Perl erzeugt dieselbe Struktur über
    ``notifyFromArray(['menustatus',$items,'add',$id])`` (Jive.pm:1972).
    """
    res = _req(["menustatus"])
    assert isinstance(res, list) and len(res) == 4, res
    assert isinstance(res[1], list) and res[1], res
    assert res[2] == "add"
    assert all(isinstance(item, dict) for item in res[1]), res


# ── displaystatus (Queries.pm:1630-1757) ─────────────────────────────────


def test_displaystatus_without_popup_is_empty():
    """Ohne gespeicherte Anzeige: ``setStatusDone`` ohne Result (:1757)."""
    assert _req(["displaystatus", "0", "2"]) == {}
    assert _req(["displaystatus", "subscribe:showbriefly"]) == {}


def test_displaystatus_answers_with_type_and_display_record():
    """Perls Ergebnisschlüssel sind ``type`` und ``display`` (:1666, :1673-1687).

    Squeezer holt genau ``display`` ab: ``Util.getRecord(data, "display")``
    (``CometClient.java:475-487``) — der alte Schlüssel ``jive`` wurde nie
    gefunden.  Ohne jive-Teil (``display <line1> <line2>``,
    ``Commands.pm:444-472`` ruft ``showBriefly({line => […]})``) nimmt Perl
    ``$screen1->{'line'}`` als Text-LISTE und ergänzt ``duration``
    (``Queries.pm:1691-1695``).
    """
    _pm()
    res = asyncio.run(JSONRPCAPI()._slim_request(
        MAC, ["displaystatus", "showBriefly:Hallo Welt", "5"]))
    assert set(res) == {"type", "display"}, res
    assert res["type"] == "showbriefly"
    assert set(res["display"]) <= {"text", "duration"}
    assert res["display"]["text"] == ["Hallo Welt"]
    assert res["display"]["duration"] == 5


def test_displaystatus_publishes_the_jive_hash_untouched():
    """Ein Popup mit jive-Teil geht WÖRTLICH als ``display`` raus.

    ``Queries.pm:1683-1689`` reicht ``$parts->{'jive'}`` unverändert an
    ``addResult('display', …)`` weiter — die Popups setzen dort
    ``text => [ $string ]`` (Commands.pm:1567/:1923/:2357), also bleibt der
    Text eine LISTE (Squeezer liest ``display``, nicht ``display.text``).
    """
    api = JSONRPCAPI()
    _pm()
    jive = {"type": "popupplay", "text": ["Zeile 1", "Zeile 2"]}
    api._popup = {"jive": jive, "kind": "showbriefly"}
    api._popup_expires = 9e9
    res = asyncio.run(api._slim_request(MAC, ["displaystatus", "0", "2"]))
    assert res["type"] == "showbriefly", res
    assert res["display"] == jive, res


# ── getstring (Queries.pm:1988-2016) ─────────────────────────────────────


def test_getstring_answers_one_key_per_token():
    """Ein Schlüssel je Token (nicht ``_getstring``), Werte sind Strings."""
    res = _req(["getstring", "NOTHING_CURRENTLY_PLAYING,CHOICE_OFF,LOW"])
    assert set(res) == {"NOTHING_CURRENTLY_PLAYING", "CHOICE_OFF", "LOW"}, res
    assert all(isinstance(v, str) for v in res.values()), res
    # Der Schlüssel existiert in unseren Tabellen (strings_de.py/…_en.py):
    assert res["NOTHING_CURRENTLY_PLAYING"] != ""


def test_getstring_unknown_token_is_an_empty_string():
    """Perl gibt für ein unbekanntes Token ``''`` (:2009-2012); live:
    ``getstring SETUP_CHOOSE_LANGUAGE`` → ``{"SETUP_CHOOSE_LANGUAGE":""}``."""
    res = _req(["getstring", "SETUP_CHOOSE_LANGUAGE,NOSUCH_TOKEN_XYZ"])
    assert res == {"SETUP_CHOOSE_LANGUAGE": "", "NOSUCH_TOKEN_XYZ": ""}, res


def test_getstring_needs_exactly_one_token_argument():
    """``['getstring','_tokens']`` (Request.pm:500) ist keine ``?``-Query:
    ``getstring`` allein, ``getstring TOKEN ?`` und ein leeres Token sind
    nicht dispatchbar (live: Socket ohne Body)."""
    assert _req(["getstring"]) == {}
    assert _req(["getstring", "SETUP_CHOOSE_LANGUAGE", "?"]) == {}
    assert _req(["getstring", "?"]) == {}


# ── readdirectory (Queries.pm:3093-3213) ─────────────────────────────────


def test_readdirectory_without_folder_counts_zero():
    """Live: ``readdirectory 0 5`` → ``{"count":0}`` — genau ein Schlüssel."""
    res = _req(["readdirectory", "0", "5"])
    assert res == {"count": 0}, res


def test_readdirectory_lists_folder_files_and_dirs(tmp_path):
    """``fsitems_loop`` trägt ``path``/``name``/``isfolder`` (:3201-3210),
    Ordner stehen vor Dateien (:3183-3193)."""
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "x.mp3").write_bytes(b"")
    res = _req(["readdirectory", "0", "10", f"folder:{tmp_path}"])
    assert res["count"] == 3, res
    names = [i["name"] for i in res["fsitems_loop"]]
    assert names == ["a_dir", "b_dir", "x.mp3"], names
    for item in res["fsitems_loop"]:
        assert set(item) == {"path", "name", "isfolder"}, item
        assert item["path"] == os.path.join(str(tmp_path), item["name"])
    assert [i["isfolder"] for i in res["fsitems_loop"]] == [1, 1, 0]


def test_readdirectory_filters_and_pages(tmp_path):
    """``filter:filesonly``/``filetype:`` und Paging über ``normalize``
    (Request.pm:1805-1839)."""
    (tmp_path / "d").mkdir()
    for f in ("a.mp3", "b.mp3", "c.flac"):
        (tmp_path / f).write_bytes(b"")
    files = _req(["readdirectory", "0", "10", f"folder:{tmp_path}",
                  "filter:filesonly"])
    assert files["count"] == 3, files
    assert all(i["isfolder"] == 0 for i in files["fsitems_loop"]), files
    mp3 = _req(["readdirectory", "0", "10", f"folder:{tmp_path}",
                "filter:filetype:mp3"])
    # Perl behält bei ``filetype:`` Ordner mit (:3142-3146), Filter greift nur
    # auf Dateien — Ordner stehen weiter vorn (:3183-3193).
    assert [i["name"] for i in mp3["fsitems_loop"]] == ["d", "a.mp3", "b.mp3"], mp3
    # index > count-1 → nicht valide, nur count
    assert _req(["readdirectory", "99", "2", f"folder:{tmp_path}"])["count"] == 4
    assert "fsitems_loop" not in _req(["readdirectory", "99", "2",
                                       f"folder:{tmp_path}"])
    # quantity 0 → nicht valide
    assert "fsitems_loop" not in _req(["readdirectory", "0", "0",
                                       f"folder:{tmp_path}"])
    page = _req(["readdirectory", "1", "1", f"folder:{tmp_path}"])
    assert len(page["fsitems_loop"]) == 1, page


def test_fs_page_mirrors_perl_normalize():
    """Randfälle von ``Request::normalize`` (Request.pm:1805-1839)."""
    assert _fs_page(0, 5, 10) == (0, 4)
    assert _fs_page(8, 5, 10) == (8, 9)          # end wird auf count-1 gekappt
    assert _fs_page(10, 5, 10) is None           # from > lastidx
    assert _fs_page(-3, 5, 10) == (0, 4)         # negative from → 0
    assert _fs_page(0, 0, 10) is None            # quantity 0
    assert _fs_page(0, 5, 0) is None             # leere Liste


def test_question_mark_slot_is_missing_for_slot0_commands(tmp_path):
    """Endet eine Anfrage auf ``?``, greift Perls Query-Slot
    (Request.pm:1011-1024). Für ``['favorites','exists','_id']``
    (Plugin.pm:82), ``['readdirectory','_index','_quantity']`` (:497) und
    ``['works','_index','_quantity']`` (:633) existiert er nicht → live
    Socket ohne Body, also ``{}``."""
    (tmp_path / "x").write_bytes(b"")
    assert _req(["favorites", "exists", "?"]) == {}
    assert _req(["readdirectory", "0", "?"]) == {}
    assert _req(["readdirectory", "?"]) == {}
    assert _req(["readdirectory", "0", "5", f"folder:{tmp_path}", "?"]) == {}
    assert _req(["works", "?"]) == {}
    assert _req(["works"]) == {"count": 0}
    assert _req(["displaystatus", "?"]) == {}


def test_read_directory_helper_ignores_missing_folder():
    assert _read_directory(["0", "5"]) == {"count": 0}
    assert _read_directory(["0", "5", "folder:/pfad/den/es/nicht/gibt"]) == \
        {"count": 0}


# ── lastscan (kein Perl-Dispatch) ────────────────────────────────────────


def test_lastscan_is_not_a_command():
    """``lastscan`` steht nicht in Perls Dispatch-Tabelle; live schließt Perl
    den Socket — die JSON-Form bleibt leer, statt ``_lastscan`` zu erfinden."""
    assert _req(["lastscan", "?"]) == {}
    assert _req(["lastscan"]) == {}


def test_serverstatus_carries_lastscan_as_number(monkeypatch):
    """Den Wert holen Controller aus ``serverstatus`` (Queries.pm:3731,
    Squeezer ``CometClient.java:355`` ``Util.getLong(data,"lastscan")``)."""
    monkeypatch.setattr(api_mod, "_db_query",
                        lambda sql, params=(): [{"t": "2026-09-13 13:58:09.830837"}])
    res = _req(["serverstatus", "0", "50"], player="")
    assert isinstance(res["lastscan"], int), res
    assert res["lastscan"] == 1789307889, res["lastscan"]


def test_serverstatus_lastscan_is_zero_without_library(monkeypatch):
    monkeypatch.setattr(api_mod, "_db_query", lambda sql, params=(): [])
    assert _req(["serverstatus", "0", "50"], player="")["lastscan"] == 0


# ── debug / irenable / linesperscreen / gototime ─────────────────────────


def test_debug_without_category_is_not_dispatchable():
    """``debug ?`` → ``_debugflag`` wäre ``'?'`` → bad params (Queries.pm:1526)."""
    assert _req(["debug", "?"]) == {}
    assert _req(["debug"]) == {}


def test_debug_with_category_answers_value():
    """Live: ``debug scan ?`` → ``{"_value":"ERROR"}`` (Queries.pm:1538-1544)."""
    res = _req(["debug", "scan", "?"])
    assert list(res) == ["_value"], res
    assert isinstance(res["_value"], str) and res["_value"], res
    assert res == {"_value": "ERROR"}, res


def test_irenable_is_a_number():
    """Live: ``{"_irenable":1}`` — Client-Default 1 (Client.pm:201)."""
    res = _req(["irenable", "?"])
    assert list(res) == ["_irenable"], res
    assert isinstance(res["_irenable"], int), res
    assert res["_irenable"] == 1


def test_irenable_without_client_closes_like_perl():
    """needClient=1 (Request.pm:507) → ohne Client ist der Aufruf nicht
    dispatchbar (live: Socket ohne Body)."""
    assert _req(["irenable", "?"], player="") == {}


def test_linesperscreen_is_a_number():
    """Live: ``{"_linesperscreen":0}`` für alle angebundenen Spieler."""
    res = _req(["linesperscreen", "?"])
    assert list(res) == ["_linesperscreen"], res
    assert isinstance(res["_linesperscreen"], int), res
    assert _req(["linesperscreen", "?"], player="") == {}


def test_gototime_answers_the_time_key():
    """``gototime`` dispatcht auf ``timeQuery`` (Request.pm:669-670) → der
    Schlüssel ist ``_time`` (Queries.pm:4786-4800), NICHT ``_gototime``."""
    res = _req(["gototime", "?"])
    assert list(res) == ["_time"], res
    assert isinstance(res["_time"], int), res
    assert _req(["gototime", "?"], player="") == {}


# ── favorites exists / libraries / works / artworkspec ───────────────────


def test_favorites_exists_answers_the_exists_key():
    """Live: ``favorites exists`` → ``{"exists":0}`` (Plugin.pm:786-815)."""
    res = _req(["favorites", "exists"])
    assert "exists" in res and res["exists"] in (0, 1), res
    res2 = _req(["favorites", "exists", "abc"])
    assert res2 == {"exists": 0}, res2


def test_favorites_exists_with_unknown_db_id_is_not_dispatchable(monkeypatch):
    """Eine numerische, unbekannte Track-ID ist bad params (Plugin.pm:795-799)
    → live Socket ohne Body."""
    monkeypatch.setattr(api_mod, "_db_query", lambda sql, params=(): [])
    assert _req(["favorites", "exists", "1"]) == {}


def test_libraries_is_empty_or_getid():
    """Live: ``libraries`` → ``{}``; ``libraries getid`` → ``{"id":0}``
    (Queries.pm:2074-2097)."""
    assert _req(["libraries"], player="") == {}
    res = _req(["libraries", "getid"])
    assert list(res) == ["id"], res
    assert isinstance(res["id"], int), res
    # ``libraries ?`` und ``getid`` ohne Client sind nicht dispatchbar.
    assert _req(["libraries", "?"]) == {}
    assert _req(["libraries", "getid"], player="") == {}


def test_works_answers_count_only():
    """Live: ``works 0 5`` → ``{"count":0}`` (Queries.pm:5062-…)."""
    assert _req(["works", "0", "5"]) == {"count": 0}


def test_artworkspec_has_no_query_form():
    """Nur ``artworkspec add _spec _name`` ist dispatchbar (Request.pm:482);
    live schließt Perl hier den Socket."""
    assert _req(["artworkspec"]) == {}
    assert _req(["artworkspec", "album"]) == {}


# ── Kein CLI-Echo mehr ───────────────────────────────────────────────────


def test_controller_commands_no_longer_echo_the_cli_line():
    """Vorher kam die Textzeile der CLI zurück (``["readdirectory 0 5"]``) —
    Apps casten die Antwort und brechen auf einer Liste."""
    for command in (
        ["readdirectory", "0", "5"],
        ["getstring", "SETUP_CHOOSE_LANGUAGE"],
        ["favorites", "exists"],
        ["libraries"],
        ["works", "0", "5"],
        ["artworkspec"],
        ["lastscan", "?"],
    ):
        res = _req(command)
        assert not isinstance(res, list), (command, res)
        assert not any(isinstance(v, str) and v.startswith("unknown command")
                       for v in (res.values() if isinstance(res, dict) else ())), \
            (command, res)
