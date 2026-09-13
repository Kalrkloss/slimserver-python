#!/usr/bin/env python3
"""Wartet auf das Ende des Bibliotheks-Scans und fasst das Ergebnis zusammen.

Read-only: fragt den Server (rescanprogress) und die DB (Zaehlungen) ab.
Beendet sich, sobald rescan:0 meldet (oder nach dem Zeitlimit).
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time

DB = "/home/keiner/.lyrion/Lyrion/Prefs/lyrion.db"
URL = "http://127.0.0.1:9000/jsonrpc.js"
DEADLINE = time.time() + 3 * 3600


def jrpc(params):
    body = json.dumps({"id": 1, "method": "slim.request", "params": params})
    out = subprocess.run(
        ["curl", "-s", "-m", "10", "-X", "POST", URL,
         "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True).stdout
    return out


def counts():
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "tracks": q("SELECT COUNT(*) FROM tracks"),
                "genres": q("SELECT COUNT(*) FROM genres"),
                "genre_track": q("SELECT COUNT(*) FROM genre_track"),
                "tracks_genres": q("SELECT COUNT(*) FROM tracks_genres"),
                "rg_tracks": q("SELECT COUNT(*) FROM tracks WHERE replay_gain IS NOT NULL"),
                "albums": q("SELECT COUNT(*) FROM albums"),
            }
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def status():
    raw = jrpc(["", ["rescanprogress"]])
    m = re.search(r"rescan%3A(\d)", raw or "")
    return (m.group(1) if m else "?"), (raw or "")[:200]


print("watcher: warte auf Scan-Ende", flush=True)
last = None
while time.time() < DEADLINE:
    state, raw = status()
    c = counts()
    line = f"rescan={state} {c}"
    if line != last:
        print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
        last = line
    if state == "0":
        print("SCAN FERTIG", flush=True)
        print("Zusammenfassung:", json.dumps(c, indent=1, ensure_ascii=False), flush=True)
        break
    time.sleep(45)
else:
    print("Zeitlimit erreicht, Scan laeuft noch", flush=True)
