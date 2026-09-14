"""Fetch the Radio-directory answers of the live Perl LMS as a test fixture.

Read-only ``slim.request`` probes (no play/scan/write command) against
192.168.1.90:9000 — same procedure as ``tools/_fetch_fav_fixtures.py`` for the
favourites fixtures.  The fixture records the exact request per case and is
used by ``tests/test_radio_structure.py`` to compare our answers'
*structure* (field sets, item types, ``cmd`` arrays, weights, order) with
Perl's — never the data (TuneIn vs. radio-browser).

Run:  ``python tools/_fetch_radio_fixture.py``  (needs the live Perl LMS).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASE = "http://192.168.1.90:9000/jsonrpc.js"
PLAYER = "00:04:20:2b:88:c8"
OUT = Path("/home/keiner/Downloads/git/slimserver-python/tests/fixtures/"
           "perl_radio_structure.json")


def query(*args: str) -> dict:
    payload = json.dumps({"id": 1, "method": "slim.request",
                          "params": [PLAYER, list(args)]})
    proc = subprocess.run(
        ["curl", "-s", "--max-time", "40", "-H", "Content-Type: application/json",
         "-d", payload, BASE], capture_output=True, text=True)
    data = json.loads(proc.stdout)
    return {"params": [PLAYER, list(args)], "result": data.get("result", {})}


def main() -> None:
    out: dict[str, dict] = {}
    out["radios_plain"] = query("radios", "0", "3")
    out["radios_menu"] = query("radios", "0", "3", "menu:radio")
    out["radios_menu_search_item"] = query("radios", "9", "1", "menu:radio")
    out["local_index"] = query("local", "items", "0", "6", "menu:local")
    sid = out["local_index"]["result"]["item_loop"][0]["actions"]["go"]["params"]["item_id"]
    out["local_stations"] = query("local", "items", "0", "4", "menu:local",
                                  f"item_id:{sid}")
    station = out["local_stations"]["result"]["item_loop"][0]["params"]["item_id"]
    out["local_station_leaf"] = query("local", "items", "0", "4", "menu:local",
                                      f"item_id:{station}")
    out["local_station_interim_cm"] = query(
        "local", "items", "0", "4", "menu:local", f"item_id:{station}",
        "isContextMenu:1", "xmlBrowseInterimCM:1")
    out["local_all_country"] = query("local", "items", "0", "4", "menu:local",
                                     f"item_id:{sid.rsplit('.', 1)[0]}.1")
    out["music_index"] = query("music", "items", "0", "3", "menu:music")
    out["location_index"] = query("location", "items", "0", "3", "menu:location")
    out["language_index"] = query("language", "items", "0", "3", "menu:language")
    out["presets_empty"] = query("presets", "items", "0", "3", "menu:presets")
    out["podcast_index"] = query("podcast", "items", "0", "3", "menu:podcast")
    out["search_plain"] = query("search", "items", "0", "6", "menu:search")
    out["search_rock"] = query("search", "items", "0", "4", "menu:search",
                               "search:rock")
    out["artists_search"] = query("artists", "0", "5", "search:radio")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    for key, value in out.items():
        result = value["result"]
        loops = [k for k in result if k.endswith("_loop")]
        print(f"{key:26} keys={list(result)} loops={loops} "
              f"count={result.get('count')}")


if __name__ == "__main__":
    main()
