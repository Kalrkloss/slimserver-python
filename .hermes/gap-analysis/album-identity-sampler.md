# Sampler-Alben: Album-Identität nach Perl (Album-Interpret statt Track-Interpret)

Symptom (User): „Lieder von Samplern werden oft nicht dem selben Album zugeordnet,
sondern eigene bekommen" — verschiedene Track-Interpreten einer Zusammenstellung
landeten als mehrere Alben statt als EIN Sampler-Album.

## 1. Perl-Beleg (read-only, /tmp/lms-ref + Live-LMS 192.168.1.90)

Album-Identität in Perl ist **Titel + Album-Interpret**, nicht der Track-Interpret:

- `Slim/Schema.pm:3065` — Album-Contributor = `ALBUMARTIST || ARTIST || TRACKARTIST`.
- `Slim/Schema.pm:1286-1296` — für eine Compilation tritt das Various-Artists-Objekt
  an die Stelle des Interpreten („Set compilation to 1 if the primary contributor is VA").
- `Slim/Schema.pm:1178-1186` — „If we have a compilation bit set - use that instead of
  trying to match on the artist … so a contributor match would fail": das
  compilation-Flag **ersetzt** die Artist-Bedingung der Album-Suche.
- `Slim/Schema.pm:1102` + `:1198-1209` — Album-Suche ohne DISC/DISCC/MUSICBRAINZ_ALBUM_ID
  über `albums.title = ?` **plus** `tracks.url LIKE "$basename%"` (derselbe Ordner);
  der Track-Contributor wird nur bei DISC/DISCC als Tiebreaker geprüft (`:1126-1166`).
- `Slim/Schema.pm:1399-1415` + `:2207-2295` (`mergeSingleVAAlbum`) — unterscheiden sich
  die Artist-Contributors der Tracks eines Albums, wird es zur Compilation mit
  Album-Contributor Various Artists.
- `Slim/Schema/Album.pm:383-406` (`Album->rescan`, Aufruf `Slim/Utils/Scanner/Local.pm:855`)
  — am Scan-Ende: Album ohne Track wird gelöscht.

Live (CLI, read-only, gleiche Bibliothek):
```
albums 0 0                 → count:7189
albums 0 0 compilation:1   → count:570
albums 0 1 album_id:14803 tags:ajlywCKl
  → album:"Metal Duets Vol. 11"  compilation:1  artist:"Diverse Interpreten"  count:1
tracks 0 100 album_id:14803 tags:ajltR
  → 15 Tracks, 15 VERSCHIEDENE Track-Interpreten (Raintime, Fairyland, Damageplan, …)
albums 0 20 search:German Top 100 Single Charts tags:ajlywCK
  → „German Top 100 Single Charts" mehrfach, jede Zeile compilation:1 /
    artist:"Diverse Interpreten" (Perl trennt nach Ordner/Jahr, nie nach Track-Interpret)
```

## 2. Ursache bei uns

- `src/lyrion/media/importer.py:562` (alt) und `:725` (alt): Album-Schlüssel wurde aus
  `info.artist` — dem **Track-Interpreten** — gebildet: `key = (sort(album), sort(artist))`.
  Jeder Track-Interpret bekam damit seine eigene Album-Zeile.
- `src/lyrion/media/scanner.py:437-440`: ALBUMARTIST/TPE2 und COMPILATION/TCMP werden
  nicht gelesen (`ScanResult` hat kein `album_artist`/`compilation`); die Dateien der
  betroffenen Sampler tragen ohnehin nur TALB + TPE1 (roh geprüft mit mutagen:
  `TALB, TPE1, TRCK, TIT2, TDRC` — kein TPE2, kein TCMP).
- Effekt in der Live-DB (roh): Titel `german top 100 single charts` lag in **63
  Album-Zeilen allein im Ordner Musik/GSC** (100 Tracks), jede mit genau 1 Contributor
  und `compilation=0`.

## 3. Fix (`src/lyrion/media/importer.py`)

- `album_identity()`: Schlüssel = `ALBUMARTIST` → sonst Various-Artists-Marker
  (`compilation`-Tag bzw. lokalisierter VA-Name) → sonst Track-Interpret
  (Perl `:3065`, `:1286-1288`, `:1293`).
- `_album_in_folder()` + `_adopt_folder_album()`: ohne Album-Artist-/Compilation-Tag
  und ohne DISC wird — wie Perl `:1206` — das Album über **Titel + gleicher Ordner**
  gesucht; weicht dessen Artist-Schlüssel ab, wird es zur Compilation
  (`compilation=1`, Schlüssel `various artists`): Perl `:1399-1415` / `:2271-2280`.
  Treffer werden pro Lauf gecacht (`self._album_folder_cache`, ID statt Objekt).
- `_prune_orphan_albums()` am Scan-Ende: Album-Zeilen ohne Track werden entfernt
  (Perl `Slim/Schema/Album.pm:383-406`); ohne das blieben die Split-Zeilen als leere
  Alben sichtbar.
- Kein Schema-Bruch: `albums.albumartist_sort` + `UNIQUE(titlesort, albumartist_sort)`
  existieren bereits (Live-DB geprüft) → **keine Migration nötig**.

## 4. Roh vorher/nachher (echte Dateien, DB-Kopie, `tools/verify_album_identity.py`)

DB-Kopie der Live-DB (`sqlite3 .backup`), echter Pfad (MediaScanner + MusicImporter,
batch_size=50 → Batch-Grenze mitten im Ordner), mode=incremental.

| Fall | vorher | nachher |
|---|---|---|
| Sampler `Musik/GSC` (100 Tracks, Titel „German Top 100 Single Charts") | **63** Album-Zeilen, Schlüssel = 63 Track-Interpreten, `compilation=0`, je 1 Album-Contributor | **1** Album-Zeile id=420, `albumartist_sort='various artists'`, `compilation=1`, 100 Tracks, 81 Album-Contributors (= Track-Interpreten ⇒ Anzeige „Diverse Interpreten" wie Perl), leere Album-Zeilen 31→0 (Scan-Ende) |
| normales Album `Musik/Ruthless`, Titel „Fallen" (10 Tracks) | 1 Album, Schlüssel `ruthless`, `compilation=0` | unverändert: 1 Album, `ruthless`, `compilation=0` |
| gleicher Titel „4", verschiedene Interpreten/Ordner (Nickelback vs. Soul Secret) | 2 Alben (`nickelback` 10 Tracks, `soul secret` 11 Tracks) | unverändert 2 Alben |

Perl im gleichen Zustand: Sampler = 1 Album, `compilation=1`, Album-Contributor
„Diverse Interpreten" (Live: 570 von 7189 Alben sind `compilation=1`).

## 5. Tests

`tests/test_album_identity_sampler.py` (13 Tests): Schlüsselbildung (Album-Artist,
Compilation-Tag, lokalisierter VA-Name), Sampler in einem Ordner → 1 Album,
Sampler über zwei Batches, Alt-Split-Reste laufen zusammen, normales Album
unverändert, gleichnamige Alben verschiedener Interpreten getrennt (auch über
ALBUMARTIST), DISC-Fall ohne Ordner-Regel, Ende-zu-Ende-Scan mit Orphan-Prune.

```
timeout 400 env LMS_TEST_SPAWN=1 .venv/bin/python3 -m pytest -q tests/ \
  --ignore=tests/test_contract.py -k 'album or import or scan or schema or sampler or database'
→ 113 passed, 1789 deselected  (EXIT=0)
```

## 6. Offener Punkt (UNKLAR)

Für getaggte Sampler (Datei trägt `TPE2`/`TCMP`) entscheidet bei uns erst der
Schlüssel, danach greift die Ordner-Regel nicht mehr — das entspricht Perl nur,
solange das Tag den Sampler auch markiert. Der Scanner liefert die Felder
`album_artist`/`compilation` noch nicht (`src/lyrion/media/scanner.py`, außerhalb
dieser Dateigrenze); der Importer liest sie per `getattr` und nutzt sie, sobald sie
da sind. Zweiter Punkt: `has_album_artist` blockiert die Ordner-Regel — Perl prüft
seine VA-Erkennung an der ARTIST-Rolle, die bei Tracks MIT ALBUMARTIST nach
TRACKARTIST verschoben wird (`:3125-3135`), d. h. die Fälle stimmen überein, sind
aber nicht byte-gleich bewiesen.
