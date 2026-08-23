#!/usr/bin/env python3
"""SqueezePlay/Jive protocol test against the Python LMS.

Simulates the exact Bayeux/Cometd flow of SqueezePlay:
  1. /meta/handshake
  2. /meta/connect (long-poll)
  3. /meta/subscribe <clientId>/**
  4. slim/request commands the UI sends at startup
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9002"
cid = None
failures = []


def post(messages):
    req = urllib.request.Request(
        f"{BASE}/cometd",
        data=json.dumps(messages).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


def _result_of(replies: list) -> dict:
    """Extract the slim.request result from a cometd reply batch.

    The server answers with two messages: the /slim/request ack and a
    second message (channel "") whose data IS the result payload.
    """
    for m in replies:
        if isinstance(m, dict) and m.get("channel") == "" and isinstance(m.get("data"), dict):
            d = m["data"]
            if "response" in d and isinstance(d["response"], str):
                return json.loads(d["response"]).get("result", {})
            if "result" in d:
                return d["result"]
            return d
        if isinstance(m, dict) and isinstance(m.get("data"), dict) and "request" in m.get("data", {}):
            continue
    return {}


# --- 1. handshake ---
r = post([{"channel": "/meta/handshake", "supportedConnectionTypes": ["long-polling", "streaming"], "version": "1.0"}])
resp = r[0] if isinstance(r, list) else r
check("handshake successful", resp.get("successful") is True, f"clientId={resp.get('clientId')}")
cid = resp.get("clientId")

# --- 2. subscribe to everything (jive does /lyrion-N/** or similar) ---
r = post([
    {"channel": "/meta/connect", "connectionType": "long-polling", "clientId": cid},
    {"channel": "/meta/subscribe", "subscription": f"/{cid}/**", "clientId": cid},
])
subs = [m for m in r if m.get("channel") == "/meta/subscribe"]
check("subscribe ok", subs and subs[0].get("successful") is True)

# --- 3. serverstatus via slim.request (jive startup call) ---
r = post([{"channel": "/slim/request", "clientId": cid, "data": {"request": ["-", ["serverstatus", "0", "5"]]}}])
res = _result_of(r)
check("serverstatus has players_loop", bool(res.get("players_loop")), f"keys={sorted(res.keys())[:6]}")

# --- 4. player status for our squeezelite ---
r = post([{"channel": "/slim/request", "clientId": cid, "data": {"request": ["02:11:22:33:44:55", ["status", "-", "1", "tags:cgal"]]}}])
res = _result_of(r)
ok = "player_name" in res and "mode" in res
check("player status shape", ok, f"player_name={res.get('player_name')} mode={res.get('mode')}")

# --- 5. menu query (home menu, jive renders this) ---
r = post([{"channel": "/slim/request", "clientId": cid, "data": {"request": ["-", ["menu", "0", "50"]]}}])
res = _result_of(r)
items = res.get("item_loop", []) or res.get("loop_loop", [])
check("menu item_loop non-empty", len(items) > 0, f"count={len(items)}")
for it in items[:3]:
    print("   menu:", it.get("id"), "|", (it.get("text") or "")[:30])

print()
print("FAILURES:", failures if failures else "none — all jive-protocol checks passed")
sys.exit(1 if failures else 0)
