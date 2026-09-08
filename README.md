# Pyrion Music Server — Python Port

A LMS-compatible streaming audio server for Squeezebox and compatible
players (Squeezelite, SqueezePlay, SqueezeESP32, ...), implemented in
Python 3. This project re-implements the SlimProto / JSON-RPC / CLI
protocol surface of the original (Perl) Pyrion Music Server.

> **Status: WORK IN PROGRESS — playback verified.** Player discovery,
> registration, control, the web UI **and audio playback** work. MP3/FLAC
> library tracks and radio streams (HTTP + HTTPS) play end-to-end on
> Squeezelite (verified via ALSA-loopback capture). A parity-closure
> program against the original Perl LMS ran through 2026-09 (security,
> playback state, dispatch/browse, library integrity, alarms/playlists,
> prefs/HTTP-range) — see [docs/protocol-gaps.md](docs/protocol-gaps.md)
> for the current remaining-work list.

---

## What works

| Area | Status |
|------|--------|
| SlimProto server (TCP 3483) | ✅ Player registration (HELO 20/36-byte + SETD), name handling, keepalive, audg volume sync on connect, disconnect grace (`forget_disconnected_client`, 300 s) |
| Discovery service (UDP 3483) | ✅ Broadcast beacons, HELO-ACK |
| JSON-RPC API (HTTP 9000) | ✅ `server.*`, `player.*`, `playlist.*` methods, `slim.request` passthrough (playlist album_id/artist_id expansion, mixer, shuffle/repeat) |
| Web auth | ✅ Optional Basic-auth gate on the `authorize` pref; CLI `login` validates the configured `password` (2026-09) |
| CLI (TCP 9090) | ✅ Status, browse (artists/albums/songs/radio), playlist control, CR/NUL-terminated wire + percent-decoded player ids |
| CometD | ✅ subscribe/unsubscribe semantics, status notify replays the stored request (pagination/menu/tags preserved) |
| Web UI | ✅ Single-file SPA (`html/index.html`, hash routing, LMS-style skin, no external deps) + bundled Material skin under `/material` |
| Library scanner | ✅ SQLite DB (`~/.lyrion/Lyrion/Prefs/lyrion.db`), 50k+ tracks, streaming incremental walk, album identity (title+artist), metadata heuristics |
| Rescan | ✅ Full/additive scan modes, `abortscan`, deletion reconciliation on full rescan (Perl parity, 2026-09) |
| Player control | ✅ power (strm stop), volume (audg), play/pause/stop/next/prev, skip-ahead seek (`strm 'a'` 24-byte frame), resume-restores-position, playlist management, favorites, alarms (per player, `fr:`/`track:`/`url:` wake sources) |
| **Audio playback** | ✅ **MP3 + FLAC end-to-end on Squeezelite v2.0.0** (direct streaming; proxy/transcode only when the player cannot decode the source format) |
| **Radio / favorites** | ✅ **Direct streaming** like the real LMS (strm → source, server `cont` with metaint, RESP round-trip); AAC/HE-AAC streams transcode-proxy to MP3; HTTPS radio plays |
| Auto-next / end-of-track | ✅ Playlist advance on player STAT `STMd`; last track → stop; local-track starts clear the radio flag |
| Test tone | ✅ `/stream.mp3?testtone=1` (440 Hz, 5 s WAV) |

## What does not work

| Area | Status |
|------|--------|
| Squeezebox hardware display | ❌ No classic-SB display/title-line support (SqueezePlay/SPA/controllers work) |
| IR / hardware remote control | ❌ Not implemented (Squeezelite IRC/keys not served) |
| Server-side HTTPS listener | ❌ Listening socket is HTTP; remote HTTPS streams play fine (player-side TLS or transcode proxy) |
| Playlist **file** import (`.m3u` on disk) | ❌ DB-saved playlists are fully supported (CLI + JSON); scanning playlist *files* from the media dir is not implemented |
| Sync (multi-player group playback) | ⏳ Group bookkeeping exists; real synchronized fan-out is the last known parity gap (deferred to the end of the 2026-09 plan) |

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
