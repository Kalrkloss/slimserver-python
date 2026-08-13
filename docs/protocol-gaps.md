# Protokoll-Gap-Analyse — Python-LMS vs. PROTOCOL.md

Diese Datei listet alle **Fehler** (weicht vom LMS-Verhalten ab) und
**Auslassungen** (fehlt ganz) des Python-Ports gegenüber der Spezifikation
in [`PROTOCOL.md`](../PROTOCOL.md). Jeder Eintrag nennt Datei/Stelle und den
Schweregrad: 🔴 kritisch · 🟠 mittel · 🟡 gering.

Stand der Analyse: 2026-08-13, Code-Basis Commit `ac346b8`.

---

## 1. SlimProto (TCP 3483) — Kapitel 2 der Spec

### 1.1 🔴 Binärer HELO-Pfad sendet kein `audg` (Gain-0-Stille)

`networking/protocol.py` `_handle_player` (binärer Zweig, Z. ~868–941): Nach dem
binären HELO (SqueezePlay/Jive, `first_byte != 'H'`) wird nur `vers` + `setd`
gesendet — **kein** `audg`-Frame. Der Squeezelite/Text-HELO-Pfad (Z. 791–798)
macht das korrekt. Squeezelite initialisiert `output.gainL/R = 0`; ohne `audg`
wird jede Amplitude mit 0 multipliziert → Decoder läuft, Output bleibt stumm.
Alle Player, die über den binären HELO-Pfad ankommen, sind davon betroffen.

### 1.2 🟠 Binärer HELO-ACK unvollständig

`protocol.py` Z. 894–899: Der ACK sendet nur `pack("<BHI", 0, 8192, 2)`
(num_ext + buffer_size + max_channels). Das `supported_commands`-u32-Bitfield
und die Extension-Tuples (aus dem `HelloAck`-Docstring Z. 58–68) fehlen.
Strikte Player, die `supported_commands` auswerten, sehen 0.

### 1.3 🟠 STAT-Events unvollständig behandelt

`protocol.py` `_handle_stat_frame` (Z. 1670–1760) behandelt nur `STMd`
(auto-next), `STMs`, sowie `pause`/`stop`/`play`/`load` (SqueezePlay-Only).
Fehlen: `STMf` (Stop/Flush-Quittung → sollte `mode=stop` setzen),
`STMp`/`STMr` (Pause/Resume-Quittung), `STMn` (Decode-Fehler → loggen),
`STMo` (Underrun), `STMt` (Timer/Heartbeat). Spec-Kapitel 2.5.

### 1.4 🟡 `aude`-Frame fehlt

Spec-Kapitel 2.4 (`aude` = Audio-Ausgänge enable/disable) ist nirgends
implementiert (kein `send_aude`). Gering, da Squeezelite das selten braucht.

### 1.5 🟡 Display-/Misc-Frames fehlen

`grfb` (Helligkeit), `grfe` (Bitmap), `serv` (Server-Wechsel), `visu`
(Visualizer), `vfdc` sind nicht implementiert. Nur für Squeezebox-Hardware-
Displays relevant; Software-Player ignorieren sie.

---

## 2. Discovery (UDP 3483) — Kapitel 3

### 2.1 ✅ Abgedeckt

`networking/discovery.py` antwortet auf `d`-Request (`D`+17B-Hostname),
`e`-TLV (`E`+NAME/IPAD/JSON/VERS/UUID) und SSDP M-SEARCH. Keine bekannten
Lücken gegenüber der Spec.

---

## 3. CLI (TCP 9090) — Kapitel 4

### 3.1 🔴 Kein percent-decoding der Parameter

`control/cli.py` `_parse_request` (Z. 236–241) nutzt `shlex.split` — Parameter
werden **nicht** percent-decoded. Die LMS-CLI verlangt URL-Stil-Escaping
(`The%20Clash` → `The Clash`). Ein Client, der korrekt escaped sendet, bekommt
das Literal `The%20Clash`. Betrifft Suchbegriffe, Namen, URLs mit Sonderzeichen.

### 3.2 🔴 Falsche Zeilen-Terminierung (blank-line statt zeilenorientiert)

`control/cli.py` `read_commands` (Z. 210–234) puffert Zeilen und dispatched
erst nach einer **Leerzeile** (`REQUEST_END = b"\n\n"`). Die LMS-CLI ist
zeilenorientiert: **ein Kommando pro Zeile, Antwort sofort nach der Zeile**
(Spec 4.1: LF/CR/0x00 als Zeilenende). Ein Client, der `players 0 2\n` ohne
abschließende Leerzeile sendet (`printf … | nc`), erhält **nie** eine Antwort.
Standard-CLI-Clients (nc, Home-Assistant, Crestron) brechen damit.

### 3.3 🟠 `search`-Kommando im falschen Format

`cli_commands.py` `cmd_search` (Z. 763–831) implementiert
`search <type> <query> [offset limit]`. Das LMS-Format (Spec 9.3) ist
`search <start> <count> term:<begriff>` mit gruppierter Antwort
(`artists_count`/`albums_count`/`tracks_count` + `artist_id`/`album_id`/
`track_id`). Völlig abweichend.

### 3.4 🟠 Browse-Queries im falschen Antwortformat + ohne Filter

`cmd_artists`/`cmd_albums`/`cmd_songs`/`cmd_genres` (Z. 1068–1174) liefern
`[Anzahl, "offset limit", "id:X", "artist:Y", …]`. Das LMS-Format (Spec 4.2)
ist ein getaggter Echo-Header: `artists 0 5 count:7 id:2 artist:Anastacia …`.
Es fehlen alle Filter außer `search:` — insbesondere `genre_id`, `album_id`,
`track_id`, `artist_id`, `year`, `tags`, `library_id`.

### 3.5 🟠 `info total …` im falschen Format

`cmd_info` (Z. 981–1013) liefert `info total duration: 123` (Doppelpunkt),
die Spec will `info total duration 123` und die Einzel-Queries
`info total genres ?` / `info total artists ?` / `info total albums ?` /
`info total songs ?` separat. Aktuell nur über das kombinierte `info`.

### 3.6 🟠 `serverstatus` (CLI) ohne `players_loop` und `subscribe`

`cmd_serverstatus` (Z. 116–140) liefert nur Text (version/uuid/name/info total
duration/player count). Spec 10.1: `serverstatus` muss die Player-Liste
(`players_loop`) und den `subscribe:`-Tag liefern. (Der JSON-RPC-Pfad in
`api.py` liefert players_loop korrekt — nur die CLI-Variante nicht.)

### 3.7 🟠 `favorites` (CLI) positional statt getaggt

`cmd_favorites` (Z. 1395–1569) nutzt **positionale** Argumente
(`favorites add <url> <title> <parent>`), die Spec (Kapitel 8) verlangt
**Tagged** (`favorites add url:… title:… item_id:…`). Dazu: flache Integer-IDs
statt der hierarchischen Punkt-Notation (`2.0.9.3`), kein `favorites exists`,
kein `favorites playlist <play|load|insert|add>`, kein `want_url`/`feedMode`/
`search`-Tag.

### 3.8 🟠 `status` (CLI) ohne Tags und `playlist_loop`

`cmd_status` (Z. 437–479) liefert einen festen Satz Zeilen
(`player_name`, `mode`, …) ohne `tags:`-Unterstützung, ohne `playlist_loop`
und mit gemapptem Mode (`mode: playing` statt `mode: play`). Spec 10.2.

### 3.10 🟠 Fehlende CLI-Kommandos (Spec-Kapitel 4/8/9/10)

Nicht registriert in `cli_commands.py` (gegen die Spec-Kommandoliste):

- `name` (Player-Name setzen/abfragen)
- `player count ?` / `player id ?` / `player name ?` / `player model ?`
  (`player` setzt nur die Default-ID, Z. 201–216)
- `sync` / `unsync` / `syncgroups`
- `playerpref` / `pref validate`
- `mixer bass` / `mixer treble` / `mixer pitch` / `mixer muting`
- `songinfo`
- `titles` (Alias von `songs`)
- `years`, `musicfolder`/`mediafolder`, `roles`, `works`, `libraries`,
  `libraries getid`
- `rescanprogress`, `abortscan`
- `playlists tracks` / `playlists new` / `playlists delete` /
  `playlists edit` / `playlists rename`
- `getstring`, `fulltextsearch`
- `displaystatus`, `menustatus`, `playerstatus`, `menu` (nur über JSON-RPC
  in `api.py` vorhanden, nicht über Telnet-CLI)

---

## 4. JSON-RPC (`/jsonrpc.js`) — Kapitel 5

### 4.1 ✅ Abgedeckt

`slim.request`-Dispatch, `None`/`"1.0"`/`"2.0"`-Toleranz, `result`-Objekt.
Batch-Requests (Array) sind im `handle_request` vorhanden.

---

## 5. Cometd/Bayeux — Kapitel 6

### 5.1 ✅ Abgedeckt

`web/cometd.py` behandelt `/meta/handshake`, `/meta/subscribe` +
`/slim/subscribe` (mit `data.response`/`data.subscription`/Top-Level-
`subscription`), `/slim/request`, `/meta/unsubscribe`. clientId stabil aus
UUID. `web/app.py` akzeptiert `/cometd` + `/cometd/*`; Streaming + Long-Poll
als getrennte Pfade.

### 5.2 🟠 `/meta/disconnect` nicht behandelt

`cometd.py` `handle_messages` hat keinen `/meta/disconnect`-Zweig (fällt in
`else` → `successful: True`, aber der Client wird **nicht** aus
`self._clients` entfernt). Verwaiste Clients sammeln sich bei Reconnects.

### 5.3 🟡 Kein `/meta/ping`

Fällt in `else` → `successful: True` (akzeptabel), aber ohne echtes Ping-
Handling/Advice.

---

## 6. Menüs (SlimBrowse) — Kapitel 7

### 6.1 🔴 `browse`-Kommando nicht implementiert → Menü-Navigation tot

`api.py` `_home_menu` (Z. 846–876) setzt `actions.go/do = {"cmd": ["browse",
<id>]}`. Das `browse`-Kommando ist zwar in der Browse-Liste (Z. 677–679)
aufgeführt, aber `_json_browse` (Z. 1069–1111) behandelt **nur**
`artists`/`albums`/`songs`/`titles`/`genres` — `browse` fällt in den `else`
→ `{"count": 0, "loop_loop": []}`. Ein Klick auf „Artists"/„Favorites" im
Controller-Menü liefert eine leere Liste → Navigation funktioniert nicht.

### 6.2 🟠 `musicfolder`/`songinfo`/`radios`/`playlists`/`info`/`contributors`
liefern leer

In `_json_browse` (Z. 677) aufgelistet, aber im else-Zweig (Z. 1105–1111)
leer. Kein `musicfolder`-Browse, kein `songinfo`.

### 6.3 🟠 Menü-Items ohne `nextWindow`/`window`/`input`

`_home_item` (Z. 849–867) liefert `text`/`node`/`parent`/`type`/`hasitems`/
`weight`/`icon`/`actions`/`browse`, aber **kein** `nextWindow`, `window`,
`input`, `radio`/`checkbox`/`slider`. Für das reine Home-Menü ausreichend,
für Widget-Menüs (Lautstärke-Slider, Checkboxen) nicht.

---

## 7. Favoriten — Kapitel 8

### 7.1 🟠 `favorites exists` fehlt

Weder in `api.py` noch in `cli_commands.py` implementiert (Spec-Kapitel 8).

### 7.2 🟠 `favorites playlist <play|load|insert|add>` fehlt

Nur `favorites play <id>` (CLI-Fallback, Z. 1428 + `_fav_play`). Die Spec
kennt `favorites playlist play/load/insert/add` mit `item_id:`.

### 7.3 🟠 `favorites items` ignoriert `want_url`/`feedMode`/`search`

`api.py` Z. 612–646 liefert immer `url` und ignoriert `want_url`, `feedMode`
(verschachtelte Hierarchie) und `search` (Namensfilter). Spec-Kapitel 8.

### 7.4 🟠 Flache IDs statt hierarchischer Punkt-Notation

`favorites.py` `_fav_to_dict` liefert `id` als Integer (DB-Primärschlüssel).
Die LMS-`id` ist die hierarchische Punkt-Notation (`2.0.9.3`). Controller, die
die Punkt-Notation erwarten, brechen. Auch `item_id:`-Adressierung fehlt.

---

## 8. Suche & Browse — Kapitel 9

### 8.1 🟠 `search` im LMS-Format fehlt (JSON-RPC)

`search` fällt in `api.py` in den CLI-Fallback (Z. 695–700) → nutzt das
falsche `cmd_search`-Format (siehe 3.3). Das LMS-`search <start> <count>
term:…` fehlt im JSON-RPC-Dispatch.

### 8.2 🟠 `_json_browse` ohne Filter und tags

`api.py` `_json_browse` (Z. 1069–1111) unterstützt nur `start`/`count` und
liefert feste Felder. Keine Filter `genre_id`/`album_id`/`track_id`/
`artist_id`/`year`/`library_id`, kein `tags:`-Buchstaben-Code (Spec 9.2),
kein `search:`-Filter (der JSON-RPC-Pfad umgeht die CLI).

### 8.3 🟠 `years`/`roles`/`works`/`libraries` fehlen

Nirgends implementiert (Spec-Kapitel 9.1).

### 8.4 🟠 `genres` inkonsistent

CLI `cmd_genres` (Z. 1156–1173) liest `SELECT DISTINCT genre FROM tracks`
(Text aus Tracks), `_json_browse` (Z. 1099–1104) liest die `genres`-Tabelle.
Zwei verschiedene Quellen für dasselbe Konzept.

---

## 9. Status — Kapitel 10

### 9.1 🟠 `status` liefert kein `sync_master`/`sync_slaves`

`PlayerState` HAT `sync_master`/`sync_slaves` (state.py Z. 41–42), aber
`_json_player_status` (api.py Z. 822–844) gibt sie **nicht** zurück. Spec 10.2:
„nur wenn synced" — synchronisierte Player zeigen daher keine Sync-Info.
(Orange Squeeze/`withPlayerStatusUpdate` liest diese Felder.)

### 9.2 🟠 `status` ignoriert `tags:`-Parameter

`_json_player_status` (Z. 728–844) liefert immer alle Felder; der `tags:`-
Buchstaben-Code (Spec 9.2/10.2) wird nicht ausgewertet. Gering, da die Apps
meist die vollen Felder brauchen — aber nicht LMS-konform.

### 9.3 🟡 Fehlende Status-Felder

`digital_volume_control`, `can_seek`, `signalstrength`, `seq_no`,
`playlist_timestamp`, `playlist mode`, `waitingToPlay`, `alarm_state` fehlen
in `_json_player_status`. (SqueezeClient-Pflichtfelder `count`/
`playlist shuffle`/`playlist repeat`/`player_connected` sind vorhanden.)

### 9.4 🟠 `serverstatus` ohne `subscribe:` und `info total duration`

`api.py` Z. 469–516 liefert version/uuid/name/httpport/player count/mediadirs/
info totals/players_loop — aber kein `subscribe`-Tag (asynchrone Push) und
kein `info total duration`.

### 9.5 🟡 `displaystatus` liefert `{}` ohne showBriefly

`api.py` Z. 685–686 gibt `{}` zurück — kein echtes `showBriefly`-Popup
(Spec 7.4). Nur Stub, um Squeezer-Crashes zu vermeiden.

---

## 10. Querschnitt / Robustheit

### 10.1 🟠 `PlayerState` deklariert genutzte Felder nicht

`state.py` deklariert nicht `elapsed`, `current_title`, `current_url`,
`shuffle`, `repeat` — sie werden per `setattr` gesetzt und per
`getattr(…, default)` gelesen (api.py Z. 796, 806, 829–830). Fragil:
Tippfehler oder vergessene Zuweisung fallen erst zur Laufzeit auf.

### 10.2 🟡 `playlist`-Typ inkonsistent

`state.py` Z. 40 deklariert `playlist: list[int]`, aber api.py Z. 1006–1009
speichert auch URL-Strings (Radio/Favoriten). Der Kommentar ist irreführend.

### 10.3 🟡 CLI-Compound-Dispatch-Falle (bekannt)

`cli.py` `_dispatch_compound` (Z. 284–304) matcht Compound-Kommandos über
Präfix, aber `cmd_favorites`/`cmd_radio`/`cmd_playlist` routen ihre
Sub-Kommandos selbst — teils redundant, teils inkonsistent (z. B. wird
`playlist add` doppelt registriert: als eigenes Kommando UND als Sub in
`cmd_playlist`).

