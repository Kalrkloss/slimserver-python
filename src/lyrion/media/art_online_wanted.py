"""Persistente „wanted"-Liste fehlender Albumcover + selbstlaufender Dienst.

Perl-Abgleich (kurz, siehe auch ``media/art_online.py`` Modul-Docstring) —
**es gibt kein Vorbild für diesen Dienst**:

* Das von ``HTML/EN/settings/wizard.json`` nur *empfohlene* Plugin
  ``MusicArtistInfo`` liegt nicht im ausgelieferten Perl-Baum (``find`` über
  den Referenzbaum: kein Treffer; nur der Wizard-Eintrag ``plugins[1].id``
  nennt den Namen).  Es holt Biografien/Reviews **beim Anzeigen** eines Titels,
  nicht in einem Hintergrund-Durchlauf.
* ``Slim/Plugin/RadioArtwork/Plugin.pm`` ist das einzige eingebaute
  Cover-Nachladen: ``registerArtworkHandler`` (``:61``), Treffer-Cache 30 Tage
  (``cacheFoundArtwork``, ``:199-201`` mit ``86400 * 30``) und eine
  **flüchtige** Warteschlange je Such-URL (``requestIsQueued``, ``:166-180``).
  Sie läuft aber *ereignisgetrieben im Wiedergabepfad* (Titelwechsel) und kennt
  keinen Zustand über einen Neustart hinaus.
* Die Scan-Warteschlange ``Slim/Music/Import.pm:806-848`` ist ebenfalls
  flüchtig (``tie %scanQueue, "Tie::IxHash"``) und verweigert im Scanner-Dienst
  die Arbeit (``:812`` ``don't initialize queue - we're the scanner``).

Übernommen wird deshalb die **Struktur** (Queue → Cache mit Ablauf → Fortschritt
veröffentlichen, für den Fortschritt: ``Slim/Utils/Progress.pm:298-341``), die
**Persistenz** dagegen ist eine Zutat dieses Ports: ohne sie gäbe es keine
Wiederaufnahme nach einem Neustart (ausdrückliche Anforderung).  Die Liste liegt
in der **eigenen** Datei des Online-Caches (``<cache_dir>/art_online.db``,
Tabellen ``artwork_online_wanted`` + ``artwork_online_meta``) — die
Bibliotheks-DB bleibt unangetastet, es wird nur in sie **gelesen**
(``albums`` ohne ``artwork``).

Was der Dienst tut:

* ``backfill()`` trägt jedes Album ohne Cover ein (Titel, Album-Artist, Jahr,
  Release-Group-MBID) — beim Serverstart und am Ende jedes Scans
  (``media/scanner.py``, ``media/scan_worker.py``), idempotent.
* ``run_pass()`` arbeitet die fälligen Einträge ab: Cache → Anbieter-Kette
  (``ArtOnlineService.find_cover``), Treffer → Cache **und** ``albums.artwork``
  (``media/importer.py`` ``store_online_cover``), Fehlschlag → ``miss`` mit
  ``next_try`` (``artworkOnlineRetryDays``; vorübergehende Fehler wie das
  MusicBrainz-Rate-Limit nur ``TRANSIENT_RETRY_SECONDS``).  Kein Endlos-Retry.
* Ändert sich die suchrelevante Konfiguration (Anbieter, Sprache, Land,
  API-Key), ist die gespeicherte Kennung eine andere: alle ``miss``-Einträge
  werden erneut fällig und **erzwungen** gesucht (``force=True`` umgeht den
  Negativ-Cache).  Der Anlass ist ausdrücklich „neuer API-Key hinzugekommen“.
* Der Dienst läuft als Task des Serverprozesses, gedrosselt (die
  MusicBrainz-Drossel sitzt im Fetcher, ``art_online.MB_MIN_INTERVAL``), pausiert
  während eines laufenden Bibliotheks-Scans und rührt den Wiedergabepfad nicht
  an (der liest weiter nur Platte/DB, ``ArtOnlineService.read_cached_album``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from lyrion.media.art_online import (
    TRANSIENT_REASON_PREFIX,
    TRANSIENT_RETRY_SECONDS,
    AlbumQuery,
    ArtOnlineCache,
    ArtOnlineService,
    ArtOnlineSettings,
    CacheEntry,
    CoverResult,
    configured_service,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prefs (eigener Satz — ``art_online.PREF_DEFAULTS`` bleibt unverändert)
# ---------------------------------------------------------------------------

#: Darf der Dienst beim Serverstart selbst anlaufen?  ``1`` = ja (Vorgabe).
PREF_WANTED_AUTO = "artworkOnlineWantedAuto"
#: Obergrenze je Durchlauf (``0`` = alle fälligen Einträge).
PREF_WANTED_PER_PASS = "artworkOnlineWantedPerPass"

WANTED_PREF_DEFAULTS: dict[str, str] = {
    PREF_WANTED_AUTO: "1",
    PREF_WANTED_PER_PASS: "0",
}

#: Ruhe zwischen zwei Durchläufen des Dienstes (Sekunden).
IDLE_SLEEP = 300.0
#: Anlaufverzögerung des ersten Durchlaufs nach dem Start (Server ist dann
#: wirklich oben; Perl publiziert seinen Fortschritt ebenfalls erst nach dem
#: Start, ``Slim/Utils/Progress.pm:298-341``).
FIRST_PASS_DELAY = 20.0
#: Kurze Pause zwischen zwei Einträgen — MusicBrainz wird im Fetcher gedrosselt
#: (1/s), die Cover-Art-Archive-Abrufe daneben sollen nicht in einer Salve laufen.
ENTRY_PAUSE = 0.25

STATUS_OPEN = "open"
STATUS_HIT = "hit"
STATUS_MISS = "miss"

#: Die drei Ergebnisse eines Eintrags im Durchlauf.
OUTCOME_HIT = "hit"
OUTCOME_MISS = "miss"
OUTCOME_CACHED_MISS = "waiting"      # Negativ-Cache noch frisch, kein Netz
OUTCOME_DISABLED = "disabled"
OUTCOME_SKIPPED = "skipped"          # abgebrochen (Stopp/Scan)


# ---------------------------------------------------------------------------
# Zeile + Zugriff auf die wanted-Tabelle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WantedEntry:
    """Ein Album ohne Cover samt Suchdaten, Status und Zeitstempeln."""

    album_key: str
    album: str
    artist: str = ""
    year: int | None = None
    mbid: str | None = None
    album_id: int | None = None
    status: str = STATUS_OPEN
    attempts: int = 0
    first_seen: int = 0
    last_try: int | None = None
    next_try: int = 0
    reason: str = ""
    source: str = ""
    fingerprint: str = ""

    def query(self) -> AlbumQuery:
        """Suchdaten als :class:`~lyrion.media.art_online.AlbumQuery`."""
        return AlbumQuery(album=self.album, artist=self.artist,
                          year=self.year, mbid=self.mbid)

    @property
    def due_at(self) -> int:
        """Zeitpunkt, ab dem der Eintrag wieder fällig ist (0 = sofort)."""
        return int(self.next_try or 0)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artwork_online_wanted (
    album_key   TEXT PRIMARY KEY,
    album       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    year        INTEGER,
    mbid        TEXT,
    album_id    INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',
    attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen  INTEGER NOT NULL DEFAULT 0,
    last_try    INTEGER,
    next_try    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wanted_due
    ON artwork_online_wanted (status, next_try);
CREATE TABLE IF NOT EXISTS artwork_online_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

#: Spalten, die eine ältere Tabelle dieses Ports fehlen könnten — additiv
#: nachgezogen (``ALTER TABLE … ADD COLUMN`` ist in SQLite migrationssicher und
#: lässt bestehende Zeilen unangetastet).
_COLUMNS: dict[str, str] = {
    "album_id": "INTEGER",
    "mbid": "TEXT",
    "last_try": "INTEGER",
    "next_try": "INTEGER NOT NULL DEFAULT 0",
    "reason": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT ''",
    "fingerprint": "TEXT NOT NULL DEFAULT ''",
}

META_FINGERPRINT = "fingerprint"
META_LAST_RUN = "last_run"
META_LAST_PASS = "last_pass"
META_LAST_REASON = "last_reason"


class WantedStore:
    """Persistente Liste der Alben ohne Cover (SQLite, additiv angelegt)."""

    def __init__(self, cache_dir: Path, db_path: Path | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.cache_dir / "art_online.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    # ---- Schema ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._schema_ready:
            conn.executescript(_SCHEMA)
            # Bestehende Datei aus einer früheren Fassung: fehlende Spalten
            # additiv nachziehen (die alte Tabelle wird NICHT neu gebaut).
            existing = {str(row["name"]) for row in
                        conn.execute("PRAGMA table_info(artwork_online_wanted)")}
            for column, decl in _COLUMNS.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE artwork_online_wanted ADD COLUMN {column} {decl}")
                    logger.info("art_online_wanted: Spalte %s additiv ergänzt", column)
            conn.commit()
            self._schema_ready = True
        return conn

    # ---- Schreiben ----

    def add(self, query: AlbumQuery, *, album_id: int | None = None,
            source: str = "", fingerprint: str = "", now: int | None = None) -> bool:
        """Neuen Eintrag aufnehmen; ein vorhandener bleibt unangetastet.

        ``INSERT OR IGNORE``: ein Album, das schon in der Liste steht (Treffer,
        Fehlschlag oder offen), wird **nicht** zurückgesetzt — sonst würde jeder
        Start die Fehlschläge erneut sofort fällig machen (kein Endlos-Retry).
        Liefert ``True``, wenn der Eintrag neu ist.
        """
        stamp = int(now if now is not None else time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO artwork_online_wanted
                (album_key, album, artist, year, mbid, album_id, status, attempts,
                 first_seen, last_try, next_try, reason, source, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 0, '', ?, ?)
                """,
                (query.key(), query.album, query.artist, query.year, query.mbid,
                 album_id, STATUS_OPEN, stamp, source, fingerprint),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def add_many(self, rows: Iterable[tuple[AlbumQuery, int | None]], *,
                 source: str = "", fingerprint: str = "",
                 now: int | None = None) -> int:
        """Viele Einträge aufnehmen; liefert die Zahl der **neuen** Einträge."""
        stamp = int(now if now is not None else time.time())
        added = 0
        with self._connect() as conn:
            for query, album_id in rows:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO artwork_online_wanted
                    (album_key, album, artist, year, mbid, album_id, status,
                     attempts, first_seen, last_try, next_try, reason, source,
                     fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 0, '', ?, ?)
                    """,
                    (query.key(), query.album, query.artist, query.year,
                     query.mbid, album_id, STATUS_OPEN, stamp, source,
                     fingerprint),
                )
                added += int(bool(cursor.rowcount))
            conn.commit()
        return added

    def mark_hit(self, album_key: str, *, provider: str = "",
                 fingerprint: str = "", attempt: bool = True,
                 now: int | None = None) -> None:
        """Eintrag als „gefunden“ stempeln (Zeitstempel + Anbieter als Grund)."""
        stamp = int(now if now is not None else time.time())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE artwork_online_wanted
                   SET status = ?, last_try = ?, next_try = 0, reason = ?,
                       attempts = attempts + ?, fingerprint = ?
                 WHERE album_key = ?
                """,
                (STATUS_HIT, stamp, f"gefunden: {provider}" if provider else "gefunden",
                 int(attempt), fingerprint, album_key),
            )
            conn.commit()

    def mark_miss(self, album_key: str, *, reason: str = "", fingerprint: str = "",
                  retry_seconds: float = 0.0, attempt: bool = True,
                  now: int | None = None) -> None:
        """Eintrag als „kein Treffer“ stempeln, nächster Versuch mit Wartezeit."""
        stamp = int(now if now is not None else time.time())
        next_try = stamp + max(0.0, float(retry_seconds))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE artwork_online_wanted
                   SET status = ?, last_try = ?, next_try = ?, reason = ?,
                       attempts = attempts + ?, fingerprint = ?
                 WHERE album_key = ?
                """,
                (STATUS_MISS, stamp, int(next_try), reason[:200],
                 int(attempt), fingerprint, album_key),
            )
            conn.commit()

    def rearm_stale(self, fingerprint: str, *, now: int | None = None) -> int:
        """Frühere Fehlschläge einer **anderen** Konfiguration wieder fällig machen.

        Genau das verlangt „neuer API-Key hinzugekommen → Liste erneut
        abarbeiten“: der Eintrag bleibt erhalten (Zähler, Zeitstempel), wird aber
        wieder ``open`` und sofort fällig.  Die alte Kennung bleibt stehen, bis
        der nächste Versuch sie ersetzt — daran erkennt der Durchlauf, dass er
        den Negativ-Cache umgehen muss (``force=True``).
        """
        stamp = int(now if now is not None else time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE artwork_online_wanted
                   SET status = ?, next_try = 0, reason = reason
                 WHERE status = ? AND fingerprint <> ?
                """,
                (STATUS_OPEN, STATUS_MISS, fingerprint),
            )
            conn.commit()
            count = int(cursor.rowcount)
        if count:
            logger.info(
                "art_online_wanted: %d Eintrag/Einträge wegen geänderter "
                "Konfiguration erneut fällig (%s)", count, fingerprint or "ohne")
        del stamp
        return count

    def set_open(self, album_key: str, *, reason: str = "",
                 now: int | None = None) -> None:
        """Eintrag zurück auf „offen“ (z. B. Cache-Datei verschwunden)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE artwork_online_wanted SET status = ?, next_try = 0, "
                "reason = ? WHERE album_key = ?",
                (STATUS_OPEN, reason[:200], album_key),
            )
            conn.commit()

    # ---- Meta ----

    def meta_get(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM artwork_online_meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO artwork_online_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()

    # ---- Lesen ----

    def get(self, album_key: str) -> WantedEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artwork_online_wanted WHERE album_key = ?",
                (album_key,),
            ).fetchone()
        return _entry_from_row(row) if row else None

    def due(self, *, now: int | None = None, limit: int = 0) -> list[WantedEntry]:
        """Fällige Einträge (``open`` oder abgelaufener ``miss``) in Reihenfolge."""
        stamp = int(now if now is not None else time.time())
        sql = ("SELECT * FROM artwork_online_wanted "
               "WHERE status IN (?, ?) AND next_try <= ? "
               "ORDER BY first_seen ASC, album_key ASC")
        params: list[Any] = [STATUS_OPEN, STATUS_MISS, stamp]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_entry_from_row(row) for row in rows]

    def all_entries(self, limit: int = 0) -> list[WantedEntry]:
        sql = ("SELECT * FROM artwork_online_wanted "
               "ORDER BY first_seen ASC, album_key ASC")
        params: list[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_entry_from_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Zahl der Einträge je Status (``open``/``hit``/``miss``)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM artwork_online_wanted GROUP BY status"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def attempts_total(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(attempts), 0) FROM artwork_online_wanted"
            ).fetchone()
        return int(row[0] or 0)

    def next_due(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(next_try) FROM artwork_online_wanted "
                "WHERE status IN (?, ?)", (STATUS_OPEN, STATUS_MISS),
            ).fetchone()
        value = row[0] if row else None
        return int(value) if value else None


def _entry_from_row(row: sqlite3.Row) -> WantedEntry:
    return WantedEntry(
        album_key=str(row["album_key"]),
        album=str(row["album"] or ""),
        artist=str(row["artist"] or ""),
        year=int(row["year"]) if row["year"] is not None else None,
        mbid=(str(row["mbid"]) if row["mbid"] else None),
        album_id=int(row["album_id"]) if row["album_id"] is not None else None,
        status=str(row["status"] or STATUS_OPEN),
        attempts=int(row["attempts"] or 0),
        first_seen=int(row["first_seen"] or 0),
        last_try=int(row["last_try"]) if row["last_try"] is not None else None,
        next_try=int(row["next_try"] or 0),
        reason=str(row["reason"] or ""),
        source=str(row["source"] or ""),
        fingerprint=str(row["fingerprint"] or ""),
    )


# ---------------------------------------------------------------------------
# Wartezeit-Regel (kein Endlos-Retry)
# ---------------------------------------------------------------------------


def retry_delay(reason: str, retry_days: int) -> float:
    """Wartezeit bis zum nächsten Versuch eines Fehlschlags (Sekunden).

    Dieselbe Politik wie der Plattencache (``art_online.py``
    ``find_cover``/``_search``): ein **vorübergehender** Fehler (Rate-Limit,
    Zeitüberschreitung, Netz weg) wird nach ``TRANSIENT_RETRY_SECONDS`` erneut
    versucht, ein definitives „nein“ des Anbieters erst nach
    ``artworkOnlineRetryDays``.
    """
    if (reason or "").startswith(TRANSIENT_REASON_PREFIX):
        return TRANSIENT_RETRY_SECONDS
    return max(0, int(retry_days)) * 86400.0


def cache_entry_wait_seconds(entry: CacheEntry, retry_days: int) -> float:
    """Restsekunden, bis ein Negativ-Cache-Eintrag wieder versucht werden darf."""
    age = time.time() - float(entry.created_at or 0)
    window = retry_delay(entry.reason or "", retry_days)
    return max(0.0, window - age)


# ---------------------------------------------------------------------------
# Bibliothek lesen (nur lesen — keine Schemaänderung)
# ---------------------------------------------------------------------------

#: Alben ohne Cover samt Suchdaten.  Der Interpret kommt aus dem
#: Album-Contributor, dessen Sortierschlüssel dem ``albums.albumartist_sort``
#: entspricht (Perls Album-Identität); gibt es den nicht, der erste
#: Contributor.  Various-Artists-Alben suchen als „Various Artists“, weil
#: MusicBrainz Sampler unter genau diesem Interpreten führt
#: (``albums.albumartist_sort`` = ``various artists``,
#: ``media/importer.py:143-148``).
_LIBRARY_SQL = """
SELECT a.id AS id,
       a.title AS title,
       a.albumartist_sort AS artist_sort,
       a.year AS year,
       a.musicbrainz_id AS mbid,
       (SELECT c.name FROM albums_contributors ac
          JOIN contributors c ON c.id = ac.contributor
         WHERE ac.album = a.id AND c.sortname = a.albumartist_sort
         LIMIT 1) AS named,
       (SELECT c.name FROM albums_contributors ac
          JOIN contributors c ON c.id = ac.contributor
         WHERE ac.album = a.id
         ORDER BY c.id LIMIT 1) AS first_named
  FROM albums a
 WHERE (a.artwork IS NULL OR a.artwork = '')
   AND a.title IS NOT NULL AND a.title <> ''
   AND lower(a.title) <> 'unknown album'
 ORDER BY a.id
"""

VA_SEARCH_ARTIST = "Various Artists"


def search_artist_for(artist_sort: str, named: str | None,
                      first_named: str | None) -> str:
    """Interpret für die MusicBrainz-Suche aus der Album-Zeile ableiten."""
    key = (artist_sort or "").strip().casefold()
    if key == "various artists":
        return VA_SEARCH_ARTIST
    return str(named or first_named or artist_sort or "").strip()


async def library_albums_without_cover() -> list[tuple[AlbumQuery, int | None]]:
    """Alben ohne ``artwork`` aus der Bibliothek lesen (reine Lesebefehle).

    Ohne initialisierte Bibliotheks-DB (Einzelscript, Test) gibt es eine leere
    Liste — der Aufrufer trägt dann nichts ein.
    """
    from lyrion.database.sqlite_helper import db_session
    from sqlalchemy import text

    out: list[tuple[AlbumQuery, int | None]] = []
    try:
        async with db_session() as session:
            rows = (await session.execute(text(_LIBRARY_SQL))).mappings().all()
    except Exception as exc:  # noqa: BLE001 - ohne Bibliothek gibt es nichts zu tun
        logger.debug("art_online_wanted: Bibliothek nicht lesbar (%s)", exc)
        return out
    for row in rows:
        artist = search_artist_for(str(row["artist_sort"] or ""), row["named"],
                                   row["first_named"])
        query = AlbumQuery(
            album=str(row["title"] or ""),
            artist=artist,
            year=int(row["year"]) if row["year"] is not None else None,
            mbid=str(row["mbid"]) if row["mbid"] else None,
        )
        out.append((query, int(row["id"]) if row["id"] is not None else None))
    return out


# ---------------------------------------------------------------------------
# Dienst
# ---------------------------------------------------------------------------


class WantedService:
    """Arbeitet die wanted-Liste im Hintergrund ab (wiederaufnehmbar)."""

    def __init__(
        self,
        service: ArtOnlineService | None = None,
        *,
        store: WantedStore | None = None,
        per_pass: int = 0,
        idle_sleep: float = IDLE_SLEEP,
        first_pass_delay: float = FIRST_PASS_DELAY,
        entry_pause: float = ENTRY_PAUSE,
        library: Callable[[], Awaitable[list[tuple[AlbumQuery, int | None]]]] | None = None,
        on_album_cover: Callable[[AlbumQuery, CoverResult], Awaitable[int] | int] | None = None,
    ) -> None:
        self.service = service or configured_service()
        self.store = store or WantedStore(self.service.settings.cache_dir)
        self.per_pass = int(per_pass or 0)
        self.idle_sleep = float(idle_sleep)
        self.first_pass_delay = float(first_pass_delay)
        self.entry_pause = float(entry_pause)
        self._library = library or library_albums_without_cover
        self._on_album_cover = on_album_cover
        self._task: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._active = False
        self._checked = 0
        self._pass_total = 0
        self._current: str | None = None
        self._last_pass: dict[str, Any] = {}
        self._last_stop_reason = ""

    # ---- Einstellungen ----

    @property
    def settings(self) -> ArtOnlineSettings:
        return self.service.settings

    @property
    def auto(self) -> bool:
        """Darf der Dienst beim Start von selbst anlaufen?"""
        return _pref_bool(PREF_WANTED_AUTO, True)

    @property
    def running(self) -> bool:
        """Läuft der Hintergrund-Task (nicht: läuft gerade ein Durchlauf)?"""
        return self._task is not None and not self._task.done()

    @property
    def active(self) -> bool:
        """Läuft gerade ein Durchlauf?"""
        return self._active

    # ---- Lebenszyklus ----

    async def start(self, *, backfill: bool = True) -> bool:
        """Dienst als Task starten (idempotent, überlebt keinen Prozessende).

        Die Liste selbst liegt in SQLite — ein Neustart verliert nichts,
        der nächste Durchlauf macht an der Stelle der Liste weiter.
        """
        if self.auto is False and not self.running:
            logger.info("art_online_wanted: Dienst nicht gestartet (%s=0)",
                        PREF_WANTED_AUTO)
            return False
        if self.running:
            return True
        if backfill:
            try:
                await self.backfill(source="startup")
            except Exception as exc:  # noqa: BLE001 - Start darf nie brechen
                logger.warning("art_online_wanted: Backfill scheiterte (%s)", exc)
        self._stop = asyncio.Event()
        self._task = asyncio.ensure_future(self._loop())
        return True

    async def stop(self) -> None:
        """Dienst anhalten (Zustand bleibt in SQLite)."""
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._active = False
        self._current = None

    def request_pass(self, *, reason: str = "gui") -> bool:
        """Einen weiteren Durchlauf anstossen (Web-GUI, Tests).

        ``False`` heisst: es läuft bereits einer — dann wird **kein** zweiter
        gestartet (die Liste wird ja gerade abgearbeitet).  Ohne laufenden Task
        (Einzelaufruf) wird nur die Weckmarke gesetzt und der Aufrufer kann
        :meth:`run_pass` direkt abwarten.
        """
        self._last_stop_reason = reason
        if self._active:
            logger.info("art_online_wanted: Durchlauf läuft bereits — %s ignoriert",
                        reason)
            return False
        self._wake.set()
        return True

    def start_pass_task(self, *, reason: str = "gui") -> bool:
        """Durchlauf als eigenen Task starten (ohne Hintergrund-Schleife)."""
        if self._active:
            return False
        self._task = asyncio.ensure_future(self.run_pass(reason=reason))
        return True

    async def _loop(self) -> None:
        """Hintergrund-Schleife: Backfill, Durchlauf, Ruhe — bis zum Stopp."""
        try:
            if self.first_pass_delay:
                await self._wait(self.first_pass_delay)
            while not self._stop.is_set():
                reason = self._last_stop_reason or "auto"
                self._last_stop_reason = ""
                try:
                    await self.run_pass(reason=reason)
                except Exception as exc:  # noqa: BLE001 - der Dienst darf nie sterben
                    logger.warning("art_online_wanted: Durchlauf scheiterte (%s)", exc)
                if self._stop.is_set():
                    break
                await self._wait(self.idle_sleep)
        except asyncio.CancelledError:  # pragma: no cover - Stopp-Pfad
            raise

    async def _wait(self, timeout: float) -> None:
        """Auf Weckmarke oder Zeitablauf warten (Stopp unterbricht sofort)."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, timeout))
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    # ---- Liste füllen ----

    async def backfill(self, *, source: str = "startup") -> int:
        """Alben ohne Cover aus der Bibliothek in die Liste aufnehmen.

        Idempotent: schon vorhandene Einträge (gleich welchen Status) bleiben,
        wie sie sind.  Liefert die Zahl der **neuen** Einträge.
        """
        entries = await self._library()
        if not entries:
            return 0
        fingerprint = self.settings.fingerprint()
        added = self.store.add_many(entries, source=source,
                                    fingerprint=fingerprint)
        if added:
            logger.info(
                "art_online_wanted: %d Album/Alben ohne Cover aufgenommen (%s, "
                "Liste: %s)", added, source, self.store.counts())
        else:
            logger.debug("art_online_wanted: Liste unverändert (%s)", source)
        return added

    # ---- Durchlauf ----

    async def run_pass(self, *, reason: str = "pass") -> dict[str, Any]:
        """Einen vollständigen Durchlauf über die fälligen Einträge fahren."""
        async with self._lock:
            settings = self.settings
            fingerprint = settings.fingerprint()
            stored = self.store.meta_get(META_FINGERPRINT)
            changed = bool(stored) and stored != fingerprint
            rearmed = self.store.rearm_stale(fingerprint) if changed else 0
            self.store.meta_set(META_FINGERPRINT, fingerprint)

            if not settings.enabled:
                logger.debug("art_online_wanted: Suche aus — Durchlauf übersprungen")
                self.store.meta_set(META_LAST_REASON, "Suche aus")
                return {"reason": reason, "checked": 0, "skipped": "disabled"}

            if _scan_is_running():
                # Der Scan besitzt die Bibliotheks-DB und stösst seine eigenen
                # Suchen an; der Hintergrunddienst wartet (Perl lässt den
                # Scanner allein arbeiten, Slim/Music/Import.pm:812-826).
                logger.info("art_online_wanted: Bibliotheks-Scan läuft — Durchlauf "
                            "verschoben")
                self.store.meta_set(META_LAST_REASON, "Scan läuft")
                return {"reason": reason, "checked": 0, "skipped": "scan"}

            try:
                await self.backfill(source="pass")
            except Exception as exc:  # noqa: BLE001
                logger.debug("art_online_wanted: Backfill im Durchlauf (%s)", exc)

            entries = self.store.due(limit=self.per_pass or 0)
            started = time.time()
            counts = {OUTCOME_HIT: 0, OUTCOME_MISS: 0, OUTCOME_CACHED_MISS: 0,
                      OUTCOME_DISABLED: 0, OUTCOME_SKIPPED: 0}
            self._active = True
            self._checked = 0
            self._pass_total = len(entries)
            logger.info(
                "art_online_wanted: Durchlauf startet (%s, %d fällige Einträge, "
                "Konfiguration %s%s)", reason, len(entries), fingerprint,
                " GEÄNDERT" if changed else "")
            try:
                for entry in entries:
                    if self._stop.is_set():
                        counts[OUTCOME_SKIPPED] += 1
                        break
                    outcome = await self._process(entry, fingerprint)
                    counts[outcome] = counts.get(outcome, 0) + 1
                    self._checked += 1
                    self._current = None
                    if self.entry_pause and outcome in (OUTCOME_HIT, OUTCOME_MISS):
                        await asyncio.sleep(self.entry_pause)
            finally:
                self._active = False
                self._current = None
                seconds = round(time.time() - started, 3)
                summary = {
                    "reason": reason,
                    "fingerprint": fingerprint,
                    "settings_changed": changed,
                    "rearmed": rearmed,
                    "checked": self._checked,
                    # Immer alle Ausgänge nennen (auch die Null): die Anzeige und
                    # die Tests lesen die Zähler ohne „key fehlt“-Sonderfälle.
                    **counts,
                    "seconds": seconds,
                }
                self._last_pass = summary
                self.store.meta_set(META_LAST_RUN, str(int(time.time())))
                self.store.meta_set(META_LAST_PASS, json.dumps(summary))
                self.store.meta_set(META_LAST_REASON, reason)
                logger.info(
                    "art_online_wanted: Durchlauf beendet (%s): %s",
                    reason, json.dumps(summary, ensure_ascii=False))
            return summary

    async def _process(self, entry: WantedEntry, fingerprint: str) -> str:
        """Einen Eintrag bearbeiten — Cache lesen, sonst suchen, dann stempeln."""
        service = self.service
        query = entry.query()
        self._current = query.album
        if not service.settings.enabled:
            return OUTCOME_DISABLED

        key = query.key()
        cached = service.cache.read_sync(key)
        force = entry.fingerprint != fingerprint

        if cached is not None and cached.status == "hit" and not force:
            cover = service.read_cached(query)
            if cover is not None:
                # Treffer liegt schon im Plattencache (z. B. aus einem früheren
                # Scan) — nur die Albumzeile nachtragen, kein Netz.
                await self._write_album_row(query, cover)
                self.store.mark_hit(key, provider=cover.provider,
                                    fingerprint=fingerprint, attempt=False)
                return OUTCOME_HIT
            # Trefferzeile ohne Datei: wieder offen und wirklich suchen.
            self.store.set_open(key, reason="Cache-Datei fehlt")

        if cached is not None and cached.status == "miss" and not force:
            wait = cache_entry_wait_seconds(cached, service.settings.retry_days)
            if wait > 0:
                # Kürzlich versucht: kein weiterer Netzversuch (kein Endlos-Retry).
                self.store.mark_miss(
                    key, reason=cached.reason or "kein Treffer",
                    fingerprint=fingerprint, retry_seconds=wait, attempt=False)
                return OUTCOME_CACHED_MISS

        try:
            cover = await service.find_cover(query, force=force)
        except Exception as exc:  # noqa: BLE001 - ein Album darf nichts brechen
            logger.warning("art_online_wanted: Suche für %r scheiterte (%s)",
                           query.album, exc)
            self.store.mark_miss(key, reason=f"unerwartet: {exc}",
                                 fingerprint=fingerprint,
                                 retry_seconds=TRANSIENT_RETRY_SECONDS)
            return OUTCOME_MISS

        if cover is not None:
            self.store.mark_hit(key, provider=cover.provider,
                                fingerprint=fingerprint)
            return OUTCOME_HIT

        fresh = service.cache.read_sync(key)
        reason = (fresh.reason if fresh is not None else "") or "kein Treffer"
        self.store.mark_miss(key, reason=reason, fingerprint=fingerprint,
                             retry_seconds=retry_delay(reason,
                                                       service.settings.retry_days))
        return OUTCOME_MISS

    async def _write_album_row(self, query: AlbumQuery, cover: CoverResult) -> None:
        """``albums.artwork`` auf das gefundene Cover setzen (Perl-Nachtrag)."""
        try:
            if self._on_album_cover is not None:
                outcome = self._on_album_cover(query, cover)
                if asyncio.iscoroutine(outcome):
                    await outcome
                return
            from lyrion.media.importer import store_online_cover

            await store_online_cover(query, cover)
        except Exception as exc:  # noqa: BLE001 - ein Cover ist nie ein Fehler
            logger.debug("art_online_wanted: Albumzeile für %r nicht gesetzt (%s)",
                         query.album, exc)

    # ---- Status (Web-GUI) ----

    def status(self) -> dict[str, Any]:
        """Zustand für Anzeige/JSON — Zähler, letzter Lauf, Fortschritt."""
        counts = self.store.counts()
        settings = self.settings
        fingerprint = settings.fingerprint()
        stored = self.store.meta_get(META_FINGERPRINT) or ""
        last_pass: dict[str, Any] = {}
        raw = self.store.meta_get(META_LAST_PASS)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    last_pass = parsed
            except ValueError:
                last_pass = {}
        last_run = self.store.meta_get(META_LAST_RUN)
        open_count = counts.get(STATUS_OPEN, 0)
        miss_count = counts.get(STATUS_MISS, 0)
        return {
            "open": open_count,
            "hit": counts.get(STATUS_HIT, 0),
            "miss": miss_count,
            "total": open_count + miss_count + counts.get(STATUS_HIT, 0),
            "attempts": self.store.attempts_total(),
            "running": self._active,
            "service_started": self.running,
            "current": self._current,
            "checked": self._checked,
            "pass_total": self._pass_total,
            "last_run": int(last_run) if last_run else None,
            "last_pass": last_pass,
            "last_reason": self.store.meta_get(META_LAST_REASON) or "",
            "settings_changed": bool(stored) and stored != fingerprint,
            "next_due": self.store.next_due(),
            "enabled": bool(settings.enabled),
            "auto": bool(self.auto),
            "providers": list(settings.effective_providers()),
            "fingerprint": fingerprint,
        }


def _pref_bool(name: str, default: bool) -> bool:
    """Pref als Ja/Nein lesen (``web/settings`` bzw. ``PREF_DEFAULTS``)."""
    try:
        from lyrion.web.settings import art_online_wanted_pref_values

        value = art_online_wanted_pref_values().get(name)
    except Exception:  # noqa: BLE001 - ohne Prefs gilt die Vorbelegung
        value = None
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "ja"):
        return True
    if text in ("0", "false", "no", "off", "nein"):
        return False
    return default


def _pref_int(name: str, default: int) -> int:
    try:
        from lyrion.web.settings import art_online_wanted_pref_values

        value = art_online_wanted_pref_values().get(name)
    except Exception:  # noqa: BLE001
        value = None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _scan_is_running() -> bool:
    """Läuft gerade ein Bibliotheks-Scan (fremder Scanner-Prozess)?"""
    try:
        from lyrion.media.scan_state import live_published_state

        return live_published_state() is not None
    except Exception:  # noqa: BLE001 - ohne Scan-Zustand wird gearbeitet
        return False


# ---------------------------------------------------------------------------
# Prozessweiter Dienst
# ---------------------------------------------------------------------------

_service: WantedService | None = None


def configured_wanted_service() -> WantedService:
    """Der wanted-Dienst dieses Prozesses (einmal angelegt, dann wiederverwendet)."""
    global _service
    if _service is None:
        from lyrion.media.importer import wire_online_artwork

        online = configured_service()
        wire_online_artwork(online)
        _service = WantedService(
            online,
            per_pass=_pref_int(PREF_WANTED_PER_PASS, 0),
        )
    return _service


def current_wanted_service() -> WantedService | None:
    """Der schon angelegte Dienst — oder ``None`` (legt keinen an)."""
    return _service


def reset_wanted_service() -> None:
    """Dienst vergessen (Tests, Konfigurationswechsel)."""
    global _service
    _service = None


async def record_missing_albums(*, source: str = "scan") -> int:
    """Alben ohne Cover in die Liste aufnehmen (Scan-Ende, Tests).

    Legt **keinen** Hintergrund-Task an: der Scan-Prozess endet nach dem Import
    (``Slim/Music/Import.pm:206-230``), nur der Server hält den Dienst am Leben.
    """
    try:
        service = configured_wanted_service()
    except Exception as exc:  # noqa: BLE001 - Listenpflege ist nie ein Scanfehler
        logger.debug("art_online_wanted: Dienst nicht verfügbar (%s)", exc)
        return 0
    try:
        return await service.backfill(source=source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("art_online_wanted: Aufnahme der Alben scheiterte (%s)", exc)
        return 0


async def start_background(*, backfill: bool = True) -> bool:
    """Hintergrund-Dienst dieses Prozesses starten (Serverstart).

    Aufrufer ist die ASGI-App (``web/app.py`` Lifespan) — ohne laufende
    Event-Loop und ohne Bibliotheks-DB ist der Aufruf wirkungslos.
    """
    return await configured_wanted_service().start(backfill=backfill)


async def stop_background() -> None:
    """Hintergrund-Dienst anhalten (Serverende)."""
    service = _service
    if service is None:
        return
    await service.stop()


def wanted_status() -> dict[str, Any]:
    """Status des Dienstes für die Web-GUI (ohne die Liste anzulegen)."""
    service = configured_wanted_service()
    return service.status()


def trigger_pass(*, reason: str = "gui") -> dict[str, Any]:
    """Neuen Durchlauf anstossen; liefert den Status *nach* dem Anstoss.

    Ohne laufenden Hintergrund-Task (z. B. Einzelscript) wird der Durchlauf
    sofort als Task gestartet, damit der GUI-Knopf auch dann etwas auslöst.
    """
    service = configured_wanted_service()
    started = service.request_pass(reason=reason)
    if started and not service.running:
        service.start_pass_task(reason=reason)
    status = service.status()
    status["started"] = bool(started)
    return status


__all__ = [
    "IDLE_SLEEP", "META_FINGERPRINT", "META_LAST_PASS", "META_LAST_RUN",
    "PREF_WANTED_AUTO", "PREF_WANTED_PER_PASS", "STATUS_HIT", "STATUS_MISS",
    "STATUS_OPEN", "VA_SEARCH_ARTIST", "WANTED_PREF_DEFAULTS", "WantedEntry",
    "WantedService", "WantedStore", "cache_entry_wait_seconds",
    "configured_wanted_service", "current_wanted_service",
    "library_albums_without_cover", "record_missing_albums", "reset_wanted_service",
    "retry_delay", "search_artist_for", "start_background", "stop_background",
    "trigger_pass", "wanted_status",
]
