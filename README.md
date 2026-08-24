# Pyrion Music Server — Python Port

A LMS-compatible streaming audio server for Squeezebox and compatible
players (Squeezelite, SqueezePlay, SqueezeESP32, ...), implemented in
Python 3. This project re-implements the SlimProto / JSON-RPC / CLI
protocol surface of the original (Perl) Pyrion Music Server.

> **Status: WORK IN PROGRESS — playback verified.** Player discovery,
> registration, control, the web UI **and audio playback** work. MP3
> library tracks and radio streams (HTTP + HTTPS) play end-to-end on
> Squeezelite (verified via ALSA-loopback capture). **FLAC decodes but
> stays silent** — see [What does not work](#what-does-not-work).

---

## What works

| Area | Status |
|------|--------|
| SlimProto server (TCP 3483) | ✅ Player registration (HELO/SETD), name handling, keepalive, audg volume sync on connect |
| Discovery service (UDP 3483) | ✅ Broadcast beacons, HELO-ACK |
| JSON-RPC API (HTTP 9000) | ✅ `server.*`, `player.*`, `playlist.*` methods, `slim.request` passthrough |
| CLI (TCP 9090) | ✅ Status, browse (artists/albums/songs/radio), playlist control |
| Web UI | ✅ Single-file SPA (`html/index.html`, hash routing, LMS-style skin, no external deps) |
| Library scanner | ✅ SQLite DB (`~/.lyrion/Lyrion/Prefs/lyrion.db`), 50k+ tracks, genres, artists, albums |
| Player control | ✅ power/volume (audg frame), play/pause/stop/next/prev (strm frames), playlist management, favorites |
| **Audio playback (MP3)** | ✅ **End-to-end verified on Squeezelite v2.0.0** (strm → HTTP `/stream.mp3` → decode → output; capture peak 0.5 FS) |
| **Radio / favorites** | ✅ **Direct streaming** like the real LMS: strm frame points at the source, player connects directly, server sends `cont` with metaint (RESP round-trip); proxy fallback |
| Auto-next / end-of-track | ✅ Playlist advance on player STAT `STMd` (decoder complete); last track → stop |
| Test tone | ✅ `/stream.mp3?testtone=1` (440 Hz, 5 s WAV) |

## What does not work

| Area | Status |
|------|--------|
| **FLAC playback** | ❌ **Not solved.** Decoder opens (`codec open: 'f'`), HTTP 200, output starts (`track_start`, `start buffer frames`) — but the captured audio stays silent. MP3 and radio work. |
| Transcoding | ❌ Not implemented (no flac→mp3, no format conversion) |
| Squeezebox hardware display | ❌ No display/title line support |
| HTTPS | ❌ HTTP only (server side; remote HTTPS radio is proxied) |
| IR / remote control | ❌ Not implemented |

## Architecture

```
src/lyrion/
├── __main__.py          # entry point; starts SlimProtoServer, DiscoveryService, CLI, web
├── networking/
│   ├── protocol.py      # SlimProto frames (HELO, strm, stat, setd, audg), player connections
│   └── discovery.py     # UDP discovery beacons
├── player/
│   ├── manager.py       # PlayerManager: register, play_track/play_url, volume/power
│   └── state.py         # PlayerState dataclass
├── music/
│   ├── scanner.py       # library scan → SQLite
│   └── radio.py         # radio station browser
├── web/
│   ├── api.py           # JSON-RPC / slim.request handlers
│   └── stream.py        # /stream.mp3 (tracks, test tone, remote proxy)
├── control/
│   ├── cli.py           # TCP CLI (9090)
│   └── cli_commands.py
└── database/
    ├── schema.py        # SQLAlchemy schema
    └── sqlite_helper.py # async DB session
```

## Run

```bash
# install (pyproject.toml) and run
python3 -m lyrion --loglevel info
# or as systemd service (see lyrion.service)
```

Web UI: http://localhost:9000/ · JSON-RPC: `POST /jsonrpc.js` · CLI: port 9090

To test without disturbing an existing (Perl) LMS on the standard ports,
use `--localfile` with different ports:

```bash
# test-ports.conf:  serverport = 9002 / slimproto_port = 3484 / cliport = 9091
python3 -m lyrion --localfile test-ports.conf --loglevel debug
/tmp/squeezelite/squeezelite -s 127.0.0.1:3484 -m 02:11:22:33:44:55 -n TestPlayer \
  -o hw:Loopback,0 -d slimproto=debug -d stream=debug -d decode=debug -d output=debug
# capture audio:  arecord -D hw:Loopback,1,0 -f S32_LE -r 44100 -c 2 -t wav /tmp/cap.wav
```

## Notes

- The strm frame layout follows the Perl LMS (`pack 'aaaaaaaCCCaCCCNnN'`
  in `Slim/Player/Squeezebox.pm`). For normal proxy streams
  `autostart='1'` (ASCII) — `'3'` would set `cont_wait` on Squeezelite
  (`autostart - '0' >= 2`) and wait forever for a `cont` frame that LMS
  never sends. `transition_type='0'` (ASCII), PCM fields `'?'`, threshold/
  output_threshold numeric.
- Like the original LMS, library tracks are fetched through the server
  (`GET /stream.mp3?player=MAC HTTP/1.0`, no Host header). **Radio
  streams are streamed DIRECTLY from the source** (stream_s `$isDirect`
  branch): the strm frame carries the source IP/port + the source request
  string (HTTP.pm requestString, `Icy-MetaData: 1`), `autostart=3`
  (direct), SSL flag `0x20` for https. After the player connects it
  forwards the source's response headers as a RESP frame; the server
  replies with a `cont` frame (`metaint, loop, guids`) so the player
  strips Icecast metadata itself. The stream keeps playing if the server
  goes away. Proxy fallback only when the source cannot be resolved
  (the proxy then strips Icecast metadata server-side — Squeezelite does
  not parse `icy-metaint` from headers).
- **audg on connect is mandatory**: Squeezelite zero-initialises its
  internal gain; without an `audg` frame every sample is multiplied by 0
  → decoder runs, output runs, but nothing is audible. The server sends
  the current volume (gain = volume * 655.36) right after HELO.
- **DSCO is end-of-stream, not a disconnect**: Squeezelite sends `DSCO`
  and reconnects whenever a stream disconnects (for local files that is
  right after the strm). The player must NOT be unregistered then —
  playlist/volume/mode survive the reconnect (only `bye`/TCP-close
  unregisters).
- **Auto-next is driven by player STAT `STMd`** (decoder complete), not
  by HTTP-send completion — the server can push a whole local file into
  the player's buffer in <1 s while it still plays for 30 s; advancing on
  send-completion restarts the track in an endless loop.
- The web port is passed into the SlimProto handler
  (`SlimProtoClient(web_port=...)`) so the strm frame's `server_port`
  and `serverstatus.httpport` follow non-standard HTTP ports.
