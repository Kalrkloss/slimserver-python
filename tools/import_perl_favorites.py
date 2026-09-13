#!/usr/bin/env python3
"""Import the Perl LMS favorites tree into this port.

Why: the apps' favourites tap depends on the tree *shape*.  Perl keeps only
folders at the top level (Chill, Nachrichten, Lokal, Dub, Trance, Rock) with
the radio streams inside; this port had streams at the root (own test data),
so the controllers could not drill in.

Source of truth: a read-only harvest of the live Perl server's
``favorites items`` calls, stored as JSON by the parent session
(``.hermes/perl-favorites.json``).  We turn that into the OPML the port's own
importer already understands (``lyrion.music.favorites`` imports
``favorites.opml`` on startup, ``src/lyrion/music/favorites.py:300-355``),
back the current ``favorites`` table up, clear it, and let the port reimport.

Usage:
    python tools/import_perl_favorites.py            # dry-run, prints the plan
    python tools/import_perl_favorites.py --write    # backup + clear + write OPML
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
TREE = REPO / ".hermes" / "perl-favorites.json"
DB = Path.home() / ".lyrion" / "Lyrion" / "Prefs" / "lyrion.db"
PREFS = DB.parent
BACKUP_DIR = REPO / ".hermes"


def _outline(node: dict, depth: int = 0) -> list[str]:
    pad = "  " * (depth + 2)
    text = escape(str(node.get("text") or "???"))
    attrs = [f'text="{text}"', f'title="{text}"']
    if node.get("url"):
        attrs.append(f'URL="{escape(str(node["url"]), {chr(34): "&quot;"})}"')
        attrs.append(f'type="{escape(str(node.get("ptype") or "audio"))}"')
    if node.get("icon"):
        attrs.append(f'icon="{escape(str(node["icon"]), {chr(34): "&quot;"})}"')
    children = node.get("children") or []
    if children:
        lines = [f"{pad}<outline {' '.join(attrs)}>"]
        for child in children:
            lines += _outline(child, depth + 1)
        lines.append(f"{pad}</outline>")
        return lines
    return [f"{pad}<outline {' '.join(attrs)}/>"]


def build_opml(tree: list[dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<opml version=\"1.0\">",
             "  <head>", "    <title>Favorites</title>", "  </head>", "  <body>"]
    for node in tree:
        lines += _outline(node)
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply (default: dry-run)")
    args = ap.parse_args()

    tree = json.loads(TREE.read_text(encoding="utf-8"))
    opml = build_opml(tree)

    def count(nodes):
        for n in nodes:
            yield n
            yield from count(n.get("children") or [])

    allnodes = list(count(tree))
    streams = [n for n in allnodes if n.get("url")]
    folders = [n for n in allnodes if not n.get("url")]
    print(f"Quelle: {TREE} — {len(tree)} Wurzeln, {len(folders)} Ordner, "
          f"{len(streams)} Streams")
    for root in tree:
        kids = root.get("children") or []
        print(f"  {str(root.get('text'))[:26]:<26} Kinder={len(kids)}")
    print(f"\nZiel:  {PREFS / 'favorites.opml'}")
    print(f"DB:    {DB}")

    db = sqlite3.connect(DB)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(favorites)")]
        rows = db.execute("SELECT * FROM favorites").fetchall()
        print(f"\nfavorites-Tabelle: {len(rows)} Zeilen, Spalten={cols}")
        if not args.write:
            print("\nDry-Run — nichts geschrieben. Mit --write anwenden.")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dump = BACKUP_DIR / f"favorites-backup-{stamp}.json"
        dump.write_text(json.dumps({"columns": cols, "rows": rows},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.copy2(DB, BACKUP_DIR / f"lyrion.db.bak-favorites-{stamp}")
        print(f"Backup: {dump}\n        {BACKUP_DIR / f'lyrion.db.bak-favorites-{stamp}'}")

        db.execute("DELETE FROM favorites")
        db.commit()
        print(f"favorites geleert (vorher {len(rows)} Zeilen)")

        (PREFS / "favorites.opml").write_text(opml, encoding="utf-8")
        print(f"OPML geschrieben: {len(opml)} Bytes")
    finally:
        db.close()
    print("\nNächster Schritt: Server neu starten — der Importer (favorites.py:300-355) "
          "übernimmt den OPML in die DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
