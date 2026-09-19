"""
Music import module for Pyrion Music Server.

Scans the configured music directory, extracts metadata with the media
scanner (mutagen) and writes tracks/albums/contributors into the SQLAlchemy
database (`tracks`, `albums`, `contributors` + junction tables).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select, text

from lyrion.database.schema import (
    Album,
    Contributor,
    Genre,
    Track,
    albums_contributors,
    tracks_albums,
    tracks_contributors,
    tracks_genres,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ImportConfig:
    """Configuration for music import.

    ``mode`` mirrors the LMS rescan modes (Slim/Control/Commands.pm
    rescanCommand): the default "full" scan reconciles deletions (tracks
    whose files vanished are removed together with orphaned albums/
    contributors); any other mode is an additive refresh that never
    deletes.
    """

    source_path: Path | None = None
    batch_size: int = 100
    overwrite_existing: bool = False
    mode: str = "full"

    @property
    def delete_missing(self) -> bool:
        """Only a full rescan may remove tracks that are gone from disk."""
        return self.mode in ("", "normal", "full")


@dataclass
class ImportStats:
    """Statistics for an import operation."""

    total_files: int = 0
    imported_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    scanned_files: int = 0
    deleted_files: int = 0
    # Album-Zeilen ohne Track, die der Scan entfernt hat
    # (Perl Slim/Schema/Album.pm:383-406 ``Album->rescan``).
    orphan_albums_removed: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


# Supported audio extensions (kept in sync with the media scanner)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    "mp3", "flac", "ogg", "oga", "m4a", "mp4", "aac", "aiff", "aif",
    "wav", "wma", "opus", "spx", "ape", "tak", "m4b", "mpc", "mp+",
    "wv", "dsf", "dff", "ac3", "mp2",
})


def _file_url(path: Path) -> str:
    """LMS-style file:// URL for a local audio file."""
    return path.as_uri()


def _sort_string(value: str) -> str:
    """Basic sort key: lowercase, strip leading articles (der/die/das/the/a/an)."""
    s = value.strip().lower()
    for article in ("the ", "a ", "an ", "der ", "die ", "das ", "le ", "la ", "les "):
        if s.startswith(article):
            s = s[len(article):]
            break
    return s


# Default tag separator — Perl ``splitList`` (Slim/Utils/Prefs.pm:178).
GENRE_SEPARATOR = ";"


def split_tag(tag: str, separator: str = GENRE_SEPARATOR) -> list[str]:
    """Split a multi-value tag the way ``Slim::Music::Info::splitTag`` does.

    Perl ``Slim/Music/Info.pm:1005-1060``: split on the ``splitList``
    separator (default ``;``), trim each part, and return the tag unchanged
    when it does not actually split.  ``R&B`` / ``Rock & Roll`` are exempt
    (Info.pm:1015-1018, Bug 774).
    """
    if not tag:
        return []
    if re.match(r"^\s*R\s*&\s*B\s*$", tag, re.I) or \
            re.match(r"^\s*Rock\s*&\s*Roll\s*$", tag, re.I):
        return [tag.strip()]
    parts = [p.strip() for p in tag.split(separator)]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        return parts
    trimmed = tag.strip()
    return [trimmed] if trimmed else []


def genre_namesearch(name: str) -> str:
    """Perl ``Genre::add`` namesearch = ``Text::ignoreCase($name, 1)``.

    ``Slim/Schema/Genre.pm:104`` calls ``ignoreCase`` → ``ignoreCaseArticles``
    (``Slim/Utils/Text.pm:134-176``): upper-case, punctuation → space, compact,
    strip.  It is the genres table's unique key (Genre.pm:31).
    """
    s = name.upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"  +", " ", s).strip()
    return s or name.upper()


# Perls ``variousArtistsObject``: der Album-Interpret einer Zusammenstellung.
# Perl benutzt ihn als Album-Contributor, sobald ``compilation`` gesetzt ist,
# und ersetzt damit die Artist-Bedingung der Album-Suche
# (Slim/Schema.pm:1286-1288, :1178-1186).
VA_ARTIST_KEY = "various artists"
# Perl vergleicht den Album-Contributor mit dem VA-Objekt (Schema.pm:1289-1295
# „Set compilation to 1 if the primary contributor is VA"); dessen Name ist
# lokalisiert (``string('VARIOUS_ARTISTS')``, strings.txt) — „Various Artists"
# bzw. „Diverse Interpreten". Beide Formen meinen dasselbe Objekt.
VA_ARTIST_NAMES = frozenset({VA_ARTIST_KEY, "diverse interpreten"})


def _album_artist_tag(info: Any) -> str:
    """ALBUMARTIST-Tag der Datei (ID3 TPE2, MP4 ``aART``, Vorbis
    ``ALBUMARTIST``) — leer, wenn das Scan-Ergebnis das Feld nicht führt."""
    for attr in ("album_artist", "albumartist", "album_artists"):
        value = getattr(info, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            return str(value[0]).strip()
    return ""


def album_identity(info: Any, file_path: Path) -> tuple[str, str, bool, int | None]:
    """Album-Identität nach Perl: ``(title_sort, album_artist_key,
    has_album_artist, disc)``.

    Perl bildet den Album-Interpreten als ``ALBUMARTIST || ARTIST ||
    TRACKARTIST`` (`Slim/Schema.pm:3065`) und speichert ihn als
    Album-Contributor (`:1289-1296`); für eine Zusammenstellung tritt das
    Various-Artists-Objekt an die Stelle des Track-Interpreten
    (`:1286-1288`), und in der Album-Suche ersetzt das compilation-Flag die
    Artist-Bedingung (`:1178-1186`, Bug-Kommentar „a contributor match would
    fail"). Der Track-Interpret entscheidet also NICHT, zu welchem Album ein
    Track gehört — er bleibt am Track (`:3100`, contributor_track).
    """
    album_name = getattr(info, "album", None) or "Unknown Album"
    track_artist = getattr(info, "artist", None) or "Unknown Artist"
    album_artist = _album_artist_tag(info)
    compilation = bool(getattr(info, "compilation", False))
    disc = getattr(info, "disc_number", None) or getattr(info, "disc", None) or None
    if album_artist:
        akey = _sort_string(album_artist)
        # Ist der Album-Interpret das VA-Objekt, ist das Album ein Sampler
        # (Perl Schema.pm:1293) — der Schlüssel bleibt kanonisch EINER.
        if akey in VA_ARTIST_NAMES:
            akey = VA_ARTIST_KEY
        return _sort_string(album_name), akey, True, disc
    if compilation:
        return _sort_string(album_name), VA_ARTIST_KEY, False, disc
    return _sort_string(album_name), _sort_string(track_artist), False, disc


def _album_dir_prefix(file_path: Path) -> str:
    """``file://…/ordner/`` — Perls ``$basename`` für die Ordner-Suche.

    Perl sucht ein Album ohne DISC/DISCC/MUSICBRAINZ_ALBUM_ID zusätzlich über
    ``tracks.url LIKE "$basename%"`` im SELBEN Ordner (Slim/Schema.pm:1198-1209:
    ``dirname($trackColumns->{'url'})``).  Wildcards im Ordnernamen werden wie
    bei Perl nicht maskiert (`_`/`%` wirken als LIKE-Platzhalter).
    """
    return _file_url(file_path).rsplit("/", 1)[0] + "/"


# ---------------------------------------------------------------------------
# Online-Cover an der Album-Zeile (``media/art_online.py``)
# ---------------------------------------------------------------------------


def album_artist_key(artist: str) -> str:
    """Album-Artist-Schlüssel für ``albums.albumartist_sort``.

    Dieselbe Bildung wie :func:`album_identity`: ``ALBUMARTIST || ARTIST``
    sortiert, und das Various-Artists-Objekt bleibt EIN kanonischer Schlüssel
    (Perl ``Slim/Schema.pm:1286-1296``, :3065).
    """
    key = _sort_string(artist) if artist else ""
    if key in VA_ARTIST_NAMES:
        return VA_ARTIST_KEY
    return key


def online_cover_path(album: str, artist: str = "",
                      year: int | None = None) -> Path | None:
    """Gecachtes Online-Cover eines Albums — **nur Platte/DB, kein Netz**.

    Perl verknüpft ``albums.artwork`` mit dem Cover des Tracks
    (``Slim/Schema.pm:1376-1381``: ``$albumHash->{artwork} =
    $trackColumns->{coverid}``; ``Slim/Utils/Scanner/Local.pm:1086-1091`` trägt
    es nach).  Der Online-Anbieter ist die letzte Stufe dieser Kette
    (``media/art_online.py``); sein Treffer steht im Plattencache — diese
    Funktion holt ihn beim Import nach, damit die Albumzeile darauf zeigt.

    Ohne laufenden Dienst (``current_service()`` ist ``None``) und ohne Treffer
    ``None``: es wird **kein** Dienst angelegt und nichts gesucht.
    """
    try:
        from lyrion.media.art_online import current_service

        service = current_service()
        if service is None:
            return None
        result = service.read_cached_album(album, artist, year)
    except Exception as exc:  # noqa: BLE001 - ein Cover ist nie ein Importfehler
        logger.debug("Online-Cover für %r nicht lesbar (%s)", album, exc)
        return None
    return result.path if result is not None else None


#: ``albums.artwork`` einer Zeile ohne Bild auf das gefundene Cover setzen.
#: Nur leere Zeilen: ein Ordner-/Tag-Bild der Sammlung schlägt den Treffer
#: (Perls Reihenfolge, ``Slim/Music/Artwork.pm:387-400``).
_ONLINE_ARTWORK_SQL = text("""
    SELECT a.id FROM albums a
    WHERE a.titlesort = :titlesort
      AND (a.artwork IS NULL OR a.artwork = '')
      AND (:year IS NULL OR a.year IS NULL OR a.year = :year)
      AND (
            :artist_key = ''
            OR a.albumartist_sort = :artist_key
            OR EXISTS (
                SELECT 1 FROM albums_contributors ac
                JOIN contributors c ON c.id = ac.contributor
                WHERE ac.album = a.id AND lower(c.name) = :artist_lower)
      )
""")


async def store_online_cover(query: Any, cover: Any) -> int:
    """``albums.artwork`` auf ein neu gefundenes Online-Cover setzen.

    Rückruf für ``ArtOnlineService.on_cover`` (``media/art_online.py:1244``),
    ausgelöst sobald ein **neuer** Treffer im Plattencache liegt.  Damit hängt
    das Cover an der Albumzeile wie in Perl, wo ``albums.artwork`` den Cover-
    Schlüssel des Tracks trägt (``Slim/Schema.pm:1376-1381``) und der Scanner
    ihn nachträgt, sobald ein Track ein Bild bekommt
    (``Slim/Utils/Scanner/Local.pm:1086-1091``) — die Album-Liste einer
    Steuerung nennt dann ``album.artwork`` statt des generischen Symbols.

    Zuordnung: ``albums.titlesort`` (dieselbe Bildung wie beim Import,
    ``_sort_string``) + Jahr (wenn beide Seiten eines haben) + Album-Artist
    (Sortierschlüssel oder Anzeige-Name des Album-Contributors).  Nur leere
    ``artwork``-Spalten werden gefüllt.  Liefert die Anzahl gesetzter Zeilen.
    """
    album = str(getattr(query, "album", "") or "")
    artist = str(getattr(query, "artist", "") or "")
    year = getattr(query, "year", None)
    path = getattr(cover, "path", None)
    if not album or path is None:
        return 0

    from lyrion.database.sqlite_helper import db_session

    params = {
        "titlesort": _sort_string(album),
        "year": int(year) if year else None,
        "artist_key": album_artist_key(artist),
        "artist_lower": artist.strip().lower(),
    }
    try:
        async with db_session() as session:
            rows = (await session.execute(_ONLINE_ARTWORK_SQL, params)).all()
            for row in rows:
                await session.execute(
                    text("UPDATE albums SET artwork = :art, artwork_front = :art "
                         "WHERE id = :id"),
                    {"art": str(path), "id": int(row[0])})
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - Rückruf darf nichts brechen
        logger.warning("Online-Cover %s nicht an der Album-Zeile %r (%s)",
                       path, album, exc)
        return 0
    if rows:
        logger.info("Online-Cover für %r: albums.artwork gesetzt (%d Zeile(n))",
                    album, len(rows))
    else:
        logger.debug("Online-Cover für %r: keine leere Album-Zeile gefunden", album)
    return len(rows)


def wire_online_artwork(service: Any) -> bool:
    """``on_cover``-Rückruf des Dienstes setzen (idempotent).

    Der Scanner und ``scan_worker`` rufen das nach dem Anlegen des Dienstes:
    Perls Scanner trägt ``album.artwork`` ebenfalls nach, sobald ein Track ein
    Bild bekommt (``Slim/Utils/Scanner/Local.pm:1086-1091``).
    """
    if service is None or getattr(service, "on_cover", None) is not None:
        return False
    service.on_cover = store_online_cover
    return True


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class MusicImporter:
    """Scans music files and imports them into the database."""

    def __init__(self, config: Optional[ImportConfig] = None) -> None:
        self.config = config or ImportConfig()
        self.stats = ImportStats()
        self._progress_callbacks: list[Callable[[ImportStats], Any]] = []
        # ``(ordner, album-titel) -> album-id``: der Ordner-Treffer wird pro
        # Lauf gemerkt (Perls Suche fragt die DB pro Track, Schema.pm:1198-1209;
        # wir halten die IDs und sparen den wiederholten LIKE-Query).
        self._album_folder_cache: dict[tuple[str, str], int] = {}
        # ``(album, artist, jahr) -> Pfad|None``: der Blick in den Online-Cache
        # (``online_cover_path``) gilt pro Album, nicht pro Track — ohne diesen
        # Merker fragt jeder Track desselben Albums denselben Cache erneut.
        self._online_cover_cache: dict[tuple[str, str, int], Path | None] = {}
        # No source path configured: fall back to the configured music folder
        # (Perl picks the OS music folder as the default media dir,
        # ``Slim/Utils/Prefs.pm:687-712`` → ``OSDetect::dirsFor('music')``).
        if self.config.source_path is None:
            from lyrion.media.music_dir import resolve_music_dir

            self.config.source_path = resolve_music_dir()

    def add_progress_callback(self, cb: Callable[[ImportStats], Any]) -> None:
        self._progress_callbacks.append(cb)

    def _online_cover(self, album_name: str, info: Any, artist: str,
                      year: int) -> Path | None:
        """Gecachtes Online-Cover dieses Albums (pro Lauf gemerkt, kein Netz).

        Die Anfrage-Schreibweise stammt vom Scan (Albumtag + ``ALBUMARTIST ||
        ARTIST``, ``media/importer.py:album_identity``); ``online_cover_path``
        vergleicht die Album-Spalten.  Ohne Dienst oder ohne Treffer ``None``.
        """
        key = (album_name, _album_artist_tag(info) or artist or "", int(year or 0))
        if key not in self._online_cover_cache:
            self._online_cover_cache[key] = online_cover_path(
                key[0], key[1], key[2] or None)
        return self._online_cover_cache[key]

    # -- main entry ---------------------------------------------------------

    async def import_music(self, progress_callback: Optional[Callable] = None) -> ImportStats:
        """Run a full scan + import of the configured music directory."""
        if progress_callback:
            self.add_progress_callback(progress_callback)

        logger.info("Starting music import from: %s", self.config.source_path)
        self.stats = ImportStats()
        self.stats.start_time = datetime.now()
        # Ordner→Album-Treffer gelten nur für diesen Lauf (Album-Zeilen können
        # zwischen zwei Scans gelöscht werden).
        self._album_folder_cache = {}
        self._online_cover_cache = {}

        from lyrion.media.scan_state import SCAN_STATE

        source = self.config.source_path
        if source is None:
            logger.error(
                "No music folder configured — set 'mediadirs' (Perl "
                "defaultMediaDirs, Slim/Utils/Prefs.pm:687-712)")
            self.stats.end_time = datetime.now()
            SCAN_STATE.finish()
            return self.stats

        # Distinguish "folder is gone" from "the share is not mounted"
        # (gvfs/FUSE) — different operator action, same silent library.  The
        # test is a real listing, not ``Path.is_dir()`` (which swallows the
        # OSError of a FUSE share and rejected a readable 305-entry folder) and
        # not a mount-table probe (a gvfs share is a plain sub-directory of the
        # one gvfsd-fuse mount, Perl only needs ``-d``,
        # Slim/Utils/Prefs.pm:707).
        from lyrion.platform import paths as platform_paths

        if not platform_paths.is_usable_dir(source):
            code, message = platform_paths.explain_missing_path(source)
            logger.error("Music directory unusable (%s): %s", code, message)
            self.stats.end_time = datetime.now()
            SCAN_STATE.finish()
            return self.stats

        self.stats.scanned_files = 0
        # total is unknown until the walk finishes (streaming pipeline);
        # start with a non-zero placeholder so progress bars work.
        self.stats.total_files = 0
        SCAN_STATE.start(total=1)

        from lyrion.database.sqlite_helper import db_session

        # ── Streaming pipeline: walk → extract → import overlap ────────
        # The directory walk (SLOW on gvfs/SMB mounts — minutes) runs as
        # a producer thread feeding a queue; import batches consume from
        # it WHILE the walk continues, so found tracks appear in the
        # library incrementally instead of after the full scan.
        import queue as _queue
        import threading as _threading
        from lyrion.media.scanner import MediaScanner, ScanConfig
        scanner = MediaScanner(config=ScanConfig(base_path=self.config.source_path))

        file_q: _queue.Queue = _queue.Queue(maxsize=2000)
        SENTINEL = object()
        # URLs of every file the walk discovers — used by the deletion
        # reconciliation after a FULL scan (additive scans skip this).
        found_urls: set[str] | None = (set() if self.config.delete_missing
                                       else None)
        # Absolute paths of every directory the walk visits — the scan writes
        # one ``content_type='dir'`` row per entry (Perl records them in
        # ``scanned_files`` with size 0, Slim/Utils/Scanner/Local/AIO.pm:62-67).
        # Collected on every scan mode; only a FULL rescan also prunes the
        # directory rows of directories that disappeared (see below).
        found_dirs: set[str] = set()
        # Set by the consumer when abortscan arrived; the producer polls it
        # so a long gvfs walk stops quickly instead of draining fully.
        abort_ev = _threading.Event()

        def _produce() -> None:
            try:
                for p in self.config.source_path.rglob("*"):
                    if abort_ev.is_set():
                        logger.info("Scan walk aborted — stopping walker")
                        break
                    if p.is_dir():
                        # Perl records every walked directory (filesize 0) while
                        # scanning (Slim/Utils/Scanner/Local/AIO.pm:62-67,
                        # :135-142); we turn each of them into a ``dir`` row
                        # below so a folder id is a real ``tracks.id``.
                        found_dirs.add(str(p))
                        continue
                    if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED_EXTENSIONS:
                        name = p.name.lower()
                        if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
                            continue
                        file_q.put(p)
                        if found_urls is not None:
                            found_urls.add(_file_url(p))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Walk aborted: %s", exc)
            finally:
                file_q.put(SENTINEL)

        walker = _threading.Thread(target=_produce, daemon=True,
                                   name="import-walk")
        walker.start()

        batch: list[Path] = []
        sem = asyncio.Semaphore(8)
        total_seen = 0
        aborted = False

        async def _extract(file_path: Path) -> tuple[Path, Any] | None:
            async with sem:
                try:
                    info = await scanner.scan_single_file(file_path)
                    return (file_path, info) if info is not None else None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Extract failed for %s: %s", file_path, exc)
                    self.stats.error_files += 1
                    return None

        loop = asyncio.get_running_loop()
        walk_done = False
        while not (walk_done and not batch):
            if not batch:
                # Refill the batch from the queue (non-blocking-ish).
                while len(batch) < self.config.batch_size:
                    item = await loop.run_in_executor(None, file_q.get)
                    if item is SENTINEL:
                        walk_done = True
                        break
                    batch.append(item)
                    total_seen += 1
            if not batch:
                break
            self.stats.total_files = max(self.stats.total_files, total_seen)

            # Abort (abortscan): stop importing, discard the pending batch,
            # keep draining the queue until the walker's SENTINEL so the
            # producer thread never blocks on a full queue.
            if SCAN_STATE.abort_requested:
                aborted = True
                abort_ev.set()
                logger.info("Scan aborted — discarding %d queued file(s)",
                            len(batch))
                batch = []
                continue

            # Phase 1: metadata extraction (parallel, no DB). The mutagen
            # C calls release the GIL in worker threads.
            extracted: list[tuple[Path, Any]] = []
            for task in asyncio.as_completed(
                    [asyncio.create_task(_extract(p)) for p in batch]):
                self.stats.scanned_files += 1
                r = await task
                if r:
                    extracted.append(r)
                SCAN_STATE.update(done=self.stats.scanned_files,
                                  total=max(total_seen, 1))
            batch = []

            # Phase 2: short-lived session, inserts only — the library
            # grows incrementally while the walk continues.  One commit per
            # batch, never one giant transaction (Perl: "Commit for every
            # chunk when using scanner.pl", Slim/Utils/Scanner/Local.pm:471;
            # scanner.pl:293-295 leaves AutoCommit off and lets the scanner
            # commit as it goes).
            async with db_session() as session:
                await self._import_batch(session, extracted)
                self.stats.imported_files += len(extracted)
                await session.commit()
            self._emit_progress()
            # Hand the scheduler a turn after every batch.  Perl services
            # pending timers while scanning (``main::idleStreams()`` every
            # third file, Slim/Utils/Scanner.pm:139-141); back-to-back batches
            # without a yield are what starved the request path when the scan
            # ran in the server process.
            await asyncio.sleep(0)
            logger.info("Imported %d/%d+ files", self.stats.scanned_files,
                        total_seen)

        walker.join(timeout=10)
        self.stats.end_time = datetime.now()

        # Verwaiste Album-Zeilen: Perl prüft nach JEDEM Scan, dass zu einem
        # Album noch mindestens ein Track existiert, und löscht es sonst
        # (``Slim::Schema::Album->rescan``, Slim/Schema/Album.pm:383-406,
        # aufgerufen am Scan-Ende Slim/Utils/Scanner/Local.pm:855). Ohne diesen
        # Schritt blieben die Zeilen eines zuvor pro Track-Interpret
        # gespaltenen Samplers als leere Alben in der Bibliothek stehen.
        if not aborted:
            self.stats.orphan_albums_removed = await self._prune_orphan_albums()

        # Directory rows: one ``content_type='dir'`` row per walked directory,
        # so a folder id is the row's ``tracks.id`` (Perl: the scan records the
        # dirs — Slim/Utils/Scanner/Local/AIO.pm:62-67/:135-142 — and the
        # browse creates the rows, Slim/Schema.pm:764-856).  Never on an
        # aborted scan (a half-walked tree would prune live folders).
        if not aborted and found_dirs:
            await self._sync_dir_rows(found_dirs)

        # Deletion reconciliation: a full scan removes tracks whose files
        # are no longer on disk (plus orphaned albums/contributors). Never
        # run on an aborted scan, and never when the walk found nothing
        # (a wrong/empty musicdir must not wipe the library).
        if not aborted and found_urls is not None and found_urls:
            self.stats.deleted_files = await self._reconcile_deletions(
                found_urls)

        self._emit_progress()
        SCAN_STATE.finish()
        logger.info(
            "Import complete: %d imported, %d deleted, %d errors, %d total "
            "(took %.1fs)",
            self.stats.imported_files, self.stats.deleted_files,
            self.stats.error_files, self.stats.total_files,
            (self.stats.end_time - self.stats.start_time).total_seconds(),
        )
        return self.stats

    async def _prune_orphan_albums(self) -> int:
        """Album-Zeilen ohne Track entfernen.

        Perl ``Slim::Schema::Album->rescan`` (Slim/Schema/Album.pm:383-406):
        „make sure at least 1 track from this album still exists in the
        database. If not, delete the album." — am Scan-Ende aufgerufen
        (Slim/Utils/Scanner/Local.pm:855).  Bei uns hängt ein Track über
        ``tracks_albums`` am Album, in Perl direkt über ``tracks.album``.
        """
        from lyrion.database.sqlite_helper import db_session

        try:
            async with db_session() as session:
                result = await session.execute(text(
                    "DELETE FROM albums WHERE id NOT IN "
                    "(SELECT album FROM tracks_albums)"))
                removed = int(result.rowcount or 0)  # type: ignore[attr-defined]
                if removed:
                    # Verwaiste Verknüpfungen der gelöschten Alben bleiben
                    # sonst als Karteileichen stehen (Perls FK-Cascade deckt
                    # sie nicht ab, wenn das Pragma aus ist).
                    await session.execute(text(
                        "DELETE FROM albums_contributors WHERE album NOT IN "
                        "(SELECT id FROM albums)"))
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - ein Scan darf daran nicht scheitern
            logger.warning("Orphan album cleanup failed: %s", exc)
            return 0
        if removed:
            logger.info("Removed %d album(s) without tracks", removed)
        return removed

    async def _sync_dir_rows(self, found_dirs: set[str]) -> tuple[int, int]:
        """Store a ``dir`` row per walked directory (Perl's folder ids).

        The rows are written on the importer's own session, so they land in
        exactly the DB this scan writes to.  ``prune`` is only set for a FULL
        scan whose walk actually found audio (same guard as the track
        reconciliation): rows of directories that no longer exist are removed
        — Perl drops them all with a full wipe (``Slim/Schema.pm:2346-2357``
        ``wipeAllData``) and keeps stale ones through a normal rescan, because
        its deleted/changed sets are filtered with ``content_type != 'dir'``
        (``Slim/Utils/Scanner/Local.pm:186``).
        """
        from lyrion.database.sqlite_helper import db_session
        from lyrion.media import dir_rows

        prune = bool(self.config.delete_missing)
        created = pruned = 0
        # ``rglob('*')`` never yields the root itself; Perl stores it too
        # (Slim/Utils/Scanner/Local/AIO.pm:62-68 "Add the root directory to
        # the database", filesize 0) — and browsing the root creates its
        # ``tracks`` row anyway (Slim/Utils/Misc.pm:1082-1090).
        directories = set(found_dirs)
        if self.config.source_path is not None:
            directories.add(str(self.config.source_path))
        try:
            async with db_session() as session:
                created, pruned = await dir_rows.sync_dir_rows(
                    session, directories,
                    root=self.config.source_path, prune=prune)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - a scan must not fail on this
            logger.warning("Directory rows could not be synced: %s", exc)
            return (0, 0)
        if created or pruned:
            logger.info("Directory rows: %d created, %d orphaned removed",
                        created, pruned)
        return (created, pruned)

    async def _reconcile_deletions(self, found_urls: set[str]) -> int:
        """Remove tracks whose files disappeared since the last full scan.

        Only tracks outside the freshly-walked URL set are deleted; join
        rows cascade (FK ON), orphaned albums/contributors/genres without
        any remaining track are removed too (Perl full-rescan semantics).
        Returns the number of deleted tracks.
        """
        from lyrion.database.sqlite_helper import db_session
        from lyrion.media import dir_rows
        from sqlalchemy import text

        if not found_urls:
            logger.warning("Full scan found no files — refusing deletion "
                           "reconciliation")
            return 0

        async with db_session() as session:
            # ``content_type != 'dir'``: Perl's deleted-set is built with the
            # same filter (Slim/Utils/Scanner/Local.pm:186 ``$ctFilter``), so a
            # directory row is never treated as a vanished audio file — its
            # cleanup is the orphan prune of ``_sync_dir_rows`` (and Perl's
            # full wipe, Slim/Schema.pm:2346-2357).
            rows = await session.execute(text(
                "SELECT url FROM tracks WHERE "
                f"{dir_rows.not_dir_clause()}"))
            db_urls = {row[0] for row in rows}
            missing = sorted(db_urls - found_urls)
            if not missing:
                return 0
            # Chunked deletes stay under SQLite's per-statement variable
            # limit even for 50k+ track libraries.
            for i in range(0, len(missing), 900):
                chunk = missing[i:i + 900]
                binds = ", ".join(f":p{n}" for n in range(len(chunk)))
                await session.execute(
                    text(f"DELETE FROM tracks WHERE url IN ({binds})"),
                    {f"p{n}": url for n, url in enumerate(chunk)})
            # Explicit join-row + orphan cleanup — required both when the
            # FK pragma is off (tests, legacy connections) and to drop
            # albums/contributors/genres that no track references anymore
            # (the FK cascades only reach the join tables).
            await session.execute(text(
                "DELETE FROM tracks_albums WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM tracks_albums WHERE album NOT IN "
                "(SELECT id FROM albums)"))
            await session.execute(text(
                "DELETE FROM tracks_contributors WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM tracks_contributors WHERE contributor NOT IN "
                "(SELECT id FROM contributors)"))
            await session.execute(text(
                "DELETE FROM tracks_genres WHERE track NOT IN "
                "(SELECT id FROM tracks)"))
            await session.execute(text(
                "DELETE FROM albums_contributors WHERE album NOT IN "
                "(SELECT id FROM albums)"))
            await session.execute(text(
                "DELETE FROM albums_contributors WHERE contributor NOT IN "
                "(SELECT id FROM contributors)"))
            await session.execute(text(
                "DELETE FROM albums WHERE id NOT IN "
                "(SELECT DISTINCT album FROM tracks_albums)"))
            await session.execute(text(
                "DELETE FROM contributors WHERE id NOT IN "
                "(SELECT DISTINCT contributor FROM tracks_contributors)"))
            await session.execute(text(
                "DELETE FROM genres WHERE id NOT IN "
                "(SELECT DISTINCT genre FROM tracks_genres)"))
            await session.commit()
            logger.info("Deletion reconciliation removed %d track(s)",
                        len(missing))
            return len(missing)

    # -- file collection ----------------------------------------------------

    async def _collect_files(self) -> list[Path]:
        """Walk the music directory and return all supported audio files."""
        loop = asyncio.get_running_loop()
        results: list[Path] = []

        def _walk() -> list[Path]:
            found: list[Path] = []
            for p in self.config.source_path.rglob("*"):
                if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED_EXTENSIONS:
                    name = p.name.lower()
                    if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
                        continue
                    found.append(p)
            return found

        results = await loop.run_in_executor(None, _walk)
        return sorted(results)

    # -- single track -------------------------------------------------------

    async def _import_batch(self, session, extracted: list[tuple[Path, Any]]) -> None:
        """Import one batch with batch-level lookups (once per batch,
        not per track)."""
        # Existing tracks for the batch URLs.
        urls = [_file_url(p) for p, _ in extracted]
        track_by_url: dict[str, Track] = {t.url: t for t in (
            await session.execute(
                select(Track).where(Track.url.in_(urls)))).scalars()}

        # Track upserts first — one flush per batch (not per track) gives
        # every new track its id for the membership sets below.
        for file_path, info in extracted:
            try:
                await self._import_track(session, file_path, info, track_by_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import failed for %s: %s", file_path, exc)
                self.stats.error_files += 1
        await session.flush()

        # Batch-level membership sets (existing join rows for these ids).
        track_ids = [t.id for t in track_by_url.values()]
        ta_set: set[tuple] = set()
        tc_set: set[tuple] = set()
        ac_set: set[tuple] = set()
        tg_set: set[tuple] = set()
        tg_tracks: set[int] = set()
        if track_ids:
            for r in (await session.execute(
                    select(tracks_albums.c.track, tracks_albums.c.album)
                    .where(tracks_albums.c.track.in_(track_ids)))).all():
                ta_set.add((r[0], r[1]))
            for r in (await session.execute(
                    select(tracks_contributors.c.track,
                           tracks_contributors.c.contributor)
                    .where(tracks_contributors.c.track.in_(track_ids)))).all():
                tc_set.add((r[0], r[1]))
            for r in (await session.execute(
                    select(tracks_genres.c.track, tracks_genres.c.genre)
                    .where(tracks_genres.c.track.in_(track_ids)))).all():
                tg_set.add((r[0], r[1]))
                tg_tracks.add(r[0])
        album_ids = [a.id for a in
                     (await session.execute(select(Album))).scalars()]
        if album_ids:
            for r in (await session.execute(
                    select(albums_contributors.c.album,
                           albums_contributors.c.contributor)
                    .where(albums_contributors.c.album.in_(album_ids)))).all():
                ac_set.add((r[0], r[1]))

        # Existing albums/contributors for the batch keys.
        # Album identity = (title sort, ALBUM-ARTIST sort): Perls
        # ``ALBUMARTIST || ARTIST || TRACKARTIST`` (Slim/Schema.pm:3065), für
        # eine Zusammenstellung das Various-Artists-Objekt (:1286-1288) —
        # NICHT der Track-Interpret (der bleibt am Track), NICHT das Jahr
        # (verschiedene Track-Jahre dürfen ein Album nicht spalten) und NICHT
        # der Titel allein (gleichnamige Alben verschiedener Interpreten
        # bleiben getrennt).
        album_keys = set()
        artist_names: set[str] = set()
        for file_path, info in extracted:
            titlesort, artist_key, _, _ = album_identity(info, file_path)
            album_keys.add((titlesort, artist_key))
            artist = getattr(info, "artist", None) or "Unknown Artist"
            if artist and artist != "Unknown Artist":
                artist_names.add(artist.strip().lower())
        # Eine Abfrage für alle Album-Zeilen dieser Titel; die Sichten
        # (exakter Schlüssel, Altbestand ohne Artist-Sort, Zeilen über
        # Titel+Jahr, Zeile per ID für den Ordner-Treffer) entstehen daraus.
        album_rows: list[Album] = list((await session.execute(
            select(Album).where(
                Album.titlesort.in_([k[0] for k in album_keys])))).scalars())
        album_by_key: dict[tuple, Album] = {
            (a.titlesort, a.albumartist_sort): a for a in album_rows
            if (a.titlesort, a.albumartist_sort) in album_keys}
        album_by_id: dict[int, Album] = {a.id: a for a in album_rows}
        # Altbestand: Alben, die vor der Spalte ``albumartist_sort`` entstanden
        # sind, tragen dort NULL (die Migration füllt nicht nach). Der Import
        # sucht über (titlesort, artist_sort), findet sie also nie und legt eine
        # zweite Zeile an — die alte UNIQUE-Bedingung (titlesort, year) lehnt
        # das ab und reißt die ganze Transaktion mit (im Live-Lauf beobachtet:
        # "UNIQUE constraint failed: albums.titlesort, albums.year").
        # Deshalb zusätzlich über (titlesort, year) indexieren, adoptieren und
        # ``albumartist_sort`` nachtragen.
        album_by_title_year: dict[tuple, Album] = {
            (a.titlesort, a.year): a for a in album_rows
            if not a.albumartist_sort}
        # Zweite Ebene: ALLE Zeilen des Stapels über (titlesort, year). Nötig,
        # weil der alte UNIQUE-Index (titlesort, year) jede zweite Zeile mit
        # gleichem Titel+Jahr verbietet: hat eine Zeile schon einen Artist-Sort
        # (z. B. durch einen Titel mit anderem Künstler-Tag adoptiert), findet
        # der exakte Schlüssel sie nicht mehr und der Insert stirbt. Bis der
        # Altindex entfernt ist, wird die Zeile hier gefunden und
        # wiederverwendet (ein vorhandener Artist-Sort bleibt stehen).
        album_by_title_year_all: dict[tuple, Album] = {
            (a.titlesort, a.year): a for a in album_rows}
        contrib_by_name: dict[str, Contributor] = {
            c.namespell: c for c in (
                await session.execute(
                    select(Contributor).where(
                        Contributor.namespell.in_(list(artist_names))))).scalars()}

        # Existing genres for the batch keys (LIB-10; Perl Genre::add()).
        genre_keys: set[str] = set()
        for _, info in extracted:
            for gname in split_tag(getattr(info, "genre", "") or ""):
                genre_keys.add(genre_namesearch(gname))
        genre_by_namespell: dict[str, Genre] = {
            g.namespell: g for g in (
                await session.execute(
                    select(Genre).where(
                        Genre.namespell.in_(list(genre_keys))))).scalars()} \
            if genre_keys else {}

        # Album/contributor links for the batch tracks.
        for file_path, info in extracted:
            try:
                await self._import_links(session, file_path, info,
                                         track_by_url, album_by_key,
                                         contrib_by_name, ta_set, tc_set, ac_set,
                                         genre_by_namespell, tg_set, tg_tracks,
                                         album_by_title_year,
                                         album_by_title_year_all,
                                         album_by_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import failed for %s: %s", file_path, exc)
                self.stats.error_files += 1

    async def _import_track(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
    ) -> None:
        """Upsert one track (no joins — those run in _import_links)."""
        url = _file_url(file_path)
        title = (info.title or file_path.stem) if hasattr(info, "title") else file_path.stem
        mtime = getattr(info, "last_modified", None)
        modtime = int(mtime.timestamp()) if mtime is not None else 0
        filesize = getattr(info, "filesize", 0) or 0
        mimetype = getattr(info, "mimetype", None)
        bitrate = getattr(info, "bitrate", None) or None
        sample_rate = getattr(info, "sample_rate", None) or None
        channels = getattr(info, "channels", None) or None
        tracknum = getattr(info, "track", None) or None
        disc = getattr(info, "disc_number", None) or getattr(info, "disc", None) or None
        comment = getattr(info, "comment", None)
        compilation = bool(getattr(info, "compilation", False))
        # duration is in milliseconds in the new ScanResult
        duration_ms = getattr(info, "duration", 0) or 0
        duration = duration_ms / 1000.0
        genre = getattr(info, "genre", None) or ""
        year = getattr(info, "year", 0) or 0
        # ReplayGain (Perl Schema.pm:2915-2945, aus den Track-Tags gemungt).
        rg_gain = getattr(info, "replay_gain", None)
        rg_peak = getattr(info, "replay_peak", None)

        track = track_by_url.get(url)
        if track is None:
            track = Track(
                url=url,
                titlesort=_sort_string(title),
                title=title,
                content_type=mimetype or self._guess_mime(file_path),
                modtime=modtime,
                filesize=filesize,
                bitrate=bitrate,
                samplerate=sample_rate,
                channels=channels,
                duration=duration,
                year=year or None,
                genre=genre or None,
                tracknum=tracknum,
                disc=disc,
                comment=comment,
                replay_gain=rg_gain,
                replay_peak=rg_peak,
                lastscanned=datetime.utcnow(),
                audio=1,
                video=0,
                disabled=0,
                compilation=1 if compilation else 0,
            )
            session.add(track)
            track_by_url[url] = track
        else:
            # Update mutable fields
            track.title = title
            track.titlesort = _sort_string(title)
            track.modtime = modtime
            track.filesize = filesize
            track.bitrate = bitrate or track.bitrate
            track.samplerate = sample_rate or track.samplerate
            track.channels = channels or track.channels
            track.duration = duration or track.duration
            track.year = year or track.year
            track.genre = genre or track.genre
            track.tracknum = tracknum or track.tracknum
            # Perl (Schema.pm:2925) schreibt nur, wenn der Tag existiert; ein
            # entfernter Tag löscht den DB-Wert also nicht.
            if rg_gain is not None:
                track.replay_gain = rg_gain
            if rg_peak is not None:
                track.replay_peak = rg_peak
            track.lastscanned = datetime.utcnow()
            await session.flush()

    async def _album_in_folder(self, session, titlesort: str,
                              prefix: str) -> Album | None:
        """Album mit diesem Titel, das im SELBEN Ordner einen Track hat.

        Perl ``Slim/Schema.pm:1206-1209``: ``tracks.url LIKE "$basename%"``
        (JOIN tracks) — die Zeile, deren Track im selben Ordner liegt, ist das
        gesuchte Album. Bevorzugt wird eine schon als Sampler markierte Zeile
        (``compilation`` / Various Artists), sonst die älteste: damit laufen
        die Reste eines früheren, pro Track-Interpret gespaltenen Scans wieder
        zusammen. Perl nimmt an dieser Stelle ohne ORDER BY die erste Zeile —
        eine Reihenfolge, die vom Scanverlauf abhängt.
        """
        stmt = (
            select(Album)
            .join(tracks_albums, tracks_albums.c.album == Album.id)
            .join(Track, Track.id == tracks_albums.c.track)
            .where(Album.titlesort == titlesort, Track.url.like(prefix + "%"))
            .order_by(Album.compilation.desc(), Album.id.asc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    def _adopt_folder_album(album: Album, artist_key: str,
                            album_by_key: dict[tuple, Album],
                            titlesort: str) -> Album:
        """Übernimmt eine Ordner-Zeile als Album dieses Tracks.

        Perl ``Slim/Schema.pm:1399-1415`` (``mergeSingleVAAlbum``,
        :2207-2295): unterscheiden sich die Track-Interpreten eines Albums,
        wird es zur Compilation — ``compilation = 1`` und Album-Contributor =
        Various Artists. Bei uns ist der Various-Artists-Schlüssel Teil der
        Album-Identität (``albumartist_sort``), deshalb wird er dort gesetzt.
        """
        if not album.albumartist_sort:
            album.albumartist_sort = artist_key
        elif (album.albumartist_sort != artist_key
                and album.albumartist_sort != VA_ARTIST_KEY):
            album.compilation = 1
            album.albumartist_sort = VA_ARTIST_KEY
        album_by_key[(titlesort, album.albumartist_sort)] = album
        return album

    async def _import_links(
        self, session, file_path: Path, info: Any,
        track_by_url: dict[str, Track],
        album_by_key: dict[tuple, Album],
        contrib_by_name: dict[str, Contributor],
        ta_set: set, tc_set: set, ac_set: set,
        genre_by_namespell: dict[str, Genre] | None = None,
        tg_set: set | None = None,
        tg_tracks: set | None = None,
        album_by_title_year: dict[tuple, Album] | None = None,
        album_by_title_year_all: dict[tuple, Album] | None = None,
        album_by_id: dict[int, Album] | None = None,
    ) -> None:
        """Album + contributor + genre links for a track (Core inserts only)."""
        url = _file_url(file_path)
        track = track_by_url[url]
        if album_by_id is None:
            album_by_id = {}
        artist = (info.artist or "Unknown Artist") if hasattr(info, "artist") else "Unknown Artist"
        album_name = info.album or "Unknown Album" if hasattr(info, "album") else "Unknown Album"
        year = getattr(info, "year", 0) or 0
        titlesort, artist_key, has_album_artist, disc = album_identity(info, file_path)
        # Perl Schema.pm:1289-1295: ist der primäre Contributor das VA-Objekt,
        # ist das Album eine Compilation.
        compilation = bool(getattr(info, "compilation", False)) or artist_key == VA_ARTIST_KEY
        key = (titlesort, artist_key)
        # Album-ReplayGain (Perl Schema.pm:1299-1322; Kommentar dort: "we do
        # want to update album gain tags if they are changed").
        album_rg = getattr(info, "album_replay_gain", None)
        album_peak = getattr(info, "album_replay_peak", None)

        album = None
        # Perls Album-Suche matcht zuerst Titel + ORDNER (``tracks.url LIKE
        # "$basename%"``, Schema.pm:1198-1209) — die Bedingung entfällt nur,
        # wenn DISC/DISCC/MUSICBRAINZ_ALBUM_ID bekannt sind. Genau das hält
        # Sampler zusammen, deren Dateien NUR TALB+TPE1 tragen (kein
        # ALBUMARTIST, kein COMPILATION): der Track-Interpret darf das Album
        # nicht spalten. Trägt die Datei ein Album-Artist- oder
        # Compilation-Tag, entscheidet dieses (=der Schlüssel), wie oben.
        folder_key = (_album_dir_prefix(file_path), titlesort)
        folder_eligible = not has_album_artist and not compilation and disc is None
        if folder_eligible:
            album = album_by_id.get(self._album_folder_cache.get(folder_key, -1))
            if album is None:
                album = await self._album_in_folder(session, titlesort, folder_key[0])
            if album is not None:
                album = self._adopt_folder_album(
                    album, artist_key, album_by_key, titlesort)
                album_by_id[album.id] = album
                self._album_folder_cache[folder_key] = album.id
        if album is None:
            album = album_by_key.get(key)
        if album is None and album_by_title_year:
            # Vor der Spalte entstandene Zeile adoptieren (Begründung beim
            # Aufbau der Map) und den fehlenden Artist-Sort nachtragen, damit
            # der nächste Lauf den exakten Schlüssel findet.
            album = album_by_title_year.pop((_sort_string(album_name),
                                             year or None), None)
            if album is not None:
                album.albumartist_sort = artist_key
                album_by_key[key] = album
        if album is None and album_by_title_year_all:
            # Zweite Ebene (siehe Map): gleiche Titel+Jahr-Zeile wiederverwenden,
            # statt in den alten UNIQUE-Index zu laufen. Ein bereits gesetzter
            # Artist-Sort bleibt unangetastet.
            album = album_by_title_year_all.get((_sort_string(album_name),
                                                 year or None))
            if album is not None and not album.albumartist_sort:
                album.albumartist_sort = artist_key
            if album is not None:
                album_by_key[key] = album
        if album is not None and compilation and not album.compilation:
            # Perl Schema.pm:1293 ("Set compilation to 1 if the primary
            # contributor is VA") bzw. :1406 (mergeSingleVAAlbum).
            album.compilation = 1
        if album is None:
            # Cover artwork: tags first (materialisiertes Tag-Bild), dann
            # cover.jpg/png/… im Ordner, zuletzt der Plattencache der
            # Online-Suche — Perl ``Slim/Music/Artwork.pm:387-400`` und die
            # Verknüpfung ``albums.artwork = Cover des Tracks``
            # (``Slim/Schema.pm:1376-1381``).
            artwork = getattr(info, "artwork_path", None) or self._online_cover(
                album_name, info, artist, year)
            album = Album(
                titlesort=_sort_string(album_name),
                title=album_name,
                albumartist_sort=artist_key,
                year=year or None,
                compilation=1 if compilation else 0,
                artwork=str(artwork) if artwork else None,
                artwork_front=str(artwork) if artwork else None,
                replay_gain=album_rg,
                replay_peak=album_peak,
            )
            session.add(album)
            await session.flush()
            album_by_key[key] = album
        else:
            if not album.artwork:
                # Album existed without artwork (e.g. imported before this
                # column was filled) — backfill from this track's folder, dann
                # aus dem Plattencache der Online-Suche.
                artwork = getattr(info, "artwork_path", None) or self._online_cover(
                    album_name, info, artist, year)
                if artwork:
                    album.artwork = str(artwork)
                    album.artwork_front = str(artwork)
            # Album-Gain aktualisieren, wenn ein Tag vorhanden ist
            # (Perl Schema.pm:1306-1310).
            if album_rg is not None:
                album.replay_gain = album_rg
            if album_peak is not None:
                album.replay_peak = album_peak
        # Retag cleanup: drop a stale album link (track re-tagged to a
        # different album) — otherwise the old album keeps listing the track.
        await session.execute(
            tracks_albums.delete().where(
                (tracks_albums.c.track == track.id)
                & (tracks_albums.c.album != album.id)))
        if (track.id, album.id) not in ta_set:
            await session.execute(
                tracks_albums.insert().values(track=track.id, album=album.id))
            ta_set.add((track.id, album.id))

        if artist and artist != "Unknown Artist":
            contrib = contrib_by_name.get(artist.strip().lower())
            if contrib is None:
                contrib = Contributor(
                    namespell=artist.strip().lower(),
                    name=artist.strip(),
                    sortname=_sort_string(artist),
                )
                session.add(contrib)
                await session.flush()
                contrib_by_name[artist.strip().lower()] = contrib
            # Retag cleanup: drop stale artist links for this track.
            await session.execute(
                tracks_contributors.delete().where(
                    (tracks_contributors.c.track == track.id)
                    & (tracks_contributors.c.contributor != contrib.id)
                    & (tracks_contributors.c.role == 1)))
            if (track.id, contrib.id) not in tc_set:
                await session.execute(
                    tracks_contributors.insert().values(
                        track=track.id, contributor=contrib.id, role=1))
                tc_set.add((track.id, contrib.id))
            if (album.id, contrib.id) not in ac_set:
                await session.execute(
                    albums_contributors.insert().values(
                        album=album.id, contributor=contrib.id, role=1))
                ac_set.add((album.id, contrib.id))

        # Genre links (LIB-10).  Perl ``Slim::Schema::Genre::add``
        # (Slim/Schema/Genre.pm:91-132): split the tag, upsert one ``genres``
        # row per value (unique key = namesearch) and ``REPLACE INTO
        # genre_track``.  A re-tag drops stale links like the album/artist
        # cleanups above.
        genre_names = split_tag(getattr(info, "genre", "") or "")
        if genre_by_namespell is None:
            genre_by_namespell = {}
        if tg_set is None:
            tg_set = set()
        if genre_names or (tg_tracks and track.id in tg_tracks):
            new_ids: list[int] = []
            for name in genre_names:
                key = genre_namesearch(name)
                genre = genre_by_namespell.get(key)
                if genre is None:
                    genre = Genre(namespell=key, name=name,
                                  sortkey=_sort_string(name))
                    session.add(genre)
                    await session.flush()
                    genre_by_namespell[key] = genre
                new_ids.append(genre.id)
                if (track.id, genre.id) not in tg_set:
                    await session.execute(
                        tracks_genres.insert().values(
                            track=track.id, genre=genre.id))
                    tg_set.add((track.id, genre.id))
            # Retag cleanup: drop genre links this track no longer carries.
            await session.execute(
                tracks_genres.delete().where(
                    (tracks_genres.c.track == track.id)
                    & (tracks_genres.c.genre.notin_(new_ids))))
            if tg_tracks is not None and new_ids:
                tg_tracks.add(track.id)

    @staticmethod
    def _guess_mime(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        mime_map = {
            "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
            "oga": "audio/ogg", "opus": "audio/ogg", "m4a": "audio/mp4",
            "aac": "audio/aac", "aiff": "audio/aiff", "aif": "audio/aiff",
            "wav": "audio/wav", "wma": "audio/x-ms-wma", "ape": "audio/x-ape",
        }
        return mime_map.get(ext, "audio/mpeg")

    # -- progress -----------------------------------------------------------

    def _emit_progress(self) -> None:
        for cb in self._progress_callbacks:
            try:
                cb(self.stats)
            except Exception:  # noqa: BLE001
                logger.exception("Progress callback failed")
