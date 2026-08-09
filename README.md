# Lyrion Music Server — Python Port

A LMS-compatible streaming audio server for Squeezebox and compatible
players (Squeezelite, SqueezePlay, SqueezeESP32, ...), implemented in
Python 3. This project re-implements the SlimProto / JSON-RPC / CLI
protocol surface of the original (Perl) Lyrion Music Server.

> **Status: WORK IN PROGRESS.** Player discovery, registration, control
> and the web UI work. **Audio playback on real SlimProto players is not
> confirmed yet** — see [What does not work](#what-does-not-work).

---

## What works

| Area | Status |
|------|--------|
| SlimProto server (TCP 3483) | ✅ Player registration (HELO/SETD), name handling, `players`/`player`/`playlist` CLI commands |
| Discovery service (UDP 3483) | ✅ Broadcast beacons, HELO-ACK |
| JSON-RPC API (HTTP 9000) | ✅ `server.*`, `player.*`, `playlist.*` methods, `slim.request` passthrough |
| CLI (TCP 9090) | ✅ Status, browse (artists/albums/songs/radio), playlist control |
| Web UI | ✅ Single-file SPA (`html/index.html`, hash routing, LMS-style skin, no external deps) |
| Library scanner | ✅ SQLite DB (`~/.lyrion/Lyrion/Prefs/lyrion.db`), 50k+ tracks, genres, artists, albums |
| Player control | ✅ power/volume/mode, play/pause/stop/next/prev, playlist management, favorites |
| Radio / favorites | ✅ Server-side proxy streaming (`/stream.mp3`), icy-metaint forwarding |
| Test tone | ✅ `/stream.mp3?testtone=1` (440 Hz, 5 s WAV) |

## What does not work

| Area | Status |
|------|--------|
| **Audio playback on SlimProto players** | ❌ **Not solved.** The strm frame is sent (LMS-compatible layout, numeric fields), the player opens the HTTP stream and reads it (`streambuf read`, `sendRESP`), but the decoder produces only silence (`_output_frames ... silence: 1`) on Squeezelite v1.9.9 as well as v2.0.0 — for both library tracks and proxied radio streams. Cause not found. |
| Transcoding | ❌ Not implemented (no flac→mp3, no format conversion) |
| Squeezebox hardware display | ❌ No display/title line support |
| HTTPS | ❌ HTTP only |
| IR / remote control | ❌ Not implemented |

## Architecture

```
src/lyrion/
├── __main__.py          # entry point; starts SlimprotoServer, DiscoveryService, CLI, web
├── networking/
│   ├── protocol.py      # SlimProto frames (HELO, strm, stat, setd), player connections
│   └── discovery.py     # UDP discovery beacons
├── player/
│   └── manager.py       # PlayerManager: register, play_track/play_url, volume/power
├── music/
│   ├── scanner.py       # library scan → SQLite
│   └── radio.py         # radio station browser
├── web/
│   ├── api.py           # JSON-RPC / slim.request handlers
│   └── stream.py        # /stream.mp3 (tracks, test tone, remote proxy)
├── control/
│   ├── cli.py           # TCP CLI (9090)
│   └── cli_commands.py
└── db/
    └── database.py      # SQLite schema + queries
```

## Run

```bash
# install (pyproject.toml) and run
python3 -m lyrion --loglevel info
# or as systemd service (see lyrion.service)
```

Web UI: http://localhost:9000/ · JSON-RPC: `POST /jsonrpc.js` · CLI: port 9090

## Notes

- The strm frame layout follows the Perl LMS (`pack 'aaaaaaaCCCaCCCNnN'`
  in `Slim/Player/Squeezebox.pm`). Field values are sent as numeric bytes;
  `autostart`/`transition_type` are sent as ASCII (`'3'`/`'0'`) so both
  Squeezelite v1.9.x (`- '0'`) and v2.0.0 parse them correctly.
- Like the original LMS, players always fetch streams through the server
  (`GET /stream.mp3?player=MAC`); the server proxies external radio URLs
  and forwards `icy-metaint` so the player can de-interleave Icecast
  metadata.
