# Lyrion Music Server — Kommunikationsprotokoll

Eine präzise Referenz der Protokolle, über die der Lyrion Music Server (LMS,
früher Squeezebox Server / Logitech Media Server) mit **Playern** (Squeezebox,
Squeezelite, SqueezePlay, SqueezeESP32 …) und **Controllern** (Jivelite,
Orange Squeeze, Squeezer, SqueezeCtrl, Material Skin, iPeng …) kommuniziert.

Diese Dokumentation fasst die offiziellen Spezifikationen und Referenz-
Implementierungen zusammen. Sie beschreibt das *Zielformat* (LMS-Verhalten),
an dem sich der Python-Port dieses Repos orientiert.

## Inhaltsübersicht

1. [Überblick: die vier Protokoll-Ebenen](#1-überblick-die-vier-protokoll-ebenen)
2. [SlimProto (TCP 3483) — Player ↔ Server](#2-slimproto-tcp-3483--player--server)
3. [Discovery (UDP 3483) — Server finden](#3-discovery-udp-3483--server-finden)
4. [CLI (TCP 9090) — Befehlsformat](#4-cli-tcp-9090--befehlsformat)
5. [JSON-RPC (HTTP 9000 `/jsonrpc.js`)](#5-json-rpc-http-9000-jsonrpcjs)
6. [Cometd/Bayeux — Push an Controller](#6-cometdbayeux--push-an-controller)
7. [Menüs (SlimBrowse / SqueezePlay-Interface)](#7-menüs-slimbrowse--squeezeplay-interface)
8. [Favoriten](#8-favoriten)
9. [Suche & Browse der Musikbibliothek](#9-suche--browse-der-musikbibliothek)
10. [Status-Abfragen](#10-status-abfragen)
11. [Quellen](#11-quellen)

---

## 1. Überblick: die vier Protokoll-Ebenen

Der Server belegt standardmäßig vier Kommunikationswege:

| Protokoll | Transport | Port | Gegenstelle | Zweck |
|-----------|-----------|------|-------------|-------|
| **SlimProto** | TCP | 3483 | Player | Registrierung, Streaming-Steuerung, Status (binär) |
| **Discovery** | UDP | 3483 | Player/Controller | Server-Auffinden im Netz (Broadcast) |
| **CLI** | TCP | 9090 | Controller/Automation | Befehle & Abfragen (Text, Telnet-Stil) |
| **HTTP/JSON-RPC** | TCP | 9000 | Controller/Web-UI | `slim.request` über `/jsonrpc.js` |
| **Cometd/Bayeux** | HTTP | 9000 | Controller (Jive-Apps) | Push/Subscription über `POST /cometd` |

Zwei grundsätzlich verschiedene Welten:

- **Player** sprechen **SlimProto** (binär, TCP 3483) — sie streamen Audio und
  melden Status über einen festen Satz binärer Frames. Sie nutzen *nicht* die
  CLI und verstehen keine Textbefehle.
- **Controller** (Apps, Web-UI, Automation) sprechen **CLI/JSON-RPC/Cometd**
  (Text bzw. JSON über HTTP) — sie steuern den Server und lesen dessen
  Datenbank (Menüs, Favoriten, Suche, Playlists), streamen aber selbst kein
  Audio.

Der Server hält beide Welten über einen gemeinsamen Player-Zustand konsistent.

---

## 2. SlimProto (TCP 3483) — Player ↔ Server

> Maßgebliche Referenzen: die offizielle Spezifikation
> [`lyrion.org/reference/slimproto-protocol/`](https://lyrion.org/reference/slimproto-protocol/)
> und die C-Struktur-Definitionen in
> [`ralph-irving/squeezelite → slimproto.h`](https://github.com/ralph-irving/squeezelite/blob/master/slimproto.h).

### 2.1 Framing ist asymmetrisch

Die Längen-/Header-Codierung unterscheidet sich je Richtung:

- **Server → Player:** `pack('n', len(payload)+4)` — 2-Byte-BE-Länge **inklusive**
  des 4-Byte-Opcodes — gefolgt von 4-Byte-ASCII-Opcode + Payload.
- **Player → Server:** 4-Byte-ASCII-Opcode + 4-Byte-BE-Länge (**nur**
  Payload-Länge) + Payload.

Für die klassischen Squeezebox-Frames gilt ein abweichendes Layout (Header mit
`[1-Byte-Command][3-Byte-BE-Länge]`), das insbesondere SqueezePlay/Jive nutzt.
Ein Server muss beide Formen am ersten Byte unterscheiden können.

### 2.2 HELO — Registrierung

Der Player meldet sich beim Server mit einem HELO-Paket an. Format (bis zu
36 Byte Grunddaten + optionale Capability-Strings):

| Feld | Länge | Bedeutung |
|------|-------|-----------|
| DeviceID | 1 | Gerätetyp: `2`=Squeezebox, `4`=Squeezebox2, `5`=Transporter, `7`=Receiver, `9`=Controller, `10`=Boom, `12`=SqueezePlay |
| Revision | 1 | Firmware-Revision |
| MAC[6] | 6 | MAC-Adresse des Players |
| UUID[16] | 16 | eindeutige Geräte-ID (neuere Firmware) |
| WLanChannelList | 2 | Bitfeld aktivierter 802.11-Kanäle |
| BytesReceived | 8 | empfangene Datenstrom-Bytes |
| Language | 2 | Ländercode |

Danach folgen **Capabilities** als komma-getrennte ASCII-Liste. Konvention:
Codec-Capabilities sind klein geschrieben (`mp3`, `flc`, `pcm`, `aif`, `aac`,
`alc`, `ogg`, `wma` …), andere beginnen groß. Wichtige Nicht-Codec-Capabilities:

- `Model=<typ>` — Gerätetyp (z. B. `squeezeplay`, `squeezelite`, `squeezeesp32`)
- `ModelName=<name>` — Anzeigename
- `MaxSampleRate=<n>` — maximale Abtastrate in Hz
- `HasDigitalOut`, `HasPreAmp`, `HasDisableDac`
- `SyncgroupID=<n>` — gewünschte Sync-Gruppe beim Serverwechsel
- `CanHTTPS=1` — Player kann TLS direkt (relevant für https-Streams)

Der Server antwortet mit einem HELO-ACK (Status `0x00`) und sendet danach
`vers` (Versionsstring) und ggf. eine `setd`-Name-Abfrage.

### 2.3 `strm` — Stream-Steuerung (Kern-Frame)

Der strm-Frame weist den Player an, einen Audiostream zu holen/starten/stoppen.
Er trägt 24 Byte Steuerdaten + einen HTTP-Request-String.

| Feld | Länge | Bedeutung |
|------|-------|-----------|
| command | 1 | `s` start · `p` pause · `u` unpause · `q` stop · `t` status · `f` flush · `a` skip-ahead |
| autostart | 1 | `0` nicht auto-starten, `1` auto-start, `2` direktes Streaming, `3` direkt+auto |
| formatbyte | 1 | `p` PCM, `m` MP3, `f` FLAC, `w` WMA, `o` Ogg, `a` AAC, `l` ALAC |
| pcmsamplesize | 1 | `1`=16, `2`=20, `3`=32 Bit; `?` bei selbstbeschreibenden Formaten |
| pcmsamplerate | 1 | `3`=44,1 kHz, `4`=48 kHz, `9`=96 kHz …; `?` selbstbeschreibend |
| pcmchannels | 1 | `1` mono, `2` stereo |
| pcmendian | 1 | `0` big, `1` little endian |
| threshold | 1 | KB Input-Puffer vor Autostart/Notify |
| spdif_enable | 1 | `0` auto, `1` an, `2` aus |
| trans_period | 1 | Überblend-Dauer in Sekunden |
| trans_type | 1 | `0` keine, `1` Crossfade, `2` Fade-in, `3` Fade-out, `4` Fade-in+out |
| flags | 1 | `0x80` Endlosschleife, `0x40` ohne Decoder-Neustart, `0x01/0x02` Polaritäts-Inversion L/R |
| output_threshold | 1 | Output-Puffer vor Playback in Zehntelsekunden |
| replay_gain | 4 | 16.16-Festkomma, `0` = keiner (Sonderbedeutung bei `u/p/a/t`) |
| server_port | 2 | Server-Port (Standard 9000) |
| server_ip | 4 | `0` = IP des Control-Servers verwenden |

Darauf folgt der HTTP-Request-String, z. B.:

```
GET /stream.mp3?player=<client-id> HTTP/1.0

```

Der `replay_gain`-Wert wird bei den Kommandos `u`, `p`, `a`, `t` umgedeutet:
`u` = Unpause-Zeitstempel, `p` = Pause-Intervall (ms), `a` = zu überspringendes
Intervall, `t` = zurückzugebender Zeitstempel (Latenzmessung).

### 2.4 Weitere Server→Player-Frames

| Opcode | Zweck |
|--------|-------|
| `audg` | Lautstärke (Gain): `old_gainL/R` (unused), `dvc`, `preamp`, `gainL/R` (16.16) |
| `aude` | Audio-Ausgänge: `spdif_enable`, `dac_enable` |
| `setd` | Player-Einstellung setzen/abfragen (id=0 → Name-Query) |
| `vers` | Versionsstring |
| `cont` | Content-Type/Metadaten-Intervall (Icecast `metaint`) für Remote-Streams |
| `stat` | STAT-Update anfordern |
| `grfb`/`grfe` | Display-Helligkeit / Bitmap (Squeezebox-Hardware) |
| `serv` | Server-Wechsel anweisen |

### 2.5 STAT — Player-Status (Player → Server)

Der Player sendet STAT-Frames periodisch (Keepalive) und als Reaktion auf
Befehle. Wichtigstes Feld ist der **Event-Code** (4-Byte-String):

| Event | Bedeutung |
|-------|-----------|
| `STMa` | Autostart (Track gestartet) |
| `STMc` | Connect (Antwort auf strm-s) |
| `STMd` | Decoder bereit — **Signal für Auto-Next** (nächster Track) |
| `STMe` | Stream-Verbindung hergestellt |
| `STMf` | Flush/gestoppt (Antwort auf strm-f / strm-q) |
| `STMh` | HTTP-Header empfangen |
| `STMl` | Puffer-Schwelle erreicht |
| `STMn` | Format nicht unterstützt / Decode-Fehler |
| `STMo` | Output-Underrun |
| `STMp` / `STMr` | Pause / Resume-Bestätigung |
| `STMs` | Track gestartet |
| `STMt` | Timer/Heartbeat |
| `STMu` | Underrun (normales Wiedergabe-Ende) |

Der Rest des STAT-Payloads liefert Puffer-/Signal-Daten; das Feld
`elapsed seconds` ist die abgespielte Zeit (Playback-Position).

Weitere Player→Server-Opcodes: `RESP` (HTTP-Response-Header des Streams),
`BODY`, `META` (Stream-Metadaten), `DSCO` (Datenstrom getrennt), `SETD`,
`BYE!` (Verbindungsende; erstes Byte `0x01` = Firmware-Update).

### 2.6 Streaming-Modell (direkt vs. Proxy)

- **Lokale Dateien:** strm zeigt auf den Server (`GET /stream.mp3?player=MAC`),
  der Server liefert den Track über HTTP aus.
- **Remote-Streams (Radio/Favoriten):** strm zeigt direkt auf die Quelle
  (Quell-IP/Port im Frame), der Player verbindet sich direkt, meldet die
  Quell-Response-Header als `RESP` zurück, und der Server antwortet mit `cont`
  (Metadaten-Intervall) — der Player strippt Icecast-Metadaten selbst. Nur als
  Fallback (z. B. https ohne `CanHTTPS`-Cap) proxied der Server.

---

## 3. Discovery (UDP 3483) — Server finden

Player und Controller finden den Server über **UDP-Broadcast auf Port 3483**.
Der Server antwortet auf Discovery-Anfragen; klassische Squeezebox-Hardware
sendet ein `d`-Paket (18 Byte: `d` + deviceid + revision + skip + MAC), Jive/
SqueezePlay nutzen zusätzlich TLV-Anfragen (Paket beginnt mit `e`) und erwarten
eine `E`-Antwort mit NAME/IPAD/JSON/VERS/UUID. Zusätzlich antwortet der Server
auf **SSDP**-M-SEARCH (UDP 1900). Der HTTP-Port (9000) wird in den
Antwortfeldern mitgeteilt.

---

## 4. CLI (TCP 9090) — Befehlsformat

Die CLI ist ein zeilenorientiertes Telnet-Protokoll für Automation (AMX,
Crestron, Home-Assistant, ioBroker …). Zeilenende ist `LF` (auch `CR` oder
`0x00` akzeptiert); Strings sind UTF-8, Parameter **percent-escaped**
(URL-Stil, z. B. `The%20Clash`).

### 4.1 Allgemeines Kommando-Format

```
<playerid> <command> <p1> <p2> …
```

- `<playerid>` ist die Player-ID (üblicherweise die MAC). Serverglobale
  Kommandos haben keine Player-ID. Ein `?` als Parameter fragt einen Wert ab.
- Die Antwort **echot** das Kommando und liefert die angefragten Daten.

### 4.2 Erweiterte Queries (Datenbank-Browse)

```
<playerid> <query> <start> <count> <tag:wert> …
```

`start` (0-basiert) und `count` steuern die Paginierung. Danach folgen
**Tagged Parameters** (`name:wert`). Die Antwort wiederholt die Query und
liefert pro Element die angeforderten Tags; ein spezieller Tag trennt die
Elemente (`id:` bei Genres/Artists/Albums, `playlist index:` bei Tracks).

Beispiel `players`:

```
Request:  "players 0 2"
Response: "players 0 2 count:2 playerindex:0 playerid:a5:41:d2:cd:cd:05
           ip:127.0.0.1:60488 name:… model:softsqueeze connected:1
           playerindex:1 playerid:00:04:20:02:00:c8 …"
```

### 4.3 Einzelwert-Abfragen (`<cmd> ?`)

Über JSON-RPC/Cometd werden Einzelwerte als `<cmd> ?` abgefragt und als
`{"_<cmd>": wert}` beantwortet (z. B. `mode ?` → `{"_mode": "play"}`,
`mixer volume ?` → `{"_volume": 50}`, `version ?` → `{"_version": "9.2.0"}`).

---

## 5. JSON-RPC (HTTP 9000 `/jsonrpc.js`)

Controller rufen die CLI alternativ über **JSON-RPC 1.0** auf:

```
POST http://<server>:9000/jsonrpc.js
{"id":1,"method":"slim.request","params":[<playerid>,[<command>,…]]}
```

Antwort: das `params`/`id`/`method`-Echo mit den Daten in `result`. Beispiel:

```
Request:  {"id":1,"method":"slim.request","params":["00:04:20:ab:cd:ef",["playlist","name","?"]]}
Response: {"params":[…],"result":{"_name":"Daily Mix"},"id":"1","method":"slim.request"}
```

Wichtig: Remote-Apps senden das `jsonrpc`-Feld oft gar nicht oder als `"1.0"`;
der Server muss `None`/`"1.0"`/`"2.0"` akzeptieren. Für serverglobale
Kommandos wird `0` als Player-ID gesetzt.

---

## 6. Cometd/Bayeux — Push an Controller

Jive-basierte Controller (Jivelite, SqueezePlay, Orange Squeeze, Squeezer,
SqueezeCtrl, Material Skin) sprechen ein **Bayeux-artiges Comet-Protokoll**
über `POST /cometd`. Ablauf:

1. **Handshake** auf `/meta/handshake` → Server vergibt eine `clientId`.
2. **Connect** auf `/meta/connect` → der Server hält die Antwort offen
   (Long-Polling bzw. Streaming) und pusht Events.
3. **Subscribe** auf `/slim/subscribe` → Ack + das initiale Ergebnis des
   `data.request` als Event auf dem Subscription-Kanal.
4. **Request** auf `/slim/request` mit `data.request = [player, [cmd,…]]` →
   Ergebnis als Event auf `data.response`.

Beispiel (Handshake → Status-Query):

```
Request:  [{"channel":"/meta/handshake"}]
Response: [{"clientId":"3a6772d3","supportedConnectionTypes":["long-polling","streaming"],
           "successful":true,"version":"1.0","channel":"/meta/handshake"}]

Request:  [{"id":"1","clientId":"3a6772d3","channel":"/slim/request",
           "data":{"response":"/slim/3a6772d3/request",
                   "request":["00:04:20:02:00:c8",["status","-","1","tags:aclKN"]]}}]
Response: [{"channel":"/slim/request","id":"1","successful":true,"clientId":"3a6772d3"},
           {"channel":"/slim/3a6772d3/request","id":"1",
            "data":{"mode":"stop","player_connected":1,"player_name":"sodco",
                    "mixer volume":90,"playlist_loop":[…]}}]
```

Anmerkungen zum Kanal-Handling: Subscribe-Kanäle kommen als `data.response`
(Material) **oder** `data.subscription` (Jive/SqueezeClient); die clientId
steckt teils nur im Kanal-Pfad (`/<cid>/slim/request`). Events für
Streaming-Clients tragen Kanäle mit `/<clientId>/`-Präfix. Der
`connectionType:"streaming"`-Fall hält eine offene chunked-HTTP-Antwort und
pusht Event-Batches — dies erfordert einen HTTP-Server, der gepipelinede POSTs
auf derselben Verbindung bei offener Antwort bedienen kann (das ist die
Anforderung, die ein nativer Stream-Server erfüllen muss).

---

## 7. Menüs (SlimBrowse / SqueezePlay-Interface)

> Maßgebliche Referenz:
> [`lyrion.org/reference/slimbrowse/`](https://lyrion.org/reference/slimbrowse/).

Jive-Controller bauen ihre Menüs aus strukturierten JSON-Antworten auf
`menu`-artige Abfragen. Das Antwortformat:

```json
{
  "base":   { "…": "Felder für das gesamte Fenster" },
  "count":  12,
  "item_loop": [
    { "text": "Rock", "icon": "rock.jpg", "actions": { "go": { "cmd": ["artists"], "params": { "genre_id": 33 } } } },
    { "…": "weiteres Item" }
  ]
}
```

### 7.1 Pflichtfelder eines Items

- `text` — Anzeigetext (kann `\n` für mehrzeilige Anzeige enthalten).
- `actions` — die beim Tastendruck auszuführenden Kommandos (siehe 7.2).

Optionale Item-Felder: `icon` (Teil-/Voll-URL), `icon-id` (Artwork-ID),
`radio`/`checkbox`/`slider`/`choiceStrings` (Widgets), `nextWindow`,
`setSelectedIndex`, `input` (Eingabe-Aufforderung), `window` (Fenster-Styling).

### 7.2 Actions

`actions` bilden Tasten auf Kommandos ab. Wichtigste Action-Namen:

- `go` — öffnet ein neues Fenster (liefert neue Browse-Daten).
- `do` — führt eine Aktion aus, ohne Browse-Daten zurückzugeben; **`do` hat
  Vorrang vor `go`**.
- `play` / `add` / `more` (Kontextmenü) / `back` / `rew` / `fwd` / `pause`.
- `on` / `off` — für Checkbox-Items.

Eine Action ist entweder ein JSON-Kommando oder eine URL:

```json
{ "go": { "cmd": ["albums"], "params": { "sort": "new", "tags": "jsjs", "menu": "tracks" } } }
```

Spezielle `params`-Platzhalter: `__INPUT__` (Nutzer-Eingabe) und
`__TAGGEDINPUT__` (Eingabe als `key:wert`). `nextWindow`-Werte steuern die
Fenster-Navigation (`home`, `nowPlaying`, `playlist`, `parent`, `refresh`,
`grandparent`, `parentNoRefresh`, `refreshOrigin`).

### 7.3 Home-Menü vs. Untermenüs

Das **Home-Menü** (die obersten Einträge) wird über eine
`menu:menu`-Anforderung geliefert und separat verwaltet. Untermenüs
(Genres → Artists → Alben → Tracks) werden durch Kaskadierung der
Standard-Queries mit einem `menu:`-Parameter erzeugt, z. B.
`genres menu:artist` → Items mit `artists menu:albums` → … → `titles menu:songinfo`.

### 7.4 `showBriefly` / `displaystatus`

Der Server kann transient Meldungen über die **displaystatus**-Subscription
senden (Popup-Texte, `type: alertWindow` für persistente Fenster).

---

## 8. Favoriten

Favoriten (Streams und Ordner) werden als Baum verwaltet; ein Favorit ist ein
Ordner (ohne URL) oder ein Stream (mit URL). Kommandos (siehe
[`lyrion.org/reference/cli/favorites/`](https://lyrion.org/reference/cli/favorites/)):

| Kommando | Zweck |
|----------|-------|
| `favorites items` | Favoriten auflisten; Tags `item_id`, `search`, `want_url`, `feedMode` |
| `favorites exists <url\|id>` | Existenz prüfen → `exists:0/1` |
| `favorites add` | Favorit hinzufügen; Tags `item_id`, `title`, `url`, `icon` |
| `favorites addlevel` | Ordner hinzufügen; Tag `title` |
| `favorites delete` | löschen; Tag `item_id` |
| `favorites rename` | umbenennen; Tags `item_id`, `title` |
| `favorites move` | verschieben; Tags `from_id`, `to_id` |
| `favorites playlist <play\|load\|insert\|add>` | Favorit abspielen/laden; Tag `item_id` |

`favorites items` liefert pro Element `id`, `name`, `hasitems`, `url` (nur mit
`want_url:1`). Die `id` ist eine hierarchische Punkt-Notation (z. B. `2.0.9.3`).
Über JSON-RPC/Cometd wird dieselbe Liste als `loop_loop`-Array geliefert
(`{id, name, url, hasitems, …}`), damit Controller sie strukturiert lesen.

---

## 9. Suche & Browse der Musikbibliothek

> Maßgebliche Referenz:
> [`lyrion.org/reference/cli/database/`](https://lyrion.org/reference/cli/database/).

### 9.1 Browse-Queries

Alle Browse-Queries folgen dem erweiterten Query-Format
(`<query> <start> <count> <tag:wert>…`) und unterstützen die Filter-Tags
`search`, `genre_id`, `artist_id`, `album_id`, `track_id`, `library_id`,
`year`, `tags`:

| Query | Liefert | Element-Trennzeichen |
|-------|---------|----------------------|
| `genres` | Genres | `id` / `genre` |
| `artists` | Interpreten | `id` / `artist` |
| `albums` | Alben | `id` / `album` (+ `year`, `artist`, `artwork_track_id` …) |
| `titles`/`songs`/`tracks` | Titel | `playlist index` (bzw. `id`/`title`) |
| `years` | Jahre | `year` |
| `musicfolder` | Ordner-Inhalt | `id` / `type` (audio/folder/playlist) |
| `playlists` | gespeicherte Playlists | `id` / `name` |
| `songinfo` | Einzel-Track-Info | einzelner Datensatz |

### 9.2 Tags (Buchstaben-Code)

`tags:` steuert, welche Felder zurückkommen. Jedes Feld hat einen Buchstaben-
Code (vollständige Liste unter `songinfo`), u. a.:

| Tag | Feld |
|-----|------|
| `a` | artist |
| `l` | album |
| `g` | genre |
| `d` | duration |
| `y` | year |
| `c` | coverid |
| `j` | coverart |
| `J` | artwork_track_id |
| `K` | artwork_url |
| `e` | album_id |
| `p` | genre_id |
| `s` | artist_id |
| `t` | tracknum |
| `u` | url |
| `r` | bitrate |
| `T` | samplerate |
| `I` | samplesize |
| `x` | remote |

### 9.3 Suche (`search`)

`search <start> <count> term:<suchbegriff>` liefert Artists, Alben, Genres und
Tracks in **einer** Antwort, gruppiert mit eigenen Zählern:

```
Request:  "search 0 20 term:al"
Response: "search 0 20 term:al count:9 artists_count:2 albums_count:1 tracks_count:6
           artist_id:2 artist:Alanis Morissette artist_id:37 artist:Alphaville
           album_id:10 album:All Time Greatest Hits …
           track_id:11 track:All I Really Want …"
```

Die Suche ist case-insensitiv und beachtet die Server-Präferenz „Search Within
Words". Alternativ filtern auch `artists`/`albums`/`genres`/`titles` mit dem
Tag `search:`.

---

## 10. Status-Abfragen

> Maßgebliche Referenz:
> [`lyrion.org/reference/cli/compoundqueries/`](https://lyrion.org/reference/cli/compoundqueries/).

### 10.1 `serverstatus`

Liefert Server-Metadaten (Version, `uuid`, `httpport`, `info total …`,
`player count`) **und** die Player-Liste (`players_loop`). Unterstützt den
`subscribe`-Tag für asynchrone Aktualisierung bei Server-Änderungen.

### 10.2 `status` (Player-Status)

`<playerid> status <start> <count> tags:<…>` liefert den vollständigen
Player-Status inklusive Playlist. Wichtige Felder: `player_name`,
`player_connected`, `power`, `mode`, `time`, `rate`, `duration`,
`mixer volume`, `playlist repeat` (0/1/2), `playlist shuffle` (0/1/2),
`playlist_tracks`, `playlist_cur_index` und die Playlist-Einträge
(`playlist index` + Tags). `-` als `start` liefert ab dem aktuellen Song.

Der `tags:`-Parameter steuert die Playlist-Felder (gleiche Buchstaben-Codes
wie `songinfo`); `DD` liefert nur die Gesamtdauer der Playlist. Der
`subscribe:`-Tag erzeugt asynchrone Status-Updates bei Player-Änderungen.

### 10.3 `displaystatus`

Subscription für Display-Update-Events (u. a. `showBriefly`-Popups, siehe 7.4).

---

## 11. Quellen

- SlimProto-Spezifikation:
  <https://lyrion.org/reference/slimproto-protocol/>
- Squeezelite (C-Referenz-Frames):
  <https://github.com/ralph-irving/squeezelite/blob/master/slimproto.h>
- CLI-Einführung & Kommandoformat:
  <https://lyrion.org/reference/cli/using-the-cli/>
- CLI-Kommandoliste & Changelog:
  <https://lyrion.org/reference/cli/introduction/>
- Favoriten-Kommandos:
  <https://lyrion.org/reference/cli/favorites/>
- Datenbank-/Browse-/Such-Kommandos:
  <https://lyrion.org/reference/cli/database/>
- Compound Queries (serverstatus/status/displaystatus):
  <https://lyrion.org/reference/cli/compoundqueries/>
- SlimBrowse/SqueezePlay-Menüformat:
  <https://lyrion.org/reference/slimbrowse/>
