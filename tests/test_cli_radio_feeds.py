"""CLI: die Radio-Unterfeeds (``radios`` + ``<tag> items``) antworten wie Perl.

Symptom (Nebenbefund, reproduziert 2026-09-18): der klassische CLI-Port 9090
beantwortete ``local items 0 20 menu:local`` nicht — die Zeile kam als
Echo/status-104 zurück —, obwohl dieselbe Abfrage über JSON-RPC funktionierte.

Perl-Referenz (read-only Baum ``/tmp/lms-ref`` @ d1d0a683d, plus wörtliche
Antworten des Live-LMS 9.1.1 auf 192.168.1.90:9090, nur lesende Kommandos,
2026-09-18)::

    radios 0 20
      → radios 0 20 sort%3Aweight count%3A10 type%3Axmlbrowser cmd%3Apresets
        icon%3A%2Fplugins%2FTuneIn%2Fhtml%2Fimages%2Fradiopresets.png
        weight%3A5 name%3AEigene%20Voreinstellungen …          (kein Client-Präfix)
    local items 0 20
      → 24:0a:c4:29:77:90 local items 0 20 title%3ALokale%20Sender
        id%3A<sid>.0 name%3ASender isaudio%3A0 hasitems%3A1
        id%3A<sid>.1 name%3AAlle%20Deutschland type%3Alink isaudio%3A0
        hasitems%3A1 count%3A2
    local items 0 20 menu:local
      → <mac> local items 0 20 menu%3Alocal offset%3A0 title%3ALokale%20Sender
        base%3AHASH(0x…) … text%3ASender type%3Alink … count%3A2 window%3AHASH(0x…)
    local playlist play menu:local item_id:<sid>.0.0
      → <mac> local playlist play menu%3Alocal item_id%3A<sid>.0.0   (nur Echo)
    local 0 3            → local 0 3              (kein Dispatch → Echo)
    doctor items 0 20    → doctor items 0 20      (kein solches Plugin)
    search items 0 20    → <mac> search items 0 20 count%3A0
    podcast items 0 20   → <mac> podcast items 0 20 title%3APodcasts … count%3A3

Perl registriert die Feeds dynamisch (``Slim/Plugin/OPMLBased.pm:108-133``:
``[<tag>,'items','_index','_quantity']`` und ``[<tag>,'playlist','_method']``
je TuneIn-Verzeichniseintrag, ``:129-132`` der ``radios``-Eintrag) und leitet
alle auf ``Slim::Control::XMLBrowser::cliQuery`` — denselben Handler, den auch
die JSON-Transporte benutzen.  Das Präfix kommt aus ``Plugin/CLI/Plugin.pm:581-585``
(``needsClient`` → ``Client::clientRandom()`` = ``(clients())[0]``,
``Client.pm:411-415``), das Ausgabeformat aus ``Request.pm:2226-2296``.

Die Stationen hinter den Zeilen kommen aus radio-browser.info (die eine
genehmigte Abweichung); diese Tests mocken den HTTP-Layer und vergleichen
*Struktur* und Ausgabeform, nie die Senderdaten.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from lyrion.control import cli_commands
from lyrion.control.cli import CLIContext, CLIHandler, get_registered_commands
from lyrion.web import radiobrowser

MAC = "1C:87:2C:47:FC:36"
#: Der Client wird wie jeder Token prozent-escaped (``Stdio.pm:131-137``);
#: Live-Mitschnitt: ``24%3A0a%3Ac4%3A29%3A77%3A90 local items 0 20 …``.
MAC_ESC = "1C%3A87%3A2C%3A47%3AFC%3A36"

STATIONS = [
    {
        "stationuuid": "aaa-1", "name": "Test FM Eins",
        "url": "http://example.invalid/one.m3u",
        "url_resolved": "http://example.invalid/one",
        "favicon": "http://example.invalid/one.png", "country": "Germany",
        "countrycode": "DE", "language": "german", "tags": "pop",
        "codec": "MP3", "bitrate": 128,
    },
]

COUNTRIES = [
    {"name": "Germany", "iso_3166_1": "DE", "stationcount": 100},
]

LANGUAGES = [
    {"name": "german", "stationcount": 50},
]


async def _fake_get_json(path: str, params: dict | None = None) -> Any:
    if path.startswith("/json/stations/"):
        return [dict(s) for s in STATIONS]
    if path == "/json/countries":
        return [dict(c) for c in COUNTRIES]
    if path == "/json/languages":
        return [dict(l) for l in LANGUAGES]
    return []


@pytest.fixture(autouse=True)
def mock_http(monkeypatch):
    """Kein Netz: gefaktes radio-browser, fester Ländercode wie im Live-Test."""
    monkeypatch.setattr(radiobrowser, "_get_json", _fake_get_json)
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()


def cli(line: str, player_id: str | None = MAC) -> str:
    """Eine CLI-Zeile durch den echten Handler schicken (wie ein Socket-Client).

    ``dispatch`` parst die Zeile selbst (inkl. führendem Player-Token →
    ``ctx.request_clientid``, ``Slim/Control/Stdio.pm:96-116``), deshalb wird
    hier die rohe Zeile übergeben, nicht (cmd, args).
    """
    handler = CLIHandler()
    ctx = CLIContext(player_id=player_id)
    lines = asyncio.run(handler.dispatch(ctx, line))
    assert len(lines) == 1, lines
    return lines[0]


def sid_of(line: str) -> str:
    match = re.search(r"id%3A([0-9a-f]{8})\.0", line)
    assert match, line
    return match.group(1)


# ---------------------------------------------------------------------------
# Registrierung — Perl registriert pro TuneIn-Eintrag ein eigenes Plugin
# ---------------------------------------------------------------------------
def test_every_radio_feed_tag_is_registered():
    """``OPMLBased.pm:117-132`` — ein Dispatch je Feed-Tag, plus ``radios``.

    ``search`` ist bereits als Bibliotheks-Query registriert; Perl gewinnt den
    Feed trotzdem, weil der Dispatch-Baum Token für Token gelaufen wird
    (``Request.pm:1010-1014``) — der Zweig sitzt daher in ``cmd_search``.
    """
    assert set(cli_commands._RADIO_FEED_TAGS) | {"search"} == set(radiobrowser.RADIO_FEEDS)
    registered = get_registered_commands()
    for tag in cli_commands._RADIO_FEED_TAGS:
        assert tag in registered, tag
    assert "radios" in registered


# ---------------------------------------------------------------------------
# radios 0 20 (Platzhalter-Liste, needsClient = 0 → kein Client-Präfix)
# ---------------------------------------------------------------------------
def test_radios_plain_form_is_unprefixed_and_unrolls_radioss_loop():
    line = cli("radios 0 3")
    assert line.startswith("radios 0 3 sort%3Aweight count%3A10 "), line
    # Perl's ``cliRadiosQuery`` plain rows (``OPMLBased.pm:249-265``).
    assert "type%3Axmlbrowser" in line
    assert "cmd%3Apresets" in line
    assert "icon%3A%2Fplugins%2FTuneIn%2Fhtml%2Fimages%2Fradiopresets.png" in line
    assert "weight%3A5" in line
    assert "name%3AEigene%20Voreinstellungen" in line
    # Ein *Loop* wird inline entrollt, der Loop-Name erscheint nie
    # (``Request.pm:2264-2281``); die Menüform (base/window) fehlt hier.
    assert "radioss_loop" not in line and "HASH" not in line
    assert "base%3A" not in line and "window%3A" not in line


def test_radios_menu_form_carries_hash_refs_and_no_offset():
    """``radios … menu:radio`` → jive-Items; Perl hat hier KEIN ``offset``."""
    line = cli("radios 0 3 menu:radio")
    assert line.startswith("radios 0 3 menu%3Aradio sort%3Aweight count%3A10 "), line
    assert "text%3AEigene%20Voreinstellungen" in line
    assert "window%3AHASH%28" in line
    assert "actions%3AHASH%28" in line
    assert "icon-id%3A%2Fplugins%2FTuneIn%2Fhtml%2Fimages%2Fradiopresets.png" in line
    assert "offset%3A" not in line


# ---------------------------------------------------------------------------
# <tag> items — Flat-Form (ohne menu:) vs. Menu-Form (mit menu:)
# ---------------------------------------------------------------------------
def test_local_items_flat_form_is_prefixed_and_perl_shaped():
    """Das Symptom-Kommando, flache Form (``XMLBrowser.pm:1378-1413``)."""
    line = cli("local items 0 20")
    assert line.startswith(
        f"{MAC_ESC} local items 0 20 title%3ALokale%20Sender "), line
    # Reihen in Perls Feldreihenfolge id/name/type/image/isaudio/hasitems.
    assert ".0 name%3ASender type%3Alink isaudio%3A0 hasitems%3A1 " in line
    assert ".1 name%3AAlle%20Germany type%3Alink isaudio%3A0 hasitems%3A1 " in line
    assert line.endswith("count%3A2"), line
    assert "HASH" not in line


def test_local_items_menu_form_matches_the_live_perl_line():
    """Dasselbe mit ``menu:local`` — base/window als Referenzen (live belegt)."""
    line = cli("local items 0 20 menu:local")
    assert line.startswith(
        f"{MAC_ESC} local items 0 20 menu%3Alocal offset%3A0 "
        "title%3ALokale%20Sender base%3AHASH%28"), line
    assert "actions%3AHASH%28" in line and "text%3ASender" in line
    assert "type%3Alink" in line
    assert "count%3A2 window%3AHASH%28" in line, line
    # Reihenfolge der Hülle wie im Live-Mitschnitt: offset, title, base,
    # item_loop…, count, window (``XMLBrowser.pm:1420-1452``).
    assert line.index("offset%3A0") < line.index("title%3A") < line.index("base%3A")
    assert line.index("base%3A") < line.index("count%3A") < line.index("window%3A")


def test_station_level_rows_are_flat_xmlbrowser_rows():
    """``local items 0 4 item_id:<sid>.0`` — Senderzeilen (live belegt)."""
    sid = sid_of(cli("local items 0 20"))
    line = cli(f"local items 0 4 item_id:{sid}.0")
    assert line.startswith(
        f"{MAC_ESC} local items 0 4 item_id%3A{sid}.0 title%3ASender "), line
    # Die Senderzeile hängt an der SID *dieser* Kaskade (``XMLBrowser.pm:341-353``
    # vergibt je Ebene einen neuen Handle); Perl: ``id%3A<sid>.0.0 name%3A…
    # type%3Aaudio image%3A%2Fimageproxy%2F… isaudio%3A1 hasitems%3A0``.
    assert re.search(
        r"id%3A[0-9a-f]{8}\.0 name%3ATest%20FM%20Eins type%3Aaudio", line), line
    assert "image%3A%2Fimageproxy%2F" in line
    assert "isaudio%3A1 hasitems%3A0" in line
    assert "count%3A" in line


def test_empty_feeds_answer_perls_count_zero_without_a_row():
    """Ohne Senderdaten bleibt Perls leere Antwort: ``count:0``, keine Zeile.

    Live ``search items 0 20`` → ``count%3A0`` (der ``Leer``-Platzhalter wird
    nur in der Menüform gebaut, ``XMLBrowser.pm:841-846``); ``podcast items``
    hat bei Perl Kategoriezeilen, hier gibt es keine Daten (Abweichung).
    """
    line = cli("podcast items 0 20")
    assert line == f"{MAC_ESC} podcast items 0 20 title%3APodcasts count%3A0", line


def test_empty_feeds_menu_form_gets_the_placeholder_row():
    line = cli("podcast items 0 20 menu:podcast")
    assert "count%3A1" in line, line
    assert "style%3AitemNoAction" in line and "action%3Anone" in line
    assert "base%3AHASH%28" in line and "window%3AHASH%28" in line


# ---------------------------------------------------------------------------
# Abgrenzung, Echo-Formen, Präfix-Regeln
# ---------------------------------------------------------------------------
def test_search_items_is_the_radio_feed_not_the_library_search(monkeypatch):
    """``Request.pm:1010-1014`` — der ``items``-Zweig gewinnt über ``_index``."""
    calls: list[Any] = []

    async def fake_search(handler, ctx, args):
        calls.append(list(args))
        return ["library-search"]

    monkeypatch.setattr(cli_commands, "_search_lms", fake_search)
    line = cli("search items 0 20")
    assert line == f"{MAC_ESC} search items 0 20 count%3A0", line
    assert calls == []
    assert cli("search 0 3 term:rock") == "library-search"
    assert calls == [["0", "3", "term:rock"]]


def test_playlist_command_answers_the_bare_prefixed_echo():
    """``OPMLBased.pm:122-125`` → cliQuery → ``setStatusDone()`` ohne Ergebnis."""
    line = cli("local playlist play menu:local item_id:abcd1234.0.0")
    assert line == (
        f"{MAC_ESC} local playlist play menu%3Alocal "
        "item_id%3Aabcd1234.0.0"), line


def test_a_bare_tag_and_an_unknown_tag_are_echoed_verbatim():
    """Kein Dispatch → Status 104, Zeile unverändert (``Plugin.pm:657-663``)."""
    assert cli("local 0 3") == "local 0 3"
    assert cli("doctor items 0 20") == "doctor items 0 20"


def test_without_a_client_the_prefix_is_omitted():
    """``Plugin.pm:592`` — kein Client vorhanden → kein Präfix (Live-Fall leer)."""
    assert cli("local items 0 20", player_id=None).startswith(
        "local items 0 20 title%3ALokale%20Sender ")


def test_an_explicit_player_token_is_kept_as_the_prefix():
    """``Stdio.pm:96-116``/``:131`` — der Token aus der Zeile wird vorangestellt."""
    other = "24:0a:c4:29:77:90"
    line = cli(f"{other} local items 0 3")
    assert line.startswith(
        "24%3A0a%3Ac4%3A29%3A77%3A90 local items 0 3 "
        "title%3ALokale%20Sender"), line


# ---------------------------------------------------------------------------
# Zeilen-Abbildung (Einheit) — Perl ``XMLBrowser.pm:1378-1413``
# ---------------------------------------------------------------------------
def test_flat_row_maps_a_station_row():
    row = cli_commands._flat_row({
        "type": "audio", "text": "Test FM", "icon": "/imageproxy/x/image.png",
        "params": {"item_id": "abcd1234.0.0", "isContextMenu": 1},
        "presetParams": {"favorites_url": "http://example.invalid/one"},
    })
    assert row is not None
    assert list(row) == ["id", "name", "type", "image", "isaudio", "hasitems"]
    assert row["id"] == "abcd1234.0.0" and row["name"] == "Test FM"
    assert row["type"] == "audio" and row["isaudio"] == 1 and row["hasitems"] == 0


def test_flat_row_maps_a_link_row():
    row = cli_commands._flat_row({
        "text": "Sender", "type": "link", "addAction": "go",
        "actions": {"go": {"cmd": ["local", "items"],
                           "params": {"menu": "local", "item_id": "abcd1234.0"}}},
    })
    assert row == {"id": "abcd1234.0", "name": "Sender", "type": "link",
                   "isaudio": 0, "hasitems": 1}


def test_flat_row_drops_the_menu_only_placeholder():
    """``XMLBrowser.pm:841-846`` — der ``Leer``-Platzhalter ist Menü-only."""
    assert cli_commands._flat_row(
        {"action": "none", "style": "itemNoAction", "text": "Leer",
         "type": "text"}) is None


def test_hash_values_render_as_perl_references():
    """``Request.pm:2291`` interpoliert Refs → ``HASH(0x…)`` (Live-Beleg s. o.)."""
    from lyrion.control.queries import perl_scalar, render_line

    assert perl_scalar({"a": 1}).startswith("HASH(0x")
    assert perl_scalar(["aa", "bb"]) == "aa,bb"           # :2284-2286
    assert perl_scalar(["aa"], in_loop=True).startswith("ARRAY(0x")
    line = render_line(results=[("base", {"actions": {}})])
    assert line.startswith("base%3AHASH%28"), line
