#!/usr/bin/env python3
"""Controller-Parität: struktureller JSON-RPC-Vergleich unser-LMS <-> Perl-LMS.

Zweck
-----
Android-/iOS-Controller (Squeezer, SqueezeCtrl, Squeeze Commander, Orange
Squeeze, iPeng, Material Skin) sprechen den JSON-RPC-Endpunkt
``/jsonrpc.js``. Damit die Frage "läuft Controller X?" nicht geraten werden
muss, schickt dieses Werkzeug eine feste Liste Controller-relevanter
Kommandos an BEIDE Server und vergleicht die Antworten **strukturell**:

* Schlüsselnamen (top-level und verschachtelt)
* Schlüssel-Reihenfolge (nur top-level)
* Wert-Typen (dict/list/str/num/bool/null)
* Loop-Schlüssel (``<mode>_loop`` vs. ``loop_loop``/``item_loop``)

Datenwerte (Zahlen, MACs, UUIDs, IPs, Strings) werden normalisiert, damit der
Report Struktur-Unterschiede zeigt und nicht Datenrauschen.

Sicherheit (STRICT READ-ONLY)
-----------------------------
Es werden ausschließlich LESENDE Kommandos gesendet, und nur solche, die in
``COMMANDS`` explizit aufgeführt und in ``FORBIDDEN``/``QUERY_ONLY`` als
lesend verifiziert sind. Schreibende Formen (``power``, ``play``, ``pref``
ohne ``?``, ``rescan``, ``favorites add``, ``sync``, ``wipecache`` …) werden
**niemals** gesendet -- sie stehen ausschließlich dokumentierend im Report
(Abschnitt "Nicht gesendete Kommandos"). ``assert_safe()`` prüft jede
einzelne Kommandozeile vor dem Senden; ein Verstoß bricht ab, statt den
Produktions-LMS zu verändern.

Benutzung
---------
    .venv/bin/python3 tools/controller_parity.py
    .venv/bin/python3 tools/controller_parity.py --only artists,albums
    .venv/bin/python3 tools/controller_parity.py --limit 20
    .venv/bin/python3 tools/controller_parity.py --ours-player 1C:87:2C:47:FC:36

Optionen: ``--only <substr[,substr]>``, ``--limit <n>``, ``--out <datei>``,
``--ours`` / ``--perl`` (JSON-RPC-URLs), ``--player`` / ``--ours-player`` /
``--perl-player`` (MAC), ``--quiet``.

Stdlib only (kein venv-Paket nötig).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

OURS_DEFAULT = "http://127.0.0.1:9000/jsonrpc.js"
PERL_DEFAULT = "http://192.168.1.90:9000/jsonrpc.js"
TIMEOUT_S = 15.0
RETRIES = 1  # nur Lese-Kommandos -> Wiederholung ist gefahrlos

# --------------------------------------------------------------------------
# Kommandoliste
# --------------------------------------------------------------------------


class Cmd(NamedTuple):
    label: str          # Anzeigename (auch Ziel für --only)
    args: tuple[str, ...]  # CLI-Argumente exakt so, wie sie gesendet werden
    player: bool        # True -> in params[0] die MAC des Zielplayers
    weight: float       # 0.5 = Nische ... 1.3 = von jedem Controller benutzt
    note: str = ""      # Kommentar für den Report


def _c(label: str, args: list[str], player: bool = True, weight: float = 1.0,
       note: str = "") -> Cmd:
    return Cmd(label, tuple(args), player, weight, note)


# Reihenfolge = Abarbeitungsreihenfolge. Alle Einträge sind lesend verifiziert.
COMMANDS: list[Cmd] = [
    # --- Server / Info -----------------------------------------------------
    _c("serverstatus", ["serverstatus"], player=False, weight=1.2),
    _c("info total ?", ["info", "total", "?"], player=False, weight=1.0,
       note="Ganzzahl-Sammelabfrage"),
    _c("info total albums ?", ["info", "total", "albums", "?"], player=False, weight=1.0),
    _c("info total artists ?", ["info", "total", "artists", "?"], player=False, weight=1.0),
    _c("info total genres ?", ["info", "total", "genres", "?"], player=False, weight=0.8),
    _c("info total songs ?", ["info", "total", "songs", "?"], player=False, weight=1.0),
    _c("info total duration ?", ["info", "total", "duration", "?"], player=False, weight=0.6),
    _c("lastscan", ["lastscan"], player=False, weight=0.8),
    _c("rescanprogress", ["rescanprogress"], player=False, weight=1.0),
    _c("pref ?", ["pref", "?"], player=False, weight=0.5,
       note="Form ohne Namespace; Controllern dient sie als Existenztest"),
    _c("pref audiodir ?", ["pref", "audiodir", "?"], player=False, weight=0.6),
    _c("libraries", ["libraries"], player=False, weight=0.9,
       note="LMS-9-Bibliotheksansicht"),
    _c("works", ["works"], player=False, weight=0.5),
    _c("roles", ["roles"], player=False, weight=0.5),
    _c("getstring", ["getstring"], player=False, weight=0.7),
    _c("getstring ?", ["getstring", "?"], player=False, weight=0.5),
    _c("can ?", ["can", "?"], player=False, weight=0.6),
    _c("can play", ["can", "play"], player=False, weight=0.6,
       note="Abfrage, ob Kommando existiert"),
    _c("debug ?", ["debug", "?"], player=False, weight=0.6,
       note="nur Leseform mit '?'; Setter wird nicht gesendet"),
    # --- Playerliste / Playerinfo ------------------------------------------
    _c("players 0 99", ["players", "0", "99"], player=False, weight=1.3,
       note="Basis jeder Controller-Verbindung"),
    _c("players", ["players"], player=False, weight=1.0),
    _c("player count ?", ["player", "count", "?"], player=False, weight=1.2),
    _c("player id ?", ["player", "id", "?"], player=True, weight=1.0),
    _c("player name ?", ["player", "name", "?"], player=True, weight=1.0),
    _c("player model ?", ["player", "model", "?"], player=True, weight=0.9),
    _c("player ip ?", ["player", "ip", "?"], player=True, weight=0.7),
    # --- Status / Now-Playing ---------------------------------------------
    _c("status", ["status"], player=True, weight=1.3),
    _c("status 0 100", ["status", "0", "100"], player=True, weight=1.3),
    _c("status 0 100 tags:gald", ["status", "0", "100", "tags:gald"], player=True, weight=1.2),
    _c("status 0 100 tags:acdtu", ["status", "0", "100", "tags:acdtu"], player=True, weight=1.1),
    _c("status 0 5 tags:cdtu", ["status", "0", "5", "tags:cdtu"], player=True, weight=1.0),
    _c("currentsong", ["currentsong"], player=True, weight=1.3),
    _c("currentsong 0 100", ["currentsong", "0", "100"], player=True, weight=1.1),
    _c("songinfo 0 10", ["songinfo", "0", "10"], player=True, weight=1.0),
    _c("title ?", ["title", "?"], player=True, weight=0.8),
    _c("duration ?", ["duration", "?"], player=True, weight=0.6),
    _c("mixer ?", ["mixer", "?"], player=True, weight=0.6),
    _c("mixer volume ?", ["mixer", "volume", "?"], player=True, weight=1.2),
    _c("mixer muting ?", ["mixer", "muting", "?"], player=True, weight=0.7),
    _c("mode ?", ["mode", "?"], player=True, weight=0.8),
    _c("time ?", ["time", "?"], player=True, weight=0.6),
    _c("playlist repeat ?", ["playlist", "repeat", "?"], player=True, weight=0.8),
    _c("playlist shuffle ?", ["playlist", "shuffle", "?"], player=True, weight=0.8),
    _c("gototime ?", ["gototime", "?"], player=True, weight=0.5),
    _c("displaystatus", ["displaystatus"], player=True, weight=0.9),
    _c("playersettings", ["playerpref", "?"], player=True, weight=0.6,
       note="Playerpref-Leseform mit '?'"),
    _c("playerpref digitalVolumeControl ?",
       ["playerpref", "digitalVolumeControl", "?"], player=True, weight=0.6),
    _c("irenable ?", ["irenable", "?"], player=True, weight=0.5),
    _c("linesperscreen ?", ["linesperscreen", "?"], player=True, weight=0.6),
    _c("alarms 0 10", ["alarms", "0", "10"], player=True, weight=0.8),
    _c("syncgroups", ["syncgroups"], player=True, weight=0.7),
    _c("artworkspec", ["artworkspec"], player=True, weight=0.6),
    _c("artworkspec ?", ["artworkspec", "?"], player=True, weight=0.5),
    # --- CLI-Playlist (nur Leseformen) -------------------------------------
    _c("playlist 0 100", ["playlist", "0", "100"], player=True, weight=1.3),
    _c("playlist tracks ?", ["playlist", "tracks", "?"], player=True, weight=0.8),
    _c("playlist name ?", ["playlist", "name", "?"], player=True, weight=0.6),
    _c("playlist modified ?", ["playlist", "modified", "?"], player=True, weight=0.5),
    _c("playlist duration ?", ["playlist", "duration", "?"], player=True, weight=0.5),
    _c("playlist id ?", ["playlist", "id", "?"], player=True, weight=0.5),
    _c("playlist artist ?", ["playlist", "artist", "?"], player=True, weight=0.5),
    _c("playlist album ?", ["playlist", "album", "?"], player=True, weight=0.5),
    _c("playlist genre ?", ["playlist", "genre", "?"], player=True, weight=0.5),
    _c("playlist year ?", ["playlist", "year", "?"], player=True, weight=0.5),
    _c("playlist url ?", ["playlist", "url", "?"], player=True, weight=0.5),
    _c("playlists 0 100", ["playlists", "0", "100"], player=True, weight=0.6),
    # --- Bibliothek (moderne Queries) -------------------------------------
    _c("artists 0 10", ["artists", "0", "10"], player=True, weight=1.3),
    _c("albums 0 10", ["albums", "0", "10"], player=True, weight=1.3),
    _c("genres 0 10", ["genres", "0", "10"], player=True, weight=1.1),
    _c("years 0 10", ["years", "0", "10"], player=True, weight=1.0),
    _c("titles 0 10", ["titles", "0", "10"], player=True, weight=1.2),
    _c("tracks 0 10", ["tracks", "0", "10"], player=True, weight=0.9),
    _c("songs 0 10", ["songs", "0", "10"], player=True, weight=0.9),
    _c("artists 0 10 search:beatles", ["artists", "0", "10", "search:beatles"], player=True, weight=1.1),
    _c("artists 0 10 sort:name", ["artists", "0", "10", "sort:name"], player=True, weight=1.0),
    _c("albums 0 10 artist_id:2", ["albums", "0", "10", "artist_id:2"], player=True, weight=1.1),
    _c("albums 0 10 year:2000", ["albums", "0", "10", "year:2000"], player=True, weight=0.9),
    _c("albums 0 10 genre_id:3 sort:album",
       ["albums", "0", "10", "genre_id:3", "sort:album"], player=True, weight=1.0),
    _c("albums 0 10 tags:j", ["albums", "0", "10", "tags:j"], player=True, weight=0.9),
    _c("titles 0 10 album_id:2 tags:title",
       ["titles", "0", "10", "album_id:2", "tags:title"], player=True, weight=1.0),
    _c("songs 0 10 genre_id:3", ["songs", "0", "10", "genre_id:3"], player=True, weight=0.8),
    _c("tracks 0 10 album_id:2", ["tracks", "0", "10", "album_id:2"], player=True, weight=0.8),
    _c("genres 0 10 sort:name", ["genres", "0", "10", "sort:name"], player=True, weight=0.9),
    _c("years 0 10 sort:year", ["years", "0", "10", "sort:year"], player=True, weight=0.7),
    # --- Browse / Menü / Ordner -------------------------------------------
    _c("browse artists 0 10", ["browse", "artists", "0", "10"], player=True, weight=1.2),
    _c("browse albums 0 10", ["browse", "albums", "0", "10"], player=True, weight=1.2),
    _c("browse genres 0 10", ["browse", "genres", "0", "10"], player=True, weight=1.0),
    _c("browse years 0 10", ["browse", "years", "0", "10"], player=True, weight=0.9),
    _c("browse playlists 0 10", ["browse", "playlists", "0", "10"], player=True, weight=0.9),
    _c("browse apps 0 10", ["browse", "apps", "0", "10"], player=True, weight=0.8),
    _c("browse radios 0 10", ["browse", "radios", "0", "10"], player=True, weight=0.8),
    _c("browse artists 0 10 genre_id:3",
       ["browse", "artists", "0", "10", "genre_id:3"], player=True, weight=1.0),
    _c("browsedb 0 10 artist", ["browsedb", "0", "10", "artist"], player=True, weight=0.7),
    _c("menu", ["menu"], player=True, weight=1.1),
    _c("menu 0 10", ["menu", "0", "10"], player=True, weight=1.1),
    _c("menustatus", ["menustatus"], player=True, weight=1.0),
    _c("musicfolder 0 100", ["musicfolder", "0", "100"], player=True, weight=1.0),
    _c("folderinfo", ["folderinfo"], player=True, weight=0.7),
    _c("folders 0 10", ["folders", "0", "10"], player=True, weight=0.6),
    _c("readdirectory 0 10", ["readdirectory", "0", "10"], player=True, weight=0.6),
    # --- Favoriten / Apps / Sonstiges -------------------------------------
    _c("favorites items 0 100", ["favorites", "items", "0", "100"], player=True, weight=1.1),
    _c("favorites exists ?", ["favorites", "exists", "?"], player=True, weight=0.7),
    _c("radios 0 10", ["radios", "0", "10"], player=True, weight=0.8),
    _c("apps 0 50", ["apps", "0", "50"], player=True, weight=0.8),
]

# --------------------------------------------------------------------------
# Sicherheitsgitter
# --------------------------------------------------------------------------

# Kommandos, die den Server/Zustand verändern -- NIE senden.
FORBIDDEN_FIRST = {
    "power", "play", "pause", "stop", "playlistcontrol", "playlistcontrol",
    "set_preset", "sync", "unsync", "button", "display", "rescan",
    "wipecache", "wipe", "add", "delete", "rename", "move", "exit",
    "shutdown", "restartserver", "restartapp", "sleep", "playerpower",
}
FORBIDDEN_TOKENS = {
    "add", "delete", "rename", "move", "play", "pause", "stop", "playlist",
    "playlistcontrol", "sync", "unsync", "rescan", "wipecache", "power",
}
# Kommandos, die nur in der '?'-Leseform erlaubt sind (ohne '?' = Setter).
QUERY_ONLY = {"pref", "playerpref", "debug", "irenable", "linesperscreen",
              "mixer", "gototime", "sort", "info", "player"}
# Schreibende Unterkommandos, die NIE auftreten dürfen.
WRITE_SUBCOMMANDS = {
    "play", "pause", "stop", "playlist", "add", "delete", "remove", "insert",
    "rename", "move", "clear", "load", "save", "index", "jump", "playlistcontrol",
    "sync", "unsync", "rescan", "wipecache", "power", "sleep", "enable", "disable",
}

ALLOWED_ARGS: frozenset = frozenset(c.args for c in COMMANDS)
# Kommandos, deren 'can <name>'-Abfrage harmlos ist (reine Existenzfrage).
READ_CMDS = {"play", "pause", "stop", "seek", "volume", "up", "down"}

# Dokumentiert, aber bewusst NICHT gesendet (Form unsicher / verändernd).
NOT_SENT: list[tuple[str, str]] = [
    ("subscribe <cmd> / unsubscribe <cmd>",
     "Event-Abo; ändert den serverseitigen Client-Zustand und braucht einen "
     "streamenden Client. Abfragende Form nicht verifizierbar -> nicht gesendet."),
    ("pref <ns> <wert> / playerpref <ns> <wert>",
     "Schreibender Setter -- nicht gesendet."),
    ("debug <level> / irenable <0|1> / linesperscreen <n>",
     "Schreibende Setter (ohne '?') -- nicht gesendet."),
    ("button / display / display <text>",
     "Verändert Display/Player -- nicht gesendet."),
    ("power / play / pause / stop / playlistcontrol / playlist play",
     "Player-Steuerung -- nicht gesendet."),
    ("sync / unsync / set_preset / alarms add|delete|update",
     "Zustandsändernd -- nicht gesendet."),
    ("favorites add|delete|rename|move / rescan / wipecache",
     "Favoriten/Scan-Schreiboperationen -- nicht gesendet."),
    ("can <cmd> ohne '?'", "Nicht verifizierbare Antwortform -> siehe 'can play'."),
]


class UnsafeCommand(RuntimeError):
    pass


def assert_safe(args: tuple[str, ...]) -> None:
    """Bricht ab, bevor ein möglicherweise veränderndes Kommando gesendet wird.

    Stufe 1: das Kommando muss exakt so in ``COMMANDS`` (lesend verifiziert)
    stehen. Stufe 2: Denylist-Prüfung als zweite Verteidigungslinie.
    """
    first = args[0].lower()
    if args not in ALLOWED_ARGS:
        raise UnsafeCommand(
            f"nicht freigegeben (nicht in COMMANDS): {args}")
    if first in FORBIDDEN_FIRST:
        raise UnsafeCommand(f"verbotenes Kommando: {args}")
    # '?'-Pflicht bei Kommandos ohne lesende Basisform
    if first in QUERY_ONLY and "?" not in args:
        raise UnsafeCommand(f"nur '?'-Leseform erlaubt: {args}")
    if first == "can":
        # reine Existenzabfrage: 'can ?' oder 'can <lesendes-kommando>'
        if len(args) != 2 or (args[1] != "?" and args[1] not in READ_CMDS):
            raise UnsafeCommand(f"can nur als Leseabfrage: {args}")
        return
    for tok in args[1:]:
        t = tok.lower()
        if t == "?" or ":" in t or t.isdigit():
            continue
        if t in FORBIDDEN_TOKENS:
            raise UnsafeCommand(f"verbotenes Token '{tok}': {args}")
        if t in WRITE_SUBCOMMANDS:
            raise UnsafeCommand(f"schreibendes Unterkommando '{tok}': {args}")
    # Spezialfälle: nur die explizit lesenden Unterformen zulassen
    if first == "playlist" and len(args) > 1:
        sub = args[1].lower()
        if sub in WRITE_SUBCOMMANDS:
            raise UnsafeCommand(f"playlist-Schreibform: {args}")
    if first == "favorites" and len(args) > 1 and args[1].lower() not in ("items", "exists"):
        raise UnsafeCommand(f"favorites nur items/exists: {args}")
    if first == "alarms" and len(args) > 1 and args[1] != "0":
        raise UnsafeCommand(f"alarms nur als Leseform: {args}")
    if first == "can" and len(args) != 2:
        raise UnsafeCommand(f"can nur als 'can <?>': {args}")


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


class Answer(NamedTuple):
    url: str
    player: str
    args: tuple
    status: str            # ok | http_<code> | conn_closed | timeout | bad_json | error
    envelope: dict         # rohe Top-Level-Antwort (oder {})
    result: Any            # result-Feld (oder None)
    raw: str               # Roh-Text (gekürzt)
    seconds: float

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def call(url: str, player: str, args: tuple) -> Answer:
    body = json.dumps({"id": 1, "method": "slim.request",
                       "params": [player, list(args)]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    last: tuple[str, str] = ("error", "")
    t0 = time.time()
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", "replace")
            dt = time.time() - t0
            try:
                env = json.loads(text)
            except Exception:
                return Answer(url, player, args, "bad_json", {}, None,
                              text[:400], dt)
            return Answer(url, player, args, "ok", env, env.get("result"),
                          text[:600], dt)
        except urllib.error.HTTPError as exc:
            last = (f"http_{exc.code}", (exc.read() or b"")[:200].decode("utf-8", "replace"))
            if exc.code not in (429, 502, 503, 504):
                break
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "timed out" in reason.lower():
                last = ("timeout", reason)
            elif "closed" in reason.lower() or "reset" in reason.lower() \
                    or "disconnected" in reason.lower():
                last = ("conn_closed", reason)
            else:
                last = ("error", reason)
        except Exception as exc:  # RemoteDisconnected etc.
            name = type(exc).__name__
            if "Disconnected" in name or "closed" in str(exc).lower():
                last = ("conn_closed", name)
            else:
                last = ("error", f"{name}: {exc}")
        if attempt < RETRIES:
            time.sleep(0.4)
    return Answer(url, player, args, last[0], {}, None, last[1], time.time() - t0)


# --------------------------------------------------------------------------
# Struktur-/Shaping-Logik
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_MAC_RE = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_LONGHEX_RE = re.compile(r"\b[0-9a-fA-F]{24,}\b")


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "num" if _NUM_RE.match(v.strip()) else "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "other"


def shape(v: Any, depth: int = 0, max_keys: int = 16, max_depth: int = 3) -> str:
    """Struktur-Signatur: Schlüsselnamen, Verschachtelung, Typen (keine Daten)."""
    t = _type_name(v)
    if t in ("null", "bool", "num", "str", "other"):
        return t
    if t == "list":
        if not v:
            return "[]"
        if depth >= max_depth:
            return "[…]"
        seen: list[str] = []
        for el in v[:6]:
            s = shape(el, depth + 1, max_keys, max_depth)
            if s not in seen:
                seen.append(s)
        body = "|".join(seen)
        return f"[{body}]"
    keys = sorted(v.keys())
    if depth >= max_depth:
        return "{…%d}" % len(keys)
    shown = keys[:max_keys]
    parts = [f"{k}:{shape(v[k], depth + 1, max_keys, max_depth)}" for k in shown]
    if len(keys) > len(shown):
        parts.append("…+%d" % (len(keys) - len(shown)))
    return "{" + ", ".join(parts) + "}"


def top_level_keys(res: Any) -> list[str]:
    return list(res.keys()) if isinstance(res, dict) else []


def loop_keys(res: Any) -> list[str]:
    """Alle Loop-Schlüssel auf Top-Level (z. B. artists_loop, item_loop, loop_loop)."""
    return [k for k in top_level_keys(res)
            if k == "item_loop" or k.endswith("_loop") or k.endswith("_loops")]


def is_unsupported_echo(res: Any, args: tuple) -> bool:
    """Erkennt unsere 'unbekanntes Kommando'-Antwort: [\"<args als String>\"]."""
    if isinstance(res, list) and len(res) == 1 and isinstance(res[0], str):
        head = res[0].split(" ", 1)[0]
        return head.lower() == args[0].lower()
    if isinstance(res, list) and len(res) == 2 and res[0] is None \
            and isinstance(res[1], list) and args[0].lower() == "menustatus":
        return False
    return False


def normalize_value(v: Any) -> Any:
    """Datenwerte für den Anhang normalisieren (MAC/IP/UUID/lange Strings)."""
    if isinstance(v, str):
        s = _MAC_RE.sub("<MAC>", v)
        s = _UUID_RE.sub("<UUID>", s)
        s = _LONGHEX_RE.sub("<HEX>", s)
        s = _IP_RE.sub("<IP>", s)
        return s if len(s) <= 120 else s[:117] + "…"
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, list):
        return [normalize_value(x) for x in v[:3]] + (["…%d" % (len(v) - 3)] if len(v) > 3 else [])
    if isinstance(v, dict):
        return {k: normalize_value(x) for k, x in list(v.items())[:18]}
    return v


def diff_keys(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    sa, sb = set(a), set(b)
    return (sorted(sa - sb), sorted(sb - sa))  # nur-B, nur-A


def evaluate(ours: Answer, perl: Answer) -> tuple[str, str, list[str]]:
    """-> (bewertung, detail, tags)."""
    tags: list[str] = []
    if not perl.ok and not ours.ok:
        return "Fehler (beide)", f"{perl.status} / {ours.status}", ["beide-fehler"]
    if not perl.ok:
        return "Fehler (Perl)", f"Perl: {perl.status}", ["perl-transport"]
    if not ours.ok:
        return "Fehler (unser)", f"unser: {ours.status}", ["unser-transport"]

    ours_echo = is_unsupported_echo(ours.result, ours.args)
    perl_echo = is_unsupported_echo(perl.result, perl.args)
    if ours_echo and not perl_echo:
        return "fehlt", "Perl liefert Daten, wir spiegeln nur das Kommando", ["fehlt"]
    if perl_echo and not ours_echo:
        return "nur unser", "Perl kennt das Kommando nicht", ["nur-unser"]

    so, sp = shape(ours.result), shape(perl.result)
    if so == sp:
        if our_order(ours) != our_order(perl):
            return "abweichend", "Struktur gleich, Schlüsselreihenfolge anders", ["order"]
        return "gleich", "", []

    detail: list[str] = []
    ko = top_level_keys(ours.result)
    kp = top_level_keys(perl.result)
    only_p, only_o = diff_keys(kp, ko)
    if ours_echo:
        tags.append("fehlt")
    # Loop-Schlüssel
    lo, lp = loop_keys(ours.result), loop_keys(perl.result)
    if set(lo) != set(lp):
        tags.append("loop-key")
        detail.append("Loop-Schlüssel: Perl `{}` vs. wir `{}`".format(
            ",".join(lp) or "—", ",".join(lo) or "—"))
    if only_p:
        detail.append("fehlt bei uns: " + ", ".join(only_p[:6]))
        tags.append("missing-keys")
    if only_o:
        detail.append("nur bei uns: " + ", ".join(only_o[:6]))
        tags.append("extra-keys")
    if isinstance(ours.result, dict) and isinstance(perl.result, dict):
        if not ours.result and perl.result:
            detail.append("wir leer, Perl mit Daten")
            tags.append("leer-vs-daten")
        elif ours.result and not perl.result:
            detail.append("wir mit Daten, Perl leer")
            tags.append("daten-vs-leer")
    # Typ-Differenzen auf Top-Level
    if isinstance(ours.result, dict) and isinstance(perl.result, dict):
        tdif = [k for k in set(ko) & set(kp)
                if _type_name(ours.result[k]) != _type_name(perl.result[k])]
        if tdif:
            detail.append("Typen anders: " + ", ".join(sorted(tdif)[:6]))
            tags.append("typing")
    if our_order(ours) != our_order(perl):
        tags.append("order")
    if not detail:
        detail.append("verschachtelte Struktur weicht ab")
        tags.append("nested")
    return "abweichend", "; ".join(detail), tags


def our_order(a: Answer) -> list[str]:
    return top_level_keys(a.result)


# --------------------------------------------------------------------------
# Bewertung / Priorisierung
# --------------------------------------------------------------------------

SEVERITY = {"fehlt": 100.0, "nur unser": 45.0, "Fehler (unser)": 60.0,
            "Fehler (Perl)": 30.0, "Fehler (beide)": 30.0, "abweichend": 40.0,
            "gleich": 0.0}


def score(cmd: Cmd, verdict: str, tags: list[str]) -> float:
    s = SEVERITY.get(verdict, 0.0) * cmd.weight
    if "loop-key" in tags:
        s += 34
    if "fehlt" in tags:
        s += 25
    if "missing-keys" in tags:
        s += 12
    if "typing" in tags:
        s += 6
    if "leer-vs-daten" in tags or "daten-vs-leer" in tags:
        s += 14
    if "order" in tags:
        s += 4
    return s


# --------------------------------------------------------------------------
# Auswahl der Zielplayer
# --------------------------------------------------------------------------


def first_player(url: str) -> tuple[str, str]:
    """Wählt einen Player für die Kommandos: bevorzugt Jive-artige Clients
    (gleiche Modellklasse wie die Controller), damit Strukturvergleiche nicht
    an unterschiedlichen Player-Fähigkeiten scheitern."""
    a = call(url, "", ("players", "0", "99"))
    if not a.ok:
        return "", ""
    res = a.result or {}
    loop = res.get("players_loop") or []
    if not isinstance(loop, list) or not loop:
        return "", ""
    ranking = ["squeezeplay", "controller", "softsqueeze", "squeezelite",
               "squeezebox2", "baby", "squeezebox", "squeezebox3", "transporter",
               "fab4", "boom", "receiver"]
    connected = [p for p in loop if isinstance(p, dict) and (p.get("connected") or 0)]
    pool = connected or [p for p in loop if isinstance(p, dict)]
    for want in ranking:
        for p in pool:
            if p.get("model") == want:
                return str(p.get("playerid", "")), str(p.get("name", ""))
    pick = pool[0]
    return str(pick.get("playerid", "")), str(pick.get("name", ""))


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def trim_raw(res: Any, depth: int = 0, max_items: int = 1) -> Any:
    """Antwort für den Anhang kürzen: Schleifen auf 1 Element, Strings auf 120."""
    if isinstance(res, list):
        head = [trim_raw(x, depth + 1, max_items) for x in res[:max_items]]
        return head + ([f"…+{len(res) - max_items} weitere"] if len(res) > max_items else [])
    if isinstance(res, dict):
        out = {}
        for k, v in list(res.items())[:20]:
            if k.endswith("_loop") or k in ("item_loop", "loop_loop") or k.endswith("_loops"):
                out[k] = trim_raw(v, depth + 1, max_items)
            else:
                out[k] = trim_raw(v, depth + 1, max_items)
        if len(res) > 20:
            out["…"] = f"+{len(res) - 20} weitere Schlüssel"
        return out
    return normalize_value(res)


def fmt_cell(s: str, width: int = 110) -> str:
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def build_report(rows, ours_url, perl_url, ours_player, perl_player,
                 ours_ver, perl_ver, started, duration) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    L: list[str] = []
    L.append("# Controller-Parität: JSON-RPC-Strukturvergleich (unser LMS vs. Perl-LMS)")
    L.append("")
    L.append(f"* Datum: {started:%Y-%m-%d %H:%M:%S}")
    L.append(f"* unser Server: `{ours_url}` (Version `{ours_ver}`, Player `{ours_player}`)")
    L.append(f"* Perl-LMS: `{perl_url}` (Version `{perl_ver}`, Player `{perl_player}`)")
    L.append(f"* verglichene Kommandos: {len(rows)} (Laufzeit {duration:.1f}s)")
    L.append("* Werkzeug: `tools/controller_parity.py` (nur lesende Kommandos)")
    L.append("")
    L.append("Verglichen wird die **Struktur** der `result`-Antwort: Schlüsselnamen, "
             "Verschachtelung, Wert-Typen, Loop-Schlüssel. Zahlen und numerische "
             "Strings werden als Typ `num` gleichgesetzt; MACs/UUIDs/IPs/lange "
             "Hex-Strings sind normalisiert. Datenwerte gehen nicht in die Bewertung ein.")
    L.append("")
    L.append("## Zusammenfassung")
    L.append("")
    L.append("| Bewertung | Anzahl |")
    L.append("|---|---|")
    for k in ("gleich", "abweichend", "fehlt", "nur unser", "Fehler (unser)",
              "Fehler (Perl)", "Fehler (beide)"):
        if counts.get(k):
            L.append(f"| {k} | {counts[k]} |")
    L.append(f"| **Summe** | **{len(rows)}** |")
    L.append("")

    # Envelope-Hinweis
    env_diff = sum(1 for r in rows if r["env_ours"] != r["env_perl"])
    if env_diff:
        L.append("### Umschlag (Envelope)")
        L.append("")
        L.append("Die JSON-RPC-Hülle unterscheidet sich bei "
                 f"{env_diff}/{len(rows)} Antworten:")
        L.append("")
        L.append(f"* unser: `{fmt_cell(str(rows[0]['env_ours']), 200)}`")
        L.append(f"* Perl:  `{fmt_cell(str(rows[0]['env_perl']), 200)}`")
        L.append("* Perl echot `method`, `params` und `id` auf Top-Level und setzt "
                 "kein `jsonrpc`-Feld; wir setzen `jsonrpc:\"2.0\"`, aber echot "
                 "`method`/`params` nicht.")
        L.append("")

    L.append("## Kurzfassung der Befunde")
    L.append("")
    loop_rows_all = [r for r in rows
                     if set(r["loops_perl"]) != set(r["loops_ours"])
                     and (r["loops_perl"] or r["loops_ours"])
                     and r["verdict"] != "gleich"]
    fehlt_rows = [r for r in rows if r["verdict"] == "fehlt"]
    mk_rows = [r for r in rows if "missing-keys" in r["tags"]]
    L.append(f"* **Loop-Schlüssel abweichend ({len(loop_rows_all)}):** "
             "unsere Listen liegen bei Teilen der Bibliotheks-/Playlist-Antworten "
             "unter anderen Schlüsseln als bei Perl — siehe Abschnitt "
             "\"Loop-Schlüssel: Perl vs. wir\".")
    if fehlt_rows:
        L.append(f"* **Kommando fehlt / wird nur gespiegelt ({len(fehlt_rows)}):** "
                 + ", ".join(f"`{r['cmd']}`" for r in fehlt_rows))
    if mk_rows:
        L.append(f"* **Top-Level-Schlüssel fehlen bei uns ({len(mk_rows)}):** "
                 + ", ".join(f"`{r['cmd']}`" for r in mk_rows[:14])
                 + ("…" if len(mk_rows) > 14 else ""))
    L.append(f"* **Perl-Transportabbruch, nicht messbar:** "
             f"{sum(1 for r in rows if 'perl-transport' in r['tags'])} Kommandos "
             "(siehe Top-Abweichungen).")
    L.append("")

    # --- Loop-Schlüssel ------------------------------------------------
    loop_rows = [r for r in rows
                 if r["loops_perl"] or r["loops_ours"]
                 if set(r["loops_perl"]) != set(r["loops_ours"])]
    if loop_rows:
        L.append("## Loop-Schlüssel: Perl vs. wir")
        L.append("")
        L.append("Client-Bibliotheken lesen die Listendaten über den **Loop-Schlüssel** "
                 "aus der Antwort (`result.<key>_loop`). Weicht der Name ab, findet "
                 "der Client die Liste nicht. Nachfolgend nur die Zeilen, in denen "
                 "die Loop-Schlüssel abweichen:")
        L.append("")
        L.append("| Kommando | Perl | wir |")
        L.append("|---|---|---|")
        for r in sorted(loop_rows, key=lambda r: r["cmd"]):
            L.append("| `{}` | {} | {} |".format(
                fmt_cell(r["cmd"], 46),
                ", ".join(f"`{k}`" for k in r["loops_perl"]) or "—",
                ", ".join(f"`{k}`" for k in r["loops_ours"]) or "—"))
        L.append("")

    L.append("## Tabelle")
    L.append("")
    L.append("| Kommando | Perl-Struktur | unsere Struktur | Bewertung |")
    L.append("|---|---|---|---|")
    for r in rows:
        L.append("| `{}` | {} | {} | {} |".format(
            fmt_cell(r["cmd"], 46), fmt_cell(r["perl_shape"], 130),
            fmt_cell(r["ours_shape"], 130), r["verdict"]))
    L.append("")

    # Top-Abweichungen
    L.append("## Top-Abweichungen, priorisiert")
    L.append("")
    all_bad = [r for r in rows if r["verdict"] != "gleich"]
    unmeasurable = [r for r in all_bad if "perl-transport" in r["tags"]]
    top = sorted([r for r in all_bad if "perl-transport" not in r["tags"]],
                 key=lambda r: (-r["score"], r["cmd"]))
    L.append(f"_{len(all_bad)} Kommandos weichen ab / fehlen / sind fehlerhaft "
             f"({len(top)} bewertbar, {len(unmeasurable)} auf der Referenz nicht "
             "messbar). Priorität = Nutzungsgewicht im Controller x Schwere x "
             "Strukturmerkmal (Loop-Schlüssel, fehlende Top-Level-Schlüssel, Typen)._")
    L.append("")
    for i, r in enumerate(top[:24], 1):
        L.append(f"{i}. **`{r['cmd']}` — {r['verdict']}** (Score {r['score']:.0f}, "
                 f"Gewicht {r['weight']})")
        if r["detail"]:
            L.append(f"   * {r['detail']}")
        L.append(f"   * Perl: `{fmt_cell(r['perl_shape'], 150)}`")
        L.append(f"   * wir:  `{fmt_cell(r['ours_shape'], 150)}`")
    if unmeasurable:
        L.append("")
        L.append("### Nicht messbar: Perl bricht die Verbindung ab (Referenz-Box)")
        L.append("")
        L.append("Diese Kommandos beantwortet die Referenz ohne Antwort (leere "
                 "Antwort, reproduzierbar und playerunabhängig). Dasselbe passiert "
                 "bei `boguscmd`/`playerstatus`/`alarm ?` — die Box droppt dort wie "
                 "bei nicht dispatchbaren Kommandos, daher ist der **Perl-"
                 "Referenzwert unbekannt** und hier keine Paritätsaussage möglich "
                 "(bekannt als CTRL-20/OQ-2):")
        L.append("")
        for r in unmeasurable:
            L.append(f"* `{r['cmd']}` — wir: `{fmt_cell(r['ours_shape'], 70)}`")
        L.append("")

    # Gruppierte Ursachen
    groups: dict[str, list[str]] = {}
    for r in top:
        for t in r["tags"]:
            groups.setdefault(t, []).append(r["cmd"])
    if groups:
        L.append("")
        L.append("### Ursachen-Cluster")
        L.append("")
        naming = {"loop-key": "Loop-Schlüssel benannt anders (`<mode>_loop` vs. `loop_loop`/`item_loop`)",
                  "fehlt": "Kommando/Antwort fehlt bzw. wird nur gespiegelt",
                  "missing-keys": "Top-Level-Schlüssel fehlen bei uns",
                  "extra-keys": "zusätzliche Top-Level-Schlüssel bei uns",
                  "typing": "Wert-Typen weichen ab",
                  "leer-vs-daten": "wir liefern leer, Perl liefert Daten",
                  "daten-vs-leer": "wir liefern Daten, Perl leer (Kontext-/Playerunterschied)",
                  "perl-transport": "Perl bricht die Verbindung ab (leere Antwort) — reproduzierbar, playerunabhängig",
                  "unser-transport": "unser Server antwortet nicht korrekt",
                  "nested": "verschachtelte Struktur weicht ab"}
        for t in sorted(groups, key=lambda k: -len(groups[k])):
            L.append(f"* **{naming.get(t, t)}** ({len(groups[t])}): "
                     + ", ".join(f"`{c}`" for c in sorted(set(groups[t]))[:12]))
    L.append("")

    L.append("## Methode & Grenzen")
    L.append("")
    L.append("* **Nur lesende Kommandos.** Jede Zeile wird vor dem Senden gegen die "
             "Whitelist in `COMMANDS` und eine Denylist geprüft "
             "(`assert_safe()`); schreibende Formen (`power`, `play`, `pref` ohne "
             "`?`, `rescan`, `favorites add`, `sync`, `wipecache`, `button`, "
             "`display` …) werden nie gesendet. `--selfcheck` prüft das ohne Netzwerk.")
    L.append("* **Reproduzierbar:** erneuter Lauf überschreibt diesen Report "
             "(`--out`, `--only <substr[,substr]>`, `--limit <n>`).")
    L.append("* **Playerwahl:** je Server wird ein verbundener Jive-artiger Player "
             "gewählt (Rangliste in `first_player()`), damit Strukturvergleiche "
             "nicht an unterschiedlichen Player-Fähigkeiten scheitern; "
             "`--player`/`--ours-player`/`--perl-player` überschreiben das.")
    L.append("* **Grenzen der Referenz:**")
    L.append("  * Kommandos, deren Perl-Antwort `<conn_closed>` ist, sind auf "
             "dieser Referenz-Box **nicht messbar** (siehe Top-Abweichungen). Die "
             "Box schließt die Verbindung dort ohne Antwort — dasselbe Verhalten "
             "zeigt sie für `boguscmd`/`playerstatus`/`alarm ?`, d. h. für nicht "
             "dispatchbare Kommandos (im Repo dokumentiert als CTRL-20 in "
             "`.hermes/gap-analysis/02-control-cli-queries.md`, OQ-2).")
    L.append("  * `browse …`/`menu` liefern auf der Referenz `{}`, weil der "
             "Jive-Menükontext (vorherige `menu`-Navigation am Player) fehlt; "
             "unsere Antworten enthalten dort Daten. Für diese Zeilen ist \"nur "
             "bei uns\" **kein** Fehler, sondern ein Kontextunterschied der Sonde.")
    L.append("  * `pref ?`/`playerpref ?` **ohne Namespace** sind auf beiden "
             "Seiten Pseudowerte (Perl `_p2:null`, wir `_pref:str`) — die "
             "namensraumlose Form ist keine sinnvolle Abfrage; für Controller "
             "relevant sind `pref <ns> ?`/`playerpref <ns> ?`.")
    L.append("  * `players` **ohne** Range liefert auf der Referenz nur `{count}`, "
             "mit Range die `players_loop`.")
    L.append("")

    # Nicht gesendet
    L.append("## Nicht gesendete Kommandos (bewusst ausgelassen)")
    L.append("")
    L.append("Alle gesendeten Kommandos sind lesend. Die folgenden Controller-"
             "relevanten Formen wurden **nicht** gesendet, weil sie den Server "
             "verändern würden oder ihre Form nicht sicher verifizierbar ist:")
    L.append("")
    for name, why in NOT_SENT:
        L.append(f"* `{name}` — {why}")
    L.append("")

    # Anhang
    L.append("## Anhang: Roh-JSON je Kommando (strukturrelevant gekürzt)")
    L.append("")
    L.append("Schleifen auf 1 Element gekürzt, Strings auf 120 Zeichen, "
             "Schlüssel auf 20 je Ebene.")
    L.append("")
    for r in rows:
        L.append(f"### `{r['cmd']}` — {r['verdict']} "
                 f"({'Player ' + r['player'] if r['player'] else 'Server-Kontext'})")
        L.append("")
        if r["detail"]:
            L.append(f"_{r['detail']}_")
            L.append("")
        L.append("Perl:")
        L.append("```json")
        L.append(json.dumps(r["raw_perl"], ensure_ascii=False, indent=1,
                            default=str)[:2600])
        L.append("```")
        L.append("unser:")
        L.append("```json")
        L.append(json.dumps(r["raw_ours"], ensure_ascii=False, indent=1,
                            default=str)[:2600])
        L.append("```")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ours", default=OURS_DEFAULT, help="JSON-RPC-URL unser Server")
    ap.add_argument("--perl", default=PERL_DEFAULT, help="JSON-RPC-URL Perl-LMS")
    ap.add_argument("--player", default=None, help="MAC für beide Server")
    ap.add_argument("--ours-player", default=None)
    ap.add_argument("--perl-player", default=None)
    ap.add_argument("--only", default=None,
                    help="nur Kommandos, deren Name diese Substrings enthält "
                         "(Komma-getrennt)")
    ap.add_argument("--limit", type=int, default=None,
                    help="maximal so viele Kommandos senden")
    ap.add_argument("--out", default=None, help="Reportdatei (Default: "
                    ".hermes/gap-analysis/controller-parity-<datum>.md)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selfcheck", action="store_true",
                    help="nur die Sicherheits-Whitelist prüfen, nichts senden")
    a = ap.parse_args(argv)

    if a.selfcheck:
        bad = 0
        for c in COMMANDS:
            try:
                assert_safe(c.args)
            except UnsafeCommand as exc:
                bad += 1
                print(f"UNSAFE: {c.label}: {exc}")
        for probe in [("power", "1"), ("play",), ("pref", "audiodir", "/tmp"),
                      ("favorites", "add", "url:x"), ("playlist", "play", "3"),
                      ("rescan",), ("sync", "a", "b"), ("debug", "1")]:
            try:
                assert_safe(probe)  # type: ignore[arg-type]
                print(f"LEAK: {probe} haette blockiert werden muessen")
                bad += 1
            except UnsafeCommand:
                pass
        print(f"selfcheck: {len(COMMANDS)} Kommandos, {bad} Probleme")
        return 1 if bad else 0

    started = _dt.datetime.now()
    out = a.out or f".hermes/gap-analysis/controller-parity-{started:%Y-%m-%d}.md"

    cmds = list(COMMANDS)
    if a.only:
        needles = [n.strip().lower() for n in a.only.split(",") if n.strip()]
        cmds = [c for c in cmds if any(n in c.label.lower() or n in " ".join(c.args).lower()
                                       for n in needles)]
        if not cmds:
            print("kein Kommando passt zu --only", file=sys.stderr)
            return 2
    if a.limit is not None:
        cmds = cmds[: max(0, a.limit)]

    ours_url, perl_url = a.ours, a.perl
    ours_player = a.ours_player or a.player
    perl_player = a.perl_player or a.player
    if not ours_player:
        ours_player, ours_name = first_player(ours_url)
    if not perl_player:
        perl_player, perl_name = first_player(perl_url)

    ours_ver = str((call(ours_url, "", ("serverstatus",)).result or {}).get("version", "?"))
    perl_ver = str((call(perl_url, "", ("serverstatus",)).result or {}).get("version", "?"))

    if not a.quiet:
        print(f"unser: {ours_url} v{ours_ver} player={ours_player}")
        print(f"perl : {perl_url} v{perl_ver} player={perl_player}")
        print(f"{len(cmds)} Kommandos")

    rows = []
    t0 = time.time()
    for c in cmds:
        assert_safe(c.args)  # Sicherheit: bricht bei Schreibkommando ab
        pl = ours_player if c.player else ""
        pp = perl_player if c.player else ""
        o = call(ours_url, pl, c.args)
        p = call(perl_url, pp, c.args)
        verdict, detail, tags = evaluate(o, p)
        row = {
            "cmd": " ".join(c.args), "label": c.label, "weight": c.weight,
            "verdict": verdict, "detail": detail, "tags": tags,
            "score": score(c, verdict, tags),
            "ours_shape": shape(o.result) if o.ok else f"<{o.status}>",
            "perl_shape": shape(p.result) if p.ok else f"<{p.status}>",
            "loops_ours": loop_keys(o.result) if o.ok else [],
            "loops_perl": loop_keys(p.result) if p.ok else [],
            "ours_status": o.status, "perl_status": p.status,
            "env_ours": (json.dumps({k: _type_name(v) for k, v in o.envelope.items()})
                         if o.ok else o.status),
            "env_perl": (json.dumps({k: _type_name(v) for k, v in p.envelope.items()})
                         if p.ok else p.status),
            "raw_ours": trim_raw(o.result) if o.ok else {"__transport__": o.status},
            "raw_perl": trim_raw(p.result) if p.ok else {"__transport__": p.status},
            "player": pp or pl,
            "note": c.note,
        }
        rows.append(row)
        if not a.quiet:
            print(f"  {verdict:14} {row['cmd']}")

    duration = time.time() - t0
    report = build_report(rows, ours_url, perl_url, ours_player, perl_player,
                          ours_ver, perl_ver, started, duration)
    from pathlib import Path
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(report, encoding="utf-8")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"\nReport: {out}")
    print("Bewertung:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"gesendet an beide Server: {len(rows)} Kommandos "
          f"({sum(1 for r in rows if r['player'])} player-, "
          f"{sum(1 for r in rows if not r['player'])} serverbezogen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
