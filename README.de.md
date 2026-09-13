# Pyrion Music Server — Python-Port

Ein LMS-kompatibler Streaming-Audio-Server für Squeezebox und kompatible
Player (Squeezelite, SqueezePlay, SqueezeESP32, ...), implementiert in
Python 3. Dieses Projekt re-implementiert die SlimProto- / JSON-RPC- /
CLI-Protokollfläche des Originals (Perl) Lyrion Music Server (LMS). Das
Python-Paket bleibt `lyrion` (`python -m lyrion`, Daten unter `~/.lyrion`).

> **Status: WORK IN PROGRESS — Wiedergabe verifiziert, 2026-09-Paritätsprogramm
> weitgehend abgeschlossen.** Player-Erkennung, Registrierung, Steuerung, die
> Web-UI und die Audiowiedergabe funktionieren; die Slimproto-Display-/IR-Frames,
> der Jive-Menü-Satz, die Streaming-Zustandsmaschine, Perls einzeiliges
> CLI-Textformat sowie die Bibliotheks-Such-/Transcoding-/ReplayGain-Regeln
> wurden gegen die Perl-Referenz re-implementiert. Die Suite ist **1174
> pytest-Tests grün** (`LMS_TEST_SPAWN=1`, ohne `tests/test_contract.py`, das
> einen laufenden Server braucht). Echte offene Punkte stehen unter
> [Was nicht funktioniert](#was-nicht-funktioniert).

---

## Was funktioniert

| Bereich | Status |
|---------|--------|
| SlimProto-Server (TCP 3483) | ✅ Player-Registrierung (HELO 20/36 Byte + SETD), Namensbehandlung, Keepalive, `audg`-Lautstärkeabgleich beim Verbinden, `aude`-Ausgangs-Aktivierung beim HELO, Trennungs-Nachfrist (`FORGET_DISCONNECTED_TIME = 300 s`, `networking/protocol.py:718`) |
| Discovery-Dienst (UDP 3483) | ✅ Broadcast-Beacons, HELO-ACK |
| SlimProto-Display-Frames | ✅ `visu` / `grfb` / `grfe` / `grfd` / `vfdc` gerendert aus den echten LMS-Bitmap-Schriften (`graphics/*.font.bmp`) mit dem Now-Playing-Zeilen-Text (`player/display.py`, `player/fonts.py`) |
| IR-/Button-Opcodes (PROT-19) | ✅ `IR`- / `BUTN`- / `KNOB`-Frames aus den Perl-IR-Tabellen in Player-Aktionen übersetzt (`Slim/Networking/Slimproto.pm:521-546`, `player/buttons.py`) |
| Streaming-Zustandsmaschine | ✅ `STOPPED` / `BUFFERING` / `PLAYING` / `PAUSED` und die Übergangstabelle nach `Slim/Player/StreamingController.pm` (`player/streaming.py`) |
| JSON-RPC-API (HTTP 9000) | ✅ `server.*`-, `player.*`-, `playlist.*`-Methoden, `slim.request`-Passthrough (Playlist-Album_id/Artist_id-Erweiterung, Mixer, Shuffle/Repeat) |
| Web-Auth | ✅ Optionales Basic-Auth-Gate am `authorize`-Pref; CLI `login` prüft das konfigurierte `password` (2026-09) |
| CometD | ✅ Subscribe/Unsubscribe-Semantik, Status-Notify spielt die gespeicherte Anfrage nach (Pagination/Menü/Tags bleiben erhalten) |
| CLI (TCP 9090) | ✅ Status, Browse (Interpreten/Alben/Songs/Radio), Playlist-Steuerung, CR/NUL-terminierter Draht + prozent-dekodierte Player-IDs; jede Antwort ist **eine prozent-escapte Zeile** wie in Perl (`Slim/Plugin/CLI/Plugin.pm:692-698`, `control/queries.py`) |
| Jive-Menüs / Settings / Alarme | ✅ SqueezePlay-Settings-Menüs (Tone, Volume, XL, Line-out, Crossfade, ReplayGain, Helligkeit, Schriften, Album-Sortierung, Datum) plus Alarm-/Sync-/Sleep-/Listen-Menüs (`Slim/Control/Jive.pm:57-162`, `control/jive.py`) |
| Web-UI | ✅ Single-File-SPA (`html/index.html`, Hash-Routing, LMS-artiger Skin, keine externen Abhängigkeiten) + gebündelter Material-Skin unter `/material` |
| Web-Settings-Seiten | ✅ Klassische LMS-Settings-Seiten mit Pref-Schreibpfaden (`web/settings.py`) |
| i18n (DE) | ✅ Jive-/JSON-RPC-Texte aus Perls `strings.txt`-Tabellen lokalisiert; aktive Sprache kommt aus dem `language`-Pref (`Slim/Utils/Strings.pm:526`, `i18n/strings_de.py`) |
| Bibliotheks-Scanner | ✅ SQLite-DB (`~/.lyrion/Lyrion/Prefs/lyrion.db`), 50k+ Titel, inkrementeller Streaming-Walk, Album-Identität (Titel+Künstler), Metadaten-Heuristiken |
| Bibliotheks-Regeln | ✅ Transcoding-Regelwerk aus `convert.conf` / `types.conf` geparst (`Slim/Player/TranscodingHelper.pm:51-135`, `media/transcoding.py`); Such-Tokenizing + Relevanz-Ranking (`Slim/Plugin/FullTextSearch/Plugin.pm:486-503`, `control/queries.py`); ReplayGain-Scanner-Seite + `strm`-Gain-Kette (`media/replaygain.py`, `player/replaygain.py`) |
| Rescan | ✅ Full-/Additive-Scan-Modi, `abortscan`, Löschabgleich beim vollen Rescan (Perl-Parität, 2026-09) |
| Player-Steuerung | ✅ Power (strm stop), Volume (audg), Play/Pause/Stop/Next/Prev, Skip-ahead-Seek (`strm 'a'` 24-Byte-Frame), Resume-stellt-Position-wieder-her, Playlist-Verwaltung, Favoriten, IR-/Button-Aktionen, Alarme (pro Player, `fr:`/`track:`/`url:`-Wake-Quellen) |
| **Audiowiedergabe** | ✅ **MP3 + FLAC end-to-end auf Squeezelite v2.0.0** (Direct Streaming; Proxy/Transcode nur, wenn der Player das Quellformat nicht dekodieren kann) |
| **Radio / Favoriten** | ✅ **Direct Streaming** wie im echten LMS (strm → Quelle, Server-`cont` mit metaint, RESP-Round-Trip); AAC/HE-AAC-Streams transcode-proxied zu MP3; HTTPS-Radio spielt |
| Auto-Next / Titelende | ✅ Playlist-Vorschub bei Player-STAT `STMd`; letzter Titel → Stop; lokale Titelstarts löschen das Radio-Flag |
| Testton | ✅ `/stream.mp3?testtone=1` (440 Hz, 5 s WAV) |

## Was nicht funktioniert

Ehrliche Abgrenzung — keine Werbung. Alles Folgende ist bewusst entweder
nicht implementiert oder nur angenähert:

| Bereich | Status |
|---------|--------|
| Externe Konverter-Binaries | ❌ Das `convert.conf`-Regelwerk wird geparst und das Profil ausgewählt, aber tatsächlich gestartet wird nur **ffmpeg**. Die `[flac]`- / `[sox]`- / `[lame]`- / …-Lookups aus `convert.conf` werden für die Profilwahl aufgelöst, aber nie gestartet (`media/transcoding.py` vs. das nur-ffmpeg-`web/stream.py`) |
| Volltextsuche-Ranking | ⚠️ Die Relevanz ist eine **LIKE-Näherung** des FullTextSearch-Plugins (`fulltext_weight`, `control/queries.py`); die SQLite-FTS-`matchinfo`-Semantik ist nicht reproduziert |
| Font-Renderer: TrueType / BiDi | ❌ Nur der Bitmap-Pfad `.font.bmp` ist portiert. Der TTF-Zweig (`Fonts.pm:318-355`, `Font::FreeType`) und die hebräische BiDi-Umkehr (`:349-354`) fehlen (`player/fonts.py`) |
| Player-`screen2` | ❌ Nicht verdrahtet. Perl zeigt diesen Album/Interpret-Screen nur auf dem Transporter (`Display.pm:859`, `Transporter.pm:319-325`); der Port sendet den einen Now-Playing- + Visualizer-Frame-Satz |
| Alarm-Details | ❌ Perls `alarmTimeoutSeconds`-Timer und die `['alarm','snooze']`-Notifications fehlen (`alarms.py`) |
| StreamingController-Buffer-Entscheide | ⏳ `_CheckPaused` / `_CheckSync` (Rebuffer/Pause je nach Buffer-Zustand) brauchen `usage()` / `bufferFullness()`-STAT-Daten eines echten Clients; die Zustandsmaschine existiert, die Füllstand-Entscheide werden nicht ausgeführt (`player/streaming.py`) |
| Sync (Multi-Player-Gruppenwiedergabe) | ⏳ Gruppen-Buchhaltung und die Jive-Sync-Menüs existieren; echtes synchronisiertes Fan-Out ist weiterhin die letzte bekannte Paritätslücke |
| Server-seitiger HTTPS-Listener | ❌ Der Lausch-Socket ist HTTP; entfernte HTTPS-Streams spielen problemlos (TLS auf Player-Seite oder Transcode-Proxy) |
| Playlist-**Datei**-Import (`.m3u` auf Platte) | ❌ In der DB gespeicherte Playlists werden voll unterstützt (CLI + JSON); das Scannen von Playlist-*Dateien* aus dem Medienverzeichnis ist nicht implementiert |

## Betrieb

- **Rescan nach Schema-Änderungen.** Einige Bibliotheksantworten lesen Spalten,
  die eine spätere Schema-Version gefüllt hat. Echte Genre-IDs (`genres.id`)
  schreibt nur der Rescan-Importer (`Slim/Schema/Genre.pm:91-132`,
  `media/importer.py`) — nach einer Schema-Änderung muss die Bibliothek deshalb
  einmal neu gescannt werden, sonst liefern `genres`-Abfragen weiterhin
  veraltete/leere IDs.
- **Neustart über `tools/restart_server.sh`, nicht von Hand.** Es wartet, bis
  der alte Prozess die Ports wirklich freigegeben hat (bis zu 90 s), startet
  dann genau eine neue Instanz und prüft alle vier Ports (9000/9090/9080/3483).
  Lehre aus zwei Doppelstarts: `kill -TERM` braucht bei großer Bibliothek
  deutlich länger als ein paar Sekunden, und ein zu früher Neustart lässt zwei
  Instanzen zurück, von denen eine 9090/3483 nicht binden kann (`OSError 98`)
  → der CLI-Listener ist tot.
- **DB-Sicherung vor Schema-Eingriffen.** Vorher die `.db`-Datei kopieren;
  `tools/_drop_stale_album_unique.py` legt vor dem Schreiben ein
  zeitgestempeltes `.bak-<ts>` an.

## Architektur

```
src/lyrion/
├── __main__.py          # Einstiegspunkt; startet SlimProtoServer, DiscoveryService, CLI, Web
├── networking/
│   ├── protocol.py      # SlimProto-Frames (HELO, strm, stat, setd, audg, aude, IR/BUTN/KNOB)
│   └── discovery.py     # UDP-Discovery-Beacons
├── player/
│   ├── manager.py       # PlayerManager: register, play_track/play_url, Volume/Power
│   ├── state.py         # PlayerState-Dataclass
│   ├── streaming.py     # StreamingController-Zustandsmaschine (Perl StreamingController.pm)
│   ├── display.py       # visu/grfb/grfe/grfd/vfdc-Frame-Rendering
│   ├── fonts.py         # LMS-Bitmap-Font-Parser (.font.bmp)
│   ├── buttons.py       # IR/BUTN/KNOB-Opcodes → Aktionen (PROT-19)
│   ├── replaygain.py    # Gain-Auswahl für strm-Frames
│   └── playerprefs.py   # Player-Prefs
├── music/
│   ├── scanner.py       # Bibliotheks-Scan → SQLite
│   └── radio.py         # Radio-Sender-Browser
├── media/
│   ├── importer.py      # SQLite-Import (Album-Identität, Genres, ReplayGain)
│   ├── transcoding.py   # convert.conf/types.conf-Regelwerk + Profilwahl
│   └── replaygain.py    # ReplayGain-Tag-Munging (Scanner-Seite)
├── control/
│   ├── cli.py           # TCP-CLI (9090)
│   ├── cli_commands.py
│   ├── jive.py          # Jive/SqueezePlay-Menüs, Settings, Alarme
│   └── queries.py       # Perl-CLI-Drahtformat + Such-Ranking
├── web/
│   ├── api.py           # JSON-RPC / slim.request-Handler
│   ├── cometd.py        # CometD-Long-Polling
│   ├── settings.py      # klassische Settings-Seiten
│   └── stream.py        # /stream.mp3 (Tracks, Testton, Remote-Proxy)
├── display/
│   └── screens.py       # Display-Screens
├── i18n/
│   ├── strings_en.py    # EN-Failsafe-Strings (Perl strings.txt)
│   └── strings_de.py    # DE-Strings
├── alarms.py            # Alarmuhr pro Player (JSON unter Prefs)
└── database/
    ├── schema.py        # SQLAlchemy-Schema
    └── sqlite_helper.py # asynchrone DB-Session
```

## Starten

```bash
# installieren (pyproject.toml) und starten
python3 -m lyrion --loglevel info
```

Web-UI: http://localhost:9000/ · JSON-RPC: `POST /jsonrpc.js` · CLI: Port 9090 ·
SlimProto/UDP: 3483. Neustart über `tools/restart_server.sh` (wartet auf echte
Port-Freigabe — siehe [Betrieb](#betrieb)); eine mitgelieferte systemd-Unit gibt
es nicht.

Um ohne Störung eines bestehenden (Perl-)LMS auf den Standardports zu testen,
`--localfile` mit anderen Ports verwenden:

```bash
# test-ports.conf:  serverport = 9002 / slimproto_port = 3484 / cliport = 9091
python3 -m lyrion --localfile test-ports.conf --loglevel debug
/tmp/squeezelite/squeezelite -s 127.0.0.1:3484 -m 02:11:22:33:44:55 -n TestPlayer \
  -o hw:Loopback,0 -d slimproto=debug -d stream=debug -d decode=debug -d output=debug
# Audio aufnehmen:  arecord -D hw:Loopback,1,0 -f S32_LE -r 44100 -c 2 -t wav /tmp/cap.wav
```

Test-Suite ausführen (ein laufender Server ist nur für die Contract-Tests nötig):

```bash
LMS_TEST_SPAWN=1 .venv/bin/python -m pytest --ignore=tests/test_contract.py
# → 1174 passed
```

## Release Notes

Jüngste Runden des 2026-09-Paritätsprogramms (alles committet und grün):

- **Slimproto-Display (PROT-18).** `visu`/`vfdc`/`grfb` ergänzt, `grfe`/`grfd`/`vfdc`
  wie Perl aus den echten `.font.bmp`-Dateien gerendert, ein framebuffer-gestütztes
  `grfe` und der Now-Playing-Display-Text; dazu Info-Totals über JSON-RPC
  (`player/display.py`, `player/fonts.py`).
- **IR-/Hardware-Buttons (PROT-19).** `IR`/`BUTN`/`KNOB`-Opcodes aus den
  Perl-IR-Tabellen in Player-Aktionen übersetzt (`player/buttons.py`).
- **StreamingController-Zustandsmaschine** gegen
  `Slim/Player/StreamingController.pm` abgeglichen (`player/streaming.py`).
- **Jive-Menüs / Settings / Alarme.** SqueezePlay-Settings-Menüs, Alarm-/Sync-/
  Sleep-/Listen-Menüs sowie die Alarm-/Preset-/Such-Befehle (`control/jive.py`).
- **CLI-Textformat (CTRL-02).** Jeder Befehl antwortet jetzt in Perls einer
  prozent-escapten Zeile (`control/queries.py`, `control/cli_commands.py`).
- **Bibliothek (LIB-10/16/17/35).** Perl-Transcoding-Regeln aus `convert.conf`,
  Such-Ranking, echte Genre-IDs, ReplayGain lesen + auf `strm` anwenden
  (`media/transcoding.py`, `control/queries.py`, `media/replaygain.py`).
- **Web-Settings-Seiten (WEB-01/02).** Klassische Settings-Seiten mit Schreibpfaden
  (`web/settings.py`).
- **i18n.** DE-Strings aus Perls `strings.txt` erzeugt (`i18n/strings_de.py`).
- **Alarme.** Snooze-Re-Arm, Snooze-Länge aus den Prefs, Pre-Alarm-State-Restore
  (`alarms.py`).
- **Betrieb.** `tools/restart_server.sh` wartet jetzt auf eine echte Port-Freigabe.

## Herkunft

Der Perl-LMS (Lyrion Music Server, public/9.2) ist die Referenz für jeden
Protokollpfad — **nur als Vorbild**, nie als Laufzeit-Abhängigkeit; der Port
linkt ihn weder noch ruft er ihn per Shell auf. Wo ein Verhalten aus Perl
stammt, trägt der Code den Beleg inline als `Slim/...pm:Zeile`
(Beispiele: `player/buttons.py` zitiert `Slim/Networking/Slimproto.pm:521-546`;
`media/transcoding.py` zitiert `Slim/Player/TranscodingHelper.pm:51-135`).

Die Zahlen in dieser README sind gegen das Repo geprüft: 1174 Tests
(`pytest --ignore=tests/test_contract.py`, `LMS_TEST_SPAWN=1`); Ports 3483
(SlimProto/UDP), 9000 (HTTP, `config.py` `serverport`), 9090 (CLI,
`config.py` `cliport`); Trennungs-Nachfrist `300 s` (`networking/protocol.py:718`).

## Hinweise

- Das strm-Frame-Layout folgt dem Perl-LMS (`pack 'aaaaaaaCCCaCCCNnN'`
  in `Slim/Player/Squeezebox.pm`; `networking/protocol.py:1869`). Für normale
  Proxy-Streams `autostart='1'` (ASCII) — `'3'` würde `cont_wait` auf
  Squeezelite setzen (`autostart - '0' >= 2`) und ewig auf ein `cont`-Frame
  warten, das LMS nie sendet. `transition_type='0'` (ASCII), PCM-Felder `'?'`,
  Threshold/output_threshold numerisch.
- Wie im Original-LMS werden Bibliothekstracks über den Server geholt
  (`GET /stream.mp3?player=MAC HTTP/1.0`, kein Host-Header). **Radio-Streams
  werden DIREKT von der Quelle gestreamt** (stream_s-`$isDirect`-Zweig): Das
  strm-Frame trägt Quell-IP/Port + den Quell-Request-String (HTTP.pm
  requestString, `Icy-MetaData: 1`), `autostart=3` (direct), SSL-Flag `0x20`
  für https. Nachdem der Player verbindet, leitet er die Response-Header der
  Quelle als RESP-Frame weiter; der Server antwortet mit einem `cont`-Frame
  (`metaint, loop, guids`), damit der Player die Icecast-Metadaten selbst
  entfernt. Der Stream spielt weiter, wenn der Server verschwindet.
  Proxy-Fallback nur, wenn die Quelle nicht aufgelöst werden kann (der Proxy
  entfernt die Icecast-Metadaten dann server-seitig — Squeezelite parst
  `icy-metaint` nicht aus Headern).
- **audg beim Verbinden ist Pflicht**: Squeezelite initialisiert seinen internen
  Gain mit 0; ohne `audg`-Frame wird jedes Sample mit 0 multipliziert →
  Decoder läuft, Output läuft, aber nichts ist hörbar. Der Server sendet die
  aktuelle Lautstärke (gain = volume * 655.36) direkt nach HELO.
- **DSCO ist Stream-Ende, kein Disconnect**: Squeezelite sendet `DSCO` und
  verbindet neu, wann immer ein Stream endet (bei lokalen Dateien direkt nach
  dem strm). Der Player darf dann NICHT abgemeldet werden —
  Playlist/Volume/Modus überleben das Reconnect (nur `bye`/TCP-Close meldet ab).
- **Auto-Next wird durch Player-STAT `STMd` getrieben** (Decoder fertig), nicht
  durch den Abschluss des HTTP-Sendens — der Server kann eine ganze lokale
  Datei in <1 s in den Puffer des Players schieben, während sie noch 30 s
  spielt; ein Vorschub bei Sende-Ende startet den Titel in einer Endlosschleife
  neu.
- Der Web-Port wird in den SlimProto-Handler durchgereicht
  (`SlimProtoClient(web_port=...)`), damit `server_port` des strm-Frames und
  `serverstatus.httpport` nicht-standardmäßigen HTTP-Ports folgen.
