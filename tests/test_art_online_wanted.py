"""Wanted-Liste + Hintergrund-Dienst des Artwork-Downloaders.

Geprüft wird, was die Aufgabe verlangt — mit rohen Werten, ohne Live-Netz
(Anbieter sind Fakes, Bilddaten sind echte JPEG-Bytes):

1. **Liste**: persistente Einträge je Album ohne Cover mit Suchdaten, Status,
   Zeitstempeln und Versuchszahl; mehrfaches Eintragen setzt nichts zurück.
2. **Dienst**: ein Durchlauf holt nachweislich ein Cover (Cache-Datei + Album-
   zeile über den Rückruf), Fehlschläge bekommen eine Wartezeit (kein
   Endlos-Retry), vorübergehende Fehler eine kurze.
3. **Wiederaufnahme**: ein Abbruch mitten im Lauf verliert nichts; ein neuer
   Dienst auf derselben Datei macht an der Stelle der Liste weiter.
4. **Konfig-/Key-Änderung**: frühere „kein Treffer“-Einträge werden erneut
   fällig und **erzwungen** gesucht (Negativ-Cache wird umgangen).
5. **Web-GUI**: die Seite zeigt Zustand und Knopf, ``status.json`` liefert die
   Zähler, ``/settings/artworkonline/run`` stösst einen Durchlauf an.
6. **Wiedergabepfad**: die Auslieferung liest weiter nur Platte/DB — kein
   einziger HTTP-Aufruf, auch nicht mit vollem Cache und offener Liste.

Perl-Abgleich: es gibt **kein** Perl-Analogon (Modul-Docstring
``media/art_online_wanted.py``; ``Slim/Plugin/RadioArtwork/Plugin.pm:166-201``
ist flüchtig und läuft im Titelwechsel, ``Slim/Music/Import.pm:806-848`` ist
eine In-Memory-Scan-Warteschlange ohne Persistenz).
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import aiosqlite
import httpx
import pytest
from PIL import Image
from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.database.schema import Album, Contributor, albums_contributors
from lyrion.database.sqlite_helper import close_db, db_session, init_db
from lyrion.media import art_online, art_online_wanted as wanted
from lyrion.media.art_online import (
    TRANSIENT_RETRY_SECONDS,
    AlbumQuery,
    ArtOnlineService,
    ArtOnlineSettings,
    FetchResponse,
    ProviderError,
)

# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def jpeg_bytes(edge: int = 500) -> bytes:
    """Echtes JPEG — die Format- und Grössenprüfung soll wirklich laufen."""
    buf = io.BytesIO()
    Image.new("RGB", (edge, edge), (30, 60, 90)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeFetcher:
    """Antwortet nach Teilstring-Routen und zählt jede Anfrage (kein Netz)."""

    def __init__(self, routes: list[tuple[str, object]] | None = None) -> None:
        self.routes = routes or []
        self.calls: list[str] = []
        #: Wenn gesetzt: erst die ersten ``allow`` Anfragen durchlassen, danach
        #: blockieren (für den „Absturz mitten im Lauf“).
        self.allow: int | None = None
        self.gate = asyncio.Event()

    async def get(self, url: str, params=None) -> FetchResponse:
        self.calls.append(url)
        if self.allow is not None and len(self.calls) > self.allow:
            await self.gate.wait()
        for needle, responder in self.routes:
            if needle in url:
                if callable(responder):
                    return responder(url, params or {})
                return responder  # type: ignore[return-value]
        raise ProviderError(f"unerwartete URL im Test: {url}")

    async def close(self) -> None:
        pass


def caa_hit(edge: int = 500) -> FetchResponse:
    return FetchResponse(status=200, content=jpeg_bytes(edge),
                         headers={"Content-Type": "image/jpeg"})


def caa_404() -> FetchResponse:
    return FetchResponse(status=404, content=b"not found",
                         headers={"Content-Type": "text/plain"})


def build_service(tmp_path, routes=None, *, settings: ArtOnlineSettings | None = None):
    """Dienst + Fetcher auf einem eigenen Cache-Verzeichnis."""
    conf = settings or ArtOnlineSettings(cache_dir=tmp_path / "cache")
    fetch = FakeFetcher(routes)
    service = ArtOnlineService(conf, fetcher=fetch,
                               db_path=tmp_path / "cache" / "art_online.db")
    service.on_cover = None            # Albumzeile schreibt der Test-Rückruf
    return service, fetch


def build_wanted(tmp_path, service, library, *, write_rows: bool = False, **kwargs):
    """Dienst auf der Liste dieses Verzeichnisses; sammelt Albumzeilen-Rückrufe.

    ``write_rows=True`` lässt den Rückruf die Albumzeile **wirklich** setzen
    (``media/importer.py`` ``store_online_cover``, wie ``wire_online_artwork``
    im Server).  Ohne das meldet der Rückruf nur „1 Zeile“ — richtig für die
    Tests, die nur die Reihenfolge prüfen; alles, was die Anzeige-Aktualisierung
    nach einem echten Nachtrag prüft, braucht die echte Zeile.
    """
    store = kwargs.pop("store", None) or wanted.WantedStore(service.settings.cache_dir)
    rows: list[tuple[str, str, str]] = []

    async def on_row(query, cover):
        rows.append((query.album, cover.provider, str(cover.path)))
        if not write_rows:
            return 1
        from lyrion.media.importer import store_online_cover

        return int(await store_online_cover(query, cover) or 0)

    # Wie ``media/importer.py`` ``wire_online_artwork``: ein neuer Treffer hängt
    # sofort an der Albumzeile (Perl ``Slim/Utils/Scanner/Local.pm:1086-1091``).
    service.on_cover = on_row
    w = wanted.WantedService(service, store=store,
                             library=lambda: asyncio.sleep(0, result=list(library)),
                             entry_pause=0, on_album_cover=on_row, **kwargs)
    return w, rows


def _run(coro):
    return asyncio.run(coro)


async def _async_add(store, fingerprint: str) -> bool:
    """Einen offenen Eintrag mit gegebener Kennung eintragen."""
    return store.add(AlbumQuery(album="Alt", artist="A", year=2000),
                     fingerprint=fingerprint)


def set_pref(name: str, value: str) -> None:
    """Pref schreiben wie das Web-GUI (Prefs-Store im Speicher)."""

    async def _do() -> None:
        await get_prefs().set(name, value)

    _run(_do())


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """Globalen Prefs-Store auf In-Memory-DB hängen (Muster ``test_art_online.py``)."""
    store = get_prefs()
    opened = False
    if getattr(store, "_db", None) is None:
        async def _open() -> None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
            await store._db.executescript(_PREF_DB_SCHEMA)

        asyncio.run(_open())
        opened = True
    yield store
    db = getattr(store, "_db", None)
    if opened and db is not None:
        try:
            asyncio.run(db.close())
        except Exception:  # noqa: BLE001 - Aufräumen darf nicht scheitern
            pass
        store._db = None


def _reset_art_prefs() -> None:
    """Die ``artworkOnline*``-Prefs auf die Vorbelegung zurückstellen.

    Die GUI-Tests speichern wirklich (Prefs-Store im Speicher) — ohne diesen
    Rückschritt hinge das Verhalten der folgenden Tests von der Reihenfolge ab
    (live gesehen: ein gespeichertes ``artworkOnlineWantedAuto=0`` stoppte den
    Hintergrund-Dienst im nächsten Test).
    """
    defaults = {**art_online.PREF_DEFAULTS, **wanted.WANTED_PREF_DEFAULTS}
    store = get_prefs()

    async def _do() -> None:
        for name, value in defaults.items():
            await store.set(name, value)

    asyncio.run(_do())


@pytest.fixture(autouse=True)
def _fresh_wanted_service():
    """Prozessweiten Dienst und Prefs vor und nach jedem Test zurücksetzen."""
    art_online.reset_service()
    wanted.reset_wanted_service()
    yield
    art_online.reset_service()
    wanted.reset_wanted_service()
    _reset_art_prefs()


# ---------------------------------------------------------------------------
# 1. Wanted-Liste: Einträge, Status, Zeitstempel, Versuchszahl
# ---------------------------------------------------------------------------


def test_list_keeps_search_data_status_and_timestamps(tmp_path):
    store = wanted.WantedStore(tmp_path)
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=2023,
                       mbid="rg-4711")

    assert store.add(query, album_id=42, source="scan", fingerprint="fp-1") is True
    assert store.add(query, album_id=42, source="startup", fingerprint="fp-2") is False

    entry = store.get(query.key())
    assert entry is not None
    assert (entry.album, entry.artist, entry.year, entry.mbid) == (
        "Point Blank", "Bonfire", 2023, "rg-4711")
    assert entry.album_id == 42 and entry.status == wanted.STATUS_OPEN
    assert entry.attempts == 0 and entry.first_seen > 0
    assert entry.last_try is None and entry.next_try == 0
    # Der erste Eintrag gewinnt: ein zweiter Lauf setzt nichts zurück.
    assert entry.source == "scan" and entry.fingerprint == "fp-1"

    store.mark_miss(query.key(), reason="kein passender Treffer",
                    fingerprint="fp-1", retry_seconds=3600)
    miss = store.get(query.key())
    assert miss is not None and miss.status == wanted.STATUS_MISS
    assert miss.attempts == 1 and miss.last_try and miss.next_try >= miss.last_try
    assert len(store.due()) == 0, "frischer Fehlschlag ist nicht sofort wieder fällig"
    assert len(store.due(now=int(time.time()) + 3700)) == 1

    store.mark_hit(query.key(), provider="coverartarchive", fingerprint="fp-1")
    hit = store.get(query.key())
    assert hit is not None and hit.status == wanted.STATUS_HIT
    assert hit.attempts == 2 and hit.next_try == 0
    assert store.counts() == {wanted.STATUS_HIT: 1}
    assert store.attempts_total() == 2


def test_store_is_additive_and_survives_an_existing_cache_db(tmp_path):
    """Die Tabelle entsteht neben dem bestehenden Cache — Daten bleiben heil.

    Bestehende Installationen haben ``art_online.db`` schon (Cache-Zeilen).
    Diese Änderung legt nur **neue** Tabellen an und rührt die alten nicht an.
    """
    import sqlite3

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    service = ArtOnlineService(ArtOnlineSettings(cache_dir=cache_dir))
    query = AlbumQuery(album="Alt", artist="Bestand", year=1999)
    service.cache.write_cover_sync(query.key(), query, jpeg_bytes(300),
                                   provider="coverartarchive", mime="image/jpeg",
                                   source_url="https://x", mbid=None,
                                   width=300, height=300)

    store = wanted.WantedStore(cache_dir, cache_dir / "art_online.db")
    store.add(AlbumQuery(album="Neu", artist="Fehlt", year=2001), source="startup")

    con = sqlite3.connect(str(cache_dir / "art_online.db"))
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"artwork_online_cache", "artwork_online_wanted",
            "artwork_online_meta"} <= tables
    # Bestehende Cache-Zeile unverändert, Trefferdatei noch da.
    assert service.cache.stats_sync() == {"hit": 1}
    assert store.counts() == {wanted.STATUS_OPEN: 1}
    # Zweites Öffnen ist idempotent (kein zweites Anlegen, kein Fehler).
    assert wanted.WantedStore(cache_dir).counts() == {wanted.STATUS_OPEN: 1}


# ---------------------------------------------------------------------------
# 2. Ein Durchlauf holt Cover und schreibt Albumzeile (Cache + Rückruf)
# ---------------------------------------------------------------------------


def test_pass_fetches_a_cover_and_writes_cache_and_album_row(tmp_path):
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=2023,
                       mbid="rg-1")
    w, rows = build_wanted(tmp_path, service, [(query, 42)])

    async def run():
        added = await w.backfill(source="startup")
        summary = await w.run_pass(reason="test")
        return added, summary

    added, summary = _run(run())
    assert added == 1
    assert summary["checked"] == 1 and summary["hit"] == 1
    # Cache-Datei liegt auf Platte und ist im Cache indexiert.
    entry = service.cache.read_sync(query.key())
    assert entry is not None and entry.status == "hit"
    assert Path(service.settings.cache_dir, entry.file_name).is_file()
    assert service.cache.stats_sync() == {"hit": 1}
    # Albumzeile: der Rückruf wurde mit Suchdaten + Treffer gerufen.
    assert rows and rows[0][0] == "Point Blank"
    assert rows[0][1] == "coverartarchive"
    # Liste: Eintrag als gefunden gestempelt, nächster Durchlauf hat nichts zu tun.
    assert w.store.counts() == {wanted.STATUS_HIT: 1}
    status = w.status()
    assert status["hit"] == 1 and status["open"] == 0 and status["last_run"]
    assert status["last_pass"]["checked"] == 1
    assert fetch.calls, "der Durchlauf muss wirklich gefragt haben"


def test_pass_marks_miss_with_retry_window_and_does_not_loop(tmp_path):
    """Fehlschlag → ``miss`` mit Wartezeit; kein sofortiger zweiter Netzversuch."""
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_404())],
                                   settings=ArtOnlineSettings(
                                       cache_dir=tmp_path / "cache",
                                       providers=("coverartarchive",),
                                       retry_days=30))
    library = [(AlbumQuery(album="Ohne Cover", artist="Niemand", year=1999,
                           mbid="rg-2"), 7)]
    w, _rows = build_wanted(tmp_path, service, library)

    async def run():
        await w.backfill()
        first = await w.run_pass(reason="test")
        calls_after_first = len(fetch.calls)
        second = await w.run_pass(reason="again")
        return first, second, calls_after_first

    first, second, calls_after_first = _run(run())
    assert first["miss"] == 1 and first["hit"] == 0
    entry = w.store.all_entries()[0]
    assert entry.status == wanted.STATUS_MISS and entry.attempts == 1
    assert entry.last_try is not None
    assert entry.next_try >= entry.last_try                 # Wartezeit gesetzt
    assert entry.next_try - int(entry.last_try) >= 29 * 86400   # retryDays
    # Zweiter Durchlauf: nichts fällig, also kein weiterer Netzaufruf.
    assert second["checked"] == 0
    assert len(fetch.calls) == calls_after_first
    assert wanted.retry_delay("kein passender Treffer", 30) == 30 * 86400
    assert wanted.retry_delay("vorübergehend: MusicBrainz 503 (Rate-Limit)", 30) \
        == TRANSIENT_RETRY_SECONDS


def test_fresh_negative_cache_entry_blocks_the_network(tmp_path):
    """Liegt der Fehlschlag schon im Cache, wird nur der Status nachgezogen."""
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_404())],
                                   settings=ArtOnlineSettings(
                                       cache_dir=tmp_path / "cache",
                                       providers=("coverartarchive",)))
    query = AlbumQuery(album="Schon Versucht", artist="Niemand", year=1990)
    service.cache.write_miss_sync(query.key(), query, "kein passender Treffer")
    w, _rows = build_wanted(tmp_path, service, [(query, 9)])

    async def run():
        # Eintrag mit der aktuellen Kennung: kein erzwungener Lauf, der
        # Negativ-Cache ist bindend (so trägt auch der Backfill ein).
        st = w.store.add(query, album_id=9, source="scan",
                         fingerprint=service.settings.fingerprint())
        assert st is True
        return await w.run_pass(reason="test")

    summary = _run(run())
    assert summary.get("waiting") == 1 and summary["checked"] == 1
    assert fetch.calls == [], "kein Netzversuch bei frischem Negativ-Cache"
    entry = w.store.get(query.key())
    assert entry is not None and entry.status == wanted.STATUS_MISS
    assert entry.attempts == 0, "ohne Netzversuch zählt kein Versuch"


# ---------------------------------------------------------------------------
# 3. Wiederaufnahme: Abbruch mitten im Lauf verliert nichts
# ---------------------------------------------------------------------------


def test_abort_mid_pass_resumes_without_loss(tmp_path):
    """Dienst hart abbrechen (wie ein Neustart) — danach weiterarbeiten."""
    library = [
        (AlbumQuery(album="Album Eins", artist="A", year=2001, mbid="rg-a"), 1),
        (AlbumQuery(album="Album Zwei", artist="B", year=2002, mbid="rg-b"), 2),
        (AlbumQuery(album="Album Drei", artist="C", year=2003, mbid="rg-c"), 3),
    ]
    cache_dir = tmp_path / "cache"
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    # Der erste Eintrag braucht zwei Aufrufe (MusicBrainz-Suchversuch, dann
    # Cover-Art-Archive); danach blockiert der Fetcher — der Absturz passiert
    # also mitten im zweiten Eintrag.
    fetch.allow = 2
    w, _rows = build_wanted(tmp_path, service, library)

    async def crash():
        await w.backfill(source="startup")
        task = asyncio.ensure_future(w.run_pass(reason="crash"))
        deadline = time.time() + 5
        while time.time() < deadline:
            if w.store.counts().get("hit", 0) >= 1:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)        # der zweite Eintrag hängt im Netz
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return w.store.counts(), len(fetch.calls)

    counts, calls = _run(crash())
    assert counts.get("hit") == 1, f"nach dem Abbruch: {counts}"
    assert counts.get("open", 0) >= 1, f"offene Einträge müssen bleiben: {counts}"

    # „Neustart“: neuer Dienst auf derselben Datei (gleiches Cache-Verzeichnis).
    fetch.allow = None
    fetch.gate.set()
    w2, _rows2 = build_wanted(tmp_path, service, library,
                              store=wanted.WantedStore(cache_dir))
    summary = _run(w2.run_pass(reason="nach Neustart"))

    assert w2.store.counts().get(wanted.STATUS_HIT) == 3
    assert summary["checked"] >= 2
    assert len(fetch.calls) > calls, "der Neustart muss die offenen Einträge holen"
    assert w2.status()["open"] == 0 and w2.status()["hit"] == 3


# ---------------------------------------------------------------------------
# 4. Konfig-/Key-Änderung: Liste wird erneut abgearbeitet
# ---------------------------------------------------------------------------


def test_new_api_key_rearms_misses_and_searches_again(tmp_path):
    """Vorherige „kein Treffer“-Einträge werden nach der Änderung erneut versucht."""
    cache_dir = tmp_path / "cache"
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_404())],
        settings=ArtOnlineSettings(cache_dir=cache_dir,
                                   providers=("coverartarchive",)))
    library = [(AlbumQuery(album="Kein Treffer", artist="Niemand", year=1999,
                           mbid="rg-x"), 11)]
    w, _rows = build_wanted(tmp_path, service, library)

    async def run():
        await w.backfill()
        first = await w.run_pass(reason="erster Lauf")
        calls_after_first = len(fetch.calls)

        # Neuer API-Key in der Konfiguration: die Kennung ändert sich.
        service.settings = ArtOnlineSettings(
            cache_dir=cache_dir, providers=("coverartarchive",),
            audiodb_key="key-aus-der-konfiguration")
        assert service.settings.fingerprint() != first["fingerprint"]
        second = await w.run_pass(reason="nach Key")
        return first, second, calls_after_first

    first, second, calls_after_first = _run(run())
    assert first["miss"] == 1
    assert second["settings_changed"] is True and second["rearmed"] == 1
    assert second["checked"] == 1, "der frühere Fehlschlag muss erneut laufen"
    assert len(fetch.calls) > calls_after_first, "erzwungene Suche = neue Anfrage"
    entry = w.store.all_entries()[0]
    assert entry.attempts == 2, "der zweite Versuch wird gezählt"
    assert entry.fingerprint == service.settings.fingerprint()
    # Ein dritter Durchlauf ohne Änderung bleibt ruhig.
    third = _run(w.run_pass(reason="ohne Änderung"))
    assert third["checked"] == 0 and third["settings_changed"] is False


def test_rearm_only_touches_stale_fingerprints(tmp_path):
    store = wanted.WantedStore(tmp_path)
    fresh = AlbumQuery(album="Frisch", artist="A", year=2000)
    old = AlbumQuery(album="Alt", artist="B", year=2001)
    store.add(fresh, fingerprint="neu")
    store.add(old, fingerprint="alt")
    store.mark_miss(fresh.key(), reason="kein passender Treffer", fingerprint="neu")
    store.mark_miss(old.key(), reason="kein passender Treffer", fingerprint="alt")

    assert store.rearm_stale("neu") == 1
    old_entry, fresh_entry = store.get(old.key()), store.get(fresh.key())
    assert old_entry is not None and old_entry.status == wanted.STATUS_OPEN
    assert fresh_entry is not None and fresh_entry.status == wanted.STATUS_MISS
    assert store.rearm_stale("neu") == 0, "idempotent"


def test_fingerprint_never_contains_the_key(tmp_path):
    conf = ArtOnlineSettings(cache_dir=tmp_path, audiodb_key="geheim-1234",
                             fanart_key="auch-geheim")
    fp = conf.fingerprint()
    assert "geheim" not in fp and len(fp) == 16
    base = ArtOnlineSettings(cache_dir=tmp_path).fingerprint()
    assert fp != base
    # Sprache/Land und Anbieter-Reihenfolge zählen ebenfalls.
    assert ArtOnlineSettings(cache_dir=tmp_path, language="de").fingerprint() != base
    assert ArtOnlineSettings(cache_dir=tmp_path,
                             providers=("fanart", "musicbrainz")).fingerprint() != base


def test_gui_settings_change_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    """Ein im GUI gespeicherter Key wirkt sofort — ohne Serverneustart.

    Der Dienst wird beim Serverstart einmal konfiguriert; ``refresh_settings``
    liest die Prefs bei jedem Durchlauf/Status neu (live gefunden: ohne das
    blieb ein neuer Key bis zum Neustart wirkungslos).
    """
    cache_dir = tmp_path / "cache"
    set_pref("artworkOnlineCacheDir", str(cache_dir))
    set_pref("artworkOnlineProviders", "coverartarchive")
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_404())],
        settings=ArtOnlineSettings(cache_dir=cache_dir,
                                   providers=("coverartarchive",)))
    # Der Dienst der Verdrahtung ist der prozessweite (nur er zieht Prefs nach).
    monkeypatch.setattr(art_online, "_service", service)
    w, _rows = build_wanted(tmp_path, service,
                            [(AlbumQuery(album="Kein Treffer", artist="N",
                                         year=1999, mbid="rg-k"), 2)])

    async def first():
        await w.backfill()
        return await w.run_pass(reason="vor der Änderung")

    assert _run(first())["miss"] == 1
    assert w.status()["settings_changed"] is False

    # Jetzt speichert das GUI einen neuen API-Key und eine andere Sprache.
    set_pref("artworkOnlineAudioDbKey", "key-aus-dem-gui")
    set_pref("artworkOnlineLanguage", "de")

    refreshed = w.refresh_from_prefs()
    status = w.status()
    summary = _run(w.run_pass(reason="nach der Änderung"))

    assert refreshed is True
    assert w.service.settings.audiodb_key == "key-aus-dem-gui"
    assert w.service.settings.language == "de"
    assert status["settings_changed"] is True
    assert summary["settings_changed"] is True and summary["rearmed"] == 1
    assert summary["checked"] == 1, "der frühere Fehlschlag läuft erneut"


def test_note_settings_change_triggers_only_on_a_real_change(tmp_path, monkeypatch):
    """``note_settings_change``: Anstoss nur bei geänderter Kennung."""
    cache_dir = tmp_path / "cache"
    set_pref("artworkOnlineCacheDir", str(cache_dir))
    set_pref("artworkOnlineProviders", "coverartarchive")
    service, _fetch = build_service(tmp_path, [], settings=ArtOnlineSettings(
        cache_dir=cache_dir, providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [])
    # Welche Einstellungen der Dienst hat, entscheidet der Prozess-Dienst.
    set_pref("artworkOnlineLanguage", "en")
    monkeypatch.setattr(art_online, "_service", service)
    monkeypatch.setattr(wanted, "_service", w)
    w.refresh_from_prefs()
    before = w.service.settings.fingerprint()
    # Ein Eintrag aus der alten Konfiguration (so sieht eine echte Liste aus).
    assert _run(_async_add(w.store, before)) is True

    async def call_note() -> tuple[bool, bool]:
        # Der Settings-Handler ruft das aus der Event-Loop (wie hier).
        return (wanted.note_settings_change(before), wanted.note_settings_change(before))

    unchanged, _again = _run(call_note())
    assert unchanged is False, "ohne Änderung kein Anstoss"

    set_pref("artworkOnlineLanguage", "de")

    async def call_note_after() -> bool:
        return wanted.note_settings_change(before)

    assert _run(call_note_after()) is True
    assert w.service.settings.language == "de"
    status = w.status()
    assert status["settings_changed"] is True
    assert status["stale_entries"] == 1, "der alte Eintrag stammt aus der alten Konfiguration"


# ---------------------------------------------------------------------------
# 5. Bibliothek lesen (nur lesen, kein Schema-Umbau)
# ---------------------------------------------------------------------------


def test_library_query_returns_albums_without_cover(tmp_path):
    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                bonfire = Contributor(namespell="bonfire", name="Bonfire",
                                      sortname="bonfire")
                va = Contributor(namespell="various artists",
                                 name="Various Artists",
                                 sortname="various artists")
                session.add_all([bonfire, va])
                await session.flush()
                albums = {
                    "cover": Album(titlesort="hat cover", title="Hat Cover",
                                   albumartist_sort="bonfire", year=1990,
                                   artwork="/tmp/cover.jpg"),
                    "point": Album(titlesort="point blank", title="Point Blank",
                                   albumartist_sort="bonfire", year=1989),
                    "mix": Album(titlesort="mix", title="Mix",
                                 albumartist_sort="various artists", year=2010),
                    "unknown": Album(titlesort="unknown album",
                                     title="Unknown Album",
                                     albumartist_sort="", year=None),
                }
                session.add_all(list(albums.values()))
                await session.flush()
                # Album-Contributor direkt setzen (kein Lazy-Load im Test).
                await session.execute(albums_contributors.insert(), [
                    {"album": albums["point"].id, "contributor": bonfire.id,
                     "role": 1},
                    {"album": albums["mix"].id, "contributor": bonfire.id,
                     "role": 1},
                    {"album": albums["mix"].id, "contributor": va.id, "role": 1},
                    {"album": albums["cover"].id, "contributor": bonfire.id,
                     "role": 1},
                ])
                await session.commit()
            return await wanted.library_albums_without_cover()
        finally:
            await close_db()

    rows = _run(run())
    titles = {q.album: (q.artist, q.year) for q, _id in rows}
    assert "Point Blank" in titles and titles["Point Blank"] == ("Bonfire", 1989)
    # Various-Artists-Sampler sucht als „Various Artists“ (so führt MusicBrainz sie).
    assert titles["Mix"] == (wanted.VA_SEARCH_ARTIST, 2010)
    assert "Hat Cover" not in titles, "Alben mit Cover gehören nicht in die Liste"
    assert "Unknown Album" not in titles, "kein Platzhalter-Album suchen"


def test_search_artist_prefers_the_album_contributor():
    assert wanted.search_artist_for("bonfire", "Bonfire", "Etwas anderes") == "Bonfire"
    assert wanted.search_artist_for("bonfire", None, "Bonfire") == "Bonfire"
    assert wanted.search_artist_for("bonfire", None, None) == "bonfire"
    assert wanted.search_artist_for("various artists", "The Elite U.F.O", None) \
        == wanted.VA_SEARCH_ARTIST


def test_scan_end_records_the_list_without_a_background_task(tmp_path, monkeypatch):
    """``record_missing_albums`` trägt ein und startet keinen Dienst."""
    service, _fetch = build_service(tmp_path, [])
    library = [(AlbumQuery(album="Aus dem Scan", artist="A", year=2020), 5)]
    monkeypatch.setattr(art_online, "_service", service)
    monkeypatch.setattr(wanted, "library_albums_without_cover",
                        lambda: asyncio.sleep(0, result=list(library)))
    wanted.reset_wanted_service()

    async def run():
        first = await wanted.record_missing_albums(source="scan")
        second = await wanted.record_missing_albums(source="startup")
        w = wanted.current_wanted_service()
        return first, second, w.store.counts(), w.running

    first, second, counts, running = _run(run())
    assert first == 1 and second == 0        # idempotent
    assert counts == {wanted.STATUS_OPEN: 1}
    assert running is False, "der Aufruf darf keinen Hintergrund-Task anlegen"


def test_scanner_scan_end_triggers_the_recording(tmp_path, monkeypatch):
    """``_finalize_scan`` nimmt die Alben ohne Cover auf (Scan-Ende)."""
    import lyrion.media.scanner as scanner_mod

    calls: list[str] = []

    async def fake_record(*, source: str = "scan") -> int:
        calls.append(source)
        return 3

    monkeypatch.setattr(wanted, "record_missing_albums", fake_record)
    scanner = scanner_mod.MediaScanner(config=scanner_mod.ScanConfig(
        base_path=tmp_path, generate_artwork=True))
    _run(scanner._finalize_scan([]))
    assert calls == ["scan"]


# ---------------------------------------------------------------------------
# 6. Web-GUI: Anzeige + Knopf (HTTP, ohne die Seite zu blockieren)
# ---------------------------------------------------------------------------


@pytest.fixture
def gui(tmp_path, monkeypatch):
    """Dienst in die Modul-Singletons hängen (die GUI nutzt sie genau so)."""
    cache_dir = tmp_path / "cache"
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())],
                                   settings=ArtOnlineSettings(cache_dir=cache_dir))
    library = [(AlbumQuery(album="GUI Album", artist="A", year=2020, mbid="rg-g"), 3)]
    monkeypatch.setattr(art_online, "_service", service)
    wanted.reset_wanted_service()
    w, rows = build_wanted(tmp_path, service, library)
    monkeypatch.setattr(wanted, "_service", w)
    return w, fetch, rows


def _request(method: str, path: str, *, data=None, settle: float = 0.0):
    """ASGI-Anfrage gegen ``create_app()`` (Muster ``test_art_online.py``)."""
    from lyrion.web.app import create_app

    async def run():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            response = await client.request(method, path, data=data)
            if settle:
                await asyncio.sleep(settle)      # Hintergrund-Task arbeiten lassen
            return response

    return asyncio.run(run())


def test_gui_page_shows_state_and_button(gui):
    res = _request("GET", "/settings/server/artworkonline.html")
    body = res.text
    assert res.status_code == 200
    assert 'id="artWantedOpen"' in body and 'id="artWantedHit"' in body
    assert 'id="artWantedMiss"' in body and 'id="artWantedLastRun"' in body
    assert 'id="artWantedRunBtn"' in body
    assert "/settings/artworkonline/status.json" in body
    assert "/settings/artworkonline/run" in body
    # Die bestehenden artworkOnline*-Einstellungen stehen weiter im Formular.
    for pref in art_online.PREF_DEFAULTS:
        assert f'name="pref_{pref}"' in body, pref
    assert 'name="pref_artworkOnlineWantedAuto"' in body


def test_gui_status_endpoint_reports_the_list(gui):
    w, _fetch, _rows = gui
    _run(w.backfill(source="startup"))
    res = _request("GET", "/settings/artworkonline/status.json")
    payload = json.loads(res.text)
    assert res.status_code == 200 and payload["ok"] is True
    assert payload["open"] == 1 and payload["hit"] == 0 and payload["miss"] == 0
    assert payload["providers"] == ["musicbrainz", "coverartarchive"]
    assert "last_run" in payload and "last_pass" in payload


def test_gui_button_triggers_a_new_pass(gui):
    """``POST …/run`` stösst an; der Fortschritt kommt über ``status.json``."""
    w, fetch, rows = gui
    _run(w.backfill(source="startup"))
    before = json.loads(_request("GET", "/settings/artworkonline/status.json").text)
    assert before["open"] == 1 and before["hit"] == 0

    started = json.loads(_request("POST", "/settings/artworkonline/run",
                                  settle=0.2).text)
    assert started["started"] is True

    after = json.loads(_request("GET", "/settings/artworkonline/status.json").text)
    assert after["hit"] == 1 and after["open"] == 0
    assert after["last_run"] and after["last_pass"]["hit"] == 1
    assert fetch.calls, "der Knopf muss eine echte Suche auslösen"
    assert rows and rows[0][0] == "GUI Album", "Albumzeile wird nachgetragen"


def test_gui_keeps_the_existing_settings_and_new_ones_are_saved(gui):
    res = _request("POST", "/settings/server/artworkonline.html", data={
        "saveSettings": "1",
        "pref_artworkOnlineProviders": "coverartarchive",
        "pref_artworkOnlineWantedAuto": "0",
        "pref_artworkOnlineWantedPerPass": "50",
    })
    assert res.status_code == 200
    from lyrion.web import settings as settings_module

    assert settings_module.load_art_online_settings().providers == ("coverartarchive",)
    values = settings_module.art_online_wanted_pref_values()
    assert values["artworkOnlineWantedAuto"] == "0"
    assert values["artworkOnlineWantedPerPass"] == "50"


def test_gui_rejects_an_invalid_per_pass_value(gui):
    before = get_prefs().get("artworkOnlineWantedPerPass")
    res = _request("POST", "/settings/server/artworkonline.html", data={
        "saveSettings": "1", "pref_artworkOnlineWantedPerPass": "viele"})
    assert res.status_code == 200 and "viele" in res.text
    assert get_prefs().get("artworkOnlineWantedPerPass") == before


# ---------------------------------------------------------------------------
# 7. Wiedergabepfad: nur Platte/DB, kein Netz
# ---------------------------------------------------------------------------


def test_cover_delivery_uses_no_network_even_with_a_full_list(tmp_path, monkeypatch):
    """Auslieferung eines gecachten Covers — kein einziger HTTP-Aufruf."""
    from lyrion.web import app as app_mod

    data = jpeg_bytes(500)
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    query = AlbumQuery(album="Point Blank", artist="Bonfire", year=1989)
    service.cache.write_cover_sync(query.key(), query, data,
                                   provider="coverartarchive", mime="image/jpeg",
                                   source_url="https://coverartarchive.org/x",
                                   mbid="e104643d", width=500, height=500)
    # Liste offen (das Album hat noch keine Albumzeile im Test-DB-Bild).
    store = wanted.WantedStore(service.settings.cache_dir)
    store.add(AlbumQuery(album="Anderes Fehlt", artist="B", year=2000))
    calls_before = len(fetch.calls)

    async def run():
        await init_db(tmp_path / "lyrion.db")
        try:
            async with db_session() as session:
                album = Album(titlesort="point blank", title="Point Blank",
                              albumartist_sort="bonfire", year=1989, artwork=None)
                session.add(album)
                await session.commit()
                album_id = int(album.id)
            monkeypatch.setattr(art_online, "_service", service)
            got, mime = await app_mod._online_cover_for_album(album_id, None)
            return got, mime
        finally:
            await close_db()

    got, mime = _run(run())
    assert got == data and mime == "image/jpeg"
    assert len(fetch.calls) == calls_before, "kein Netzzugriff im Auslieferungspfad"
    assert store.counts() == {wanted.STATUS_OPEN: 1}


def test_service_reads_the_cache_without_a_fetcher(tmp_path):
    service, fetch = build_service(tmp_path, [])
    query = AlbumQuery(album="Nur Platte", artist="A", year=2001)
    service.cache.write_cover_sync(query.key(), query, jpeg_bytes(300),
                                   provider="coverartarchive", mime="image/jpeg",
                                   source_url="https://x", mbid=None,
                                   width=300, height=300)
    result = service.read_cached(query)
    assert result is not None and result.path.is_file()
    assert service.read_cached_album("Nur Platte", "A", 2001) is not None
    assert fetch.calls == []


# ---------------------------------------------------------------------------
# 8. Dienst-Lebenszyklus (Task, Stopp, Start ohne Scan)
# ---------------------------------------------------------------------------


def test_background_task_runs_off_the_list_without_a_scan(tmp_path):
    """Der Dienst arbeitet ohne Scan: Start → Durchlauf → Stopp."""
    from lyrion.media import art_online_wanted as wanted_mod

    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    library = [(AlbumQuery(album="Ohne Scan", artist="A", year=2020, mbid="rg-s"), 8)]
    store = wanted.WantedStore(service.settings.cache_dir)
    assert store.add(AlbumQuery(album="Ohne Scan", artist="A", year=2020,
                                mbid="rg-s"), source="startup") is True

    async def run():
        w = wanted_mod.WantedService(
            service, store=store,
            library=lambda: asyncio.sleep(0, result=list(library)),
            entry_pause=0, idle_sleep=0.05, first_pass_delay=0)
        started = await w.start()
        assert started is True and w.running is True
        deadline = time.time() + 5
        while time.time() < deadline and store.counts().get("hit", 0) == 0:
            await asyncio.sleep(0.01)
        counts = store.counts()
        await w.stop()
        return counts, w.running

    counts, running = _run(run())
    assert counts == {wanted.STATUS_HIT: 1}
    assert running is False
    assert fetch.calls, "der Hintergrund-Dienst muss wirklich gesucht haben"


def test_auto_pref_off_keeps_the_service_stopped(tmp_path, monkeypatch):
    service, _fetch = build_service(tmp_path, [])
    monkeypatch.setattr(wanted, "art_online_wanted_pref_values",
                        lambda: {"artworkOnlineWantedAuto": "0"}, raising=False)
    monkeypatch.setattr("lyrion.web.settings.art_online_wanted_pref_values",
                        lambda: {"artworkOnlineWantedAuto": "0"})
    w = wanted.WantedService(service, library=lambda: asyncio.sleep(0, result=[]),
                             first_pass_delay=0, idle_sleep=0.05)

    async def run():
        started = await w.start(backfill=False)
        running = w.running
        await w.stop()
        return started, running

    assert _run(run()) == (False, False)


def test_pass_is_skipped_while_a_library_scan_runs(tmp_path, monkeypatch):
    """Während eines fremden Scans wartet der Dienst (Import.pm:812-826)."""
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    w, _rows = build_wanted(tmp_path, service,
                            [(AlbumQuery(album="Warten", artist="A", year=2020,
                                         mbid="rg-w"), 4)])
    monkeypatch.setattr(wanted, "_scan_is_running", lambda: True)

    async def run():
        await w.backfill()
        summary = await w.run_pass(reason="scan läuft")
        return summary, w.status()

    summary, status = _run(run())
    assert summary.get("skipped") == "scan" and summary["checked"] == 0
    assert fetch.calls == [], "während des Scans keine Netzabrufe"
    assert w.store.counts() == {wanted.STATUS_OPEN: 1}, "Eintrag bleibt offen"
    assert status["last_reason"] == "Scan läuft"


def test_disabled_search_does_not_touch_the_box(tmp_path):
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache", enabled=False))
    w, _rows = build_wanted(tmp_path, service,
                            [(AlbumQuery(album="Aus", artist="A", year=2020), 1)])

    async def run():
        await w.backfill()
        return await w.run_pass(reason="aus")

    summary = _run(run())
    assert summary.get("skipped") == "disabled"
    assert fetch.calls == []


def test_per_pass_limit_bounds_one_run(tmp_path):
    library = [(AlbumQuery(album=f"Album {i}", artist="A", year=2000 + i,
                           mbid=f"rg-{i}"), i) for i in range(4)]
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    w, _rows = build_wanted(tmp_path, service, library, per_pass=2)

    async def run():
        await w.backfill()
        first = await w.run_pass(reason="begrenzt")
        second = await w.run_pass(reason="weiter")
        return first, second

    first, second = _run(run())
    assert first["checked"] == 2 and second["checked"] == 2
    assert w.store.counts() == {wanted.STATUS_HIT: 4}
    assert len([c for c in fetch.calls if "coverartarchive" in c]) >= 4


def test_status_uses_stored_last_run_after_a_restart(tmp_path):
    """„letzter Lauf“ steht in der Datei — der Neustart verliert ihn nicht."""
    service, _fetch = build_service(tmp_path, [])
    store = wanted.WantedStore(service.settings.cache_dir)
    store.meta_set(wanted.META_LAST_RUN, "1700000000")
    store.meta_set(wanted.META_LAST_PASS, json.dumps({"checked": 5, "hit": 2}))
    fresh = wanted.WantedService(service, store=wanted.WantedStore(
        service.settings.cache_dir), library=lambda: asyncio.sleep(0, result=[]))
    status = fresh.status()
    assert status["last_run"] == 1700000000
    assert status["last_pass"] == {"checked": 5, "hit": 2}
    assert status["running"] is False


# ---------------------------------------------------------------------------
# 7. Vorrang: das Album des gerade laufenden Titels
# ---------------------------------------------------------------------------

PLAYER_MAC = "1c:87:2c:47:fc:36"


@pytest.fixture(autouse=True)
def _library_engine_after_each_test():
    """Die Bibliotheks-Engine nach jedem Test schliessen (sie bleibt offen).

    ``_track_album_library`` legt die Bibliothek an und lässt sie **offen** —
    der Vorrang liest sie während des Durchlaufs über ``db_session()``; ein
    vorher geschlossener Motor liesse ``albums_for_tracks`` leer laufen.
    """
    yield
    asyncio.run(close_db())


def _track_album_library(tmp_path):
    """Bibliothek: ein Album **ohne** Cover und eines mit, je ein Track.

    Track und Album hängen wie in der echten Bibliothek über ``tracks_albums``
    zusammen (``lyrion/database/schema.py`` — ``tracks`` hat keine
    ``album``-Spalte, Perls ``tracks.album`` heisst hier die Verknüpfung).
    Die Engine bleibt offen (Aufräumen: ``_library_engine_after_each_test``).
    """
    from lyrion.database.schema import Track, tracks_albums

    async def run():
        await init_db(tmp_path / "lyrion.db")
        async with db_session() as session:
            bonfire = Contributor(namespell="bonfire", name="Bonfire",
                                  sortname="bonfire")
            session.add(bonfire)
            await session.flush()
            without = Album(titlesort="point blank", title="Point Blank",
                            albumartist_sort="bonfire", year=1989,
                            musicbrainz_id="rg-point")
            with_cover = Album(titlesort="hat cover", title="Hat Cover",
                               albumartist_sort="bonfire", year=1990,
                               artwork="/tmp/cover.jpg",
                               musicbrainz_id="rg-hat")
            session.add_all([without, with_cover])
            await session.flush()
            await session.execute(albums_contributors.insert(), [
                {"album": without.id, "contributor": bonfire.id, "role": 1},
                {"album": with_cover.id, "contributor": bonfire.id,
                 "role": 1},
            ])
            t_without = Track(titlesort="point blank", title="Point Blank",
                              url="file:///music/pb/01.flac")
            t_with = Track(titlesort="hat cover", title="Hat Cover",
                           url="file:///music/hc/01.flac")
            session.add_all([t_without, t_with])
            await session.flush()
            await session.execute(tracks_albums.insert(), [
                {"track": t_without.id, "album": without.id},
                {"track": t_with.id, "album": with_cover.id},
            ])
            await session.commit()
            return {"album_without": int(without.id),
                    "album_with": int(with_cover.id),
                    "track_without": int(t_without.id),
                    "track_with": int(t_with.id)}

    return _run(run())


def _playing_player(track_id, *, mac=PLAYER_MAC, mode="play"):
    """Einen laufenden Spieler in den Prozess-Manager setzen (kein Netzzugang)."""
    from lyrion.player.manager import PlayerManager
    from lyrion.player.state import PlayerState

    player = PlayerState(mac=mac, name="Taverne", ip="192.168.1.130",
                         port=57536, model="squeezeplay", model_name="SB Player",
                         connected=True, power=True, mode=mode,
                         current_track_id=track_id,
                         playlist=[track_id], playlist_position=0,
                         playlist_total=1)
    manager = PlayerManager()
    manager.players = {mac: player}
    return manager, player


def _album_artwork(album_id: int) -> str:
    """``albums.artwork`` einer Zeile lesen — roher Beleg des Nachtrags."""
    from sqlalchemy import text

    async def _do() -> str:
        async with db_session() as session:
            return str((await session.execute(
                text("SELECT artwork FROM albums WHERE id = :i"),
                {"i": int(album_id)})).scalar() or "")

    return _run(_do())


def _capture_newmetadata():
    """``playlist newmetadata``-Meldungen mitschneiden (Perls Notify-Weg)."""
    from lyrion.control import notifications

    # Ein Pump-Task aus einem früheren Test hängt an dessen (geschlossenem)
    # Loop; ``notify_from_array`` legt dann keinen neuen an und die Warteschlange
    # bliebe stehen.  ``reset()`` räumt Warteschlange, Zuhörer und Task auf.
    notifications.reset()
    seen: list[tuple] = []

    def _capture(note):
        seen.append((note.client_id, list(note.verbs)))

    notifications.subscribe(_capture)
    return notifications, seen, _capture


def test_playing_album_is_searched_first_and_the_queue_keeps_going(tmp_path):
    """Vorrang wirkt: das laufende Album zuerst, die übrigen danach.

    Perl-Beleg für den Anzeige-Weg: ``Slim/Plugin/RadioArtwork/Plugin.pm:337``
    (``notifyFromArray($c, ['newmetadata'])``, sobald ein Cover nachträglich
    eintrifft).
    """
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    library = [
        (AlbumQuery(album="Zuerst", artist="A", year=2001, mbid="rg-a"), 101),
        (AlbumQuery(album="Point Blank", artist="Bonfire", year=1989,
                    mbid="rg-point"), ids["album_without"]),
        (AlbumQuery(album="Zuletzt", artist="Z", year=2002, mbid="rg-z"), 103),
    ]
    w, rows = build_wanted(tmp_path, service, library, write_rows=True)
    _manager, _player = _playing_player(ids["track_without"])
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="playing")
        await asyncio.sleep(0.05)          # Notify-Pumpe laufen lassen
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["checked"] == 3, "der Vorrang darf die übrigen nicht verdrängen"
    assert summary["hit"] == 3
    # Reihenfolge: das laufende Album stand NICHT vorne, wurde aber zuerst bearbeitet.
    assert rows and rows[0][0] == "Point Blank"
    assert w.status()["priority"] == ["Point Blank"]
    # Die gespielte Albumzeile trägt das Cover wirklich (roher Beleg, dass die
    # Anzeige danach ein neues ``artwork_track_id`` liefert).
    assert _album_artwork(ids["album_without"]) == rows[0][2]
    # Anzeige: 'newmetadata' an genau den laufenden Spieler — Perl schickt die
    # einverbige Form (``notifyFromArray($c, ['newmetadata'])``,
    # ``Slim/Plugin/RadioArtwork/Plugin.pm:337``); ``[['playlist','newmetadata']]``
    # ist Perls *Filter* (eine Gruppe, zwei Alternativen, ``Request.pm:2381-2395``)
    # und trifft sie mit.
    assert (PLAYER_MAC, ["newmetadata"]) in seen
    assert [n for n in seen if n[1] == ["newmetadata"]] == [
        (PLAYER_MAC, ["newmetadata"])], \
        "nur das laufende Album meldet die Anzeige nach"
    # Kein Aushungern: die beiden anderen Alben wurden ebenfalls gesucht.
    assert len([c for c in fetch.calls if "coverartarchive" in c]) == 3


def test_playing_album_with_cover_is_not_prioritised(tmp_path):
    """„Nur bei echtem Bedarf“: ein Album mit Cover bekommt keinen Vorrang."""
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, rows = build_wanted(tmp_path, service, [
        (AlbumQuery(album="Anderes", artist="A", year=2001, mbid="rg-a"), 1)])
    _manager, _player = _playing_player(ids["track_with"])
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="playing")
        await asyncio.sleep(0.05)
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["checked"] == 1 and w.status()["priority"] == []
    assert [r[0] for r in rows] == ["Anderes"]
    assert seen == [], "ohne laufendes Album ohne Cover keine Meldung"


def test_priority_request_is_synchronous_and_does_no_io(tmp_path):
    """Der Wiedergabepfad blockiert nie: kein Netz, kein Bibliotheks-Zugriff."""
    service, fetch = build_service(tmp_path, [("coverartarchive.org", caa_hit())])
    w, _rows = build_wanted(tmp_path, service, [])

    def _no_db(*_a, **_k):        # jeder Bibliotheks-Zugriff wäre ein Fehler
        raise AssertionError("der Vorrang darf die Bibliothek nicht anfassen")

    w._library = _no_db
    started = time.time()
    assert w.request_priority(PLAYER_MAC, 4711) is True
    assert time.time() - started < 0.05
    assert fetch.calls == [], "kein Netzaufruf im Wiedergabepfad"
    assert w.status()["priority_pending"] is True


def test_same_track_played_again_does_not_search_twice(tmp_path):
    """Derselbe Titel mehrfach ⇒ ein Eintrag, ein Netzaufruf."""
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [], write_rows=True)
    _manager, _player = _playing_player(ids["track_without"])

    async def run():
        first = await w.run_pass(reason="playing")
        calls_after_first = len(fetch.calls)
        # Nochmal „abspielen“ (derselbe Titel) — kein zweiter Netzaufruf.
        w.request_priority(PLAYER_MAC, ids["track_without"])
        second = await w.run_pass(reason="playing-again")
        return first, second, calls_after_first

    first, second, calls_after_first = _run(run())
    assert first["hit"] == 1 and first["checked"] == 1
    assert second["checked"] == 0, "das Album hat jetzt ein Cover — nichts zu tun"
    assert len(fetch.calls) == calls_after_first
    assert len(w.store.all_entries()) == 1


def test_playing_album_with_a_cached_hit_but_empty_row_gets_the_cover(tmp_path):
    """Treffer im Plattencache, Album-Zeile leer (zweite Zeile, gleicher Schlüssel).

    Der Listenstatus ``hit`` heisst „Cover liegt im Plattencache“ — **nicht**
    „diese Album-Zeile hat es schon“.  Genau das passiert in der echten
    Bibliothek, wenn zwei Album-Zeilen denselben (normalisierten) Schlüssel
    bilden: die gespielte Zeile ist leer, der Eintrag ein Treffer.  Sie darf
    deshalb nicht übersprungen werden; ``_process`` trägt das Cover **ohne
    Netz** aus dem Plattencache nach und meldet die Anzeige nach.
    """
    from sqlalchemy import text

    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [], write_rows=True)
    _run(w.run_pass(reason="erster Durchlauf"))   # sucht und trägt ein

    async def _clear() -> None:
        async with db_session() as session:
            await session.execute(text(
                "UPDATE albums SET artwork = '', artwork_front = '' WHERE id = :i"),
                {"i": ids["album_without"]})
            await session.commit()

    assert _album_artwork(ids["album_without"]), \
        "erster Durchlauf hat die Albumzeile gesetzt"
    _run(_clear())                                # „zweite Zeile“: leer, Treffer im Cache
    calls = len(fetch.calls)
    _manager, _player = _playing_player(ids["track_without"])
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="playing")
        await asyncio.sleep(0.05)
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["hit"] == 1 and w.status()["priority"] == ["Point Blank"]
    assert len(fetch.calls) == calls, "Treffer aus dem Plattencache — kein Netz"
    assert _album_artwork(ids["album_without"]), \
        "die gespielte Albumzeile trägt jetzt das Cover"
    assert seen == [(PLAYER_MAC, ["newmetadata"])]


def _add_twin_album(title: str = "Point Blank !") -> dict:
    """Zweite Album-Zeile mit demselben normalisierten Titel (gleicher Schlüssel).

    Genau der Fall der gewachsenen Bibliothek: „Kill em All“ und „Kill 'em all“
    sind zwei Zeilen, aber ein Suchschlüssel (``AlbumQuery.key`` normalisiert).
    """
    from lyrion.database.schema import Track, tracks_albums

    async def run():
        async with db_session() as session:
            album = Album(titlesort=title.lower(), title=title,
                          albumartist_sort="bonfire", year=1989,
                          musicbrainz_id="rg-point")
            track = Track(titlesort=title.lower(), title=title,
                          url="file:///music/pb/02.flac")
            session.add_all([album, track])
            await session.flush()
            await session.execute(tracks_albums.insert(),
                                  [{"track": track.id, "album": album.id}])
            await session.commit()
            return {"album_twin": int(album.id), "track_twin": int(track.id)}

    return _run(run())


def test_duplicate_album_rows_share_the_key_and_still_notify(tmp_path):
    """Zwei Album-Zeilen, ein Schlüssel: die gespielte leere Zeile bekommt das
    Cover und die Anzeige wird gemeldet.

    Der Eintrag gehört der **anderen** Zeile (``entry.album_id``), das Cover
    liegt schon im Plattencache.  Gespielt wird die leere Zeile: sie ist das
    Album ohne Cover, ihr Cover ist das gesetzte — die Meldung darf deshalb
    nicht an der abweichenden Album-ID scheitern.
    """
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [
        (AlbumQuery(album="Point Blank", artist="Bonfire", year=1989,
                    mbid="rg-point"), ids["album_without"])], write_rows=True)
    _run(w.run_pass(reason="erster Durchlauf"))       # Eintrag + Plattencache
    twin = _add_twin_album()
    assert twin["album_twin"] != ids["album_without"]
    assert _album_artwork(ids["album_without"]), "erste Zeile hat das Cover"
    assert not _album_artwork(twin["album_twin"]), "die zweite Zeile ist leer"
    entry = w.store.get(AlbumQuery(album="Point Blank", artist="Bonfire",
                                   year=1989).key())
    assert entry is not None and entry.album_id == ids["album_without"], \
        "der Eintrag gehört der ersten Zeile"

    calls = len(fetch.calls)
    _manager, _player = _playing_player(twin["track_twin"])
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="playing")
        await asyncio.sleep(0.05)
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["hit"] == 1
    assert len(fetch.calls) == calls, "Cover liegt im Plattencache — kein Netz"
    assert _album_artwork(twin["album_twin"]), \
        "die gespielte zweite Zeile trägt jetzt das Cover"
    assert seen == [(PLAYER_MAC, ["newmetadata"])]


def test_radio_stream_never_gets_priority(tmp_path):
    """Radio unverändert: ein Strom hat keine Track-ID.

    Also kein Vorrang (die Liste läuft normal weiter) und keine Anzeige-Meldung
    über ein Album-Cover — die Anzeige eines Streams hängt an den Metadaten des
    Stroms (``STMu``/``httpCover``), nicht an ``albums.artwork``.
    """
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, rows = build_wanted(tmp_path, service, [
        (AlbumQuery(album="Point Blank", artist="Bonfire", year=1989,
                    mbid="rg-point"), ids["album_without"])])
    _manager, player = _playing_player(None)
    player.remote = 1
    player.current_url = "http://regiocast.streamabc.net/x.mp3"
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="radio")
        await asyncio.sleep(0.05)
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["checked"] == 1 and summary["hit"] == 1
    assert w.status()["priority"] == []
    assert [r[0] for r in rows] == ["Point Blank"]
    assert seen == [], "ein Radio-Strom bekommt keine Album-Cover-Meldung"


def test_track_change_mid_pass_is_inserted_next(tmp_path):
    """Ein Titelwechsel während des Durchlaufs wird sofort eingeschoben."""
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    library = [
        (AlbumQuery(album="Eins", artist="A", year=2001, mbid="rg-1"), 101),
        (AlbumQuery(album="Point Blank", artist="Bonfire", year=1989,
                    mbid="rg-point"), ids["album_without"]),
        (AlbumQuery(album="Drei", artist="C", year=2003, mbid="rg-3"), 103),
    ]
    w, rows = build_wanted(tmp_path, service, library)
    _manager, player = _playing_player(None, mode="stop")
    seen: list[str] = []
    original = w._process

    async def _spy(entry, fingerprint, **kwargs):
        seen.append(entry.album)
        if entry.album == "Eins":            # Titel startet mitten im Durchlauf
            player.current_track_id = ids["track_without"]
            player.mode = "play"
        return await original(entry, fingerprint, **kwargs)

    w._process = _spy

    async def run():
        return await w.run_pass(reason="mid")

    summary = _run(run())
    assert len(seen) == 3 and seen[0] != "Point Blank"
    assert seen[1] == "Point Blank", \
        "der Titelwechsel muss als Nächstes eingeschoben werden"
    assert summary["checked"] == 3


def test_cover_arrival_for_another_album_pushes_nothing(tmp_path):
    """Ein Cover für ein fremdes Album meldet der Anzeige nichts.

    Der laufende Titel gehört hier zu einem Album, das **schon** ein Cover hat
    — es gibt also keinen Vorrang und keinen zweiten Cover-Eingang, der die
    Meldung erklären könnte.  Geprüft wird damit die Album-Bindung der Meldung:
    ein Cover für ein fremdes Album darf *nicht* an den laufenden Spieler
    gehen (sonst würde jede Suche jede Anzeige neu laden).
    """
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [
        (AlbumQuery(album="Fremd", artist="A", year=2001, mbid="rg-a"), 99)])
    _manager, _player = _playing_player(ids["track_with"])
    notes, seen, capture = _capture_newmetadata()

    async def run():
        summary = await w.run_pass(reason="other")
        await asyncio.sleep(0.05)
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["hit"] == 1 and seen == []
    assert w.status()["priority"] == [], "ein Album mit Cover bekommt keinen Vorrang"


def test_cover_arrival_reexecutes_the_playing_clients_status(tmp_path, monkeypatch):
    """Nach dem Treffer läuft das Status-Abonnement des Spielers neu — das ist
    die Anzeige-Aktualisierung.

    Perl-Kette: ``notifyFromArray`` (:838-853) → ``notify`` (:2005-2053, der
    Client hört mit) → ``%subscribers``/``autoExecuteFilter``
    (``Slim/Control/Queries.pm:3925-3993``, ``statusQuery_filter`` liefert 1.3)
    → ``__autoexecute`` (:2055-2102) führt die Anfrage des Clients neu aus.
    Im Port ist das der Weg über :func:`_reexecute_status`
    (CLI-``status subscribe:n`` **und** Cometd/Jive-``playerstatus``).
    """
    ids = _track_album_library(tmp_path)
    service, fetch = build_service(
        tmp_path, [("coverartarchive.org", caa_hit())],
        settings=ArtOnlineSettings(cache_dir=tmp_path / "cache",
                                   providers=("coverartarchive",)))
    w, _rows = build_wanted(tmp_path, service, [
        (AlbumQuery(album="Point Blank", artist="Bonfire", year=1989,
                    mbid="rg-point"), ids["album_without"])], write_rows=True)
    _manager, _player = _playing_player(ids["track_without"])
    notes, seen, capture = _capture_newmetadata()
    reexecuted: list[str] = []
    monkeypatch.setattr(notes, "_reexecute_status", reexecuted.append)

    async def run():
        summary = await w.run_pass(reason="playing")
        await asyncio.sleep(0.45)          # Perl: relevant(1.3) - 1 Sekunde
        return summary

    try:
        summary = _run(run())
    finally:
        notes.unsubscribe(capture)

    assert summary["hit"] == 1
    assert seen == [(PLAYER_MAC, ["newmetadata"])]
    # Die Meldung trifft Perls Filter (``[['playlist','newmetadata']]`` = eine
    # Gruppe, zwei Alternativen) und löst damit die Neuausteilung aus.
    assert notes.status_query_filter(
        notes.Notification.from_array(PLAYER_MAC, ["newmetadata"]),
        PLAYER_MAC) > 0
    assert reexecuted == [PLAYER_MAC], \
        "der Status des laufenden Spielers muss neu ausgeführt werden"
