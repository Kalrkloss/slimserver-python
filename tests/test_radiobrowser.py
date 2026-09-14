"""radio-browser.info data source — mapping, cache, endpoints, degradation.

The user decided that the Radio directory is fed from radio-browser.info
instead of TuneIn (the one approved deviation from Perl); everything the
clients see stays Perl-shaped (see ``tests/test_radio_structure.py``).  This
module pins down the data source's contract:

* the endpoints and query parameters (``hidebroken``/``order``/``reverse``),
* the mandatory *speaking* User-Agent,
* the field mapping (``url_resolved`` before ``url``, favicon, country,
  language, codec, bitrate, ``stationuuid``),
* the short result cache,
* and that every failure degrades to an **empty** list — never an exception,
  never invented data.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any, Optional

import aiosqlite
import pytest

from lyrion.web import radiobrowser
from lyrion.web.radiobrowser import RadioNode, Station

ROW = {
    "stationuuid": "uuid-1",
    "name": "Test FM",
    "url": "http://playlist.invalid/list.m3u",
    "url_resolved": "http://stream.invalid/live.mp3",
    "favicon": "http://stream.invalid/logo.png",
    "country": "Germany",
    "countrycode": "de",
    "language": "german",
    "tags": "pop,rock",
    "codec": "MP3",
    "bitrate": 192,
}


def _static_json(payload: Any):
    """An async stand-in for :func:`radiobrowser._get_json`."""
    async def _fake(path: str, params: dict | None = None) -> Any:
        return payload
    return _fake


def _json_by_path(mapping: dict):
    """Async stand-in answering per URL prefix (like the real API)."""
    async def _fake(path: str, params: dict | None = None) -> Any:
        for prefix, payload in mapping.items():
            if path.startswith(prefix):
                return payload
        return []
    return _fake


@pytest.fixture(autouse=True)
def clean_cache():
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()
    yield
    radiobrowser._cache.clear()
    radiobrowser._sids.clear()


# ── the station mapping ───────────────────────────────────────────────────

def test_station_mapping_prefers_url_resolved():
    station = Station.from_json(ROW)
    assert station is not None
    assert station.url == ROW["url_resolved"]
    assert station.name == "Test FM"
    assert station.favicon == "http://stream.invalid/logo.png"
    assert station.country == "Germany" and station.countrycode == "DE"
    assert station.language == "german" and station.tags == "pop,rock"
    assert station.codec == "MP3" and station.bitrate == 192
    assert station.stationuuid == "uuid-1"
    assert station.title == "Test FM"


def test_station_mapping_falls_back_to_url_and_drops_dead_rows():
    station = Station.from_json({"name": "Only URL",
                                 "url": "https://only.example/x"})
    assert station is not None and station.url == "https://only.example/x"
    assert station.bitrate == 0 and station.favicon == ""
    # a station whose only URL is not http(s) cannot be played → dropped
    assert Station.from_json({"name": "x", "url": "file:///tmp/x"}) is None
    assert Station.from_json({"name": "x", "url": ""}) is None
    assert Station.from_json({"name": "x", "url_resolved": ""}) is None
    assert Station.from_json("not a dict") is None


def test_station_title_never_empty():
    station = Station.from_json({"url": "https://a.example/b"})
    assert station is not None
    assert station.title == "https://a.example/b"


def test_station_icon_uses_perls_icon_rule():
    """``XMLBrowser.pm:1171`` — http(s) logo → ``icon``, else ``icon-id``."""
    key, url = radiobrowser.station_icon(Station(favicon="http://a.example/f.ico"))
    assert key == "icon"
    assert url == "/imageproxy/http%3A%2F%2Fa.example%2Ff.ico/image.png"
    key, url = radiobrowser.station_icon(Station(favicon="html/images/radio.png"))
    assert key == "icon-id" and url == "html/images/radio.png"
    assert radiobrowser.station_icon(Station()) == ("", "")


# ── HTTP: endpoints, parameters, user agent ───────────────────────────────

def _capture_requests(monkeypatch) -> list[str]:
    """Record the URL of every ``urllib`` request radio-browser makes."""
    seen: list[str] = []

    class _Response:
        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc) -> None:
            return None

    def fake_urlopen(request, timeout=None):
        seen.append(request.full_url)
        return _Response([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_user_agent_and_timeout_are_sent(monkeypatch):
    captured: dict = {}

    class _Response:
        def read(self) -> bytes:
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert asyncio.run(radiobrowser.search_stations("jazz")) == []
    assert captured["ua"] == radiobrowser.USER_AGENT
    assert "PyrionMusicServer" in captured["ua"] and "gtfe.de" in captured["ua"]
    assert captured["timeout"] == radiobrowser.HTTP_TIMEOUT
    assert captured["url"].startswith("https://de1.api.radio-browser.info"
                                     "/json/stations/search?")


def test_search_query_parameters(monkeypatch):
    seen = _capture_requests(monkeypatch)
    asyncio.run(radiobrowser.search_stations("jazz & soul", limit=7, offset=14))
    url = seen[0]
    assert "/json/stations/search?" in url
    assert "name=jazz+%26+soul" in url
    assert "limit=7" in url and "offset=14" in url
    assert "hidebroken=true" in url
    assert "order=clickcount" in url and "reverse=true" in url


def test_tag_country_language_endpoints(monkeypatch):
    seen = _capture_requests(monkeypatch)
    asyncio.run(radiobrowser.stations_by_tag("rock"))
    asyncio.run(radiobrowser.stations_by_country("de", limit=3))
    asyncio.run(radiobrowser.stations_by_language("german", limit=2))
    asyncio.run(radiobrowser.countries())
    asyncio.run(radiobrowser.languages())
    assert "/json/stations/bytagexact/rock?" in seen[0]
    assert "/json/stations/bycountrycodeexact/DE?" in seen[1]
    assert "/json/stations/bylanguage/german?" in seen[2]
    assert seen[3].endswith("/json/countries")
    assert seen[4].endswith("/json/languages")
    # every station list is filtered/ordered by the same rules
    for url in seen[:3]:
        assert "hidebroken=true" in url and "reverse=true" in url


def test_empty_arguments_never_hit_the_network(monkeypatch):
    seen = _capture_requests(monkeypatch)
    assert asyncio.run(radiobrowser.search_stations("")) == []
    assert asyncio.run(radiobrowser.stations_by_tag("")) == []
    assert asyncio.run(radiobrowser.stations_by_country("deutschland")) == []
    assert asyncio.run(radiobrowser.stations_by_language("   ")) == []
    assert seen == []


# ── host fallback, cache, degradation ─────────────────────────────────────

def test_first_working_host_wins_and_failures_are_skipped(monkeypatch):
    tries: list[str] = []

    def fake(host, path, params):
        tries.append(host)
        if host != "nl1.api.radio-browser.info":
            raise OSError("unreachable")
        return [dict(ROW)]

    monkeypatch.setattr(radiobrowser, "_http_get_json", fake)
    stations = asyncio.run(radiobrowser.search_stations("x"))
    assert [s.name for s in stations] == ["Test FM"]
    assert tries == list(radiobrowser.RADIO_BROWSER_HOSTS)
    assert radiobrowser.RADIO_BROWSER_HOSTS[0] == "de1.api.radio-browser.info"


def test_unreachable_api_degrades_to_an_empty_list(monkeypatch):
    def boom(host, path, params):
        raise OSError("no route to host")

    monkeypatch.setattr(radiobrowser, "_http_get_json", boom)
    assert asyncio.run(radiobrowser.search_stations("jazz")) == []
    assert asyncio.run(radiobrowser.stations_by_tag("rock")) == []
    assert asyncio.run(radiobrowser.countries()) == []
    assert asyncio.run(radiobrowser.languages()) == []


def test_malformed_payload_is_ignored(monkeypatch):
    monkeypatch.setattr(radiobrowser, "_get_json",
                        _static_json({"error": "nope"}))
    assert asyncio.run(radiobrowser.search_stations("jazz")) == []
    # a list with unusable rows keeps only the usable ones
    monkeypatch.setattr(radiobrowser, "_get_json",
                        _static_json([dict(ROW), {"name": "dead", "url": ""}]))
    stations = asyncio.run(radiobrowser.search_stations("jazz"))
    assert [s.name for s in stations] == ["Test FM"]


def test_results_are_cached_for_the_ttl(monkeypatch):
    calls: list[str] = []

    def fake(host, path, params):
        calls.append(radiobrowser._cache_key(path, params))
        return [dict(ROW)]

    monkeypatch.setattr(radiobrowser, "_http_get_json", fake)
    asyncio.run(radiobrowser.search_stations("jazz"))
    asyncio.run(radiobrowser.search_stations("jazz"))
    assert len(calls) == 1, "the second call must be served from the cache"
    # a different query is not served from the same entry
    asyncio.run(radiobrowser.search_stations("rock"))
    assert len(calls) == 2
    # after the TTL the API is asked again
    clock = radiobrowser.time.monotonic()
    monkeypatch.setattr(radiobrowser.time, "monotonic",
                        lambda: clock + radiobrowser.CACHE_TTL + 1)
    asyncio.run(radiobrowser.search_stations("jazz"))
    assert len(calls) == 3
    assert radiobrowser.CACHE_TTL >= 60 and radiobrowser.CACHE_TTL <= 300


def test_countries_and_languages_are_sorted_and_cleaned(monkeypatch):
    payload = {
        "/json/countries": [
            {"name": "Zimbabwe", "iso_3166_1": "zw", "stationcount": 5},
            {"name": "", "iso_3166_1": "XX", "stationcount": 1},
            {"name": "Andorra", "iso_3166_1": "ad", "stationcount": 12},
        ],
        "/json/languages": [
            {"name": "german", "stationcount": 50},
            {"name": "#english", "stationcount": 3},
            {"name": "+7 languages", "stationcount": 1},
            {"name": "1", "stationcount": 1},
            {"name": "Afrikaans", "stationcount": 2},
        ],
    }
    monkeypatch.setattr(radiobrowser, "_get_json", _json_by_path(payload))
    assert [c["name"] for c in asyncio.run(radiobrowser.countries())] == \
        ["Andorra", "Zimbabwe"]
    assert [c["code"] for c in asyncio.run(radiobrowser.countries())] == \
        ["AD", "ZW"]
    assert [l["name"] for l in asyncio.run(radiobrowser.languages())] == \
        ["Afrikaans", "german"]
    assert asyncio.run(radiobrowser.country_name("ad")) == "Andorra"
    # unknown code → the code itself (never a fabricated name)
    assert asyncio.run(radiobrowser.country_name("zz")) == "ZZ"


# ── node identity: the browse-session handle and item ids ─────────────────

def test_item_ids_round_trip_to_the_same_station(monkeypatch):
    """``<sid>.<index>`` (Perl's crumb) resolves to the very same station."""
    rows = [dict(ROW), dict(ROW, stationuuid="uuid-2", name="Second FM")]

    async def fake(path: str, params: dict | None = None):
        offset = int((params or {}).get("offset") or 0)
        return [dict(r) for r in rows[offset:]]

    monkeypatch.setattr(radiobrowser, "_get_json", fake)
    node = RadioNode("stations", "music", "rock", "Rock")
    sid = radiobrowser.node_sid(node)
    level = asyncio.run(radiobrowser.level_for(node, start=0, qty=10))
    assert level.items[0]["params"]["item_id"] == f"{sid}.0"
    assert level.items[0]["text"] == "Test FM"

    parsed, indices, search = radiobrowser.parse_item_id(f"{sid}.0")
    assert parsed is not None and parsed == node
    assert indices == [0] and search == ""
    resolved, station, index = asyncio.run(radiobrowser.resolve(parsed, indices))
    assert resolved == node and index == 0
    assert station is not None and station.name == "Test FM"

    _node, second, _index = asyncio.run(radiobrowser.resolve(parsed, [1]))
    assert second is not None and second.name == "Second FM"


def test_search_item_ids_carry_the_term_like_perl():
    """``XMLBrowser.pm:356-358/445-447`` — ``<sid>_<search>.<index>``."""
    node = RadioNode("stations", "search", "rock & roll", "")
    sid = radiobrowser.node_sid(node)
    parsed, indices, search = radiobrowser.parse_item_id(f"{sid}_rock.1")
    assert parsed == node and indices == [1] and search == "rock"
    # an unknown handle (session expired) resolves to nothing
    parsed, indices, search = radiobrowser.parse_item_id("deadbeef.3")
    assert parsed is None and indices == [3] and search == ""


def test_index_and_station_levels(monkeypatch):
    monkeypatch.setattr(radiobrowser, "_get_json", _static_json([dict(ROW)]))
    index = RadioNode("index", "music", "", "Musik")
    rows = asyncio.run(radiobrowser.index_rows(index))
    assert [label for label, _ in rows][:3] == ["Pop", "Rock", "Jazz"]
    assert all(child.kind == "stations" and child.feed == "music"
               for _label, child in rows)
    child = asyncio.run(radiobrowser.index_child(index, 1))
    assert child is not None and child.arg == "rock" and child.title == "Rock"
    assert asyncio.run(radiobrowser.index_child(index, 999)) is None

    # an index level answers link rows, the station level audio rows
    level = asyncio.run(radiobrowser.level_for(index, start=0, qty=3))
    assert level.title == "Musik" and len(level.items) == 3
    assert {i["type"] for i in level.items} == {"link"}
    assert level.total == len(rows)
    station_level = asyncio.run(radiobrowser.level_for(child, start=0, qty=5))
    assert {i["type"] for i in station_level.items} == {"audio"}
    assert station_level.title == "Rock"


def test_count_hints_that_more_pages_exist(monkeypatch):
    """radio-browser has no total — the +1 marker replaces TuneIn's."""
    monkeypatch.setattr(radiobrowser, "_get_json",
                        _static_json([dict(ROW)] * 3))
    node = RadioNode("stations", "music", "rock", "Rock")
    short = asyncio.run(radiobrowser.level_for(node, start=0, qty=5))
    assert len(short.items) == 3 and short.total == 3      # the feed ends
    page = asyncio.run(radiobrowser.level_for(node, start=0, qty=2))
    assert len(page.items) == 2 and page.total == 3        # one more exists


def test_search_level_title_uses_the_query():
    node = RadioNode("stations", "search", "jazz", "")
    assert radiobrowser.SEARCH_TITLE.format(term=node.arg) == \
        "Suchergebnisse: jazz"


def test_local_country_from_locale_and_pref(monkeypatch):
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    assert radiobrowser.local_country() == "DE"
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert radiobrowser.local_country() == "US"
    monkeypatch.delenv("LC_ALL", raising=False)
    # No locale at all: the timezone answers (see the country tests below);
    # with an unknown zone the shipped fallback is left.
    monkeypatch.setenv("TZ", "Mars/Olympus")
    assert radiobrowser.local_country() == radiobrowser.DEFAULT_COUNTRY


# ── the country of the ``local`` node: pref + first-start derivation ───────
#
# Perl precedent for a *derived* pref default: ``Slim/Utils/Prefs.pm:161``
# (``'language' => \&defaultLanguage``) → ``:676-678`` →
# ``Slim/Utils/OS.pm:399-414`` (``POSIX::setlocale(LC_CTYPE)``, up-cased, codeset
# and territory stripped).  Perl has no country pref at all (its ``local`` node
# is TuneIn's IP-geolocated one, ``TuneIn.pm:38-40``).

@pytest.fixture(autouse=True)
def _no_stored_country_pref():
    """Kein ``radiobrowser_country`` aus einem anderen Testmodul.

    Der ``PreferenceStore`` ist ein Singleton; ``tests/test_web_settings.py``
    schreibt dieselbe Pref. ``get`` liest den Cache, deshalb genügt dessen
    Leeren — die Ableitung ist damit wieder der „nie gesetzt"-Fall.
    """
    from lyrion.config import get_prefs

    get_prefs()._cache.pop(radiobrowser.COUNTRY_PREF, None)
    yield


def _no_locale(monkeypatch) -> None:
    for var in radiobrowser._LOCALE_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("locale,expected", [
    ("de_DE.UTF-8", "DE"),
    ("en_US.UTF-8", "US"),
    ("en-US", "US"),
    ("de_DE.ISO-8859-1@euro", "DE"),
    ("fr_FR", "FR"),
    ("C", ""),            # POSIX: no territory — Perl answers 'EN' (OS.pm:411)
    ("POSIX", ""),
    ("en", ""),           # language without a territory
    ("", ""),
])
def test_region_of_locale_is_the_territory(locale, expected):
    assert radiobrowser._region_of_locale(locale) == expected


def test_derived_country_takes_the_locale_before_the_timezone(monkeypatch):
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("TZ", "America/New_York")
    assert radiobrowser.derived_country() == "DE"

    # ``LC_ALL`` decides — LANG is not consulted (POSIX precedence, the order
    # ``POSIX::setlocale`` resolves in, OS.pm:399-404): with LC_ALL=C the
    # timezone answers even though LANG names a territory.
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.setattr(radiobrowser, "_zone_table",
                        lambda: {"Europe/Berlin": "DE"})
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert radiobrowser.derived_country() == "DE"


def test_derived_country_falls_back_to_the_timezone(monkeypatch):
    _no_locale(monkeypatch)
    monkeypatch.setattr(radiobrowser, "_zone_table",
                        lambda: {"Europe/Berlin": "DE",
                                 "America/New_York": "US"})
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert radiobrowser.derived_country() == "DE"
    monkeypatch.setenv("TZ", "America/New_York")
    assert radiobrowser.derived_country() == "US"
    # A zone the OS map does not know, and no locale: the shipped fallback.
    monkeypatch.setenv("TZ", "Mars/Olympus")
    assert radiobrowser.derived_country() == radiobrowser.DEFAULT_COUNTRY


def test_derived_country_reads_etc_timezone_when_tz_is_unset(monkeypatch):
    """``TZ`` unset → ``/etc/timezone`` (the task's fallback chain)."""
    _no_locale(monkeypatch)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(radiobrowser, "_zone_table",
                        lambda: {"Europe/Berlin": "DE"})

    class _FakePath:
        def __init__(self, path):
            self.path = str(path)

        def read_text(self, **_kwargs):
            return "Europe/Berlin\n"

    monkeypatch.setattr(radiobrowser, "Path", _FakePath)
    assert radiobrowser.derived_country() == "DE"


def test_zone_table_is_the_operating_systems_own_map():
    """The zone → country map is read from ``zone.tab``, not invented."""
    table = radiobrowser._zone_table()
    if not table:
        pytest.skip("no /usr/share/zoneinfo/zone.tab on this host")
    assert table.get("Europe/Berlin") == "DE"
    assert table.get("America/New_York") == "US"


def test_local_country_pref_wins_and_empty_means_all_countries(monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: "fr")
    assert radiobrowser.local_country() == "FR"          # pref, not the locale
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: "")
    assert radiobrowser.local_country() == ""            # "" = all countries
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: "  ")
    assert radiobrowser.local_country() == ""            # blank behaves the same
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: None)
    assert radiobrowser.local_country() == "US"          # never set → derived


@pytest.fixture
def prefs_store():
    """In-memory preference store (the pattern of ``tests/test_web_settings.py``)."""
    from lyrion.config import _PREF_DB_SCHEMA, get_prefs

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
        except Exception:  # noqa: BLE001 — Aufräumen darf nicht scheitern
            pass
        store._db = None


async def _clear_pref(store, name: str) -> None:
    store._cache.pop(name, None)
    if store._db is not None:
        await store._db.execute("DELETE FROM prefhash WHERE name = ?", (name,))
        await store._db.commit()


def test_ensure_country_pref_stores_the_derivation_once(monkeypatch, prefs_store):
    """Perl ``Slim/Utils/Prefs/Base.pm:178-234``: the CODE default runs once."""
    name = radiobrowser.COUNTRY_PREF
    asyncio.run(_clear_pref(prefs_store, name))
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert asyncio.run(radiobrowser.ensure_country_pref()) == "DE"
    assert prefs_store.get(name) == "DE"

    # From then on the stored value wins — a later locale change (or an empty
    # "all countries" choice) is never overwritten by the derivation.
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert asyncio.run(radiobrowser.ensure_country_pref()) == "DE"
    asyncio.run(prefs_store.set(name, ""))
    assert asyncio.run(radiobrowser.ensure_country_pref()) == ""
    assert prefs_store.get(name) == ""
    asyncio.run(_clear_pref(prefs_store, name))


def test_local_feed_without_a_country_asks_for_the_unfiltered_list(monkeypatch):
    """"" (empty pref) = all countries → ``/json/stations/search``, no filter."""
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: "")
    seen: list[tuple[str, dict]] = []

    async def fake(path: str, params: Optional[dict] = None):
        seen.append((path, params or {}))
        return [ROW]

    monkeypatch.setattr(radiobrowser, "_get_json", fake)

    rows = asyncio.run(radiobrowser.index_rows(RadioNode("index", "local")))
    assert [label for label, _child in rows] == [radiobrowser.STATIONS_TITLE]
    child = rows[0][1]
    assert (child.kind, child.feed, child.arg) == ("stations", "local", "")

    stations = asyncio.run(radiobrowser.stations_for(child, limit=20, offset=0))
    assert [s.name for s in stations] == ["Test FM"]
    path, params = seen[-1]
    assert path == "/json/stations/search"
    assert not any("country" in key for key in params), params
    assert params["order"] == "clickcount" and params["hidebroken"] == "true"


def test_local_feed_with_a_country_pref_filters_by_that_country(monkeypatch):
    """The pref, not the locale, decides — even with an English locale."""
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setattr(radiobrowser, "_stored_country", lambda: "de")
    seen: list[str] = []

    async def fake(path: str, params: Optional[dict] = None):
        seen.append(path)
        if path == "/json/countries":
            return [{"name": "Germany", "iso_3166_1": "DE", "stationcount": 9}]
        return [ROW]

    monkeypatch.setattr(radiobrowser, "_get_json", fake)

    rows = asyncio.run(radiobrowser.index_rows(RadioNode("index", "local")))
    assert [label for label, _child in rows] == \
        [radiobrowser.STATIONS_TITLE, "Alle Germany"]
    child = rows[0][1]
    assert (child.feed, child.arg) == ("local", "DE")
    asyncio.run(radiobrowser.stations_for(child, limit=20, offset=0))
    assert seen[-1] == "/json/stations/bycountrycodeexact/DE"
