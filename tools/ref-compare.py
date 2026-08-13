#!/usr/bin/env python3
"""Reference comparison: Python-LMS (9000) vs. Perl-LMS (9003).

Queries both servers with identical slim.request commands and reports
structural differences (missing keys, wrong types, empty vs. filled).
Run with the Perl reference server on 9003:
  sudo -u squeezeboxserver env HOME=/var/lib/squeezeboxserver \
    /usr/sbin/squeezeboxserver --prefsdir ... --httpport 9003 ...
"""
import json
import sys
import urllib.request

PY = 9000
PERL = 9003


def ask(port: int, params: list) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/jsonrpc.js",
        data=json.dumps({"id": 1, "method": "slim.request", "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())["result"]
    except Exception as exc:
        return {"__error__": str(exc)}


def item_shape(item: dict) -> dict:
    """Key -> (type, sample value) for structural comparison."""
    out = {}
    for k, v in item.items():
        if isinstance(v, dict):
            out[k] = {"_dict_": item_shape(v)}
        elif isinstance(v, list):
            out[k] = {"_list_len_": len(v)}
        else:
            out[k] = type(v).__name__
    return out


def compare(name: str, params: list, focus_keys: list | None = None) -> None:
    py = ask(PY, params)
    perl = ask(PERL, params)
    print(f"\n=== {name} ===")
    print(f"  Request: {params}")
    if "__error__" in perl:
        print(f"  PERL: {perl['__error__']}")
        return
    if "__error__" in py:
        print(f"  PYTHON: {py['__error__']}")
        return
    # Top-level keys
    py_keys = set(py.keys())
    perl_keys = set(perl.keys())
    print(f"  Perl top-level keys: {sorted(perl_keys)}")
    print(f"  Py   top-level keys: {sorted(py_keys)}")
    missing = perl_keys - py_keys
    if missing:
        print(f"  ! FEHLT in Python: {sorted(missing)}")
    for k in sorted(perl_keys & py_keys):
        pv, yv = perl.get(k), py.get(k)
        if isinstance(pv, list) and isinstance(yv, list):
            print(f"  {k}: perl={len(pv)} items, py={len(yv)} items")
            if pv and yv and focus_keys is None:
                ps, ys = item_shape(pv[0]), item_shape(yv[0])
                if ps != ys:
                    print(f"    ! Item-Struktur unterschiedlich:")
                    print(f"      PERL: {json.dumps(ps, ensure_ascii=False)[:250]}")
                    print(f"      PY:   {json.dumps(ys, ensure_ascii=False)[:250]}")
                else:
                    print(f"    Item-Struktur identisch: {sorted(ps)}")
        elif isinstance(pv, dict) and isinstance(yv, dict):
            print(f"  {k}: perl keys={sorted(pv.keys())} py keys={sorted(yv.keys())}")
        else:
            print(f"  {k}: perl={pv!r} py={yv!r}")


if __name__ == "__main__":
    compare("favorites items", ["", ["favorites", "items", "0", "100"]])
    compare("favorites Ordner", ["", ["favorites", "items", "0", "100", "item_id:0.0"]])
    compare("search", ["", ["search", "0", "5", "term:rock"]])
    compare("serverstatus", ["", ["serverstatus", "0", "5"]])
    compare("artists browse", ["", ["artists", "0", "2"]])
    compare("albums browse", ["", ["albums", "0", "2"]])
    compare("titles browse", ["", ["titles", "0", "2"]])
    compare("songinfo", ["", ["songinfo", "0", "100", "track_id:2"]])
