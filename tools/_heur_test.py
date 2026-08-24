"""Unit tests for the metadata heuristics (run: python3 tools/_heur_test.py)."""
import sys
sys.path.insert(0, "src")

from lyrion.media.heuristics import (
    apply_heuristics, guess_from_filename, guess_from_folders,
)

FAIL = 0

def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")

# ── filename patterns ────────────────────────────────────────────────
g = guess_from_filename("01 - Rage - End of All Days")
check("num-artist-title track", g["track"], 1)
check("num-artist-title artist", g["artist"], "Rage")
check("num-artist-title title", g["title"], "End of All Days")

g = guess_from_filename("03. Higher Ground")
check("num.title track", g["track"], 3)
check("num.title title", g["title"], "Higher Ground")

g = guess_from_filename("Sunset Orion")
check("plain title", g["title"], "Sunset Orion")

g = guess_from_filename("State Azure - Sunset Orion")
check("artist-title artist", g["artist"], "State Azure")
check("artist-title title", g["title"], "Sunset Orion")

g = guess_from_filename("07_State Azure_Sunset")
check("underscore track", g["track"], 7)

# ── folder patterns ──────────────────────────────────────────────────
f = guess_from_folders(["Metal", "Rage - End of All Days (1996)"])
check("folder artist", f.get("artist"), "Rage")
check("folder album", f.get("album"), "End of All Days")
check("folder year", f.get("year"), 1996)
check("folder genre", f.get("genre"), "Metal")

f = guess_from_folders(["Pop", "AC_DC - Back In Black"])
check("folder2 artist", f.get("artist"), "AC DC")  # underscore → space in spaceless name
check("folder2 album", f.get("album"), "Back In Black")

f = guess_from_folders(["Musik", "Aerosmith", "Pump (1989)"])
check("folder3 artist", f.get("artist"), "Aerosmith")
check("folder3 album", f.get("album"), "Pump")
check("folder3 year", f.get("year"), 1989)

f = guess_from_folders(["VA - Miroque Vol. XV-2008"])
check("va folder album", f.get("album"), "Miroque Vol. XV")
check("va folder year", f.get("year"), 2008)

f = guess_from_folders(["Rock", "Queen - A Night at the Opera (1975) CD1"])
# CD1 suffix folder → treated as disc subfolder, album from parent handled
print("disc folder result:", f)

# ── full merge: sparse tags + folder + filename ──────────────────────
from pathlib import Path
p = Path("/media/Musik/Metal/Rage - End of All Days (1996)/04 - Deep in the Morning.mp3")
g = apply_heuristics(
    file_path=p, library_root="/media/Musik",
    title="", artist="", album="", genre="", year=0, track=0,
)
check("merge title", g.title, "Deep in the Morning")
check("merge artist", g.artist, "Rage")
check("merge album", g.album, "End of All Days")
check("merge year", g.year, 1996)
check("merge track", g.track, 4)
check("merge genre", g.genre, "Metal")
check("merge src artist", g.sources.get("artist"), "folder")
check("merge src title", g.sources.get("title"), "filename")

# tags win over heuristics
g2 = apply_heuristics(
    file_path=p, library_root="/media/Musik",
    title="Tagged Title", artist="Tag Artist", album="Tag Album",
    genre="TagGenre", year=2000, track=9,
)
check("tag wins title", g2.title, "Tagged Title")
check("tag wins artist", g2.artist, "Tag Artist")
check("tag wins track", g2.track, 9)

# no tags at all, no useful folders → fallbacks
g3 = apply_heuristics(
    file_path=Path("/media/Musik/misc/track01.mp3"),
    library_root="/media/Musik",
    title="", artist="", album="", genre="", year=0, track=0,
)
check("fallback title", g3.title, "track01")
check("fallback album", g3.album, "misc")

print()
print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
