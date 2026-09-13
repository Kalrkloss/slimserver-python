# Controller-Parität: JSON-RPC-Strukturvergleich (unser LMS vs. Perl-LMS)

* Datum: 2026-09-13 17:18:27
* unser Server: `http://127.0.0.1:9000/jsonrpc.js` (Version `9.2.0`, Player `1C:87:2C:47:FC:36`)
* Perl-LMS: `http://192.168.1.90:9000/jsonrpc.js` (Version `9.1.1`, Player `00:00:00:00:00:00`)
* verglichene Kommandos: 103 (Laufzeit 347.4s)
* Werkzeug: `tools/controller_parity.py` (nur lesende Kommandos)

Verglichen wird die **Struktur** der `result`-Antwort: Schlüsselnamen, Verschachtelung, Wert-Typen, Loop-Schlüssel. Zahlen und numerische Strings werden als Typ `num` gleichgesetzt; MACs/UUIDs/IPs/lange Hex-Strings sind normalisiert. Datenwerte gehen nicht in die Bewertung ein.

## Zusammenfassung

| Bewertung | Anzahl |
|---|---|
| gleich | 21 |
| abweichend | 48 |
| fehlt | 12 |
| Fehler (unser) | 2 |
| Fehler (Perl) | 20 |
| **Summe** | **103** |

### Umschlag (Envelope)

Die JSON-RPC-Hülle unterscheidet sich bei 103/103 Antworten:

* unser: `{"jsonrpc": "num", "result": "dict", "id": "num"}`
* Perl:  `{"result": "dict", "params": "list", "id": "num", "method": "str"}`
* Perl echot `method`, `params` und `id` auf Top-Level und setzt kein `jsonrpc`-Feld; wir setzen `jsonrpc:"2.0"`, aber echot `method`/`params` nicht.

## Kurzfassung der Befunde

* **Loop-Schlüssel abweichend (36):** unsere Listen liegen bei Teilen der Bibliotheks-/Playlist-Antworten unter anderen Schlüsseln als bei Perl — siehe Abschnitt "Loop-Schlüssel: Perl vs. wir".
* **Kommando fehlt / wird nur gespiegelt (12):** `rescanprogress`, `roles`, `player count ?`, `player id ?`, `player name ?`, `player model ?`, `player ip ?`, `years 0 10`, `tracks 0 10`, `tracks 0 10 album_id:2`, `years 0 10 sort:year`, `apps 0 50`
* **Top-Level-Schlüssel fehlen bei uns (17):** `pref ?`, `pref audiodir ?`, `status`, `status 0 100`, `status 0 100 tags:gald`, `status 0 100 tags:acdtu`, `status 0 5 tags:cdtu`, `playlist repeat ?`, `playlist shuffle ?`, `playerpref ?`, `playlist tracks ?`, `playlist name ?`, `playlist modified ?`, `playlist duration ?`…
* **Perl-Transportabbruch, nicht messbar:** 20 Kommandos (siehe Top-Abweichungen).

## Loop-Schlüssel: Perl vs. wir

Client-Bibliotheken lesen die Listendaten über den **Loop-Schlüssel** aus der Antwort (`result.<key>_loop`). Weicht der Name ab, findet der Client die Liste nicht. Nachfolgend nur die Zeilen, in denen die Loop-Schlüssel abweichen:

| Kommando | Perl | wir |
|---|---|---|
| `alarms 0 10` | — | `alarm_loop` |
| `albums 0 10` | `albums_loop` | `loop_loop`, `item_loop`, `albums_loop` |
| `albums 0 10 artist_id:2` | — | `loop_loop`, `item_loop`, `albums_loop` |
| `albums 0 10 genre_id:3 sort:album` | — | `loop_loop`, `item_loop`, `albums_loop` |
| `albums 0 10 tags:j` | `albums_loop` | `loop_loop`, `item_loop`, `albums_loop` |
| `albums 0 10 year:2000` | `albums_loop` | `loop_loop`, `item_loop`, `albums_loop` |
| `apps 0 50` | `appss_loop` | — |
| `artists 0 10` | `artists_loop` | `loop_loop`, `item_loop`, `artists_loop` |
| `artists 0 10 search:beatles` | `artists_loop` | `loop_loop`, `item_loop`, `artists_loop` |
| `artists 0 10 sort:name` | — | `loop_loop`, `item_loop`, `artists_loop` |
| `browse albums 0 10` | — | `loop_loop`, `item_loop`, `albums_loop` |
| `browse apps 0 10` | — | `loop_loop`, `item_loop` |
| `browse artists 0 10` | — | `loop_loop`, `item_loop`, `artists_loop` |
| `browse artists 0 10 genre_id:3` | — | `loop_loop`, `item_loop`, `artists_loop` |
| `browse genres 0 10` | — | `loop_loop`, `item_loop`, `genres_loop` |
| `browse playlists 0 10` | — | `loop_loop`, `item_loop` |
| `browse years 0 10` | — | `loop_loop`, `item_loop` |
| `favorites items 0 100` | `loop_loop` | `loop_loop`, `item_loop` |
| `genres 0 10` | `genres_loop` | `loop_loop`, `item_loop`, `genres_loop` |
| `genres 0 10 sort:name` | `genres_loop` | `loop_loop`, `item_loop`, `genres_loop` |
| `menu` | — | `item_loop` |
| `menu 0 10` | — | `item_loop` |
| `musicfolder 0 100` | `folder_loop` | `loop_loop`, `item_loop`, `musicfolder_loop` |
| `players` | — | `players_loop` |
| `playlists 0 100` | — | `playlists_loop` |
| `radios 0 10` | `radioss_loop` | — |
| `serverstatus` | — | `players_loop` |
| `songinfo 0 10` | — | `loop_loop`, `item_loop` |
| `songs 0 10` | `titles_loop` | `loop_loop`, `item_loop`, `titles_loop` |
| `songs 0 10 genre_id:3` | — | `loop_loop`, `item_loop`, `titles_loop` |
| `status` | — | `playlist_loop` |
| `titles 0 10` | `titles_loop` | `loop_loop`, `item_loop`, `titles_loop` |
| `titles 0 10 album_id:2 tags:title` | — | `loop_loop`, `item_loop`, `titles_loop` |
| `tracks 0 10` | `titles_loop` | — |
| `years 0 10` | `years_loop` | — |
| `years 0 10 sort:year` | `years_loop` | — |

## Tabelle

| Kommando | Perl-Struktur | unsere Struktur | Bewertung |
|---|---|---|---|
| `serverstatus` | {httpport:num, info total albums:num, info total artists:num, info total duration:num, info total genres:num, info total songs:nu… | {count:num, httpport:num, info total albums:num, info total artists:num, info total duration:num, info total genres:num, info tot… | abweichend |
| `info total ?` | <conn_closed> | {} | Fehler (Perl) |
| `info total albums ?` | {_albums:num} | {_albums:num} | gleich |
| `info total artists ?` | {_artists:num} | {_artists:num} | gleich |
| `info total genres ?` | {_genres:num} | {_genres:num} | gleich |
| `info total songs ?` | {_songs:num} | {_songs:num} | gleich |
| `info total duration ?` | {_duration:num} | {_duration:num} | gleich |
| `lastscan` | <conn_closed> | {} | Fehler (Perl) |
| `rescanprogress` | {rescan:num} | [str] | fehlt |
| `pref ?` | {_p2:null} | {_pref:str} | abweichend |
| `pref audiodir ?` | {_p2:null} | {audiodir:str} | abweichend |
| `libraries` | {} | {} | gleich |
| `works` | {count:num} | {count:num} | gleich |
| `roles` | {count:num} | [str] | fehlt |
| `getstring` | {} | {} | gleich |
| `getstring ?` | <conn_closed> | {} | Fehler (Perl) |
| `can ?` | {_can:num} | {_can:str} | abweichend |
| `can play` | <conn_closed> | [str] | Fehler (Perl) |
| `debug ?` | <conn_closed> | {} | Fehler (Perl) |
| `players 0 99` | {count:num, players_loop:[{canpoweroff:num, connected:num, displaytype:str, firmware:str, ip:str, isplayer:num, isplaying:num, mo… | {count:num, players_loop:[{canpoweroff:num, connected:num, displaytype:str, firmware:str, ip:str, isplayer:num, isplaying:num, mo… | abweichend |
| `players` | {count:num} | {count:num, players_loop:[{canpoweroff:num, connected:num, displaytype:str, firmware:str, ip:str, isplayer:num, isplaying:num, mo… | abweichend |
| `player count ?` | {_count:num} | [str] | fehlt |
| `player id ?` | {_id:str} | [str] | fehlt |
| `player name ?` | {_name:str} | [str] | fehlt |
| `player model ?` | {_model:str} | [str] | fehlt |
| `player ip ?` | {_ip:str} | [str] | fehlt |
| `status` | {current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str,… | {album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_cont… | abweichend |
| `status 0 100` | {current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str,… | {album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_cont… | abweichend |
| `status 0 100 tags:gald` | {current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str,… | {album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_cont… | abweichend |
| `status 0 100 tags:acdtu` | {current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str,… | {album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_cont… | abweichend |
| `status 0 5 tags:cdtu` | {current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str,… | {album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_cont… | abweichend |
| `currentsong` | <conn_closed> | [str] | Fehler (Perl) |
| `currentsong 0 100` | <conn_closed> | [str] | Fehler (Perl) |
| `songinfo 0 10` | <conn_closed> | {count:num, item_loop:[], loop_loop:[], offset:num} | Fehler (Perl) |
| `title ?` | {_title:str} | {_title:str} | gleich |
| `duration ?` | {_duration:num} | {_duration:num} | gleich |
| `mixer ?` | <conn_closed> | {_mixer:str} | Fehler (Perl) |
| `mixer volume ?` | {_volume:num} | {_volume:num} | gleich |
| `mixer muting ?` | {_muting:null} | {_muting:num} | abweichend |
| `mode ?` | {_mode:str} | {_mode:str} | gleich |
| `time ?` | {_time:num} | {_time:num} | gleich |
| `playlist repeat ?` | {_repeat:num} | {} | abweichend |
| `playlist shuffle ?` | {_shuffle:num} | {} | abweichend |
| `gototime ?` | {_time:num} | {_time:num} | gleich |
| `displaystatus` | {} | {} | gleich |
| `playerpref ?` | {_p2:null} | {_playerpref:str} | abweichend |
| `playerpref digitalVolumeControl ?` | {_p2:num} | {_p2:num} | gleich |
| `irenable ?` | {_irenable:num} | {_irenable:num} | gleich |
| `linesperscreen ?` | {_linesperscreen:num} | {_linesperscreen:num} | gleich |
| `alarms 0 10` | {count:num, fade:num} | {alarm_loop:[], count:num, fade:num} | abweichend |
| `syncgroups` | <conn_closed> | [str] | Fehler (Perl) |
| `artworkspec` | <conn_closed> | {} | Fehler (Perl) |
| `artworkspec ?` | <conn_closed> | {} | Fehler (Perl) |
| `playlist 0 100` | <conn_closed> | {} | Fehler (Perl) |
| `playlist tracks ?` | {_tracks:num} | {} | abweichend |
| `playlist name ?` | {_name:str} | {} | abweichend |
| `playlist modified ?` | {_modified:num} | {} | abweichend |
| `playlist duration ?` | {_duration:num} | {} | abweichend |
| `playlist id ?` | <conn_closed> | {} | Fehler (Perl) |
| `playlist artist ?` | {_artist:str} | {} | abweichend |
| `playlist album ?` | {} | {} | gleich |
| `playlist genre ?` | {} | {} | gleich |
| `playlist year ?` | <conn_closed> | {} | Fehler (Perl) |
| `playlist url ?` | {_url:null} | {} | abweichend |
| `playlists 0 100` | {} | {count:num, playlists_loop:[{id:num, playlist:str}]} | abweichend |
| `artists 0 10` | {artists_loop:[{artist:str, favorites_url:str, id:num}\|{artist:num, favorites_url:str, id:num}], count:num} | {artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, it… | abweichend |
| `albums 0 10` | {albums_loop:[{album:str, favorites_title:str, favorites_url:str, id:num, performance:str}], count:num} | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `genres 0 10` | {count:num, genres_loop:[{favorites_url:str, genre:null, id:num}]} | {count:num, genres_loop:[{actions:{…1}, favorites_url:str, genre:str, hasitems:num, id:num, name:str, text:str, title:str, type:s… | abweichend |
| `years 0 10` | {count:num, years_loop:[{favorites_url:str, year:num}]} | [str] | fehlt |
| `titles 0 10` | {count:num, titles_loop:[{album:str, artist:str, duration:num, genre:str, id:num, title:str}]} | {count:num, item_loop:[{actions:{…2}, album:str, artist:str, artwork_url:str, coverart:num, coverid:num, duration:num, genre:str,… | abweichend |
| `tracks 0 10` | {count:num, titles_loop:[{album:str, artist:str, duration:num, genre:str, id:num, title:str}]} | [str] | fehlt |
| `songs 0 10` | {count:num, titles_loop:[{album:str, artist:str, duration:num, genre:str, id:num, title:str}]} | {count:num, item_loop:[{actions:{…2}, album:str, artist:str, artwork_url:str, coverart:num, coverid:num, duration:num, genre:str,… | abweichend |
| `artists 0 10 search:beatles` | {artists_loop:[{artist:str, favorites_url:str, id:num}], count:num} | {artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, it… | abweichend |
| `artists 0 10 sort:name` | <conn_closed> | {artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, it… | Fehler (Perl) |
| `albums 0 10 artist_id:2` | {count:num} | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `albums 0 10 year:2000` | {albums_loop:[{album:str, favorites_title:str, favorites_url:str, id:num, performance:str}], count:num} | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `albums 0 10 genre_id:3 sort:album` | {count:num} | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `albums 0 10 tags:j` | {albums_loop:[{favorites_title:null, favorites_url:str, id:num, performance:str}\|{artwork_track_id:str, favorites_title:null, fa… | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `titles 0 10 album_id:2 tags:title` | {count:num} | {count:num, item_loop:[{actions:{…2}, album:str, artist:str, duration:num, genre:str, hasitems:num, id:num, text:str, title:str, … | abweichend |
| `songs 0 10 genre_id:3` | {count:num} | {count:num, item_loop:[{actions:{…2}, album:str, artist:str, artwork_url:str, coverart:num, coverid:num, duration:num, genre:str,… | abweichend |
| `tracks 0 10 album_id:2` | {count:num} | [str] | fehlt |
| `genres 0 10 sort:name` | {count:num, genres_loop:[{favorites_url:str, genre:null, id:num}]} | {count:num, genres_loop:[{actions:{…1}, favorites_url:str, genre:str, hasitems:num, id:num, name:str, text:str, title:str, type:s… | abweichend |
| `years 0 10 sort:year` | {count:num, years_loop:[{favorites_url:str, year:num}]} | [str] | fehlt |
| `browse artists 0 10` | {} | {artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, it… | abweichend |
| `browse albums 0 10` | {} | {albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, … | abweichend |
| `browse genres 0 10` | {} | {count:num, genres_loop:[{actions:{…1}, favorites_url:str, genre:str, hasitems:num, id:num, name:str, text:str, title:str, type:s… | abweichend |
| `browse years 0 10` | {} | {count:num, item_loop:[], loop_loop:[], offset:num} | abweichend |
| `browse playlists 0 10` | {} | {count:num, item_loop:[], loop_loop:[], offset:num} | abweichend |
| `browse apps 0 10` | {} | {count:num, item_loop:[], loop_loop:[], offset:num} | abweichend |
| `browse radios 0 10` | {} | <error> | Fehler (unser) |
| `browse artists 0 10 genre_id:3` | {} | {artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}\|{actions:{…2},… | abweichend |
| `browsedb 0 10 artist` | <conn_closed> | [str] | Fehler (Perl) |
| `menu` | {} | {base:{id:str, name:str}, count:num, item_loop:[{hasitems:num, id:str, index:num, isANode:num, node:str, text:str, weight:num}\|{… | abweichend |
| `menu 0 10` | {} | {base:{id:str, name:str}, count:num, item_loop:[{hasitems:num, id:str, index:num, isANode:num, node:str, text:str, weight:num}\|{… | abweichend |
| `menustatus` | {} | [null\|[{hasitems:num, id:str, isANode:num, node:str, text:str, weight:num}\|{actions:{…2}, id:str, node:str, text:str, weight:nu… | abweichend |
| `musicfolder 0 100` | {count:num, folder_loop:[{filename:str, id:num, type:str}]} | {count:num, item_loop:[{hasitems:num, id:str, name:str, text:str, title:str, type:str}], loop_loop:[{hasitems:num, id:str, name:s… | abweichend |
| `folderinfo` | <conn_closed> | [str] | Fehler (Perl) |
| `folders 0 10` | <conn_closed> | [str] | Fehler (Perl) |
| `readdirectory 0 10` | {count:num} | {count:num} | gleich |
| `favorites items 0 100` | {count:num, loop_loop:[{hasitems:num, id:str, image:str, isaudio:num, name:str}], title:str} | {count:num, item_loop:[{actions:{…1}, hasitems:num, id:num, image:str, isaudio:num, name:str, position:num, text:str, title:str, … | abweichend |
| `favorites exists ?` | <conn_closed> | {} | Fehler (Perl) |
| `radios 0 10` | {count:num, radioss_loop:[{cmd:str, icon:str, name:str, type:str, weight:num}]} | <error> | Fehler (unser) |
| `apps 0 50` | {appss_loop:[{cmd:str, icon:str, name:str, type:str, weight:num}], count:num} | [str] | fehlt |

## Top-Abweichungen, priorisiert

_82 Kommandos weichen ab / fehlen / sind fehlerhaft (62 bewertbar, 20 auf der Referenz nicht messbar). Priorität = Nutzungsgewicht im Controller x Schwere x Strukturmerkmal (Loop-Schlüssel, fehlende Top-Level-Schlüssel, Typen)._

1. **`player count ?` — fehlt** (Score 145, Gewicht 1.2)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{_count:num}`
   * wir:  `[str]`
2. **`player id ?` — fehlt** (Score 125, Gewicht 1.0)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{_id:str}`
   * wir:  `[str]`
3. **`player name ?` — fehlt** (Score 125, Gewicht 1.0)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{_name:str}`
   * wir:  `[str]`
4. **`rescanprogress` — fehlt** (Score 125, Gewicht 1.0)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{rescan:num}`
   * wir:  `[str]`
5. **`years 0 10` — fehlt** (Score 125, Gewicht 1.0)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{count:num, years_loop:[{favorites_url:str, year:num}]}`
   * wir:  `[str]`
6. **`player model ?` — fehlt** (Score 115, Gewicht 0.9)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{_model:str}`
   * wir:  `[str]`
7. **`tracks 0 10` — fehlt** (Score 115, Gewicht 0.9)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{count:num, titles_loop:[{album:str, artist:str, duration:num, genre:str, id:num, title:str}]}`
   * wir:  `[str]`
8. **`apps 0 50` — fehlt** (Score 105, Gewicht 0.8)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{appss_loop:[{cmd:str, icon:str, name:str, type:str, weight:num}], count:num}`
   * wir:  `[str]`
9. **`tracks 0 10 album_id:2` — fehlt** (Score 105, Gewicht 0.8)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{count:num}`
   * wir:  `[str]`
10. **`status` — abweichend** (Score 102, Gewicht 1.3)
   * Loop-Schlüssel: Perl `—` vs. wir `playlist_loop`; fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url
   * Perl: `{current_title:str, digital_volume_control:num, mixer volume:num, mode:str, player_connected:num, player_ip:str, player_name:str, playlist mode:str, …`
   * wir:  `{album:str, artist:str, count:num, current_album:str, current_artist:str, current_title:str, current_url:str, digital_volume_control:num, duration:nu…`
11. **`browse albums 0 10` — abweichend** (Score 100, Gewicht 1.2)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: albums_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, title:str, type:str,…`
12. **`browse artists 0 10` — abweichend** (Score 100, Gewicht 1.2)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: artists_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, item_loop:[{actions:{……`
13. **`menu` — abweichend** (Score 96, Gewicht 1.1)
   * Loop-Schlüssel: Perl `—` vs. wir `item_loop`; nur bei uns: base, count, item_loop, offset, title; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{base:{id:str, name:str}, count:num, item_loop:[{hasitems:num, id:str, index:num, isANode:num, node:str, text:str, weight:num}\|{actions:{…2}, id:str…`
14. **`menu 0 10` — abweichend** (Score 96, Gewicht 1.1)
   * Loop-Schlüssel: Perl `—` vs. wir `item_loop`; nur bei uns: base, count, item_loop, offset, title; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{base:{id:str, name:str}, count:num, item_loop:[{hasitems:num, id:str, index:num, isANode:num, node:str, text:str, weight:num}\|{actions:{…2}, id:str…`
15. **`player ip ?` — fehlt** (Score 95, Gewicht 0.7)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{_ip:str}`
   * wir:  `[str]`
16. **`years 0 10 sort:year` — fehlt** (Score 95, Gewicht 0.7)
   * Perl liefert Daten, wir spiegeln nur das Kommando
   * Perl: `{count:num, years_loop:[{favorites_url:str, year:num}]}`
   * wir:  `[str]`
17. **`browse artists 0 10 genre_id:3` — abweichend** (Score 92, Gewicht 1.0)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: artists_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}\|{actions:{…2}, artist:num, favorit…`
18. **`browse genres 0 10` — abweichend** (Score 92, Gewicht 1.0)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,genres_loop`; nur bei uns: count, genres_loop, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{count:num, genres_loop:[{actions:{…1}, favorites_url:str, genre:str, hasitems:num, id:num, name:str, text:str, title:str, type:str}\|{actions:{…1}, …`
19. **`albums 0 10` — abweichend** (Score 90, Gewicht 1.3)
   * Loop-Schlüssel: Perl `albums_loop` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: item_loop, loop_loop, offset
   * Perl: `{albums_loop:[{album:str, favorites_title:str, favorites_url:str, id:num, performance:str}], count:num}`
   * wir:  `{albums_loop:[{actions:{…2}, album:str, favorites_title:str, favorites_url:str, hasitems:num, id:num, performance:str, text:str, title:str, type:str,…`
20. **`artists 0 10` — abweichend** (Score 90, Gewicht 1.3)
   * Loop-Schlüssel: Perl `artists_loop` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: item_loop, loop_loop, offset
   * Perl: `{artists_loop:[{artist:str, favorites_url:str, id:num}\|{artist:num, favorites_url:str, id:num}], count:num}`
   * wir:  `{artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems:num, id:num, text:str, title:str, type:str}], count:num, item_loop:[{actions:{……`
21. **`musicfolder 0 100` — abweichend** (Score 90, Gewicht 1.0)
   * Loop-Schlüssel: Perl `folder_loop` vs. wir `loop_loop,item_loop,musicfolder_loop`; fehlt bei uns: folder_loop; nur bei uns: item_loop, loop_loop, musicfolder_loop, offset
   * Perl: `{count:num, folder_loop:[{filename:str, id:num, type:str}]}`
   * wir:  `{count:num, item_loop:[{hasitems:num, id:str, name:str, text:str, title:str, type:str}], loop_loop:[{hasitems:num, id:str, name:str, text:str, title:…`
22. **`browse playlists 0 10` — abweichend** (Score 88, Gewicht 0.9)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop`; nur bei uns: count, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{count:num, item_loop:[], loop_loop:[], offset:num}`
23. **`browse years 0 10` — abweichend** (Score 88, Gewicht 0.9)
   * Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop`; nur bei uns: count, item_loop, loop_loop, offset; wir mit Daten, Perl leer
   * Perl: `{}`
   * wir:  `{count:num, item_loop:[], loop_loop:[], offset:num}`
24. **`serverstatus` — abweichend** (Score 86, Gewicht 1.2)
   * Loop-Schlüssel: Perl `—` vs. wir `players_loop`; nur bei uns: count, mediadirs, name, notice, players_loop, scan
   * Perl: `{httpport:num, info total albums:num, info total artists:num, info total duration:num, info total genres:num, info total songs:num, ip:str, lastscan:…`
   * wir:  `{count:num, httpport:num, info total albums:num, info total artists:num, info total duration:num, info total genres:num, info total songs:num, ip:str…`

### Nicht messbar: Perl bricht die Verbindung ab (Referenz-Box)

Diese Kommandos beantwortet die Referenz ohne Antwort (leere Antwort, reproduzierbar und playerunabhängig). Dasselbe passiert bei `boguscmd`/`playerstatus`/`alarm ?` — die Box droppt dort wie bei nicht dispatchbaren Kommandos, daher ist der **Perl-Referenzwert unbekannt** und hier keine Paritätsaussage möglich (bekannt als CTRL-20/OQ-2):

* `info total ?` — wir: `{}`
* `lastscan` — wir: `{}`
* `getstring ?` — wir: `{}`
* `can play` — wir: `[str]`
* `debug ?` — wir: `{}`
* `currentsong` — wir: `[str]`
* `currentsong 0 100` — wir: `[str]`
* `songinfo 0 10` — wir: `{count:num, item_loop:[], loop_loop:[], offset:num}`
* `mixer ?` — wir: `{_mixer:str}`
* `syncgroups` — wir: `[str]`
* `artworkspec` — wir: `{}`
* `artworkspec ?` — wir: `{}`
* `playlist 0 100` — wir: `{}`
* `playlist id ?` — wir: `{}`
* `playlist year ?` — wir: `{}`
* `artists 0 10 sort:name` — wir: `{artists_loop:[{actions:{…2}, artist:str, favorites_url:str, hasitems…`
* `browsedb 0 10 artist` — wir: `[str]`
* `folderinfo` — wir: `[str]`
* `folders 0 10` — wir: `[str]`
* `favorites exists ?` — wir: `{}`


### Ursachen-Cluster

* **order** (45): `alarms 0 10`, `albums 0 10`, `albums 0 10 artist_id:2`, `albums 0 10 genre_id:3 sort:album`, `albums 0 10 tags:j`, `albums 0 10 year:2000`, `artists 0 10`, `artists 0 10 search:beatles`, `browse albums 0 10`, `browse apps 0 10`, `browse artists 0 10`, `browse artists 0 10 genre_id:3`
* **zusätzliche Top-Level-Schlüssel bei uns** (36): `alarms 0 10`, `albums 0 10`, `albums 0 10 artist_id:2`, `albums 0 10 genre_id:3 sort:album`, `albums 0 10 tags:j`, `albums 0 10 year:2000`, `artists 0 10`, `artists 0 10 search:beatles`, `browse albums 0 10`, `browse apps 0 10`, `browse artists 0 10`, `browse artists 0 10 genre_id:3`
* **Loop-Schlüssel benannt anders (`<mode>_loop` vs. `loop_loop`/`item_loop`)** (29): `alarms 0 10`, `albums 0 10`, `albums 0 10 artist_id:2`, `albums 0 10 genre_id:3 sort:album`, `albums 0 10 tags:j`, `albums 0 10 year:2000`, `artists 0 10`, `artists 0 10 search:beatles`, `browse albums 0 10`, `browse apps 0 10`, `browse artists 0 10`, `browse artists 0 10 genre_id:3`
* **Top-Level-Schlüssel fehlen bei uns** (17): `musicfolder 0 100`, `playerpref ?`, `playlist artist ?`, `playlist duration ?`, `playlist modified ?`, `playlist name ?`, `playlist repeat ?`, `playlist shuffle ?`, `playlist tracks ?`, `playlist url ?`, `pref ?`, `pref audiodir ?`
* **Kommando/Antwort fehlt bzw. wird nur gespiegelt** (12): `apps 0 50`, `player count ?`, `player id ?`, `player ip ?`, `player model ?`, `player name ?`, `rescanprogress`, `roles`, `tracks 0 10`, `tracks 0 10 album_id:2`, `years 0 10`, `years 0 10 sort:year`
* **wir liefern Daten, Perl leer (Kontext-/Playerunterschied)** (10): `browse albums 0 10`, `browse apps 0 10`, `browse artists 0 10`, `browse artists 0 10 genre_id:3`, `browse genres 0 10`, `browse playlists 0 10`, `browse years 0 10`, `menu`, `menu 0 10`, `playlists 0 100`
* **wir liefern leer, Perl liefert Daten** (8): `playlist artist ?`, `playlist duration ?`, `playlist modified ?`, `playlist name ?`, `playlist repeat ?`, `playlist shuffle ?`, `playlist tracks ?`, `playlist url ?`
* **verschachtelte Struktur weicht ab** (2): `menustatus`, `players 0 99`
* **unser Server antwortet nicht korrekt** (2): `browse radios 0 10`, `radios 0 10`
* **Wert-Typen weichen ab** (2): `can ?`, `mixer muting ?`

## Methode & Grenzen

* **Nur lesende Kommandos.** Jede Zeile wird vor dem Senden gegen die Whitelist in `COMMANDS` und eine Denylist geprüft (`assert_safe()`); schreibende Formen (`power`, `play`, `pref` ohne `?`, `rescan`, `favorites add`, `sync`, `wipecache`, `button`, `display` …) werden nie gesendet. `--selfcheck` prüft das ohne Netzwerk.
* **Reproduzierbar:** erneuter Lauf überschreibt diesen Report (`--out`, `--only <substr[,substr]>`, `--limit <n>`).
* **Playerwahl:** je Server wird ein verbundener Jive-artiger Player gewählt (Rangliste in `first_player()`), damit Strukturvergleiche nicht an unterschiedlichen Player-Fähigkeiten scheitern; `--player`/`--ours-player`/`--perl-player` überschreiben das.
* **Grenzen der Referenz:**
  * Kommandos, deren Perl-Antwort `<conn_closed>` ist, sind auf dieser Referenz-Box **nicht messbar** (siehe Top-Abweichungen). Die Box schließt die Verbindung dort ohne Antwort — dasselbe Verhalten zeigt sie für `boguscmd`/`playerstatus`/`alarm ?`, d. h. für nicht dispatchbare Kommandos (im Repo dokumentiert als CTRL-20 in `.hermes/gap-analysis/02-control-cli-queries.md`, OQ-2).
  * `browse …`/`menu` liefern auf der Referenz `{}`, weil der Jive-Menükontext (vorherige `menu`-Navigation am Player) fehlt; unsere Antworten enthalten dort Daten. Für diese Zeilen ist "nur bei uns" **kein** Fehler, sondern ein Kontextunterschied der Sonde.
  * `pref ?`/`playerpref ?` **ohne Namespace** sind auf beiden Seiten Pseudowerte (Perl `_p2:null`, wir `_pref:str`) — die namensraumlose Form ist keine sinnvolle Abfrage; für Controller relevant sind `pref <ns> ?`/`playerpref <ns> ?`.
  * `players` **ohne** Range liefert auf der Referenz nur `{count}`, mit Range die `players_loop`.

## Nicht gesendete Kommandos (bewusst ausgelassen)

Alle gesendeten Kommandos sind lesend. Die folgenden Controller-relevanten Formen wurden **nicht** gesendet, weil sie den Server verändern würden oder ihre Form nicht sicher verifizierbar ist:

* `subscribe <cmd> / unsubscribe <cmd>` — Event-Abo; ändert den serverseitigen Client-Zustand und braucht einen streamenden Client. Abfragende Form nicht verifizierbar -> nicht gesendet.
* `pref <ns> <wert> / playerpref <ns> <wert>` — Schreibender Setter -- nicht gesendet.
* `debug <level> / irenable <0|1> / linesperscreen <n>` — Schreibende Setter (ohne '?') -- nicht gesendet.
* `button / display / display <text>` — Verändert Display/Player -- nicht gesendet.
* `power / play / pause / stop / playlistcontrol / playlist play` — Player-Steuerung -- nicht gesendet.
* `sync / unsync / set_preset / alarms add|delete|update` — Zustandsändernd -- nicht gesendet.
* `favorites add|delete|rename|move / rescan / wipecache` — Favoriten/Scan-Schreiboperationen -- nicht gesendet.
* `can <cmd> ohne '?'` — Nicht verifizierbare Antwortform -> siehe 'can play'.

## Anhang: Roh-JSON je Kommando (strukturrelevant gekürzt)

Schleifen auf 1 Element gekürzt, Strings auf 120 Zeichen, Schlüssel auf 20 je Ebene.

### `serverstatus` — abweichend (Server-Kontext)

_Loop-Schlüssel: Perl `—` vs. wir `players_loop`; nur bei uns: count, mediadirs, name, notice, players_loop, scan_

Perl:
```json
{
 "player count": 2,
 "other player count": 1,
 "uuid": "<UUID>",
 "info total genres": 762,
 "info total albums": 7189,
 "version": "9.1.1",
 "info total duration": 22851708.851,
 "info total artists": 11170,
 "lastscan": "1789312697",
 "ip": "<IP>",
 "httpport": "9000",
 "info total songs": 80218
}
```
unser:
```json
{
 "version": "9.2.0",
 "uuid": "<UUID>",
 "name": "Pyrion",
 "httpport": 9000,
 "ip": "<IP>",
 "player count": 1,
 "other player count": 0,
 "lastscan": 1789307889,
 "mediadirs": [],
 "info total genres": 819,
 "info total artists": 10604,
 "info total albums": 9270,
 "info total songs": 80441,
 "info total duration": 23077790,
 "notice": "",
 "scan": {
  "scanning": false,
  "progress": 0,
  "total_files": 0,
  "done_files": 0
 },
 "count": 1,
 "players_loop": [
  {
   "playerindex": "0",
   "playerid": "<MAC>",
   "uuid": "<HEX>",
   "ip": "<IP>",
   "name": "Taverne",
   "model": "squeezeplay",
   "modelname": "SqueezePlay",
   "power": 1,
   "isplaying": 1,
   "isplayer": 1,
   "canpoweroff": 1,
   "connected": 1,
   "firmware": "9.0.0-r1583",
   "displaytype": "none",
   "seq_no": 644120
  }
 ]
}
```
### `info total ?` — Fehler (Perl) (Server-Kontext)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `info total albums ?` — gleich (Server-Kontext)

Perl:
```json
{
 "_albums": 7189
}
```
unser:
```json
{
 "_albums": 9270
}
```
### `info total artists ?` — gleich (Server-Kontext)

Perl:
```json
{
 "_artists": 11170
}
```
unser:
```json
{
 "_artists": 10604
}
```
### `info total genres ?` — gleich (Server-Kontext)

Perl:
```json
{
 "_genres": 762
}
```
unser:
```json
{
 "_genres": 819
}
```
### `info total songs ?` — gleich (Server-Kontext)

Perl:
```json
{
 "_songs": 80218
}
```
unser:
```json
{
 "_songs": 80441
}
```
### `info total duration ?` — gleich (Server-Kontext)

Perl:
```json
{
 "_duration": 22851708.851
}
```
unser:
```json
{
 "_duration": 23077790.768
}
```
### `lastscan` — Fehler (Perl) (Server-Kontext)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `rescanprogress` — fehlt (Server-Kontext)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "rescan": 0
}
```
unser:
```json
[
 "rescanprogress rescan%3A0"
]
```
### `pref ?` — abweichend (Server-Kontext)

_fehlt bei uns: _p2; nur bei uns: _pref_

Perl:
```json
{
 "_p2": null
}
```
unser:
```json
{
 "_pref": ""
}
```
### `pref audiodir ?` — abweichend (Server-Kontext)

_fehlt bei uns: _p2; nur bei uns: audiodir_

Perl:
```json
{
 "_p2": null
}
```
unser:
```json
{
 "audiodir": ""
}
```
### `libraries` — gleich (Server-Kontext)

Perl:
```json
{}
```
unser:
```json
{}
```
### `works` — gleich (Server-Kontext)

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 0
}
```
### `roles` — fehlt (Server-Kontext)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 6
}
```
unser:
```json
[
 "roles"
]
```
### `getstring` — gleich (Server-Kontext)

Perl:
```json
{}
```
unser:
```json
{}
```
### `getstring ?` — Fehler (Perl) (Server-Kontext)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `can ?` — abweichend (Server-Kontext)

_Typen anders: _can_

Perl:
```json
{
 "_can": 0
}
```
unser:
```json
{
 "_can": ""
}
```
### `can play` — Fehler (Perl) (Server-Kontext)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "can play"
]
```
### `debug ?` — Fehler (Perl) (Server-Kontext)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `players 0 99` — abweichend (Server-Kontext)

_verschachtelte Struktur weicht ab_

Perl:
```json
{
 "players_loop": [
  {
   "firmware": "v1.9.9-1449",
   "ip": "<IP>",
   "displaytype": "none",
   "connected": 1,
   "model": "squeezelite",
   "playerid": "<MAC>",
   "playerindex": "0",
   "isplaying": 0,
   "name": "Taverne",
   "canpoweroff": 1,
   "uuid": null,
   "seq_no": 0,
   "modelname": "SqueezeLite",
   "isplayer": 1,
   "power": 1
  },
  "…+1 weitere"
 ],
 "count": 2
}
```
unser:
```json
{
 "count": 1,
 "players_loop": [
  {
   "playerindex": 0,
   "playerid": "<MAC>",
   "uuid": "<HEX>",
   "ip": "<IP>",
   "name": "Taverne",
   "model": "squeezeplay",
   "modelname": "SqueezePlay",
   "power": 1,
   "isplaying": 1,
   "isplayer": 1,
   "canpoweroff": 1,
   "connected": 1,
   "firmware": "9.0.0-r1583",
   "seq_no": 658789,
   "displaytype": "none"
  }
 ]
}
```
### `players` — abweichend (Server-Kontext)

_Loop-Schlüssel: Perl `—` vs. wir `players_loop`; nur bei uns: players_loop_

Perl:
```json
{
 "count": 2
}
```
unser:
```json
{
 "count": 1,
 "players_loop": [
  {
   "playerindex": 0,
   "playerid": "<MAC>",
   "uuid": "<HEX>",
   "ip": "<IP>",
   "name": "Taverne",
   "model": "squeezeplay",
   "modelname": "SqueezePlay",
   "power": 1,
   "isplaying": 1,
   "isplayer": 1,
   "canpoweroff": 1,
   "connected": 1,
   "firmware": "9.0.0-r1583",
   "seq_no": 659486,
   "displaytype": "none"
  }
 ]
}
```
### `player count ?` — fehlt (Server-Kontext)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "_count": 2
}
```
unser:
```json
[
 "player count 1"
]
```
### `player id ?` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "_id": "<MAC>"
}
```
unser:
```json
[
 "player id %3F 1C%3A87%3A2C%3A47%3AFC%3A36"
]
```
### `player name ?` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "_name": "Taverne"
}
```
unser:
```json
[
 "player name %3F Taverne"
]
```
### `player model ?` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "_model": "squeezelite"
}
```
unser:
```json
[
 "player model %3F squeezeplay"
]
```
### `player ip ?` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "_ip": "<IP>"
}
```
unser:
```json
[
 "player ip %3F"
]
```
### `status` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `playlist_loop`; fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url_

Perl:
```json
{
 "randomplay": 0,
 "mode": "stop",
 "player_name": "Taverne",
 "signalstrength": 0,
 "playlist mode": "off",
 "playlist_cur_index": "0",
 "current_title": "Goabert - pulchra somnium",
 "playlist_timestamp": 1789138661.22475,
 "playlist_tracks": 1,
 "power": 1,
 "playlist repeat": 0,
 "rate": 1,
 "use_volume_control": 1,
 "mixer volume": 56,
 "player_connected": 1,
 "remote": 1,
 "seq_no": 0,
 "time": 0,
 "player_ip": "<IP>",
 "digital_volume_control": 1,
 "…": "+2 weitere Schlüssel"
}
```
unser:
```json
{
 "mode": "play",
 "power": 1,
 "player_name": "Taverne",
 "player_connected": 1,
 "remote": 1,
 "playlist shuffle": 0,
 "playlist repeat": 0,
 "mixer volume": 47,
 "playlist_tracks": 0,
 "playlist_cur_index": "0",
 "time": 354.191,
 "rate": 1,
 "playlist mode": "off",
 "randomplay": 0,
 "digital_volume_control": 1,
 "use_volume_control": 1,
 "signalstrength": 0,
 "seq_no": 664030,
 "playlist_timestamp": 1789312787.0016534,
 "playlist_loop": [],
 "…": "+12 weitere Schlüssel"
}
```
### `status 0 100` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url_

Perl:
```json
{
 "player_name": "Taverne",
 "mode": "stop",
 "randomplay": 0,
 "playlist_timestamp": 1789138661.22475,
 "playlist_tracks": 1,
 "power": 1,
 "signalstrength": 0,
 "current_title": "Goabert - pulchra somnium",
 "playlist_cur_index": "0",
 "playlist mode": "off",
 "rate": 1,
 "playlist_loop": [
  {
   "title": "pulchra somnium",
   "playlist index": 0,
   "id": "-94115167939280"
  }
 ],
 "player_connected": 1,
 "mixer volume": 56,
 "use_volume_control": 1,
 "playlist repeat": 0,
 "digital_volume_control": 1,
 "time": 0,
 "player_ip": "<IP>",
 "seq_no": 0,
 "…": "+3 weitere Schlüssel"
}
```
unser:
```json
{
 "mode": "play",
 "power": 1,
 "player_name": "Taverne",
 "player_connected": 1,
 "remote": 1,
 "playlist shuffle": 0,
 "playlist repeat": 0,
 "mixer volume": 47,
 "playlist_tracks": 0,
 "playlist_cur_index": "0",
 "time": 354.191,
 "rate": 1,
 "playlist mode": "off",
 "randomplay": 0,
 "digital_volume_control": 1,
 "use_volume_control": 1,
 "signalstrength": 0,
 "seq_no": 664700,
 "playlist_timestamp": 1789312790.105155,
 "playlist_loop": [],
 "…": "+12 weitere Schlüssel"
}
```
### `status 0 100 tags:gald` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url_

Perl:
```json
{
 "digital_volume_control": 1,
 "player_ip": "<IP>",
 "time": 0,
 "seq_no": 0,
 "playlist shuffle": 0,
 "remoteMeta": {
  "id": "-94115167939280",
  "title": "pulchra somnium",
  "artist": "Goabert",
  "duration": "0"
 },
 "remote": 1,
 "rate": 1,
 "playlist_loop": [
  {
   "id": "-94115167939280",
   "artist": "Goabert",
   "playlist index": 0,
   "title": "pulchra somnium",
   "duration": "0"
  }
 ],
 "player_connected": 1,
 "use_volume_control": 1,
 "mixer volume": 56,
 "playlist repeat": 0,
 "playlist_timestamp": 1789138661.22475,
 "power": 1,
 "playlist_tracks": 1,
 "signalstrength": 0,
 "current_title": "Goabert - pulchra somnium",
 "playlist_cur_index": "0",
 "playlist mode": "off",
 "…": "+3 weitere Schlüssel"
}
```
unser:
```json
{
 "mode": "play",
 "power": 1,
 "player_name": "Taverne",
 "player_connected": 1,
 "remote": 1,
 "playlist shuffle": 0,
 "playlist repeat": 0,
 "mixer volume": 47,
 "playlist_tracks": 0,
 "playlist_cur_index": "0",
 "time": 354.191,
 "rate": 1,
 "playlist mode": "off",
 "randomplay": 0,
 "digital_volume_control": 1,
 "use_volume_control": 1,
 "signalstrength": 0,
 "seq_no": 665602,
 "playlist_timestamp": 1789312793.2012935,
 "playlist_loop": [],
 "…": "+12 weitere Schlüssel"
}
```
### `status 0 100 tags:acdtu` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url_

Perl:
```json
{
 "playlist_loop": [
  {
   "coverid": "-94115167939280",
   "id": "-94115167939280",
   "artist": "Goabert",
   "duration": "0",
   "url": "http://strm112.1.fm/ambientpsy_mobile_mp3",
   "playlist index": 0,
   "title": "pulchra somnium"
  }
 ],
 "rate": 1,
 "player_connected": 1,
 "use_volume_control": 1,
 "mixer volume": 56,
 "playlist repeat": 0,
 "digital_volume_control": 1,
 "player_ip": "<IP>",
 "time": 0,
 "seq_no": 0,
 "playlist shuffle": 0,
 "remoteMeta": {
  "id": "-94115167939280",
  "title": "pulchra somnium",
  "artist": "Goabert",
  "coverid": "-94115167939280",
  "duration": "0",
  "url": "http://strm112.1.fm/ambientpsy_mobile_mp3"
 },
 "remote": 1,
 "player_name": "Taverne",
 "mode": "stop",
 "randomplay": 0,
 "playlist_timestamp": 1789138661.22475,
 "playlist_tracks": 1,
 "power": 1,
 "signalstrength": 0,
 "…": "+3 weitere Schlüssel"
}
```
unser:
```json
{
 "mode": "play",
 "power": 1,
 "player_name": "Taverne",
 "player_connected": 1,
 "remote": 1,
 "playlist shuffle": 0,
 "playlist repeat": 0,
 "mixer volume": 47,
 "playlist_tracks": 0,
 "playlist_cur_index": "0",
 "time": 354.191,
 "rate": 1,
 "playlist mode": "off",
 "randomplay": 0,
 "digital_volume_control": 1,
 "use_volume_control": 1,
 "signalstrength": 0,
 "seq_no": 666290,
 "playlist_timestamp": 1789312796.5786462,
 "playlist_loop": [],
 "…": "+12 weitere Schlüssel"
}
```
### `status 0 5 tags:cdtu` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: remoteMeta; nur bei uns: album, artist, count, current_album, current_artist, current_url_

Perl:
```json
{
 "power": 1,
 "playlist_tracks": 1,
 "playlist_timestamp": 1789138661.22475,
 "playlist mode": "off",
 "current_title": "Goabert - pulchra somnium",
 "playlist_cur_index": "0",
 "signalstrength": 0,
 "mode": "stop",
 "player_name": "Taverne",
 "randomplay": 0,
 "playlist shuffle": 0,
 "remoteMeta": {
  "id": "-94115167939280",
  "title": "pulchra somnium",
  "coverid": "-94115167939280",
  "duration": "0",
  "url": "http://strm112.1.fm/ambientpsy_mobile_mp3"
 },
 "seq_no": 0,
 "digital_volume_control": 1,
 "player_ip": "<IP>",
 "time": 0,
 "remote": 1,
 "mixer volume": 56,
 "use_volume_control": 1,
 "player_connected": 1,
 "…": "+3 weitere Schlüssel"
}
```
unser:
```json
{
 "mode": "play",
 "power": 1,
 "player_name": "Taverne",
 "player_connected": 1,
 "remote": 1,
 "playlist shuffle": 0,
 "playlist repeat": 0,
 "mixer volume": 47,
 "playlist_tracks": 0,
 "playlist_cur_index": "0",
 "time": 354.191,
 "rate": 1,
 "playlist mode": "off",
 "randomplay": 0,
 "digital_volume_control": 1,
 "use_volume_control": 1,
 "signalstrength": 0,
 "seq_no": 667054,
 "playlist_timestamp": 1789312799.6829944,
 "playlist_loop": [],
 "…": "+12 weitere Schlüssel"
}
```
### `currentsong` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "currentsong"
]
```
### `currentsong 0 100` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "currentsong 0 100"
]
```
### `songinfo 0 10` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{
 "count": 0,
 "offset": 0,
 "loop_loop": [],
 "item_loop": []
}
```
### `title ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_title": "pulchra somnium"
}
```
unser:
```json
{
 "_title": ""
}
```
### `duration ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_duration": "0"
}
```
unser:
```json
{
 "_duration": 0.0
}
```
### `mixer ?` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{
 "_mixer": ""
}
```
### `mixer volume ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_volume": "56"
}
```
unser:
```json
{
 "_volume": "47"
}
```
### `mixer muting ?` — abweichend (Player 00:00:00:00:00:00)

_Typen anders: _muting_

Perl:
```json
{
 "_muting": null
}
```
unser:
```json
{
 "_muting": "0"
}
```
### `mode ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_mode": "stop"
}
```
unser:
```json
{
 "_mode": "play"
}
```
### `time ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_time": 0
}
```
unser:
```json
{
 "_time": 354
}
```
### `playlist repeat ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _repeat; wir leer, Perl mit Daten_

Perl:
```json
{
 "_repeat": "0"
}
```
unser:
```json
{}
```
### `playlist shuffle ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _shuffle; wir leer, Perl mit Daten_

Perl:
```json
{
 "_shuffle": "0"
}
```
unser:
```json
{}
```
### `gototime ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_time": 0
}
```
unser:
```json
{
 "_time": 354
}
```
### `displaystatus` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{}
```
unser:
```json
{}
```
### `playerpref ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _p2; nur bei uns: _playerpref_

Perl:
```json
{
 "_p2": null
}
```
unser:
```json
{
 "_playerpref": ""
}
```
### `playerpref digitalVolumeControl ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_p2": "1"
}
```
unser:
```json
{
 "_p2": "1"
}
```
### `irenable ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_irenable": 1
}
```
unser:
```json
{
 "_irenable": 1
}
```
### `linesperscreen ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "_linesperscreen": 0
}
```
unser:
```json
{
 "_linesperscreen": 0
}
```
### `alarms 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `alarm_loop`; nur bei uns: alarm_loop_

Perl:
```json
{
 "count": 0,
 "fade": "1"
}
```
unser:
```json
{
 "count": 0,
 "fade": 0,
 "alarm_loop": []
}
```
### `syncgroups` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "syncgroups"
]
```
### `artworkspec` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `artworkspec ?` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `playlist 0 100` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `playlist tracks ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _tracks; wir leer, Perl mit Daten_

Perl:
```json
{
 "_tracks": 1
}
```
unser:
```json
{}
```
### `playlist name ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _name; wir leer, Perl mit Daten_

Perl:
```json
{
 "_name": "1.FM - Ambient Psychill"
}
```
unser:
```json
{}
```
### `playlist modified ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _modified; wir leer, Perl mit Daten_

Perl:
```json
{
 "_modified": 1
}
```
unser:
```json
{}
```
### `playlist duration ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _duration; wir leer, Perl mit Daten_

Perl:
```json
{
 "_duration": "0"
}
```
unser:
```json
{}
```
### `playlist id ?` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `playlist artist ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _artist; wir leer, Perl mit Daten_

Perl:
```json
{
 "_artist": "Goabert"
}
```
unser:
```json
{}
```
### `playlist album ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{}
```
unser:
```json
{}
```
### `playlist genre ?` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{}
```
unser:
```json
{}
```
### `playlist year ?` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `playlist url ?` — abweichend (Player 00:00:00:00:00:00)

_fehlt bei uns: _url; wir leer, Perl mit Daten_

Perl:
```json
{
 "_url": null
}
```
unser:
```json
{}
```
### `playlists 0 100` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `playlists_loop`; nur bei uns: count, playlists_loop; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 1,
 "playlists_loop": [
  {
   "id": 1,
   "playlist": "Test-Playlist"
  }
 ]
}
```
### `artists 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `artists_loop` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 11170,
 "artists_loop": [
  {
   "artist": "?",
   "id": 23284,
   "favorites_url": "db:contributor.name=%3F"
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 10604,
 "offset": 0,
 "loop_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "artists_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `albums 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `albums_loop` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 7189,
 "albums_loop": [
  {
   "performance": "",
   "favorites_title": "-",
   "favorites_url": "db:album.title=-&contributor.name=blamstrain",
   "album": "-",
   "id": 11018
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 9270,
 "offset": 0,
 "loop_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "albums_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `genres 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `genres_loop` vs. wir `loop_loop,item_loop,genres_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "genres_loop": [
  {
   "id": 1727,
   "favorites_url": "db:genre.name=",
   "genre": null
  },
  "…+9 weitere"
 ],
 "count": 762
}
```
unser:
```json
{
 "count": 713,
 "offset": 0,
 "loop_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "genres_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `years 0 10` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 65,
 "years_loop": [
  {
   "year": 0,
   "favorites_url": "db:year.id=0"
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
[
 "years 0 10 year%3A2026 favorites_url%3Adb%3Ayear.id%3D2026 year%3A2025 favorites_url%3Adb%3Ayear.id%3D2025 year%3A202…"
]
```
### `titles 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `titles_loop` vs. wir `loop_loop,item_loop,titles_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 80218,
 "titles_loop": [
  {
   "id": 129497,
   "title": "!!!!!!!",
   "genre": "Alternative",
   "artist": "Billie Eilish",
   "album": "WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?",
   "duration": 13.609
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 80441,
 "offset": 0,
 "loop_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "titles_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ]
}
```
### `tracks 0 10` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 80218,
 "titles_loop": [
  {
   "id": 129497,
   "title": "!!!!!!!",
   "genre": "Alternative",
   "artist": "Billie Eilish",
   "album": "WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?",
   "duration": 13.609
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
[
 "tracks 0 10"
]
```
### `songs 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `titles_loop` vs. wir `loop_loop,item_loop,titles_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 80218,
 "titles_loop": [
  {
   "id": 129497,
   "title": "!!!!!!!",
   "genre": "Alternative",
   "artist": "Billie Eilish",
   "album": "WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?",
   "duration": 13.609
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 80441,
 "offset": 0,
 "loop_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "titles_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ]
}
```
### `artists 0 10 search:beatles` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `artists_loop` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "artists_loop": [
  {
   "favorites_url": "db:contributor.name=The%20Beatles",
   "id": 27586,
   "artist": "The Beatles"
  }
 ],
 "count": 1
}
```
unser:
```json
{
 "count": 1,
 "offset": 0,
 "loop_loop": [
  {
   "id": 8154,
   "artist": "The Beatles",
   "favorites_url": "db:contributor.name=The%20Beatles",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8154,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8154
     }
    }
   },
   "text": "The Beatles",
   "title": "The Beatles",
   "type": "outline",
   "hasitems": 1
  }
 ],
 "item_loop": [
  {
   "id": 8154,
   "artist": "The Beatles",
   "favorites_url": "db:contributor.name=The%20Beatles",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8154,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8154
     }
    }
   },
   "text": "The Beatles",
   "title": "The Beatles",
   "type": "outline",
   "hasitems": 1
  }
 ],
 "artists_loop": [
  {
   "id": 8154,
   "artist": "The Beatles",
   "favorites_url": "db:contributor.name=The%20Beatles",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8154,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8154
     }
    }
   },
   "text": "The Beatles",
   "title": "The Beatles",
   "type": "outline",
   "hasitems": 1
  }
 ]
}
```
### `artists 0 10 sort:name` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{
 "count": 10604,
 "offset": 0,
 "loop_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "artists_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `albums 0 10 artist_id:2` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: albums_loop, item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 1,
 "offset": 0,
 "loop_loop": [
  {
   "id": 2,
   "album": "Open Your Mind And Your Trousers",
   "performance": "",
   "favorites_url": "db:album.title=Open%20Your%20Mind%20And%20Your%20Trousers",
   "favorites_title": "Open Your Mind And Your Trousers",
   "year": 2024,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 2,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 2
     }
    }
   },
   "text": "Open Your Mind And Your Trousers",
   "title": "Open Your Mind And Your Trousers",
   "type": "outline",
   "hasitems": 1
  }
 ],
 "item_loop": [
  {
   "id": 2,
   "album": "Open Your Mind And Your Trousers",
   "performance": "",
   "favorites_url": "db:album.title=Open%20Your%20Mind%20And%20Your%20Trousers",
   "favorites_title": "Open Your Mind And Your Trousers",
   "year": 2024,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 2,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 2
     }
    }
   },
   "text": "Open Your Mind And Your Trousers",
   "title": "Open Your Mind And Your Trousers",
   "type": "outline",
   "hasitems": 1
  }
 ],
 "albums_loop": [
  {
   "id": 2,
   "album": "Open Your Mind And Your Trousers",
   "performance": "",
   "favorites_url": "db:album.title=Open%20Your%20Mind%20And%20Your%20Trousers",
   "favorites_title": "Open Your Mind And Your Trousers",
   "year": 2024,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 2,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 2
     }
    }
   },
   "text": "Open Your Mind And Your Trousers",
   "title": "Open Your Mind And Your Trousers",
   "type": "outline",
   "hasitems": 1
  }
 ]
}
```
### `albums 0 10 year:2000` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `albums_loop` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 171,
 "albums_loop": [
  {
   "performance": "",
   "favorites_title": "20th Century Masters: The Millennium Collection: Best Of Gloria Gaynor",
   "favorites_url": "db:album.title=20th%20Century%20Masters%3A%20The%20Millennium%20Collection%3A%20Best%20Of%20Gloria%20Gaynor&contribut…",
   "album": "20th Century Masters: The Millennium Collection: Best Of Gloria Gaynor",
   "id": 11952
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 177,
 "offset": 0,
 "loop_loop": [
  {
   "id": 1770,
   "album": "...The Last Embrace",
   "performance": "",
   "favorites_url": "db:album.title=...The%20Last%20Embrace",
   "favorites_title": "...The Last Embrace",
   "year": 2000,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1770,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1770
     }
    }
   },
   "text": "...The Last Embrace",
   "title": "...The Last Embrace",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 1770,
   "album": "...The Last Embrace",
   "performance": "",
   "favorites_url": "db:album.title=...The%20Last%20Embrace",
   "favorites_title": "...The Last Embrace",
   "year": 2000,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1770,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1770
     }
    }
   },
   "text": "...The Last Embrace",
   "title": "...The Last Embrace",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "albums_loop": [
  {
   "id": 1770,
   "album": "...The Last Embrace",
   "performance": "",
   "favorites_url": "db:album.title=...The%20Last%20Embrace",
   "favorites_title": "...The Last Embrace",
   "year": 2000,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1770,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1770
     }
    }
   },
   "text": "...The Last Embrace",
   "title": "...The Last Embrace",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `albums 0 10 genre_id:3 sort:album` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: albums_loop, item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 3001,
 "offset": 0,
 "loop_loop": [
  {
   "id": 1598,
   "album": "#1 - Maike 2004",
   "performance": "",
   "favorites_url": "db:album.title=%231%20-%20Maike%202004",
   "favorites_title": "#1 - Maike 2004",
   "year": 0,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1598,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1598
     }
    }
   },
   "text": "#1 - Maike 2004",
   "title": "#1 - Maike 2004",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 1598,
   "album": "#1 - Maike 2004",
   "performance": "",
   "favorites_url": "db:album.title=%231%20-%20Maike%202004",
   "favorites_title": "#1 - Maike 2004",
   "year": 0,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1598,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1598
     }
    }
   },
   "text": "#1 - Maike 2004",
   "title": "#1 - Maike 2004",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "albums_loop": [
  {
   "id": 1598,
   "album": "#1 - Maike 2004",
   "performance": "",
   "favorites_url": "db:album.title=%231%20-%20Maike%202004",
   "favorites_title": "#1 - Maike 2004",
   "year": 0,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 1598,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 1598
     }
    }
   },
   "text": "#1 - Maike 2004",
   "title": "#1 - Maike 2004",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `albums 0 10 tags:j` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `albums_loop` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "count": 7189,
 "albums_loop": [
  {
   "id": 11018,
   "favorites_url": "db:album.title=&contributor.name=blamstrain",
   "favorites_title": null,
   "performance": ""
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
{
 "count": 9270,
 "offset": 0,
 "loop_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "albums_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `titles 0 10 album_id:2 tags:title` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,titles_loop`; nur bei uns: item_loop, loop_loop, offset, titles_loop_

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 15,
 "offset": 0,
 "loop_loop": [
  {
   "id": 16,
   "title": "ARDRHU",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/Scooter/11-scooter-ardrhu.mp3",
   "duration": 233.382,
   "artist": "Scooter",
   "album": "Open Your Mind And Your Trousers",
   "genre": "Dance",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 16
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 16
     }
    }
   },
   "text": "ARDRHU",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 16,
   "title": "ARDRHU",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/Scooter/11-scooter-ardrhu.mp3",
   "duration": 233.382,
   "artist": "Scooter",
   "album": "Open Your Mind And Your Trousers",
   "genre": "Dance",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 16
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 16
     }
    }
   },
   "text": "ARDRHU",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "titles_loop": [
  {
   "id": 16,
   "title": "ARDRHU",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/Scooter/11-scooter-ardrhu.mp3",
   "duration": 233.382,
   "artist": "Scooter",
   "album": "Open Your Mind And Your Trousers",
   "genre": "Dance",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 16
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 16
     }
    }
   },
   "text": "ARDRHU",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `songs 0 10 genre_id:3` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,titles_loop`; nur bei uns: item_loop, loop_loop, offset, titles_loop_

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 80441,
 "offset": 0,
 "loop_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ],
 "titles_loop": [
  {
   "id": 79462,
   "title": "!!!!!!!",
   "url": "file:///run/user/1000/gvfs/smb-share%3Aserver%3Dmedia.local%2Cshare%3Dmedia/Musik/BGAYTR~Z/Billie%20Eilish%20-%20When…",
   "duration": 0,
   "artist": "Billie Eilish",
   "album": "When We All Fall Asleep, Where Do We Go [320]",
   "genre": "BGAYTR~Z",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "songinfo"
     ],
     "params": {
      "track_id": 79462
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "track_id": 79462
     }
    }
   },
   "coverid": 9301,
   "coverart": 1,
   "artwork_url": "/music/9301/cover.jpg",
   "text": "!!!!!!!",
   "type": "outline",
   "hasitems": 1,
   "icon": "/music/9301/cover.jpg",
   "icon-id": 9301
  },
  "…+9 weitere"
 ]
}
```
### `tracks 0 10 album_id:2` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 0
}
```
unser:
```json
[
 "tracks 0 10 album_id%3A2"
]
```
### `genres 0 10 sort:name` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `genres_loop` vs. wir `loop_loop,item_loop,genres_loop`; nur bei uns: item_loop, loop_loop, offset_

Perl:
```json
{
 "genres_loop": [
  {
   "id": 1727,
   "favorites_url": "db:genre.name=",
   "genre": null
  },
  "…+9 weitere"
 ],
 "count": 762
}
```
unser:
```json
{
 "count": 713,
 "offset": 0,
 "loop_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "genres_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `years 0 10 sort:year` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 65,
 "years_loop": [
  {
   "favorites_url": "db:year.id=0",
   "year": 0
  },
  "…+9 weitere"
 ]
}
```
unser:
```json
[
 "years 0 10 sort%3Ayear year%3A2026 favorites_url%3Adb%3Ayear.id%3D2026 year%3A2025 favorites_url%3Adb%3Ayear.id%3D202…"
]
```
### `browse artists 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: artists_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 10604,
 "offset": 0,
 "loop_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "artists_loop": [
  {
   "id": 8283,
   "artist": "\"tennessee\" Ernie Ford",
   "favorites_url": "db:contributor.name=%22tennessee%22%20Ernie%20Ford",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 8283,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 8283
     }
    }
   },
   "text": "\"tennessee\" Ernie Ford",
   "title": "\"tennessee\" Ernie Ford",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `browse albums 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,albums_loop`; nur bei uns: albums_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 9270,
 "offset": 0,
 "loop_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "albums_loop": [
  {
   "id": 6541,
   "album": "\"A\"",
   "performance": "",
   "favorites_url": "db:album.title=%22A%22",
   "favorites_title": "\"A\"",
   "year": 1980,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "titles"
     ],
     "params": {
      "album_id": 6541,
      "menu": "songinfo"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "album_id": 6541
     }
    }
   },
   "text": "\"A\"",
   "title": "\"A\"",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `browse genres 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,genres_loop`; nur bei uns: count, genres_loop, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 713,
 "offset": 0,
 "loop_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "genres_loop": [
  {
   "id": 667,
   "genre": "(152)",
   "favorites_url": "db:genre.name=%28152%29",
   "name": "(152)",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "artists"
     ],
     "params": {
      "genre_id": 667,
      "menu": "albums"
     }
    }
   },
   "text": "(152)",
   "title": "(152)",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `browse years 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop`; nur bei uns: count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 0,
 "offset": 0,
 "loop_loop": [],
 "item_loop": []
}
```
### `browse playlists 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop`; nur bei uns: count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 0,
 "offset": 0,
 "loop_loop": [],
 "item_loop": []
}
```
### `browse apps 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop`; nur bei uns: count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 0,
 "offset": 0,
 "loop_loop": [],
 "item_loop": []
}
```
### `browse radios 0 10` — Fehler (unser) (Player 00:00:00:00:00:00)

_unser: error_

Perl:
```json
{}
```
unser:
```json
{
 "__transport__": "error"
}
```
### `browse artists 0 10 genre_id:3` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `loop_loop,item_loop,artists_loop`; nur bei uns: artists_loop, count, item_loop, loop_loop, offset; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "count": 2585,
 "offset": 0,
 "loop_loop": [
  {
   "id": 7068,
   "artist": "----- Ashes",
   "favorites_url": "db:contributor.name=-----%20Ashes",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 7068,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 7068
     }
    }
   },
   "text": "----- Ashes",
   "title": "----- Ashes",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "item_loop": [
  {
   "id": 7068,
   "artist": "----- Ashes",
   "favorites_url": "db:contributor.name=-----%20Ashes",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 7068,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 7068
     }
    }
   },
   "text": "----- Ashes",
   "title": "----- Ashes",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ],
 "artists_loop": [
  {
   "id": 7068,
   "artist": "----- Ashes",
   "favorites_url": "db:contributor.name=-----%20Ashes",
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "albums"
     ],
     "params": {
      "artist_id": 7068,
      "menu": "tracks"
     }
    },
    "play": {
     "player": 0,
     "cmd": [
      "playlist",
      "…+1 weitere"
     ],
     "params": {
      "artist_id": 7068
     }
    }
   },
   "text": "----- Ashes",
   "title": "----- Ashes",
   "type": "outline",
   "hasitems": 1
  },
  "…+9 weitere"
 ]
}
```
### `browsedb 0 10 artist` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "browsedb 0 10 artist"
]
```
### `menu` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `item_loop`; nur bei uns: base, count, item_loop, offset, title; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "item_loop": [
  {
   "id": "myMusic",
   "text": "Eigene Musik",
   "node": "home",
   "isANode": 1,
   "weight": 11,
   "hasitems": 1,
   "index": 0
  },
  "…+21 weitere"
 ],
 "count": 22,
 "offset": 0,
 "base": {
  "id": "",
  "name": "Hauptmenü"
 },
 "title": "Hauptmenü"
}
```
### `menu 0 10` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `—` vs. wir `item_loop`; nur bei uns: base, count, item_loop, offset, title; wir mit Daten, Perl leer_

Perl:
```json
{}
```
unser:
```json
{
 "item_loop": [
  {
   "id": "myMusic",
   "text": "Eigene Musik",
   "node": "home",
   "isANode": 1,
   "weight": 11,
   "hasitems": 1,
   "index": 0
  },
  "…+9 weitere"
 ],
 "count": 22,
 "offset": 0,
 "base": {
  "id": "",
  "name": "Hauptmenü"
 },
 "title": "Hauptmenü"
}
```
### `menustatus` — abweichend (Player 00:00:00:00:00:00)

_verschachtelte Struktur weicht ab_

Perl:
```json
{}
```
unser:
```json
[
 null,
 "…+3 weitere"
]
```
### `musicfolder 0 100` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `folder_loop` vs. wir `loop_loop,item_loop,musicfolder_loop`; fehlt bei uns: folder_loop; nur bei uns: item_loop, loop_loop, musicfolder_loop, offset_

Perl:
```json
{
 "folder_loop": [
  {
   "id": 204572,
   "filename": "6MzM6F.Fetenhits_Rock_Classics_Best_Of-3CD-2020-NoGroup.nfo",
   "type": "folder"
  },
  "…+99 weitere"
 ],
 "count": 303
}
```
unser:
```json
{
 "count": 1,
 "offset": 0,
 "loop_loop": [
  {
   "id": "file:///run",
   "name": "run",
   "text": "run",
   "type": "outline",
   "hasitems": 1,
   "title": "run"
  }
 ],
 "item_loop": [
  {
   "id": "file:///run",
   "name": "run",
   "text": "run",
   "type": "outline",
   "hasitems": 1,
   "title": "run"
  }
 ],
 "musicfolder_loop": [
  {
   "id": "file:///run",
   "name": "run",
   "text": "run",
   "type": "outline",
   "hasitems": 1,
   "title": "run"
  }
 ]
}
```
### `folderinfo` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "folderinfo"
]
```
### `folders 0 10` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
[
 "folders 0 10"
]
```
### `readdirectory 0 10` — gleich (Player 00:00:00:00:00:00)

Perl:
```json
{
 "count": 0
}
```
unser:
```json
{
 "count": 0
}
```
### `favorites items 0 100` — abweichend (Player 00:00:00:00:00:00)

_Loop-Schlüssel: Perl `loop_loop` vs. wir `loop_loop,item_loop`; nur bei uns: item_loop, offset_

Perl:
```json
{
 "count": 8,
 "loop_loop": [
  {
   "id": "18bf9689.0",
   "name": "Chill",
   "image": "html/images/favorites.png",
   "isaudio": 0,
   "hasitems": 1
  },
  "…+7 weitere"
 ],
 "title": "Favorites"
}
```
unser:
```json
{
 "count": 12,
 "offset": 0,
 "loop_loop": [
  {
   "id": "0.0",
   "name": "Chillout",
   "image": "html/images/favorites.png",
   "isaudio": 0,
   "hasitems": 1,
   "position": 0,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "favorites",
      "…+1 weitere"
     ],
     "params": {
      "item_id": "0.0"
     }
    }
   },
   "text": "Chillout",
   "title": "Chillout",
   "type": "outline"
  },
  "…+11 weitere"
 ],
 "item_loop": [
  {
   "id": "0.0",
   "name": "Chillout",
   "image": "html/images/favorites.png",
   "isaudio": 0,
   "hasitems": 1,
   "position": 0,
   "actions": {
    "go": {
     "player": 0,
     "cmd": [
      "favorites",
      "…+1 weitere"
     ],
     "params": {
      "item_id": "0.0"
     }
    }
   },
   "text": "Chillout",
   "title": "Chillout",
   "type": "outline"
  },
  "…+11 weitere"
 ],
 "title": "Favoriten"
}
```
### `favorites exists ?` — Fehler (Perl) (Player 00:00:00:00:00:00)

_Perl: conn_closed_

Perl:
```json
{
 "__transport__": "conn_closed"
}
```
unser:
```json
{}
```
### `radios 0 10` — Fehler (unser) (Player 00:00:00:00:00:00)

_unser: error_

Perl:
```json
{
 "radioss_loop": [
  {
   "name": "Eigene Voreinstellungen",
   "weight": 5,
   "icon": "/plugins/TuneIn/html/images/radiopresets.png",
   "type": "xmlbrowser",
   "cmd": "presets"
  },
  "…+9 weitere"
 ],
 "count": 10
}
```
unser:
```json
{
 "__transport__": "error"
}
```
### `apps 0 50` — fehlt (Player 00:00:00:00:00:00)

_Perl liefert Daten, wir spiegeln nur das Kommando_

Perl:
```json
{
 "count": 1,
 "appss_loop": [
  {
   "name": "Sounds & Effekte",
   "weight": 90,
   "icon": "plugins/Sounds/html/images/icon.png",
   "cmd": "sounds",
   "type": "xmlbrowser"
  }
 ]
}
```
unser:
```json
[
 "apps 0 50"
]
```
