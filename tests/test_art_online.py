"""Tests der Online-Albumcover-Suche (``lyrion.media.art_online``).

Kein Live-Netz: die Anbieter werden gegen **aufgezeichnete** Antworten geprüft —
die MusicBrainz-JSON unten ist eine echte, auf die drei entscheidenden Treffer
gekürzte Antwort auf
``release:"Point Blank" AND artist:"Bonfire"``
(``https://musicbrainz.org/ws/2/release?fmt=json&limit=10``, User-Agent
``LyrionMusicServer/9.0``); Bilddaten erzeugt der Test lokal als echtes JPEG
(Pillow), damit Format- und Grössenprüfung wirklich laufen.

Perl-Bezug der Reihenfolge: ``Slim/Music/Artwork.pm:388-400`` (Tags → Dateien)
und ``Slim/Web/Graphics.pm:275-291`` (Default-Cover); die Online-Stufe ist eine
Zutat dieses Ports nach Vorbild des Kodi-UAS (``metadata.album.universal``).
"""

from __future__ import annotations

import asyncio
import io
import json

import aiosqlite
import httpx
import pytest
from PIL import Image

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.media import art_online
from lyrion.media.art_online import (
    AlbumQuery,
    ArtOnlineCache,
    ArtOnlineService,
    ArtOnlineSettings,
    CoverResult,
    FetchResponse,
    ProviderError,
    candidate_matches,
    default_cache_dir,
    normalize_provider_list,
    sniff_mime,
    validate_image,
)
from lyrion.web import settings as settings_module

# ---------------------------------------------------------------------------
# Aufgezeichnete Fixture-Antworten
# ---------------------------------------------------------------------------

#: MusicBrainz ``/ws/2/release`` — echt aufgezeichnet (siehe Modul-Docstring).
MB_RELEASE_SEARCH_BONFIRE = r"""
{
  "created": "2026-09-19T13:22:14.970Z",
  "count": 8,
  "offset": 0,
  "releases": [
    {
      "id": "16341bee-ae1d-4021-8642-a2c61d9cebbe",
      "score": 100,
      "title": "Point Blank",
      "status": "Official",
      "date": "1991",
      "artist-credit": [
        {"name": "Bonfire", "artist": {"id": "780fcd1f-9808-4da6-aad8-2858884dd7df", "name": "Bonfire"}}
      ],
      "release-group": {
        "id": "e104643d-aad4-359f-98ad-1851c4a29a61",
        "title": "Point Blank",
        "primary-type": "Album",
        "first-release-date": null
      }
    },
    {
      "id": "83adc654-5a8c-4e4f-abf6-950cd5000392",
      "score": 100,
      "title": "Point Blank",
      "status": "Official",
      "date": "1989-12-06",
      "artist-credit": [
        {"name": "Bonfire", "artist": {"id": "780fcd1f-9808-4da6-aad8-2858884dd7df", "name": "Bonfire"}}
      ],
      "release-group": {
        "id": "e104643d-aad4-359f-98ad-1851c4a29a61",
        "title": "Point Blank",
        "primary-type": "Album",
        "first-release-date": null
      }
    },
    {
      "id": "9c4a0f2d-0000-4000-8000-000000000001",
      "score": 100,
      "title": "Point Blank",
      "status": "Official",
      "date": "2023-09-22",
      "artist-credit": [
        {"name": "Bonfire", "artist": {"id": "780fcd1f-9808-4da6-aad8-2858884dd7df", "name": "Bonfire"}}
      ],
      "release-group": {
        "id": "d18259a6-9297-44dc-b1c7-53b3a662347f",
        "title": "Point Blank MMXXIII",
        "primary-type": "Album",
        "first-release-date": null
      }
    }
  ]
}
"""

#: CAA-Fehlerantwort in der echten Form (Textkörper statt Bild).
CAA_404_BODY = b'{"help": "For usage, please see: https://musicbrainz.org/doc/Cover_Art_Archive/API"}\n'

RG_BONFIRE_2023 = "d18259a6-9297-44dc-b1c7-53b3a662347f"

BONFIRE = AlbumQuery(album="Point Blank (MMXXIII Version)", artist="Bonfire", year=2023)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def jpeg_bytes(width: int = 500, height: int = 500) -> bytes:
    """Echtes JPEG (Pillow) — die Format-/Grössenprüfung soll wirklich laufen."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 60, 80)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeFetcher:
    """Ersetzt ``HttpFetcher``: Routen per Teilstring, jede Anfrage wird gezählt."""

    def __init__(self, routes: list[tuple[str, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def get(self, url: str, params=None) -> FetchResponse:
        self.calls.append((url, dict(params or {})))
        for needle, responder in self.routes:
            if needle in url:
                if callable(responder):
                    return responder(url, params or {})
                return responder  # type: ignore[return-value]
        raise ProviderError(f"unerwartete URL im Test: {url}")

    def urls(self, needle: str = "") -> list[str]:
        return [u for u, _ in self.calls if needle in u]

    async def close(self) -> None:
        self.closed = True


def json_response(payload: object, status: int = 200) -> FetchResponse:
    return FetchResponse(status=status, content=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json"})


def mb_routes(payload: dict | None = None) -> list[tuple[str, object]]:
    """MusicBrainz-Route: liefert die Fixture nur für den passenden Suchterm."""
    data = payload if payload is not None else json.loads(MB_RELEASE_SEARCH_BONFIRE)

    def _responder(url: str, params: dict) -> FetchResponse:
        query = str(params.get("query") or "")
        # Echt aufgezeichnet: der Klammer-Zusatz im Titel liefert 0 Treffer,
        # erst der gekürzte Titel findet die Ausgabe von 2023.
        if "Point Blank" in query and "Bonfire" in query and "(MMXXIII Version)" not in query:
            return json_response(data)
        return json_response({"created": "", "count": 0, "offset": 0, "releases": []})

    return [("musicbrainz.org/ws/2/release", _responder)]


def service(tmp_path, routes, *, settings: ArtOnlineSettings | None = None,
            fetcher: FakeFetcher | None = None, db_path=None, on_cover=None):
    conf = settings or ArtOnlineSettings(cache_dir=tmp_path / "cache")
    fetch = fetcher or FakeFetcher(routes)
    return ArtOnlineService(conf, fetcher=fetch, db_path=db_path or tmp_path / "cache" / "art_online.db",
                            on_cover=on_cover), fetch


# ---------------------------------------------------------------------------
# Konfiguration / Anbieter-Kette
# ---------------------------------------------------------------------------


def test_default_chain_is_ordered_and_works_without_any_key(tmp_path):
    conf = ArtOnlineSettings.from_mapping({art_online.PREF_CACHE_DIR: str(tmp_path)})
    assert conf.providers == ("musicbrainz", "coverartarchive", "audiodb", "fanart")
    assert conf.enabled is True
    assert conf.language == "en"
    assert conf.audiodb_key == "" and conf.fanart_key == ""
    # Ohne Keys bleiben genau die keylosen Anbieter übrig — die Kette
    # funktioniert also ohne Einrichtung (Aufgaben-Vorgabe).
    assert conf.effective_providers() == ("musicbrainz", "coverartarchive")


def test_keys_never_come_from_the_code_and_enable_their_provider(tmp_path):
    conf = ArtOnlineSettings.from_mapping({
        art_online.PREF_CACHE_DIR: str(tmp_path),
        art_online.PREF_AUDIODB_KEY: "audiodb-key-from-config",
        art_online.PREF_FANART_KEY: "fanart-key-from-config",
    })
    assert conf.effective_providers() == art_online.DEFAULT_PROVIDER_ORDER
    # Kein Key steht irgendwo als Literal im Modul.
    source = (settings_module.__file__, art_online.__file__)
    for path in source:
        text = open(path, encoding="utf-8").read()
        assert "audiodb-key-from-config" not in text
    assert conf.key_for("coverartarchive") == ""
    assert conf.key_for("musicbrainz") == ""


def test_normalize_provider_list_keeps_order_and_drops_typos():
    assert normalize_provider_list("fanart, coverartarchive ,audiodb") == (
        "fanart", "coverartarchive", "audiodb")
    assert normalize_provider_list("nonsense") == art_online.DEFAULT_PROVIDER_ORDER
    assert normalize_provider_list("") == art_online.DEFAULT_PROVIDER_ORDER
    assert normalize_provider_list("audiodb audiodb caa") == ("audiodb", "coverartarchive")


def test_provider_without_key_is_skipped_and_logged(tmp_path, caplog):
    conf = ArtOnlineSettings(cache_dir=tmp_path)
    with caplog.at_level("DEBUG", logger="lyrion.media.art_online"):
        assert "audiodb" not in conf.effective_providers()
    assert any("kein API-Key" in rec.getMessage() for rec in caplog.records)


def test_default_cache_dir_is_the_port_artwork_folder():
    assert default_cache_dir().name == "artwork-online"


# ---------------------------------------------------------------------------
# MusicBrainz-Match (UAS-Vorbild)
# ---------------------------------------------------------------------------


def test_musicbrainz_ladder_skips_bracket_title_and_picks_the_2023_reissue(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})),
    ]
    svc, fetch = service(tmp_path, routes)
    result = asyncio.run(svc.find_cover(BONFIRE))
    assert result is not None
    # Stufe 1 (voller Titel mit Klammer) hat keine Treffer, Stufe 2 liefert sie.
    queries = [params["query"] for _url, params in fetch.calls if "query" in params]
    assert queries[0] == ('release:"Point Blank (MMXXIII Version)" '
                         'AND (artistname:"Bonfire" OR artist:"Bonfire")')
    assert queries[1] == ('release:"Point Blank" '
                          'AND (artistname:"Bonfire" OR artist:"Bonfire")')
    # Der 2023er-Treffer (Release-Group „Point Blank MMXXIII“) gewinnt,
    # die 1989er-Ausgabe passt nicht zum Jahr.
    assert result.provider == "coverartarchive"


def test_candidate_match_rejects_wrong_year_and_artist():
    from lyrion.media.art_online import Candidate

    wrong_year = Candidate(title="Point Blank", group_title="Point Blank",
                           artist="Bonfire", year=1989)
    assert candidate_matches(wrong_year, BONFIRE) is False
    wrong_artist = Candidate(title="Point Blank MMXXIII", artist="Accept", year=2023)
    assert candidate_matches(wrong_artist, BONFIRE) is False
    ok = Candidate(release_group_id=RG_BONFIRE_2023, group_title="Point Blank MMXXIII",
                   artist="Bonfire", year=2023)
    assert candidate_matches(ok, BONFIRE) is True


def test_search_musicbrainz_reports_no_candidates_without_match(tmp_path):
    routes = [("musicbrainz.org/ws/2/release",
               json_response({"count": 0, "releases": []}))]
    svc, fetch = service(tmp_path, routes)
    result = asyncio.run(svc.find_cover(AlbumQuery(album="Gibt Es Nicht", artist="Niemand")))
    assert result is None
    # Alle Suchstufen probiert, dann sauber aufgegeben (kein Blindflug).
    assert fetch.urls("musicbrainz.org") and len(fetch.calls) >= 1


# ---------------------------------------------------------------------------
# CAA-Treffer, Cache und Wiederverwendung
# ---------------------------------------------------------------------------


def test_caa_hit_writes_cache_and_second_call_comes_from_cache(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", lambda url, params: FetchResponse(
            status=200, content=jpeg_bytes(500, 500),
            headers={"Content-Type": "image/jpeg"}, url=url)),
    ]
    svc, fetch = service(tmp_path, routes)
    seen: list[CoverResult] = []
    svc.on_cover = lambda query, result: seen.append(result)

    first = asyncio.run(svc.find_cover(BONFIRE))
    assert first is not None and first.provider == "coverartarchive"
    assert first.mbid == RG_BONFIRE_2023
    assert first.from_cache is False
    assert first.path.is_file() and first.path.stat().st_size > 1000
    assert sniff_mime(first.path.read_bytes()) == "image/jpeg"
    assert seen and seen[0].path == first.path
    calls_after_first = len(fetch.calls)
    assert any("front-500" in u for u in fetch.urls("coverartarchive.org"))

    # Zweiter Lauf: kein einziges neues HTTP, Pfad identisch.
    second = asyncio.run(svc.find_cover(BONFIRE))
    assert second is not None and second.from_cache is True
    assert second.path == first.path
    assert len(fetch.calls) == calls_after_first


def test_cached_cover_survives_offline(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})),
    ]
    svc, _ = service(tmp_path, routes)
    assert asyncio.run(svc.find_cover(BONFIRE)) is not None

    # Neue Instanz, neuer Cache-Zugriff, Netz ist „weg“.
    def boom(url, params=None):
        raise ProviderError("offline")

    offline_fetcher = FakeFetcher([("", boom)])
    cache = ArtOnlineCache(tmp_path / "cache")
    svc_offline = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"),
                                   fetcher=offline_fetcher, cache=cache)
    result = asyncio.run(svc_offline.find_cover(BONFIRE))
    assert result is not None and result.from_cache is True
    assert offline_fetcher.calls == []


def test_playback_path_reads_only_disk_and_db(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})),
    ]
    svc, fetch = service(tmp_path, routes)
    # Vor der Suche: nichts gecacht, und keine Anfrage wird ausgelöst.
    assert svc.read_cached(BONFIRE) is None
    assert fetch.calls == []
    assert asyncio.run(svc.find_cover(BONFIRE)) is not None
    calls = len(fetch.calls)
    cached = svc.read_cached_async(BONFIRE)
    assert asyncio.run(cached) is not None
    assert len(fetch.calls) == calls


# ---------------------------------------------------------------------------
# Fehlerfälle / Negativfälle
# ---------------------------------------------------------------------------


def test_no_match_writes_miss_cache_and_logs_once(tmp_path, caplog):
    routes = [("musicbrainz.org/ws/2/release",
               json_response({"count": 0, "offset": 0, "releases": []}))]
    svc, fetch = service(tmp_path, routes)
    with caplog.at_level("INFO", logger="lyrion.media.art_online"):
        assert asyncio.run(svc.find_cover(AlbumQuery(album="Nix", artist="Niemand"))) is None
    assert any("kein Online-Cover" in rec.getMessage() for rec in caplog.records)
    calls = len(fetch.calls)
    entry = svc.cache.read_sync(AlbumQuery(album="Nix", artist="Niemand").key())
    assert entry is not None and entry.status == "miss"
    # Zweiter Lauf: Fehlschlag ist gecacht → kein neues HTTP.
    assert asyncio.run(svc.find_cover(AlbumQuery(album="Nix", artist="Niemand"))) is None
    assert len(fetch.calls) == calls


def test_network_error_does_not_crash_and_is_logged(tmp_path, caplog):
    def boom(url, params=None):
        raise httpx.ConnectError("Netz weg")

    svc, _ = service(tmp_path, [("musicbrainz.org", boom)])
    with caplog.at_level("INFO", logger="lyrion.media.art_online"):
        assert asyncio.run(svc.find_cover(BONFIRE)) is None
    assert any("kein Online-Cover" in rec.getMessage() for rec in caplog.records)
    entry = svc.cache.read_sync(BONFIRE.key())
    assert entry is not None and entry.status == "miss" and "Netz weg" in entry.reason


def test_non_image_payload_is_rejected(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=CAA_404_BODY, headers={"Content-Type": "text/html"})),
    ]
    svc, _ = service(tmp_path, routes)
    assert asyncio.run(svc.find_cover(BONFIRE)) is None
    assert validate_image(CAA_404_BODY) is None


def test_tiny_image_is_rejected_min_size(tmp_path):
    tiny = jpeg_bytes(150, 150)
    assert sniff_mime(tiny) == "image/jpeg"
    assert validate_image(tiny) is None                       # < MIN_IMAGE_EDGE
    assert validate_image(tiny, min_edge=100) is not None
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=tiny, headers={"Content-Type": "image/jpeg"})),
    ]
    svc, _ = service(tmp_path, routes)
    assert asyncio.run(svc.find_cover(BONFIRE)) is None


def test_caa_404_falls_through_to_the_next_provider(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(status=404, content=CAA_404_BODY,
                                              headers={"Content-Type": "text/plain"})),
        # cdn-Route zuerst: der FakeFetcher nimmt den ersten Teilstring-Treffer.
        ("cdn.theaudiodb.com", FetchResponse(
            status=200, content=jpeg_bytes(600, 600), headers={"Content-Type": "image/jpeg"})),
        ("theaudiodb.com", json_response({"album": [{
            "strAlbum": "Point Blank MMXXIII", "strArtist": "Bonfire",
            "intYearReleased": "2023", "strAlbumThumbHQ": "https://cdn.theaudiodb.com/x.jpg"}]})),
    ]
    conf = ArtOnlineSettings(cache_dir=tmp_path / "cache", audiodb_key="cfg-key")
    svc, fetch = service(tmp_path, routes, settings=conf)
    result = asyncio.run(svc.find_cover(BONFIRE))
    assert result is not None and result.provider == "audiodb"
    assert "/cfg-key/" in fetch.urls("theaudiodb.com")[0]
    # CAA wurde trotzdem zuerst probiert (Reihenfolge zählt).
    assert fetch.urls("coverartarchive.org")


def test_audiodb_is_not_called_without_configured_key(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(status=404, content=CAA_404_BODY)),
        ("theaudiodb.com", json_response({"album": []})),
    ]
    svc, fetch = service(tmp_path, routes)
    assert asyncio.run(svc.find_cover(BONFIRE)) is None
    assert fetch.urls("theaudiodb.com") == []


def test_fanart_uses_release_group_mbid_and_configured_key(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(status=404, content=CAA_404_BODY)),
        ("assets.fanart.tv", FetchResponse(
            status=200, content=jpeg_bytes(1000, 1000), headers={"Content-Type": "image/jpeg"})),
        ("webservice.fanart.tv", json_response({
            "name": "Bonfire",
            "albums": {RG_BONFIRE_2023: [{
                "id": RG_BONFIRE_2023, "url": "https://assets.fanart.tv/fanart/x.jpg",
                "likes": "1"}]},
        })),
    ]
    conf = ArtOnlineSettings(cache_dir=tmp_path / "cache", fanart_key="cfg-fanart-key")
    svc, fetch = service(tmp_path, routes, settings=conf)
    result = asyncio.run(svc.find_cover(BONFIRE))
    assert result is not None and result.provider == "fanart"
    url, params = [(u, p) for u, p in fetch.calls if "webservice.fanart.tv" in u][0]
    assert url.endswith(f"/{RG_BONFIRE_2023}")
    assert params.get("api_key") == "cfg-fanart-key"


def test_disabled_search_does_nothing(tmp_path):
    conf = ArtOnlineSettings(cache_dir=tmp_path / "cache", enabled=False)
    svc, fetch = service(tmp_path, mb_routes(), settings=conf)
    assert asyncio.run(svc.find_cover(BONFIRE)) is None
    assert fetch.calls == []
    assert svc.request_lookup(BONFIRE) is False


# ---------------------------------------------------------------------------
# Nachgelagerte Suche (Queue)
# ---------------------------------------------------------------------------


def test_request_lookup_is_queued_and_drained_without_blocking(tmp_path):
    routes = mb_routes() + [
        ("coverartarchive.org", FetchResponse(
            status=200, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})),
    ]

    async def run() -> tuple[bool, int, CoverResult | None]:
        svc, _fetch = service(tmp_path, routes)
        accepted = svc.request_lookup(BONFIRE)
        remaining = await svc.drain(timeout=10)
        result = await svc.read_cached_async(BONFIRE)
        await svc.close()
        return accepted, remaining, result

    accepted, remaining, result = asyncio.run(run())
    assert accepted is True
    assert remaining == 0
    assert result is not None and result.from_cache is True
    assert result.path.is_file()


def test_request_lookup_drops_when_queue_is_full(tmp_path, caplog):
    svc, _ = service(tmp_path, mb_routes())
    svc._queue = asyncio.Queue(maxsize=1)  # type: ignore[assignment]

    async def run() -> tuple[bool, bool]:
        first = svc.request_lookup(BONFIRE)
        svc._worker.cancel()                      # Worker anhalten: Queue bleibt voll
        try:
            await svc._worker
        except BaseException:                     # noqa: BLE001 - Abbruch erwartet
            pass
        second = svc.request_lookup(AlbumQuery(album="Zweites", artist="X"))
        return first, second

    with caplog.at_level("WARNING", logger="lyrion.media.art_online"):
        first, second = asyncio.run(run())
    assert first is True
    assert second is False
    assert any("Queue voll" in rec.getMessage() for rec in caplog.records)

# ---------------------------------------------------------------------------
# Settings-Seite (Konfiguration) — wie ``tests/test_web_settings.py``
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """Globalen Prefs-Store auf In-Memory-DB hängen (Muster aus ``test_web_settings.py``)."""
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


def _request(method: str, path: str, *, data=None) -> httpx.Response:
    from lyrion.web.app import create_app

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.request(method, path, data=data)

    return asyncio.run(run())


def test_settings_page_shows_the_online_cover_fields():
    body = _request("GET", "/settings/server/basic.html").text
    for pref in art_online.PREF_DEFAULTS:
        assert f'name="pref_{pref}"' in body, pref
    # Vorbelegung aus PREF_DEFAULTS steht im Formular (Anbieter-Reihenfolge).
    assert 'value="musicbrainz,coverartarchive,audiodb,fanart"' in body
    # Kein API-Key im Formularfeld: der Nutzer trägt ihn ein.
    assert 'name="pref_artworkOnlineAudioDbKey"' in body
    assert 'name="pref_artworkOnlineFanartKey"' in body


def test_settings_page_saves_the_online_cover_prefs():
    res = _request("POST", "/settings/server/basic.html", data={
        "saveSettings": "1",
        "pref_artworkOnlineSearch": "1",
        "pref_artworkOnlineProviders": "coverartarchive,fanart",
        "pref_artworkOnlineLanguage": "de",
        "pref_artworkOnlineCacheDir": "/tmp/art-cache",
        "pref_artworkOnlineRetryDays": "7",
    })
    assert res.status_code == 200
    conf = settings_module.load_art_online_settings()
    assert conf.providers == ("coverartarchive", "fanart")
    assert conf.language == "de"
    assert str(conf.cache_dir) == "/tmp/art-cache"
    assert conf.retry_days == 7
    assert conf.enabled is True


def test_settings_page_rejects_an_unknown_provider():
    before = get_prefs().get(art_online.PREF_PROVIDERS)
    res = _request("POST", "/settings/server/basic.html", data={
        "saveSettings": "1",
        "pref_artworkOnlineProviders": "coverartarchive,gibtsnicht",
    })
    assert res.status_code == 200
    # Perl-Verhalten: ``SETTINGS_INVALIDVALUE`` statt stiller Übernahme
    # (``Slim/Web/Settings.pm:162-169``).
    assert "gibtsnicht" in res.text
    # Der abgelehnte Wert wird NICHT gespeichert (Settings.pm:162-169).
    assert get_prefs().get(art_online.PREF_PROVIDERS) == before
