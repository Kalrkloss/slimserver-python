#!/usr/bin/env python3
"""Scan-latency probe: is the server still answerable while a scan runs?

Boots a throwaway server on the test ports (``test-ports.conf``) with its own
temp serverdata, builds a small synthetic library of real (tiny) WAV files,
sets ``musicdir`` to it, measures CLI ``status`` round-trips while idle,
triggers ``rescan`` and keeps measuring while the scan runs.

Usage::

    .venv/bin/python3 tools/scan_probe.py --music-dir /tmp/probe/music \\
        --files 400 --out /tmp/probe/before.json

Exit code 0 when the probe ran; the JSON holds the numbers (idle baseline,
samples during the scan, scan duration, scan mode reported by ``rescan ?``).
The probe never touches the live server: everything runs on 9002/9003/9091/3484
from ``test-ports.conf`` and in its own temp serverdata dir.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

WEB_PORT = 9002
CLI_PORT = 9091
SLIMPROTO_PORT = 3484
TEST_PLAYER = "02:11:22:33:44:55"


# ---------------------------------------------------------------------------
# library + player
# ---------------------------------------------------------------------------

def make_library(music_dir: Path, count: int) -> None:
    """``count`` tiny but real WAV files (mutagen parses them)."""
    music_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        folder = music_dir / f"Album{i // 10:03d}"
        folder.mkdir(exist_ok=True)
        path = folder / f"track{i:04d}.wav"
        if path.exists():
            continue
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)


def register_player(timeout: float = 20.0) -> socket.socket:
    """Minimal SlimProto HELO (same frame as tests/conftest.py)."""
    caps = b"Model=squeezelite,ModelName=Probe,CanHTTPS=0"
    body = (struct.pack(">I", 36 + len(caps))
            + bytes([4, 0])
            + bytes.fromhex(TEST_PLAYER.replace(":", ""))
            + b"\x00" * 16
            + struct.pack(">H", 0)
            + struct.pack(">I", 0)
            + struct.pack(">I", 0)
            + b"en"
            + caps)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", SLIMPROTO_PORT), timeout=2)
            s.sendall(b"HELO" + body)
            time.sleep(0.3)
            return s
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("SlimProto-Port nicht bereit")


def wait_port(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------

def cli_request(command: str, timeout: float = 10.0) -> tuple[float, str]:
    """Send one CLI line, return (seconds, reply line)."""
    start = time.perf_counter()
    with socket.create_connection(("127.0.0.1", CLI_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((command + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return time.perf_counter() - start, data.decode("utf-8", "replace").strip()


def jsonrpc_request(method: str, params: list, timeout: float = 30.0) -> dict:
    import urllib.request

    payload = json.dumps({"id": 1, "method": "slim.request",
                          "params": params}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}/jsonrpc.js", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-dir", required=True)
    ap.add_argument("--serverdata", default=None)
    ap.add_argument("--files", type=int, default=400)
    ap.add_argument("--idle-samples", type=int, default=5)
    ap.add_argument("--during-samples", type=int, default=400)
    ap.add_argument("--during-sleep", type=float, default=0.05)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-server", action="store_true")
    ap.add_argument("--start-server", action="store_true",
                    help="boot the throwaway server yourself (default: expect "
                         "one already running on the test ports)")
    args = ap.parse_args()

    music_dir = Path(args.music_dir).resolve()
    serverdata = Path(args.serverdata or (music_dir.parent / "serverdata")).resolve()
    serverdata.mkdir(parents=True, exist_ok=True)
    make_library(music_dir, args.files)

    proc = None
    if args.start_server:
        env = dict(os.environ, LYRION_SERVERDATA=str(serverdata))
        proc = subprocess.Popen(
            [sys.executable, "-m", "lyrion", "--localfile", "test-ports.conf",
             "--loglevel", "warning"],
            cwd=str(REPO), env=env,
            stdout=open(serverdata / "probe-server.log", "wb"),
            stderr=subprocess.STDOUT)
        if not wait_port(WEB_PORT):
            proc.kill()
            raise SystemExit("Test-Server nicht bereit (Port 9002)")

    player = register_player()
    jsonrpc_request("slim.request", ["", ["serverpref", "musicdir",
                                          str(music_dir)]])

    idle: list[float] = []
    for _ in range(args.idle_samples):
        dt, _reply = cli_request("status 0 20")
        idle.append(dt)
        time.sleep(0.2)

    # Trigger the scan and measure round-trips while it runs.
    t_scan_start = time.perf_counter()
    jsonrpc_request("slim.request", ["", ["rescan"]])
    during: list[dict] = []
    rescan_flag = None
    progress = None
    scan_seconds = None

    for _ in range(args.during_samples):
        try:
            dt, reply = cli_request("status 0 20")
            _dt2, rescan_reply = cli_request("rescan ?")
            _dt3, progress_reply = cli_request("rescanprogress")
        except OSError as exc:  # socket.timeout is an OSError subclass
            during.append({"status_s": float("inf"),
                           "reply_head": f"TIMEOUT: {exc}", "rescan": "?"})
            continue
        # "rescan 0" / "rescan 1" — the bare _rescan result (Queries.pm:3214)
        token = rescan_reply.split()
        rescan_flag = token[-1] if token else ""
        progress = progress_reply.split()[-1] if progress_reply else ""
        during.append({"status_s": dt, "reply_head": reply[:40],
                       "rescan": rescan_flag})
        if rescan_flag == "0":
            scan_seconds = time.perf_counter() - t_scan_start
            break
        time.sleep(args.during_sleep)

    after: list[float] = []
    for _ in range(args.idle_samples):
        dt, _reply = cli_request("status 0 20")
        after.append(dt)
        time.sleep(0.2)

    result = {
        "music_dir": str(music_dir),
        "files": args.files,
        "idle_status_s": idle,
        "during_scan_status_s": [d["status_s"] for d in during],
        "during_scan_count": len(during),
        "rescan_flag_during": during[0]["rescan"] if during else None,
        "progress_last": progress,
        "scan_seconds": scan_seconds,
        "after_scan_status_s": after,
    }

    def stats(vals: list[float]) -> dict:
        return {"n": len(vals),
                "min_ms": round(min(vals) * 1000, 1) if vals else None,
                "median_ms": round(statistics.median(vals) * 1000, 1) if vals else None,
                "max_ms": round(max(vals) * 1000, 1) if vals else None}

    result["summary"] = {
        "idle": stats(idle),
        "during_scan": stats(result["during_scan_status_s"]),
        "after_scan": stats(after),
        "during_scan_over_100ms": sum(
            1 for v in result["during_scan_status_s"] if v > 0.1),
    }

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result["summary"], indent=2))
    print("scan_seconds:", scan_seconds)

    try:
        player.close()
    except OSError:
        pass
    if proc is not None and not args.keep_server:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
