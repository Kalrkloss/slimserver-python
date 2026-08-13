# Protokoll-Behebungsplan — Python-LMS

Pläne zur Behebung der Fehler und Auslassungen aus
[`protocol-gaps.md`](protocol-gaps.md). Priorisiert nach Impact; jede Phase
ist unabhängig commitbar und verifizierbar. IDs verweisen auf die
Gap-Nummern.

Die Verifikations-Disziplin bleibt: nach jeder Phase `tools/test-clients.py`
(grün gegen 9000 + 9080) + Ad-hoc-Verifikationsskript
(`/tmp/hermes-verify-*.py`) für den geänderten Pfad, dann Commit + Push.

---

## Phase 1 — Kritische Fehler (blockieren echte Clients)

### P1-1 · CLI zeilenorientiert machen (Gap 3.2)

**Problem:** `read_commands` wartet auf eine Leerzeile; LMS-CLI ist eine Zeile
= ein Kommando, Antwort sofort.

**Plan:**
1. `control/cli.py` `read_commands`: statt Puffern + `b"\n\n"`-Terminator
   **jede** nicht-leere Zeile sofort als `(cmd, args)` yielden.
2. `write_responses`/`connect`: Antwort direkt nach der Zeile schreiben,
   abschließende Leerzeile als Trenner BEHALTEN (LMS beendet die Antwort
   mit `\n` und optional einer Leerzeile — beides akzeptieren Clients).
3. Kompatibilität: weiterhin `\r`, `\n`, `\x00` als Zeilenende akzeptieren
   (`rstrip(b"\r\n")` ist da; `\x00` zusätzlich strippen).
4. **Verifikation:** `printf "version ?\n" | nc 127.0.0.1 9091` muss eine
   Antwort liefern (aktuell: Timeout). Ad-hoc-Skript gegen den CLI-Socket.

### P1-2 · percent-decoding der CLI-Parameter (Gap 3.1)

**Problem:** Parameter werden nicht URL-decoded (`The%20Clash` bleibt literal).

**Plan:**
1. `control/cli.py` `_parse_request`: nach `shlex.split` jeden Parameter mit
   `urllib.parse.unquote` decoden.
2. Achtung: `%`-Zeichen, die KEIN gültiges `%HH` sind, unverändert lassen
   (tolerant: `unquote` mit `errors="ignore"`-Fallback).
3. **Verifikation:** `printf "artists 0 5 search:The%20Clash\n" | nc …`
   liefert gefilterte Treffer.

### P1-3 · `browse`-Kommando implementieren (Gap 6.1)

**Problem:** Menü-`go`-Actions zeigen auf `browse <id>`, das leer antwortet →
Controller-Navigation tot.

**Plan:**
1. `api.py` `_json_browse`: `browse` als Alias behandeln — `browse artists`
   → `artists`-Query, `browse albums` → `albums`, `browse songs` → `songs`,
   `browse genres` → `genres`, `browse favorites` → `favorites items`-Resultat,
   `browse radios` → Radio-Liste.
2. Antwortformat: `loop_loop`-Array mit `id`/`name`/`text` (für Menü-Rendering),
   passend zu den `_home_menu`-Items.
3. **Verifikation:** `slim.request "" ["browse", "artists"]` → nicht-leere
   `loop_loop`; Jivelite/Orange Squeeze: Klick auf „Artists" öffnet die Liste.

---

## Phase 2 — CLI-Kommandos vervollständigen (Gap 3.5–3.10)

### P2-1 · Browse-Queries im LMS-Format + Filter (Gap 3.4)

**Plan:**
1. `cmd_artists`/`cmd_albums`/`cmd_songs`/`cmd_genres`: Antwort auf
   LMS-Tagged-Format umstellen: Echo-Header + `count:<n>` + pro Item
   `id:<n> <feld>:<wert>` (Item-Trenner `id:`).
2. Filter ergänzen: `genre_id`, `album_id`, `track_id`, `artist_id`, `year`,
   `tags:` (Buchstaben-Code → Spalten-Mapping).
3. `cmd_songs`: `titles`-Alias registrieren.
4. **Verifikation:** `artists 0 5` → `artists 0 5 count:7 id:2 artist:…`;
   `artists 0 5 genre_id:7` filtert korrekt.

### P2-2 · `info total …` korrigieren (Gap 3.5)

**Plan:**
1. `info total genres ?` / `info total artists ?` / `info total albums ?` /
   `info total songs ?` / `info total duration ?` als separate Sub-Queries.
2. Antwort ohne Doppelpunkt: `info total songs 18`.
3. **Verifikation:** `info total songs ?` → `info total songs <n>`.

### P2-3 · `serverstatus` (CLI) mit players_loop + subscribe (Gap 3.6)

**Plan:**
1. `cmd_serverstatus`: die Player-Liste als getaggte `playerindex:`-Blöcke
   anhängen (wie `players`), plus `info total …`-Zeilen.
2. `subscribe:`-Tag: `ctx.subscribed_player`-Mechanik erweitern → bei
   Server-Änderung asynchrone Antwort (nutzt die `_subscriptions`-Queues in
   `CLIHandler`).
3. **Verifikation:** `serverstatus` liefert Player; `serverstatus subscribe:5`
   pusht bei Änderung.

### P2-4 · `favorites` (CLI) auf Tagged-Format + exists + playlist (Gap 3.7, 7.1, 7.2)

**Plan:**
1. `cmd_favorites`: Sub-Kommandos auf Tagged-Parameter umstellen
   (`favorites add url:… title:…`, `favorites delete item_id:…`).
2. `favorites exists <url|id>` implementieren → `exists:0/1`.
3. `favorites playlist <play|load|insert|add> item_id:…` implementieren.
4. `want_url`-Tag in `_fav_items` respektieren.
5. **Verifikation:** alle Sub-Kommandos per CLI-Test; `favorites exists`
   auf vorhandenem/fehlendem Favorit.

### P2-5 · `status` (CLI) mit Tags + playlist_loop (Gap 3.8)

**Plan:**
1. `cmd_status`: `mode` unmapped liefern (`play`/`pause`/`stop`), `tags:`-
   Parameter auswerten (Buchstaben → Felder), `playlist_loop`-ähnliche
   `playlist index:`-Blöcke anhängen.
2. **Verifikation:** `status 0 2 tags:gald` liefert die Felder.

### P2-6 · `subscribe`/`unsubscribe` echt machen (Gap 3.9)

**Plan:**
1. `CLIHandler` hat `_subscriptions`-Queues — die Player-Status-Änderungen
   (aus `_handle_stat_frame`) in die Queues pushen; `write_responses`-Loop
   liefert asynchrone Updates an subscribed Clients.
2. `subscribe <player> <interval>`: bei jeder Änderung + Intervall-Tick den
   Status senden; `unsubscribe` stoppt.
3. **Verifikation:** Client subscribed, Player wechselt Mode → Update kommt.

### P2-7 · Fehlende CLI-Kommandos nachziehen (Gap 3.10)

**Plan (nach Impact geordnet):**
1. `name` (setzen/abfragen) + `player count ?`/`player id ?`/`player name ?`/
   `player model ?`.
2. `mixer bass/treble/pitch/muting` (Queries + audg/muting-State).
3. `songinfo`, `titles` (Alias), `musicfolder`, `years`.
4. `sync`/`unsync`/`syncgroups` (auf `PlayerManager.sync_players`/
   `unsync_player` mappen).
5. `rescanprogress`/`abortscan`, `playlists tracks/new/delete/edit/rename`.
6. `displaystatus`/`menustatus`/`playerstatus`/`menu` als CLI-Pendants zu den
   JSON-RPC-Handlern (Dispatch teilen).

---

## Phase 3 — JSON-RPC / Cometd / Menü-Details

### P3-1 · `search` im LMS-Format (Gap 8.1, 3.3)

**Plan:**
1. Neuer `cmd_search` (oder `_json_search`) im LMS-Format:
   `search <start> <count> term:<begriff>` → gruppierte Antwort
   `count`/`artists_count`/`albums_count`/`genres_count`/`tracks_count` +
   `artist_id`/`artist`, `album_id`/`album`, `genre_id`/`genre`,
   `track_id`/`track`.
2. `api.py` `_slim_request`: `search` auf diesen Handler dispatchen (nicht
   CLI-Fallback). Der alte `search <type> <query>` bleibt als alias/für die
   Web-UI erhalten, wenn nötig.
3. **Verifikation:** `search 0 20 term:al` liefert die vier Gruppen mit
   Zählern.

### P3-2 · `_json_browse` Filter + tags + musicfolder/songinfo (Gap 8.2, 8.3, 6.2)

**Plan:**
1. `_json_browse`: Filter `genre_id`/`album_id`/`track_id`/`artist_id`/
   `year`/`library_id`/`search` auswerten (JOINs auf die Join-Tabellen).
2. `tags:`-Buchstaben-Code → Spalten-Mapping (mindestens `a`/`l`/`g`/`d`/
   `y`/`c`/`u`/`t`/`r`).
3. `musicfolder` (Ordner-Browse), `songinfo` (Einzel-Track), `years`
   implementieren.
4. `genres` auf die `genres`-Tabelle vereinheitlichen (CLI + JSON-RPC,
   Gap 8.4).
5. **Verifikation:** `albums 0 10 genre_id:7 tags:ly` → korrekte Felder;
   `songinfo 0 10 track_id:2` → voller Datensatz.

### P3-3 · Favoriten: hierarchische IDs + feedMode (Gap 7.3, 7.4)

**Plan:**
1. `favorites.py`: `_fav_to_dict` liefert zusätzlich die hierarchische
   Punkt-Notation (Pfad der Positionen vom Root: `2.0.9.3`) als `id`.
   Flache DB-ID intern behalten, aber nach außen die Punkt-Notation liefern
   (LMS-kompatibel).
2. `item_id:`-Adressierung in `favorites items`/`delete`/`rename`/`move`
   auflösen (Punkt-Notation → DB-Pfad).
3. `feedMode:1` → verschachtelte Hierarchie (`items`-Arrays pro Ebene).
4. **Verifikation:** `favorites items item_id:2.0` liefert die Kinder;
   `feedMode:1` liefert den Baum.

### P3-4 · Cometd `/meta/disconnect` + Ping (Gap 5.2, 5.3)

**Plan:**
1. `cometd.py` `handle_messages`: `/meta/disconnect` → `remove(cid)` +
   `successful: True`.
2. `/meta/ping` → `successful: True` + ggf. `advice`.
3. **Verifikation:** Handshake → disconnect → `get(cid)` ist `None`.

### P3-5 · Menü-Items mit `nextWindow`/`window` (Gap 6.3)

**Plan:**
1. `_home_item`/`_home_menu`: `nextWindow: "refresh"` für Browse-Items,
   `window`-Block (`windowStyle: "text_list"`) ergänzen.
2. Optional: Slider/Checkbox-Widgets für Einstellungs-Menüs (später).
3. **Verifikation:** Jivelite rendert die Items und navigiert.

---

## Phase 4 — Status vervollständigen (Gap 9.1–9.5)

### P4-1 · `status` mit sync-Feldern + fehlenden Feldern (Gap 9.1, 9.3)

**Plan:**
1. `_json_player_status`: `sync_master` (nur wenn gesetzt), `sync_slaves`
   (komma-getrennt), `digital_volume_control`, `can_seek`, `signalstrength`,
   `seq_no`, `playlist_timestamp` ergänzen.
2. **Verifikation:** synchronisierter Player zeigt `sync_master`/
   `sync_slaves`.

### P4-2 · `status` `tags:` auswerten (Gap 9.2)

**Plan:**
1. `_json_player_status`: `tags:`-Parameter parsen, nur angefragte Felder
   liefern (Default `gald`-ähnlich, aber die Apps brauchen die vollen Felder
   → bei fehlendem tags weiterhin alles liefern, um Regressions zu vermeiden).
2. **Verifikation:** `status - 1 tags:acd` liefert nur die angefragten.

### P4-3 · `serverstatus` subscribe + info total duration (Gap 9.4)

**Plan:**
1. `info total duration` in `_json_browse`/serverstatus ergänzen.
2. `subscribe:`-Tag: bei Server-Änderung asynchron über Cometd pushen
   (nutzt die Cometd-Event-Queues).
3. **Verifikation:** `serverstatus subscribe:5` pusht bei Player-Connect.

### P4-4 · `displaystatus` showBriefly (Gap 9.5)

**Plan:**
1. `displaystatus`-Handler: `showBriefly`-artige Popup-Payloads liefern
   (`jive`-Block mit `text`/`type`/`duration`), wenn ein Popup ansteht.
2. Geringe Priorität — aktuell `{}` ist funktional ausreichend für die Apps.

---

## Phase 5 — SlimProto-Restlücken (Gap 1.1–1.5)

### P5-1 · audg-on-connect auch im binären HELO-Pfad (Gap 1.1) 🔴

**Plan:**
1. `_handle_player` binärer Zweig: nach `vers`+`setd` ebenfalls
   `send_volume_to_player(mac, volume)` aufrufen (wie der Text-Pfad).
2. **Verifikation:** SqueezePlay/Jive-Player (binäres HELO) spielt mit Ton
   (Loopback-Capture, sonst Gain-0-Stille).

### P5-2 · Binärer HELO-ACK mit supported_commands (Gap 1.2)

**Plan:**
1. ACK um das `supported_commands`-u32-Bitfield ergänzen
   (`pack("<BHI", num_ext, buffer, channels)` → `pack("<BHII", …)`).
2. **Verifikation:** binärer HELO → ACK mit 11 Bytes, `supp_cmds` != 0.

### P5-3 · STAT STMx vollständig (Gap 1.3)

**Plan:**
1. `_handle_stat_frame`: `STMf` → `mode=stop` (Stop-Quittung), `STMp`/
   `STMr` → `mode=pause`/`play`, `STMn` → Fehler loggen + ggf. `mode=stop`,
   `STMo` → Underrun loggen, `STMt` → Heartbeat (kein State-Change).
2. **Verifikation:** Stop → STMf setzt mode=stop; Pause → STMp setzt pause.

### P5-4 · `aude`-Frame + Display-Frames (Gap 1.4, 1.5)

**Plan (niedrige Priorität, nur Squeezebox-Hardware):**
1. `send_aude(mac, spdif, dac)` — `aude`-Frame.
2. `grfb`/`grfe`/`serv`/`visu` als optionale Frames (später, wenn
   Hardware-Player unterstützt werden).

---

## Phase 6 — Robustheit (Gap 10.1–10.3)

### P6-1 · `PlayerState`-Felder deklarieren (Gap 10.1)

**Plan:**
1. `state.py`: `elapsed: float = 0`, `current_title: str = ""`,
   `current_url: Optional[str] = None`, `shuffle: int = 0`,
   `repeat: int = 0` deklarieren.
2. `_json_player_status`/`_json_control` auf die deklarierten Felder
   umstellen (statt `getattr`-Default).
3. **Verifikation:** Status-Test nach Play/Pause (keine `AttributeError`).

### P6-2 · `playlist`-Typ korrigieren (Gap 10.2)

**Plan:**
1. `state.py`: `playlist: list[int | str]` (Track-ID oder Stream-URL) +
   Kommentar aktualisieren.
2. **Verifikation:** Type-Check/Import-Smoke.

### P6-3 · Compound-Dispatch konsolidieren (Gap 10.3)

**Plan:**
1. Doppelte Registrierung (`playlist add` als eigenes Kommando UND Sub in
   `cmd_playlist`) auflösen — ein Weg pro Sub-Kommando.
2. **Verifikation:** `playlist add 42` und `playlist play 0` über CLI.

---

## Empfohlene Reihenfolge

1. **Phase 1** (P1-1, P1-2, P1-3) — blockiert echte CLI-Clients und die
   Controller-Menü-Navigation.
2. **Phase 5-P5-1** (audg im binären HELO) — blockiert Ton für Jive-Player.
3. **Phase 2** (CLI-Kommandos) — Vervollständigung.
4. **Phase 3** (Suche/Browse/Favoriten/Cometd) — Controller-Funktionalität.
5. **Phase 4** (Status) — Sync-/Status-Info.
6. **Phase 6** (Robustheit) — Abschluss.

Jede Phase ist einzeln commitbar; nach jeder Phase:
`tools/test-clients.py` (9000 + 9080) + Ad-hoc-Verifikation + Commit/Push.

## Status

| Plan-ID | Gap | Status |
|---------|-----|--------|
| P1-1 | 3.2 | ✅ erledigt in `6e5fe21` (CLI zeilenorientiert + cli_server auf CLIHandler) |
| P1-2 | 3.1 | ✅ erledigt in `6e5fe21` (percent-decoding + Player-ID-Erkennung) |
| P1-3 | 6.1, 8.2(Teil) | ✅ erledigt in `6e5fe21` (`browse`-Kommando, _json_browse-SQL-Fixes) |
| P5-1 | 1.1 | ✅ erledigt in `6e5fe21` (audg im binären HELO, Socket-verifiziert) |
| — | 3.2(Zusatz), 10.3(Teil) | ✅ erledigt in `6e5fe21`: cli_server.py war ein Stub (fast alle Kommandos → „ok"); delegiert jetzt an die Registry. MAC-Normalisierung in allen Player-Lookups. `mixer volume` direkt. |
| P2-1…P2-7, P3-1…P3-5, P4-1…P4-4, P5-2…P5-4, P6-1…P6-3 | restliche Gaps | ⏳ offen |

Phase-2-Status (Commit `bc53774`):

| Plan-ID | Gap | Status |
|---------|-----|--------|
| P2-1 | 3.4 | ✅ Browse-Queries im LMS-Tagged-Format (`count:`) + Filter (`search`, `artist_id`, `album_id`, `track_id`, `year`, `genre`) + `titles`-Alias |
| P2-2 | 3.5 | ✅ `info total <genres\|artists\|albums\|songs\|duration> [?]` ohne Doppelpunkt, echte DB-Zahlen |
| P2-3 | 3.6 | ✅ `serverstatus` mit version/uuid/name/httpport, info-Totals, `player count` + `players_loop` (playerindex/playerid/name/…) |
| P2-4 | 3.7, 7.1, 7.2 | ✅ favorites: Tagged-Add (`url:`/`title:`/`parent:`), `exists`, `playlist <play\|load\|insert\|add> item_id:` |
| P2-5 | 3.8 | ✅ `status`: Mode ungemappt (`play`/`pause`/`stop`), `tags:`-Auswertung, Playlist-Loop (`playlist index:N title:… artist:… album:…`), `subscribe:`-Tag |
| P2-6 | 3.9 | ✅ subscribe echt: Queue je Player, Push bei STAT-Änderung (`CLIHandler.notify_subscribers`) + Keep-Alive-Intervall; Live-Test: Push mit `player_name: Küche` |
| P2-7 | 3.10 | ✅ `name`, `player count\|id\|name\|model`, `sync`/`unsync`/`syncgroups`, `mixer bass\|treble\|pitch\|muting`, `songinfo`, `years`, `musicfolder`, `playlists tracks\|new\|delete\|rename`, `menu`, `playerstatus`, `displaystatus`, `rescanprogress`, `abortscan` |
| Zusatz | — | ✅ `_write_db`-Helper (Playlist-CRUD); Test-Restart-Fix: `name ?` setzt nicht mehr den Namen „?" |

Verifikation Phase 2: Suite `all checks passed` (9000+9080), Ad-hoc-Skript
`/tmp/hermes-verify-s939esg1.py` 21/21, CLI-Socket-Live-Tests (artists
count:5725, genre-Filter 6981 Treffer, info total songs 56503, songinfo
vollständig, Subscribe-Push mit Player-Daten, `name Küche` Query/Set).

Phase-3-Status (Commit `23c4902`):

| Plan-ID | Gap | Status |
|---------|-----|--------|
| P3-1 | 8.1, 3.3 | ✅ `search <start> <count> term:<x>` im LMS-Format (JSON gruppiert: count + artists/albums/genres/tracks_count + Loops; CLI-`_search_lms` analog). Alt-Format `search <type> <query>` bleibt. |
| P3-2 | 8.2, 8.3, 6.2 | ✅ `_json_browse` Filter (`genre:`/`genre_id:` (als Index in die DISTINCT-Genre-Liste, genres-Tabelle leer)/`album_id:`/`track_id:`/`artist_id:`/`year:`/`search:`), `musicfolder`- und `songinfo`-JSON-Zweige |
| P3-3 | 7.3, 7.4 | ✅ Favoriten liefern hierarchische Punkt-Notation (`0.0`, `0.3.1`) als `id` (DB-ID als `dbid`); `item_id:`-Auflösung via `FavoritesManager.resolve_path`; `feedMode:1` → eingebettete `items`; CLI-Subs (play/delete/move/rename/playlist) akzeptieren Pfad-IDs |
| P3-4 | 5.2, 5.3 | ✅ `/meta/disconnect` entfernt den Client + `successful:True`; `/meta/ping` → `successful:True` |
| P3-5 | 6.3 | ✅ Menü-Items mit `nextWindow:"refresh"` + `window:{windowStyle:"text_list", title, hasMore}` |

Verifikation Phase 3: Suite `all checks passed`, Ad-hoc-Skript
`/tmp/hermes-verify-pg20eywg.py` 15/15, Live-Tests (search metal → count 555
gruppiert; albums genre:Rock → 652; songinfo vollständig; favorites id
`0.0` + item_id:0.0 → 5 Kinder; Cometd disconnect/ping; menu nextWindow).

Phase-4-Status (Commit `0acf491`):

| Plan-ID | Gap | Status |
|---------|-----|--------|
| P4-1 | 9.1, 9.3 | ✅ `status` liefert `sync_master`/`sync_slaves` (nur wenn synced), `digital_volume_control`, `can_seek` (1 für lokale Tracks), `signalstrength`, `seq_no`, `playlist_timestamp`, `waitingToPlay`, `alarm_state` |
| P4-2 | 9.2 | ✅ `tags:<code>` filtert die `item_loop`-Felder (t=title a=artist l=album d=duration u=url; `trackType` immer, OSQ-NPE-Schutz); ohne tags weiterhin alle Felder |
| P4-3 | 9.4 | ✅ `serverstatus` mit echten DB-Totals (`info total songs/duration/artists/albums/genres`, waren hartkodiert 0) + `subscribe:`-Tag; Cometd-Push bei Player-Connect/Disconnect (`CometdManager.notify_server_status` + Singleton `get_manager()`, Trigger in protocol.py HELO/bye) |
| P4-4 | 9.5 | ✅ `displaystatus` mit `showBriefly:<text> [<dauer>]` → jive-Popup-Block mit Ablauf-Timer; Abfrage liefert aktiven Popup oder `{}` |

Verifikation Phase 4: Suite `all checks passed`, Ad-hoc-Skript
`/tmp/hermes-verify-s6o68brc.py` 19/19, Live-Tests (status sync/optional
Felder; serverstatus songs 58603/duration 17278167/artists 5725/albums 5135 +
subscribe; displaystatus Popup setzen+abrufen).

Phase-5+6-Status (Commit `fbe3ff9`):

| Plan-ID | Gap | Status |
|---------|-----|--------|
| P5-2 | 1.2 | ✅ Binärer HELO-ACK jetzt 11 Bytes (`<BHII`: num_ext/buffer/max_channels/`supported_commands`=0x07 strm\|audg\|aude) — Socket-verifiziert |
| P5-3 | 1.3 | ✅ STAT-`STMf` (Flush/Stop → stop, außer bei pause), `STMp`/`STMr` (pause/play), `STMn` (decode error → stop + Log), `STMo` (underrun → Log, kein State-Change) |
| P5-4 | 1.4 | ✅ `send_aude(mac, spdif, dac)`-Frame-Sender. Display-Frames (grfb/grfe/serv/visu) bewusst offen (nur Squeezebox-Hardware) |
| P6-1 | 10.1 | ✅ `PlayerState` deklariert jetzt `elapsed`/`current_title`/`current_url`/`shuffle`/`repeat` (waren setattr-dynamisch) |
| P6-2 | 10.2 | ✅ `playlist: list[int \| str]` (Track-ID oder Stream-URL) |
| P6-3 | 10.3 | ✅ 10 tote Sub-Registrierungen entfernt (`playlist play/add/clear/save/load`, `radio add/delete/search/top/play`) — die Basis-Handler routen die Subs; ein Weg pro Sub-Kommando |

Verifikation Phase 5+6: Suite `all checks passed`, Ad-hoc-Skript
`/tmp/hermes-verify-sh7ipbpt.py` 15/15, Live/Unit-Tests (HELO-ACK 11 Bytes
supported_commands=7; STMf/p/r/n/o State-Transitions; aude-Frame; PlayerState-
Felder; playlist/radio-Subs via Basis-Handler).

Damit sind alle Phasen des Fix-Plans abgeschlossen: P1, P2, P3, P4, P5, P6 ✅

