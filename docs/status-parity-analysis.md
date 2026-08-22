# Status-Parität Python vs Perl LMS

Vergleich status (JSON) Python vs Perl:

Python liefert zuviel/anders:
- "count" (Perl: nicht in status; nur playlist_tracks)
- "item_loop" (Perl: nur playlist_loop)
- "playlist shuffle": 1 (war durch meinen Test gesetzt; ok)
- "playlist repeat": STRING "0" — Perl: int 0
- "playlist mode": fehlt in Python (Perl: "off")
- "digital_volume_control": 0 (Perl: 1) — Wert falsch
- "can_seek", "waitingToPlay", "alarm_state": Perl sendet die (in dieser Antwort) nicht
- "use_volume_control": fehlt (Perl: 1)
- "player_ip": fehlt (Perl: "192.168.1.131:36704")
- "randomplay": fehlt (Perl: 0)
- seq_no: int 0 vs Perl string "2"
- playlist_cur_index: int vs Perl string "0"
- Top-Level artist/title/album/duration: Perl hat die NICHT auf Top-Level (nur in remoteMeta/playlist_loop)
- playlist_loop item keys: Perl nutzt "playlist index":0 statt "index":0; kein trackType/album_id/url/bitrate/samplerate/type/filesize ohne passende tags

Fazit Fixes für _json_player_status:
1. Entferne: count, item_loop, can_seek, waitingToPlay, alarm_state (nur wenn gefordert), Top-Level artist/title/album/duration
2. Typen: playlist repeat/shuffle als int; seq_no als String; playlist_cur_index als String? 
   -> Achtung Squeezer erwartet evtl. int. Perl macht Strings. Wir folgen Perl.
3. Ergänze: "playlist mode" (off/repeat/repeat-one?), randomplay=shuffle-Spiegel, use_volume_control=1, player_ip
4. playlist_loop items: "playlist index" statt "index"; tags-gefiltert bleiben.
