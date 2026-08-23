# Transkodierung (Format-Fallback) — Stand

**Status: TEILWEISE IMPLEMENTIERT**
(2026-08-23: lokaler Datei-Fallback + Radio-AAC-Fallback aktiv; Feintuning offen)

## Was implementiert ist

### 1. Lokale Dateien (`/stream.mp3?id=…&transcode=1`)
- `send_strm_to_player` prüft `supported_formats` des Players (aus HELO-Model)
- Kann der Player den Codec nicht, wird auf PCM (`codec='p'`) + `&transcode=1`
  umgeschaltet; `stream_track` transkodiert via ffmpeg zu rohem PCM
- Ohne ffmpeg: Warnung im Log + Statusleisten-Notice
  „ffmpeg not found - transcoding not possible" (serverstatus-Feld `notice`),
  der Stream wird trotzdem (direkt) versucht

### 2. Radio-Streams (AAC) — Commit nach SWR3-Bug
- Problem: squeezelites faad2 versagt stumm bei vielen Radio-AAC-Varianten
  (HE-AAC/SBR): Decoder konsumiert Bytes (`faad_decode consume…`), aber der
  Output startet nie (STMf/STMc, dann kein STMt mehr) → keine Störungen mehr,
  sondern Stille. Zuvor (codec falsch 'm'): mad_decode-Wall → starke Störungen.
- Fix-Kette:
  a) Codec-Erkennung aus URL (`_guess_codec_from_url`, .aac/.flac/.ogg/…)
     statt fest 'm' in `play_url` + `_play_playlist_item`
  b) `send_remote_stream`: codec 'a' → Proxy mit ffmpeg-Re-Encoding zu MP3
     (`_send_proxy_stream(..., transcode="mp3")`, Endpoint
     `/stream.mp3?remote=<url>&transcode=mp3`)
  c) `_proxy_remote(transcode=…)`: holt Upstream (httpx, TLS-fähig),
     piped durch ffmpeg (`_transcode_pipe`) → libmp3lame 192k live
- Radio-Underruns führen nicht mehr zu "Track end" (`_advance_after_track`
  bricht bei `player.remote=1` ab); STMt-Heartbeat reconciliert mode→play

## Bekannte Einschränkungen / offene Punkte (zurückgestellt)

- **Wiedergabe-Stutter bei Transcode:** Live-Transkodierung kostet CPU;
  bei Lastspitzen kann der Stream-Puffer leerlaufen (kurze Stops).
  Optionen: Ziel-Bitrate senken (192k→128k), Player-Puffer vergrößern
  (squeezelite `-b <streambuf>:<outputbuf>` in KB), oder schnelleren Server.
- AAC wird pauschal über den Proxy geroutet — Player mit nachweislich
  funktionierendem AAC-Dekoder könnten direkt spielen (feine Granularung
  pro Player-Modell möglich).
- OGG/Vorbis-Transcodes sind vorbereitet (ctype_map), aber ungetestet.
- „ffmpeg fehlt"-Notice für Radio-Fallback: setzt aktuell nur beim
  lokalen Datei-Pfad an; der Radio-Pfad loggt stattdessen.
