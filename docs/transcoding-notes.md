# Transkodierung (Format-Fallback) — Stand & zurückgestellt

**Status: IMPLEMENTIERT, aber WEITERE ARBEIT ZURÜCKGESTELLT**
(Entscheidung am 2026-08-23: „Mach eine Notiz, aber stelle die Transkodierung erstmal zurück.")

## Was gebaut wurde (Commit `1399487bd`)

Ein Format-Fallback, der greift, wenn ein Player das Audioformat eines
Sources nicht nativ dekodieren kann. Der Direktstream bleibt die Norm;
Transcodierung läuft nur bei Bedarf (keine unnötige Server-Last).

1. **Player-Format-Erkennung** — `supported_formats` pro Player aus dem
   HELO-`Model`:
   - `squeezelite` / `squeezeplay` → moderne Codec-Menge
     (mp3, flac, aac, ogg, wav, aiff, pcm)
   - klassische Squeezebox (squeezebox/2/3/classic) → kein AAC/ALAC
   - unbekannte Modelle / leere Liste → „kann alles" (nie unnötig transkodieren)
   - Dateien: `src/lyrion/player/state.py`, `src/lyrion/player/manager.py`
     (`_formats_for_model`)

2. **Transcode-Entscheidung** — `protocol.py: _player_can_decode()` +
   `send_strm_to_player`: wenn der Player den Codec nicht native kann,
   wird das `strm`-Frame auf `codec='p'` (PCM) gesetzt und die Request-URL
   um `&transcode=1` ergänzt.

3. **ffmpeg-PCM-Pipeline** — `src/lyrion/web/stream.py`:
   - `ffmpeg_available()` (cached), `ffprobe_audio_info()`
   - `_transcode_to_pcm_pipe()`: Datei via ffmpeg → rohes Little-Endian-PCM
     (Rate/Kanäle/Bits aus ffprobe der Quelle, kein Resampling)
   - `?transcode=1`-Zweig in `stream_track`

4. **Graceful ohne ffmpeg** — Wenn ffmpeg fehlt, läuft der LMS normal
   weiter (liefert den Source direkt) und zeigt in der Statusleiste:
   `ffmpeg not found - transcoding not possible`.
   - `src/lyrion/web/api.py`: `_SERVER_NOTICE` + `set_server_notice()`,
     `notice`-Feld in `serverstatus`
   - `html/index.html`: Statusleisten-Notice-Slot + serverstatus-Poll

## Verifiziert

- Transcode-Endpoint: FLAC → PCM korrekt (529200B, `audio/L16`,
  kein `fLaC`-Header mehr, sofort beim ersten Sample).
- Entscheidungslogik (Unit-Test):
  - SqueezeLite → flac ✓, aac ✓ (kann alles)
  - Classic Squeezebox → flac ✓, **aac ✗** (→ Transcode)
  - unbekannt / None → kann alles ✓
- Normale FLAC-Wiedergabe nach den Änderungen weiterhin sauber
  („ein Ton" vom Nutzer bestätigt; kein Bruch durch den Fallback).
- `test-clients.py` + `test-jive-cometd.py` grün; `hermes verify` → ok.

## Offene Punkte (weil zurückgestellt — NICHT anfangen)

- **Doppel-`strm`:** Im Log erscheint bei einem Play manchmal zweimal
  `Sent strm to ... track=2 codec=f` (hörbar aber nur ein Ton). Ursache
  ungeklärt — evtl. doppelter Command-Trigger. Nicht weiter verfolgt.
- **Live-Test „ffmpeg fehlt"** nicht end-to-end geprüft, da ffmpeg
  inzwischen installiert ist. Die Notice erscheint nur, wenn der Player
  einen Codec nicht kann UND ffmpeg fehlt. Logik per Unit-Test bestätigt.
- **Simulierter „Classic Squeezebox"-Player** (der AAC/FLAC nicht kann)
  wurde nicht als end-to-end Fallback-Test gefahren.

## Wieder aufnehmen

Wenn die Transkodierung wieder aufgenommen werden soll: obige offene
Punkte zuerst (Doppel-`strm`, echter „ffmpeg fehlt" / Classic-Player-Test).
