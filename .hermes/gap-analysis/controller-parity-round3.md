# Controller-Parität Runde 3 — Abarbeitung des Harness-Reports

* Datum: 2026-09-13
* Grundlage: `.hermes/gap-analysis/controller-parity-after-fixes.md` (Harness
  `tools/controller_parity.py`, 103 Kommandos, unser Server vs. Perl 9.1.1)
* Perl-Referenz: read-only `/tmp/lms-ref` + Live-Box `192.168.1.90:9000`
* Geänderte Dateien: `src/lyrion/web/api.py`, `tests/test_controller_parity_fixes.py`
* **Der Harness-Report wurde NICHT neu erzeugt**: auf `127.0.0.1:9000` läuft
  weiter der Dev-Server mit dem alten Code (Neustart laut Auftrag verboten) —
  eine Live-Probe gegen `127.0.0.1:9000` zeigt deshalb noch den Vorher-Stand.
  Belegt sind die Korrekturen durch Perl-Fundstellen +
  `tests/test_controller_parity_fixes.py` (76 Tests, EXIT 0).

Die drei Kategorien des Auftrags sind unten je Kommando ausgewiesen:
**[1] echte Fehler (korrigiert)**, **[2] bewusste Zusätze (belassen)**,
**[3] nicht messbar / Perl bricht ab (nicht angefasst)**.

## 1. Echte Fehler → korrigiert

Perl-Referenz und Live-Antworten (nur lesend, Perl 9.1.1, Player
`00:00:00:00:00:00`, Queue = 1.FM-Stream, 2026-09-13) wörtlich:

```
playlist repeat ?   -> {"_repeat":"0"}        playlist album ?    -> {}
playlist shuffle ?  -> {"_shuffle":"0"}       playlist genre ?    -> {}
playlist tracks ?   -> {"_tracks":1}          playlist title ?    -> {"_title":"pulchra somnium"}
playlist index ?    -> {"_index":"0"}         playlist path ?     -> {"_path":"http://strm112.1.fm/ambientpsy_mobile_mp3"}
playlist modified ? -> {"_modified":1}        playlist remote ?   -> {"_remote":1}
playlist name ?     -> {"_name":"1.FM - Ambient Psychill"}
playlist url ?      -> {"_url":null}          playlist duration ? -> {"_duration":"0"}
playlist artist ?   -> {"_artist":"Goabert"}
can ?               -> {"_can":0}             can play ?          -> {"_can":1}
pref ?              -> {"_p2":null}           pref audiodir ?     -> {"_p2":null}
playerpref ?        -> {"_p2":null}
```

| Kommando | Perl-Sollform | bei uns vorher | bei uns jetzt | Beleg |
|---|---|---|---|---|
| `playlist repeat ?` | `{_repeat:"0"}` | `{}` | `{_repeat:0}` | `playlistXQuery` Queries.pm:2724-2725 |
| `playlist shuffle ?` | `{_shuffle:"0"}` | `{}` | `{_shuffle:0}` | Queries.pm:2727-2728 |
| `playlist tracks ?` | `{_tracks:1}` | `{}` | `{_tracks:N}` | Queries.pm:2743-2744 (`Playlist::count`) |
| `playlist index ?` / `jump ?` | `{_index:"0"}` | `{}` | `{_index:N}` | Queries.pm:2730-2731 (`Source::playingSongIndex`) |
| `playlist modified ?` | `{_modified:1}` (undef → null, solange die Queue nie verändert wurde) | `{}` | `{_modified:null\|1}` | Queries.pm:2740-2741; die Kommandos setzen 1 (Commands.pm:831/:912/:1058/:1506), Laden einer gespeicherten Playlist 0 (:1162) |
| `playlist name ?` | `{_name:<remote_title>}` beim Stream, **kein** Result beim lokalen Track | `{}` | dito Perl | Queries.pm:2733-2734 + :2766-2767 (Tag `N` = `remote_title`) |
| `playlist url ?` | `{_url:null}` ohne gespeicherte Playlist | `{}` | `{_url:null\|<url>}` | Queries.pm:2736-2738, `Client.pm:1212-1228` |
| `playlist duration ?` | `{_duration:"0"}` | `{}` | `{_duration:<secs>}` | Queries.pm:2763-2764 (`_songData`, Tag `d`) |
| `playlist artist ?` | `{_artist:"Goabert"}` | `{}` | `{_artist:<name>}` | Queries.pm:2755-2768 (Tag `a`) |
| `playlist album ?` / `genre ?` | `{}` ohne Daten | `{}` | `{}` (bleibt leer, kein Fake) | Queries.pm:2755-2768 |
| `playlist title ?` | `{_title:"pulchra somnium"}` | `{}` | `{_title:<titel>}` | Queries.pm:2755-2768 |
| `playlist path ?` | `{_path:"http://…"}` bzw. `{_path:0}` | `{}` | dito Perl | Queries.pm:2746-2748 |
| `playlist remote ?` | `{_remote:1}`, ohne URL kein Result | `{}` | dito Perl | Queries.pm:2750-2753 |
| `can ?` | `{_can:0}` (ZAHL) | `{_can:""}` (String) | `{_can:0}` | `canQuery` Plugin/CLI/Plugin.pm:761-778 + :791, Dispatch Request.pm:400-401 |
| `can <cmd> ?` | `{_can:1}`, wenn dispatchbar (:791) | `["can play 0"]` (Liste) | `{_can:1\|0}` | Plugin/CLI/Plugin.pm:783/:791 |
| `pref ?` | `{_p2:null}` | `{_pref:""}` | `{_p2:null}` | `prefQuery` Queries.pm:3045-3048 |
| `pref audiodir ?` | `{_p2:null}` | `{audiodir:""}` (erfundener Schlüssel) | `{_p2:<pref\|null>}` | Queries.pm:3045-3048 |
| `playerpref ?` (ohne Namen) | `{_p2:null}` | `{_playerpref:""}` | `{_p2:null}` | Queries.pm:3009-3051 |

Nicht geändert: `playerpref <name> ?` bleibt wie bisher (`_p2` mit
`_PLAYERPREF_DEFAULTS`, unbekannte Pref `""`) — `tests/test_playerpref.py:186-188`
pinnt das.

## 2. Bewusste Zusätze → NICHT entfernt (nur gelistet)

* **Loop-Alias-Schlüssel** `item_loop`/`loop_loop`/`offset` in allen
  Bibliotheks-/Ordner-/Favoriten-Antworten neben Perls `<modus>_loop`
  (`artists_loop`, `albums_loop`, `genres_loop`, `titles_loop`, `folder_loop`,
  `radioss_loop`, `appss_loop`). Kein Alias hat dabei einen Perl-Schlüssel
  verdrängt — `folder_loop` ist weiter vorhanden (Report-Zeile
  `musicfolder 0 100`), `radioss_loop` ebenso (`radios 0 10`).
* `status`: `duration` und `title` auf Antwort-Ebene (Code-Kommentar bei
  `result["duration"]`: SqueezePlay/SqueezeClient brechen ohne Zahl bzw. mit 0
  ab) und `item_loop` = `playlist_loop`, wenn die Queue nicht leer ist (Jive
  `Player.lua:272-273`); `playlist_loop: []` auch bei leerer Queue
  (SqueezeClient/SPA lesen den Schlüssel).
* `serverstatus`: zusätzlich `count`, `players_loop`, `name`, `mediadirs`,
  `notice`, `scan`. Perl liefert `players_loop` nur bei gültigem
  `normalize(_index,_quantity,count)` (Queries.pm:3819-3827; ein `count` gibt es
  dort gar nicht) — Jive-Controller abonnieren aber
  `serverstatus 0 50 subscribe:60`, und Squeezer fragt die Playerliste ohne
  Range ab (Code-Kommentar im serverstatus-Abschnitt von `web/api.py`).
* `players` **ohne** Range liefert bei uns `players_loop`; Perl antwortet nur
  `{count}` (`normalize` schlägt fehl, Queries.pm:2605-2608). Bewusst, weil
  Squeezer die Liste ohne Range liest (auch im Harness als „nur bei uns“
  dokumentiert).
* `alarms 0 10`: `alarm_loop: []` bleibt (Perl fügt den Loop nur bei
  `count > 0` hinzu). `fade` ist bei Perl ein numerischer String — der Harness
  setzt das mit unserer Zahl gleich.
* `menustatus`, `apps 0 50`, `browse …`/`menu` mit Daten gegen Perls `{}`:
  Kontextunterschied der Sonde (die Referenz-Box hat keinen Jive-Menükontext,
  siehe „Methode & Grenzen“ im Harness-Report), keine Strukturfälschung.
* `playlists 0 100`: wir senden `count`+`playlists_loop` auch ohne Treffer;
  Perl antwortet `{}`, weil `count` dort nur innerhalb `if ($valid)` steht
  (Queries.pm:2932-2984, `normalize(index, quantity, count)`). Unsere Box hat
  eine gespeicherte Playlist, die Referenz nicht — Datenunterschied.
  Zum Vergleich: `musicfolder` sendet `count` bei Perl IMMER, auch außerhalb
  von `if ($valid)` (Queries.pm:2471) — unsere Form ist dort also Perl-gleich.
* `favorites items`: `item_loop` neben Perls `loop_loop` (dieselbe
  Alias-Regel).

## 3. Nicht messbar / Perl bricht ab → nicht angefasst

Unverändert die 20 Kommandos aus dem Harness-Abschnitt „Nicht messbar“:
`info total ?`, `lastscan`, `getstring ?`, `can play` (ohne `?`),
`debug ?`, `currentsong`, `currentsong 0 100`, `songinfo 0 10`, `mixer ?`,
`syncgroups`, `artworkspec`, `artworkspec ?`, `playlist 0 100`,
`playlist id ?`, `playlist year ?`, `artists 0 10 sort:name`,
`browsedb 0 10 artist`, `folderinfo`, `folders 0 10`, `favorites exists ?`.
(`playlist 0 100`, `playlist id ?`, `playlist year ?` sind bad dispatch —
`Request.pm:548-591` hat keinen passenden Eintrag; unsere `{}`-Antwort bleibt.)

## 4. Die drei bekannten offenen Punkte

1. **`status`/`count` im Nicht-Menü-Pfad**: Perl-Beleg geprüft — `count` wird
   ausschließlich innerhalb `if ($menuMode)` gesetzt
   (Queries.pm:4318-4320: `my $menuCount = $songCount?$songCount+2:0;
   $request->addResult("count", $menuCount);`), `offset` ebenso nur dort
   (:4433). Entscheidung daher: **Feld nur im Menü-Pfad**, kein Default-`count`
   für alle `status`-Antworten. Neu abgesichert durch
   `test_status_plain_has_no_count_or_offset_even_with_a_queue`.
   Ein Squeezer-`count` ohne Default ist kein Gegenargument — Squeezer läuft
   gegen Perl, das `count` dort ebenso wenig sendet.
2. **`apps` mit leerer `appss_loop`**: dokumentiert, **nicht gefakt**. Dieser
   Port hat keine `is_app`/OPML-Plugins (Perls Box liefert live
   `{"count":1,"appss_loop":[{"type":"xmlbrowser","cmd":"sounds",…}]}` über
   `Slim/Plugin/OPMLBased.pm:26-28`); die Perl-Form (Schlüssel `appss_loop`
   + `count`) wird eingehalten, die Liste bleibt leer
   (`tests/test_controller_parity_fixes.py::test_apps_is_count_plus_appss_loop`).
3. **`folder_loop.id`**: bleibt unsere Datei-URL (`str`) statt Perls
   numerischer id (Perl-Quelle `mediafolderQuery`, Queries.pm:2387-2465).
   Unsere Quelle ist `lyrion.media.folders.musicfolder_result`
   (Dateisystem-Aufzählung, kein DB-Ordnerbaum) — belegt dokumentiert, nicht
   geändert, weil `media/*` außerhalb des Auftrags liegt.

## 5. Grenzen dieser Runde

* `mixer muting ?`: Perl liefert
  `$prefs->client($client)->get("mute")` (Queries.pm:2132) und damit `null`,
  solange nie gemutet wurde (Live-Probe im Harness-Report: `{"_muting":null}`).
  Unser `PlayerState.mute` ist ein Bool ohne „nie gesetzt“-Zustand, und
  `player/*` ist im Auftrag gesperrt; `tests/test_mixer.py:89` pinnt `0`.
  Bewusst NICHT geändert, hier dokumentiert.
* Der CLI-Pfad (`control/cli_commands.py::_playlist_entity`) wurde nicht
  angefasst: er erzeugt einzeilige Render-Ergebnisse (Form durch
  `tests/test_cli_render_rest.py:730-733` gepinnt) und weicht bei
  `name`/`modified`/`url` weiter von Perl ab. Controller sprechen JSON-RPC;
  der JSON-Pfad ist jetzt Perl-konform.
* `status`-Vergleich `rate`/`remoteMeta`: Perl setzt beide nur innerhalb
  `if (my $song = $client->playingSong())` (Queries.pm:4085-4102, `rate` :4097).
  Im Harness-Lauf hatte „unser“ Player eine leere Queue, die Referenz spielte
  einen Stream — Daten-/Kontextunterschied, kein Strukturfehler. Die
  Live-Probe gegen unseren Dev-Server (spielt gerade einen Stream) enthält
  beide Felder.
