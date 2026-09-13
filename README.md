# Pyrion Music Server — Python Port

A LMS-compatible streaming audio server for Squeezebox and compatible
players (Squeezelite, SqueezePlay, SqueezeESP32, ...), implemented in
Python 3. This project re-implements the SlimProto / JSON-RPC / CLI
protocol surface of the original (Perl) Lyrion Music Server (LMS). The
Python package stays `lyrion` (`python -m lyrion`, data under `~/.lyrion`).

> **Status: WORK IN PROGRESS — playback verified, 2026-09 parity program
> largely closed.** Player discovery, registration, control, the web UI and
> audio playback work; the Slimproto display/IR frames, the Jive menu set,
> the streaming state machine, Perl's single-line CLI text format and the
> library search/transcoding/ReplayGain rules were re-implemented against the
> Perl reference. The suite is **1174 pytest tests green** (`LMS_TEST_SPAWN=1`,
> excluding `tests/test_contract.py`, which needs a live server). Genuinely
> open points are listed under [What does not work](#what-does-not-work).

---

## What works

| Area | Status |
|------|--------|
| SlimProto server (TCP 3483) | ✅ Player registration (HELO 20/36-byte + SETD), name handling, keepalive, `audg` volume sync on connect, `aude` output-enable on HELO, disconnect grace (`FORGET_DISCONNECTED_TIME = 300 s`, `networking/protocol.py:718`) |
| Discovery service (UDP 3483) | ✅ Broadcast beacons, HELO-ACK |
| SlimProto display frames | ✅ `visu` / `grfb` / `grfe` / `grfd` / `vfdc` rendered from the real LMS bitmap fonts (`graphics/*.font.bmp`) with the now-playing line text (`player/display.py`, `player/fonts.py`) |
| IR / button opcodes (PROT-19) | ✅ `IR` / `BUTN` / `KNOB` frames translated into player actions from the Perl IR tables (`Slim/Networking/Slimproto.pm:521-546`, `player/buttons.py`) |
| Streaming state machine | ✅ `STOPPED` / `BUFFERING` / `PLAYING` / `PAUSED` and the transition table mirroring `Slim/Player/StreamingController.pm` (`player/streaming.py`) |
| JSON-RPC API (HTTP 9000) | ✅ `server.*`, `player.*`, `playlist.*` methods, `slim.request` passthrough (playlist album_id/artist_id expansion, mixer, shuffle/repeat) |
| Web auth | ✅ Optional Basic-auth gate on the `authorize` pref; CLI `login` validates the configured `password` (2026-09) |
| CometD | ✅ subscribe/unsubscribe semantics, status notify replays the stored request (pagination/menu/tags preserved) |
| CLI (TCP 9090) | ✅ Status, browse (artists/albums/songs/radio), playlist control, CR/NUL-terminated wire + percent-decoded player ids; every response is **one percent-escaped line** exactly like Perl (`Slim/Plugin/CLI/Plugin.pm:692-698`, `control/queries.py`) |
| Jive menus / settings / alarms | ✅ SqueezePlay settings menus (tone, volume, XL, line-out, crossfade, ReplayGain, brightness, fonts, album sort, date) plus the alarm/sync/sleep/list menus (`Slim/Control/Jive.pm:57-162`, `control/jive.py`) |
| Web UI | ✅ Single-file SPA (`html/index.html`, hash routing, LMS-style skin, no external deps) + bundled Material skin under `/material` |
| Web settings pages | ✅ Classic LMS settings pages with pref write paths (`web/settings.py`) |
| i18n (DE) | ✅ Jive/JSON-RPC texts localized from Perl's `strings.txt` tables; active language comes from the `language` pref (`Slim/Utils/Strings.pm:526`, `i18n/strings_de.py`) |
| Library scanner | ✅ SQLite DB (`~/.lyrion/Lyrion/Prefs/lyrion.db`), 50k+ tracks, streaming incremental walk, album identity (title+artist), metadata heuristics |
| Library rules | ✅ Transcoding rule table parsed from `convert.conf` / `types.conf` (`Slim/Player/TranscodingHelper.pm:51-135`, `media/transcoding.py`); search tokenizing + relevance ranking (`Slim/Plugin/FullTextSearch/Plugin.pm:486-503`, `control/queries.py`); ReplayGain scanner side + `strm` gain chain (`media/replaygain.py`, `player/replaygain.py`) |
| Rescan | ✅ Full/additive scan modes, `abortscan`, deletion reconciliation on full rescan (Perl parity, 2026-09) |
| Player control | ✅ power (strm stop), volume (audg), play/pause/stop/next/prev, skip-ahead seek (`strm 'a'` 24-byte frame), resume-restores-position, playlist management, favorites, IR/button actions, alarms (per player, `fr:`/`track:`/`url:` wake sources) |
| **Audio playback** | ✅ **MP3 + FLAC end-to-end on Squeezelite v2.0.0** (direct streaming; proxy/transcode only when the player cannot decode the source format) |
| **Radio / favorites** | ✅ **Direct streaming** like the real LMS (strm → source, server `cont` with metaint, RESP round-trip); AAC/HE-AAC streams transcode-proxy to MP3; HTTPS radio plays |
| Auto-next / end-of-track | ✅ Playlist advance on player STAT `STMd`; last track → stop; local-track starts clear the radio flag |
| Test tone | ✅ `/stream.mp3?testtone=1` (440 Hz, 5 s WAV) |

## What does not work

Honest boundaries — no marketing. Everything below is deliberately either
unimplemented or only approximated:

| Area | Status |
|------|--------|
| External transcoder binaries | ❌ The `convert.conf` rule table is parsed and the profile is selected, but only **ffmpeg** is actually spawned. The `[flac]` / `[sox]` / `[lame]` / … lookups from `convert.conf` are resolved for profile choice, never launched (`media/transcoding.py` vs the ffmpeg-only `web/stream.py`) |
| Full-text-search ranking | ⚠️ Relevance is a **LIKE-based approximation** of the FullTextSearch plugin (`fulltext_weight`, `control/queries.py`); SQLite FTS `matchinfo` scoring is not reproduced |
| Font renderer: TrueType / BiDi | ❌ Only the bitmap `.font.bmp` path is ported. The TTF branch (`Fonts.pm:318-355`, `Font::FreeType`) and the Hebrew BiDi reversal (`:349-354`) are missing (`player/fonts.py`) |
| Player `screen2` | ❌ Not wired. Perl shows this album/artist screen only on the Transporter (`Display.pm:859`, `Transporter.pm:319-325`); the port sends the single now-playing + visualizer frame set |
| Alarm details | ❌ Perl's `alarmTimeoutSeconds` timer and the `['alarm','snooze']` notifications are missing (`alarms.py`) |
| StreamingController buffer decisions | ⏳ `_CheckPaused` / `_CheckSync` (rebuffer/pause on buffer state) need a real client's `usage()` / `bufferFullness()` STAT data; the state machine exists, the fill-level decisions are not exercised (`player/streaming.py`) |
| Sync (multi-player group playback) | ⏳ Group bookkeeping and the Jive sync menus exist; real synchronized fan-out is still the last known parity gap |
| Server-side HTTPS listener | ❌ The listening socket is HTTP; remote HTTPS streams play fine (player-side TLS or transcode proxy) |
| Playlist **file** import (`.m3u` on disk) | ❌ DB-saved playlists are fully supported (CLI + JSON); scanning playlist *files* from the media dir is not implemented |

## Operations

- **Rescan after schema changes.** A few library answers read columns a later
  schema version filled. Real genre IDs (`genres.id`) are only written by the
  rescan importer (`Slim/Schema/Genre.pm:91-132`, `media/importer.py`), so
  after any schema change the library must be rescanned once — otherwise
  `genres` queries keep returning stale/empty IDs.
- **Restart via `tools/restart_server.sh`, not by hand.** It waits until the
  old process has really released the ports (up to 90 s) before starting
  exactly one new instance, then verifies all four ports (9000/9090/9080/3483).
  Lesson from two double-starts: `kill -TERM` needs noticeably longer than a
  few seconds with a large library, and a premature restart leaves two
  instances where one cannot bind 9090/3483 (`OSError 98`) → the CLI listener
  is dead.
- **Back up the DB before schema interventions.** Copy the `.db` file first;
  `tools/_drop_stale_album_unique.py` does a timestamped `.bak-<ts>` before it
  writes.

## Architecture

```
src/lyrion/
├── __main__.py          # entry point; starts SlimProtoServer, DiscoveryService, CLI, web
├── networking/
│   ├── protocol.py      # SlimProto frames (HELO, strm, stat, setd, audg, aude, IR/BUTN/KNOB)
│   └── discovery.py     # UDP discovery beacons
├── player/
│   ├── manager.py       # PlayerManager: register, play_track/play_url, volume/power
│   ├── state.py         # PlayerState dataclass
│   ├── streaming.py     # StreamingController state machine (Perl StreamingController.pm)
│   ├── display.py       # visu/grfb/grfe/grfd/vfdc frame rendering
│   ├── fonts.py         # LMS bitmap-font (.font.bmp) parser
│   ├── buttons.py       # IR/BUTN/KNOB opcodes → actions (PROT-19)
│   ├── replaygain.py    # gain selection applied to strm frames
│   └── playerprefs.py   # per-player prefs
├── music/
│   ├── scanner.py       # library scan → SQLite
│   └── radio.py         # radio station browser
├── media/
│   ├── importer.py      # SQLite import (album identity, genres, ReplayGain)
│   ├── transcoding.py   # convert.conf/types.conf rule table + profile selection
│   └── replaygain.py    # ReplayGain tag munging (scanner side)
├── control/
│   ├── cli.py           # TCP CLI (9090)
│   ├── cli_commands.py
│   ├── jive.py          # Jive/SqueezePlay menus, settings, alarms
│   └── queries.py       # Perl CLI wire format + search ranking
├── web/
│   ├── api.py           # JSON-RPC / slim.request handlers
│   ├── cometd.py        # CometD long-polling
│   ├── settings.py      # classic settings pages
│   └── stream.py        # /stream.mp3 (tracks, test tone, remote proxy)
├── display/
│   └── screens.py       # display screens
├── i18n/
│   ├── strings_en.py    # EN failsafe strings (Perl strings.txt)
│   └── strings_de.py    # DE strings
├── alarms.py            # per-player alarm clock (JSON under Prefs)
└── database/
    ├── schema.py        # SQLAlchemy schema
    └── sqlite_helper.py # async DB session
```

## Run

```bash
# install (pyproject.toml) and run
python3 -m lyrion --loglevel info
```

Web UI: http://localhost:9000/ · JSON-RPC: `POST /jsonrpc.js` · CLI: port 9090 ·
SlimProto/UDP: 3483. Restart with `tools/restart_server.sh` (waits for a real
port release — see [Operations](#operations)); there is no shipped systemd unit.

To test without disturbing an existing (Perl) LMS on the standard ports,
use `--localfile` with different ports:

```bash
# test-ports.conf:  serverport = 9002 / slimproto_port = 3484 / cliport = 9091
python3 -m lyrion --localfile test-ports.conf --loglevel debug
/tmp/squeezelite/squeezelite -s 127.0.0.1:3484 -m 02:11:22:33:44:55 -n TestPlayer \
  -o hw:Loopback,0 -d slimproto=debug -d stream=debug -d decode=debug -d output=debug
# capture audio:  arecord -D hw:Loopback,1,0 -f S32_LE -r 44100 -c 2 -t wav /tmp/cap.wav
```

Run the suite (a live server is not required except for the contract tests):

```bash
LMS_TEST_SPAWN=1 .venv/bin/python -m pytest --ignore=tests/test_contract.py
# → 1174 passed
```

## Release notes

Recent rounds of the 2026-09 parity program (all committed and green):

- **Slimproto display (PROT-18).** `visu`/`vfdc`/`grfb` added, `grfe`/`grfd`/`vfdc`
  rendered like Perl from the real `.font.bmp` files, a framebuffer-backed
  `grfe`, and the now-playing display text; plus info totals over JSON-RPC
  (`player/display.py`, `player/fonts.py`).
- **IR / hardware buttons (PROT-19).** `IR`/`BUTN`/`KNOB` opcodes translated
  into player actions from the Perl IR tables (`player/buttons.py`).
- **StreamingController state machine** checked against
  `Slim/Player/StreamingController.pm` (`player/streaming.py`).
- **Jive menus / settings / alarms.** SqueezePlay settings menus, alarm/sync/
  sleep/list menus and the alarm/preset/search commands (`control/jive.py`).
- **CLI text format (CTRL-02).** Every command now answers Perl's single
  percent-escaped line (`control/queries.py`, `control/cli_commands.py`).
- **Library (LIB-10/16/17/35).** Perl transcode rules from `convert.conf`,
  search ranking, real genre IDs, ReplayGain read + `strm` application
  (`media/transcoding.py`, `control/queries.py`, `media/replaygain.py`).
- **Web settings pages (WEB-01/02).** Classic settings pages with write paths
  (`web/settings.py`).
- **i18n.** DE strings generated from Perl's `strings.txt` (`i18n/strings_de.py`).
- **Alarms.** Snooze re-arm, snooze length from prefs, pre-alarm state restore
  (`alarms.py`).
- **Operations.** `tools/restart_server.sh` now waits for a real port release.

## Provenance

The Perl LMS (Lyrion Music Server, public/9.2) is the reference for every
protocol path — a **design reference only**, never a runtime dependency; the
port neither links it nor shells out to it. Where a behaviour is taken from
Perl, the port's code carries the evidence inline as `Slim/...pm:line`
(examples: `player/buttons.py` cites `Slim/Networking/Slimproto.pm:521-546`;
`media/transcoding.py` cites `Slim/Player/TranscodingHelper.pm:51-135`).

Numbers in this README are checked against the repo: 1174 tests
(`pytest --ignore=tests/test_contract.py`, `LMS_TEST_SPAWN=1`); ports 3483
(SlimProto/UDP), 9000 (HTTP, `config.py` `serverport`), 9090 (CLI,
`config.py` `cliport`); disconnect grace `300 s` (`networking/protocol.py:718`).

## Notes

- The strm frame layout follows the Perl LMS (`pack 'aaaaaaaCCCaCCCNnN'`
  in `Slim/Player/Squeezebox.pm`; `networking/protocol.py:1869`). For normal proxy streams
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
