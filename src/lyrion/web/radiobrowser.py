"""radio-browser.info as the station source of the Radio directory.

Perl reference (read-only checkout in ``/tmp/lms91/slimserver-public-9.1``,
public/9.1) — the *client-visible* structure this module reproduces:

* ``Slim/Plugin/InternetRadio/Plugin.pm:42-59`` — ``_initRadio`` fetches the
  TuneIn directory index (``TuneIn.pm:83`` ``MAIN_URL`` =
  ``http://opml.radiotime.com/Index.aspx?partnerId=16``) and
  ``buildMenus``/``generate`` (:78-205) create one dynamic OPMLBased plugin
  per directory item with ``tag => lc $subclass``, ``menu => 'radios'``,
  ``weight => $item->{weight}``, ``type => $item->{type}`` (:148-153).
* ``Slim/Plugin/OPMLBased.pm:117-132`` — each of those plugins registers
  ``[<tag>,'items','_index','_quantity']`` and ``[<tag>,'playlist','_method']``
  on ``Slim::Control::XMLBrowser::cliQuery``; the Radio menu itself registers
  ``['radios','_index','_quantity']`` → ``cliRadiosQuery`` (:181-280).
* ``Slim/Plugin/OPMLBased.pm:200-265`` — the answer shape of that menu query:
  with a ``menu:`` param the jive item
  ``{text, weight, icon-id, actions.go{cmd:[tag,'items'], params:{menu:tag}},
  window:{titleStyle:'album'}}`` (:204-219) plus the ``input`` block for a
  ``type=search`` entry (:221-246); without ``menu:`` the plain
  ``{cmd,name,type,icon,weight}`` form (:249-265, ``type`` mapped
  ``link→xmlbrowser`` / ``search→xmlbrowser_search`` at :250-256).
* ``Slim/Plugin/InternetRadio/TuneIn.pm:33-81`` — the ``MENUS`` table that
  fixes the tag → {icon, weight} mapping, :151-164 the unshifted
  ``My Presets`` entry (tag ``presets``, weight 5).
* ``Slim/Control/XMLBrowser.pm:1053-1270`` — the item renderer of a sub-feed
  (``type``/``text``/``icon``/``icon-id``/``params``/``presetParams``/
  ``goAction``/``style``), :1119-1126 + :1434-1441 the ``windowStyle`` rule,
  :837-846 the ``Leer``/``EMPTY`` placeholder of an empty feed, :960-990 the
  ``base.actions`` table, :924-931 the ``add-hold`` action, :1798-1808
  ``_jivePresetBase``, :1925-1944 ``_favoritesParams``, :1259-1272 the
  ``play``/``playControl`` row branch, :353-358/:387-395/:445-447 the crumb
  (item id) encoding.

Data source (user decision, explicitly approved — the one deviation from
Perl): **radio-browser.info instead of TuneIn**.  Structures, tags, item ids
and item shapes stay Perl-equal; only the stations behind them come from
https://api.radio-browser.info (spoken ``User-Agent`` required, results
cached briefly, failures degrade to an empty list — never an exception and
never invented data).

Paging (the ``count`` of an answered level) is Perl's paging: ``count`` is the
**feed's total**, not the size of the answered window (``Slim/Control/
XMLBrowser.pm:792-793`` ``my $count = $subFeed->{'total'}``, sliced by
``Slim/Control/Request.pm:1805-1839`` ``normalize``).  TuneIn ships that total
with the feed; radio-browser publishes none per query, so the total comes from
the *facet index* the leaf is built from — ``/json/countries`` and
``/json/languages`` carry a ``stationcount``, ``/json/tags/<tag>`` the exact
tag's (see :func:`node_total`).  ``search`` and the unfiltered ``local`` have
no such index: their level is its own (bounded) list, whose length is the
count (:func:`full_stations`).  Either way the number is *stable* across the
pages of one browse — Jive's list treats ``count`` as the length of the whole
long list and discards its cache the moment two answers disagree
(``share/jive/applets/SlimBrowser/DB.lua:63`` and :125-136), which a
page-dependent count (the port's earlier ``start + len + 1`` marker) never
survives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("lyrion.web.radiobrowser")

#: radio-browser mirrors.  ``de1`` first (the primary the task names), the
#: round-robin ``all`` and ``nl1`` as fallbacks; tried in order until one
#: answers.  CNAME's of ``api.radio-browser.info`` are deliberately avoided:
#: the host name must stay a fixed string so cache keys stay stable.
RADIO_BROWSER_HOSTS: tuple[str, ...] = (
    "de1.api.radio-browser.info",
    "all.api.radio-browser.info",
    "nl1.api.radio-browser.info",
)

#: radio-browser's API rules ask for a *speaking* User-Agent ("do not use the
#: default one of your http library") — mandatory, not optional: generic
#: clients are rate-limited or rejected.
USER_AGENT = "PyrionMusicServer/9.2 (https://gtfe.de)"

#: Single request timeout.  A browse query must answer fast: the live
#: 2026-09-13 incident showed a ``radios`` answer stalling >25 s breaks the
#: controller.  8 s per host, at most three hosts.
HTTP_TIMEOUT = 8.0

#: Short result cache.  Perl keeps a fetched feed for the whole browse session
#: (``XMLBrowser.pm:360-371``, ``CACHE_TIME`` 3600) — for TuneIn that is fine
#: because the directory rarely changes.  radio-browser's station base changes
#: constantly (click counts, ``hidebroken``) and the API is a free community
#: service with rate limits, so this port caches only long enough to serve one
#: browse session plus a re-entry: 180 s.  Longer would show stale search
#: results, shorter would hammer the API on every keypress.
CACHE_TTL = 180.0

#: Bounds of the cache/registry dicts.
_MAX_CACHE = 256
_MAX_SIDS = 512

#: Ceiling of one answer's page size.  radio-browser has no documented cap
#: (live 2026-09-19: ``limit=10000`` answers 6001 rows) — this is *our* bound,
#: and it must not be smaller than what a controller asks for: Jive requests
#: 200 rows per chunk (``share/jive/applets/SlimBrowser/DB.lua:48``
#: ``local BLOCK_SIZE = 200``, ``SlimBrowserApplet.lua:211`` ``qty =
#: DB:getBlockSize()``), and ``DB.menuItems`` stores the answer under the key
#: ``floor(cFrom/BLOCK_SIZE)`` (``DB.lua:186-201``) — a *short* page leaves a
#: hole in that chunk.  The old cap of 100 handed a 200-row client 100 rows and
#: with them a list that looked cut off.
_MAX_LIMIT = 1000

_cache: dict[str, tuple[float, Any]] = {}
_sids: dict[str, "RadioNode"] = {}


# ── the station record ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Station:
    """One radio-browser station, mapped to the fields Perl's feed items use.

    ``url_resolved`` is preferred over ``url`` (radio-browser's own advice);
    a station without a usable http(s) URL is dropped instead of being passed
    on as a dead row.
    """

    stationuuid: str = ""
    name: str = ""
    url: str = ""
    favicon: str = ""
    country: str = ""
    countrycode: str = ""
    language: str = ""
    tags: str = ""
    codec: str = ""
    bitrate: int = 0

    @classmethod
    def from_json(cls, row: Any) -> Optional["Station"]:
        if not isinstance(row, dict):
            return None
        url = str(row.get("url_resolved") or row.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return None
        bitrate = row.get("bitrate")
        try:
            bitrate = int(bitrate) if bitrate else 0
        except (TypeError, ValueError):
            bitrate = 0
        return cls(
            stationuuid=str(row.get("stationuuid") or ""),
            name=str(row.get("name") or "").strip(),
            url=url,
            favicon=str(row.get("favicon") or "").strip(),
            country=str(row.get("country") or "").strip(),
            countrycode=str(row.get("countrycode") or "").strip().upper(),
            language=str(row.get("language") or "").strip(),
            tags=str(row.get("tags") or "").strip(),
            codec=str(row.get("codec") or "").strip(),
            bitrate=bitrate,
        )

    @property
    def title(self) -> str:
        """The row's ``text`` — Perl's ``getTitle($name, $item)``.

        ``XMLBrowser.pm:1986-1994``: the name, falling back to the title and
        then to ''.  radio-browser rows always carry a name; a nameless row
        must not render as an empty list line.
        """
        return self.name or self.url


# ── HTTP + cache ──────────────────────────────────────────────────────────

def _cache_key(path: str, params: Optional[dict]) -> str:
    return path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))


def _http_get_json(host: str, path: str, params: Optional[dict]) -> Any:
    """One blocking GET (runs in a worker thread, see :func:`_get_json`)."""
    url = f"https://{host}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


async def _get_json(path: str, params: Optional[dict] = None) -> Any:
    """Cached JSON GET against radio-browser; ``None`` on any failure.

    Never raises: the callers build Perl-conform *empty* feeds from ``None``
    (a controller asking for a station list must get a valid answer, not an
    error).  The blocking ``urllib`` call runs in a worker thread so the event
    loop — and with it every other client request — is not stalled.
    """
    key = _cache_key(path, params)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL:
        return hit[1]
    for host in RADIO_BROWSER_HOSTS:
        try:
            data = await asyncio.to_thread(_http_get_json, host, path, params)
        except Exception as exc:  # noqa: BLE001 — any transport/JSON error
            logger.debug("radio-browser %s%s failed: %s", host, path, exc)
            continue
        if not isinstance(data, (list, dict)):
            logger.debug("radio-browser %s%s: unexpected payload", host, path)
            continue
        if len(_cache) >= _MAX_CACHE:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (now, data)
        return data
    logger.info("radio-browser unreachable for %s — answering empty", path)
    return None


def _stations(payload: Any) -> list[Station]:
    if not isinstance(payload, list):
        return []
    out: list[Station] = []
    for row in payload:
        station = Station.from_json(row)
        if station is not None:
            out.append(station)
    return out


def _capped(limit: Any) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 20
    return max(1, min(limit, _MAX_LIMIT))


#: radio-browser's station-search parameters: ``hidebroken`` drops stations
#: whose last check failed, ``order=clickcount`` + ``reverse=true`` puts the
#: most listened-to station first (TuneIn's directory order is popularity too).
_SEARCH_PARAMS = {
    "hidebroken": "true",
    "order": "clickcount",
    "reverse": "true",
}


def _station_params(limit: Any, offset: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": _capped(limit)}
    try:
        params["offset"] = max(0, int(offset))
    except (TypeError, ValueError):
        params["offset"] = 0
    params.update(_SEARCH_PARAMS)
    return params


async def search_stations(name: str, limit: int = 20,
                          offset: int = 0) -> list[Station]:
    """``/json/stations/search?name=<q>&limit=&offset=&hidebroken=true…``."""
    name = (name or "").strip()
    if not name:
        return []
    params = _station_params(limit, offset)
    params["name"] = name
    return _stations(await _get_json("/json/stations/search", params))


async def stations_by_tag(tag: str, limit: int = 20,
                          offset: int = 0) -> list[Station]:
    """``/json/stations/bytagexact/<tag>`` — music/news/sports/talk leaves."""
    tag = (tag or "").strip()
    if not tag:
        return []
    return _stations(await _get_json(
        "/json/stations/bytagexact/" + urllib.parse.quote(tag, safe=""),
        _station_params(limit, offset)))


async def stations_by_country(code: str, limit: int = 20,
                              offset: int = 0) -> list[Station]:
    """``/json/stations/bycountrycodeexact/<cc>`` — local / location leaves."""
    code = (code or "").strip().upper()
    if len(code) != 2:
        return []
    return _stations(await _get_json(
        "/json/stations/bycountrycodeexact/" + urllib.parse.quote(code, safe=""),
        _station_params(limit, offset)))


async def stations_by_language(language: str, limit: int = 20,
                               offset: int = 0) -> list[Station]:
    """``/json/stations/bylanguage/<lang>`` — the ``language`` sub-feed."""
    language = (language or "").strip()
    if not language:
        return []
    return _stations(await _get_json(
        "/json/stations/bylanguage/" + urllib.parse.quote(language, safe=""),
        _station_params(limit, offset)))


async def top_stations(limit: int = 20, offset: int = 0) -> list[Station]:
    """``/json/stations/search`` without a filter — radio-browser's top list.

    The "all countries" answer of the ``local`` node (:data:`COUNTRY_PREF`
    empty): the same parameters as every other search (``hidebroken``, ordered
    by ``clickcount``, :data:`_SEARCH_PARAMS`) but no name/tag/country filter,
    i.e. the stations the community listens to most.  Perl has no unfiltered
    "local" feed (its ``local`` is always the geolocated country,
    ``TuneIn.pm:38-40``), so this is the honest rendering of "no country
    filter" — never an invented list.
    """
    return _stations(await _get_json("/json/stations/search",
                                     _station_params(limit, offset)))


async def full_stations(node: "RadioNode") -> list[Station]:
    """The *whole* station list of a node radio-browser publishes no total for.

    ``search`` and the unfiltered ``local`` (``COUNTRY_PREF`` empty) have no
    facet index to read a count from, so the level's list itself is the total:
    one cached request of up to :data:`_MAX_LIMIT` rows, whose length becomes
    the answer's ``count`` and whose slices are the pages.  That is Perl's
    model — its feed holds the whole cached document (``XMLBrowser.pm:353-371``
    ``CACHE_TIME`` 3600) and every ``_index``/``_quantity`` request slices that
    in-memory list (``dynamicAutoQuery``/``normalize``).  A term with more hits
    than :data:`_MAX_LIMIT` therefore ends after those rows, exactly as TuneIn's
    own directory listing ends at its document bound.
    """
    return await stations_for(node, limit=_MAX_LIMIT, offset=0)


async def country_total(code: str) -> Optional[int]:
    """radio-browser's own station count of a country (``/json/countries``).

    Perl's ``count`` is the *feed's* item total — a stable number the
    controllers use to size the list and to fetch the next page
    (``Slim/Control/XMLBrowser.pm:792-793`` ``my $count = $subFeed->{'total'};
    $count ||= defined $items ? scalar @$items : 0;`` and the same ``$count``
    ``Slim/Control/Request.pm:1805-1839`` ``normalize`` slices against).  TuneIn
    hands that total over with the feed; radio-browser answers no per-query
    total, but its country index carries ``stationcount`` (live 2026-09-19:
    ``DE`` → 6397).  The facet counts every station of the country, the
    ``hidebroken`` filter below delivers 6001 of them (live) — so it is an
    *upper bound* of the deliverable list: every row the source has stays
    reachable, the last page is merely short.  ``None`` = the code is unknown to
    the index (no count to publish).
    """
    code = (code or "").strip().upper()
    if len(code) != 2:
        return None
    for row in await countries():
        if row["code"] == code:
            return int(row["stationcount"])
    return None


async def language_total(name: str) -> Optional[int]:
    """The ``stationcount`` of one language (``/json/languages``, exact match).

    Same rule as :func:`country_total`; the index's junk names (``#english``,
    ``1``, …) are dropped by :func:`languages` exactly as they are for the
    language *index* level, so no dead count can be published.
    """
    want = (name or "").strip().casefold()
    if not want:
        return None
    for row in await languages():
        if str(row["name"]).strip().casefold() == want:
            return int(row["stationcount"])
    return None


async def tag_total(tag: str) -> Optional[int]:
    """The ``stationcount`` of one tag (``/json/tags/<tag>``, exact match).

    ``/json/tags/<term>`` is radio-browser's tag *search*: it answers every tag
    containing the term (live ``pop`` → 297 rows, 12 KB, ~0.17 s) and the entry
    with the exact name is the one the ``bytagexact`` station list below is
    built from.  ``None`` when no entry carries the exact name — then the level
    falls back to its own list length (:func:`full_stations`).
    """
    want = (tag or "").strip()
    if not want:
        return None
    payload = await _get_json("/json/tags/" + urllib.parse.quote(want, safe=""))
    if not isinstance(payload, list):
        return None
    for row in payload:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "").strip().casefold() != want.casefold():
            continue
        try:
            return int(row.get("stationcount") or 0)
        except (TypeError, ValueError):
            return None
    return None


async def node_total(node: "RadioNode") -> Optional[int]:
    """The published total of a station level, ``None`` when there is none.

    The index a leaf's stations come from is the same index that carries its
    ``stationcount`` (country list, language list, tag search) — the one cheap,
    *stable* number :func:`level_for` can put into ``count``.  ``search``, the
    unfiltered ``local`` and any leaf the index does not know have no such
    number and are answered from their own list (:func:`full_stations`).
    """
    arg = (node.arg or "").strip()
    if not arg:
        return None
    if node.feed in ("local", "location"):
        return await country_total(arg)
    if node.feed == "language":
        return await language_total(arg)
    if node.feed in TAG_INDEX:
        return await tag_total(arg)
    return None


async def countries() -> list[dict]:
    """``/json/countries`` → ``[{name, code, stationcount}]`` (name-sorted)."""
    payload = await _get_json("/json/countries")
    if not isinstance(payload, list):
        return []
    out = [{"name": str(r.get("name") or "").strip(),
            "code": str(r.get("iso_3166_1") or "").strip().upper(),
            "stationcount": int(r.get("stationcount") or 0)}
           for r in payload if isinstance(r, dict)]
    out = [r for r in out if r["code"] and r["name"]]
    out.sort(key=lambda r: r["name"].casefold())
    return out


async def languages() -> list[dict]:
    """``/json/languages`` → ``[{name, stationcount}]`` (name-sorted).

    radio-browser's list mixes in artefacts that are neither languages nor
    resolvable by ``bylanguage`` — ``#english`` (a tag), ``+7 languages``,
    ``1`` (measured 2026-09-14: 661 rows, the first ones junk).  Perl's TuneIn
    list carries only real language names ("Aboriginal", "Afrikaans", …), so
    rows whose name does not start with a letter are dropped instead of
    producing dead links.
    """
    payload = await _get_json("/json/languages")
    if not isinstance(payload, list):
        return []
    out = [{"name": str(r.get("name") or "").strip(),
            "stationcount": int(r.get("stationcount") or 0)}
           for r in payload if isinstance(r, dict)]
    out = [r for r in out if r["name"] and r["name"][:1].isalpha()]
    out.sort(key=lambda r: r["name"].casefold())
    return out


async def country_name(code: str) -> str:
    """Country name for an ISO code (``/json/countries``); the code as fallback."""
    code = (code or "").strip().upper()
    for row in await countries():
        if row["code"] == code:
            return row["name"]
    return code


# ── the Perl node catalogue ───────────────────────────────────────────────

#: Perl's MENUS table (``TuneIn.pm:33-81``) in the order ``parseMenu``
#: (:118-168) builds it, with the unshifted ``My Presets`` entry first.
#: Live Perl 9.1.1 (192.168.1.90:9000, read-only 2026-09-14, ``radios 0 20``
#: and ``radios 0 20 menu:radio``) answers exactly these 10 nodes with
#: ``count`` 10 and the weights 5/20/30/40/45/50/55/56/70/110.  ``TuneIn.pm``'s
#: MENUS additionally knows ``world`` (weight 60); it is absent from the
#: LMS-9 directory OPML and therefore also absent live.
ROOT_NODES: tuple[dict[str, Any], ...] = (
    {"tag": "presets", "weight": 5, "type": "link",
     "text": "Eigene Voreinstellungen",
     "icon": "/plugins/TuneIn/html/images/radiopresets.png"},
    {"tag": "local", "weight": 20, "type": "link", "text": "Lokales Radio",
     "icon": "/plugins/TuneIn/html/images/radiolocal.png"},
    {"tag": "music", "weight": 30, "type": "link", "text": "Musik",
     "icon": "/plugins/TuneIn/html/images/radiomusic.png"},
    {"tag": "sports", "weight": 40, "type": "link", "text": "Sport",
     "icon": "/plugins/TuneIn/html/images/radiosports.png"},
    {"tag": "news", "weight": 45, "type": "link", "text": "Nachrichten",
     "icon": "/plugins/TuneIn/html/images/radionews.png"},
    {"tag": "talk", "weight": 50, "type": "link", "text": "Talksendungen",
     "icon": "/plugins/TuneIn/html/images/radiotalk.png"},
    {"tag": "location", "weight": 55, "type": "link", "text": "Nach Standort",
     "icon": "/plugins/TuneIn/html/images/radioworld.png"},
    {"tag": "language", "weight": 56, "type": "link", "text": "Nach Sprache",
     "icon": "/plugins/TuneIn/html/images/radioworld.png"},
    {"tag": "podcast", "weight": 70, "type": "link", "text": "Podcasts",
     "icon": "/plugins/TuneIn/html/images/podcasts.png"},
    {"tag": "search", "weight": 110, "type": "search",
     "text": "TuneIn durchsuchen",
     "icon": "/plugins/TuneIn/html/images/radiosearch.png"},
)

#: CLI tags of the registered sub-feeds (``OPMLBased.pm:117-125`` plus Perl's
#: ``Slim/Plugin/Sounds`` app feed, whose ``['sounds','items',…]`` dispatch
#: this port answers with Perl's placeholder — see :func:`level_for`).
RADIO_FEEDS: frozenset[str] = frozenset(
    [str(n["tag"]) for n in ROOT_NODES] + ["sounds"])

#: Live titles of the sub-feeds.  Perl takes them from the *TuneIn OPML* (the
#: feed's ``<title>``), not from a server string table — with radio-browser as
#: the source there is no such title, so the measured Perl titles are used
#: verbatim (``local items`` → "Lokale Sender", ``location`` → "Orte",
#: ``language`` → "Sprachen", ``news`` → "Nachrichten & Talk",
#: ``talk`` → "Talk").
FEED_TITLES: dict[str, str] = {
    "local": "Lokale Sender",
    "music": "Musik",
    "sports": "Sport",
    "news": "Nachrichten & Talk",
    "talk": "Talk",
    "location": "Orte",
    "language": "Sprachen",
    "podcast": "Podcasts",
    "sounds": "Sounds & Effekte",
}

#: Title of the level below a tag/country/language index and of a search.
STATIONS_TITLE = "Sender"
SEARCH_TITLE = "Suchergebnisse: {term}"

#: The tag index of the music/news/sports/talk nodes.  Perl's nodes carry
#: *category links* (music → "Country", "Weltmusik", "Top 40/Pop", …); the
#: radio-browser equivalent is a tag list whose leaf is served by
#: ``/json/stations/bytagexact/<tag>``.  Verified 2026-09-14 against
#: radio-browser: every tag below returns stations (``tennis`` returns none
#: and was dropped).
TAG_INDEX: dict[str, tuple[str, ...]] = {
    "music": ("pop", "rock", "jazz", "classical", "electronic", "hiphop",
              "country", "folk", "metal", "soul", "reggae", "blues", "dance",
              "oldies", "chillout", "alternative"),
    "sports": ("sport", "football", "soccer", "motorsport", "basketball",
               "hockey"),
    "news": ("news", "politics", "economy", "culture"),
    "talk": ("talk", "comedy", "education", "science", "community"),
}

#: The country the ``local`` node shows.  Perl gets "nearby stations" from
#: TuneIn (IP geolocation, ``Slim/Plugin/InternetRadio/TuneIn.pm`` ``local``
#: entry at :38-40) and has **no** pref for it — the whole ``%defaults`` table
#: (``Slim/Utils/Prefs.pm:132-279``) carries neither ``country`` nor any
#: location pref.  With radio-browser as the source (the approved deviation
#: above) the country becomes a server pref the user can set in the web UI:
#: ISO-3166-1 alpha-2 (``DE``); the *empty* value means "all countries".
COUNTRY_PREF = "radiobrowser_country"

#: Default of :func:`derived_country` when neither the locale nor the timezone
#: names a region.  Perl's locale default is the language ``EN``
#: (``Slim/Utils/OS.pm:411`` ``return $language || 'EN';``); the equivalent
#: fallback for a *country* is the value this port used before the pref existed.
DEFAULT_COUNTRY = "DE"

#: Locale variables in POSIX precedence order (``LC_ALL`` overrides the
#: category, which overrides ``LANG``).
_LOCALE_VARS: tuple[str, ...] = ("LC_ALL", "LC_MESSAGES", "LANG")

#: The operating system's own timezone → ISO-3166 map (one line per zone:
#: ``country<TAB>coordinates<TAB>zone<TAB>comment``).  Nothing is invented here:
#: the OS ships the mapping, so no hand-written zone table can go stale.
_ZONE_TAB = Path("/usr/share/zoneinfo/zone.tab")
_ZONE_TABLE: dict[str, str] = {}
_ZONE_TABLE_READ = False


def _region_of_locale(value: str) -> str:
    """Territory of a POSIX locale string (``de_DE.UTF-8`` → ``DE``), else ``""``.

    Perl parses the very same string in ``Slim/Utils/OS.pm:406-414``
    (``_parseLanguage``, reached through ``getSystemLanguage`` :399-404 and the
    ``language`` default :675-677)::

        $language = uc($language);          # :409
        $language =~ s/\\.UTF.*$//;          # :410
        $language =~ s/(?:_|-|\\.)\\w+$//;   # :411

    — it upper-cases, drops the codeset and then drops everything behind
    ``_``/``-``/``.``, because it wants the *language*
    (``de_DE.UTF-8`` → ``DE`` → ``DE`` is accidental; ``en_US.UTF-8`` → ``EN``).
    A country is exactly the piece Perl throws away, so the same two steps run
    here with the roles swapped: the codeset/modifier is split off first
    (``[.@]``, matching :410), then the territory behind the separator is the
    answer.  ``LANG=C``/``POSIX`` name no territory (Perl answers its ``EN``
    default at :411) → ``""``, so the caller can try the timezone.
    """
    text = (value or "").strip().upper()
    if not text:
        return ""
    text = re.split(r"[.@]", text, 1)[0]              # OS.pm:410  s/\.UTF.*$//
    parts = re.split(r"[-_]", text)                   # OS.pm:411  language[_territory]
    if len(parts) < 2:
        return ""
    region = parts[1].strip()
    return region if len(region) == 2 and region.isalpha() else ""


def _zone_table() -> dict[str, str]:
    """``zone.tab`` as ``{IANA zone: ISO country}`` (read once, cached)."""
    global _ZONE_TABLE_READ
    if _ZONE_TABLE_READ:
        return _ZONE_TABLE
    _ZONE_TABLE_READ = True
    try:
        text = _ZONE_TAB.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # no tzdata / other OS layout
        logger.debug("radio country: no %s: %s", _ZONE_TAB, exc)
        return _ZONE_TABLE
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 3:
            _ZONE_TABLE[fields[2].strip()] = fields[0].strip().upper()
    return _ZONE_TABLE


def _region_of_timezone() -> str:
    """Country of the server timezone — ``TZ`` first, then ``/etc/timezone``.

    The task's fallback when the locale names no region.  Perl has no
    counterpart (it never derives a country), so the only rule applied is the
    OS's own: the zone name is looked up in ``zone.tab`` (:func:`_zone_table`).
    """
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if not name:
        try:
            name = Path("/etc/timezone").read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.debug("radio country: no /etc/timezone: %s", exc)
            return ""
    if not name:
        return ""
    return _zone_table().get(name, "")


def _locale_region() -> str:
    """Region of the server locale under POSIX precedence, else ``""``.

    ``LC_ALL`` overrides every category, then the category (``LC_MESSAGES``)
    wins over ``LANG`` — the order ``POSIX::setlocale(LC_CTYPE)`` resolves in,
    which is what Perl reads (``Slim/Utils/OS.pm:399-404``).  The first variable
    that is *set and non-empty* therefore decides; if it names no territory the
    answer is empty and the caller goes on to the timezone, exactly like Perl
    answers its ``EN`` default for ``LANG=C`` (``:411``).
    """
    for var in _LOCALE_VARS:
        value = os.environ.get(var, "")
        if value.strip():
            return _region_of_locale(value)
    return ""


def derived_country() -> str:
    """First-start value of :data:`COUNTRY_PREF` from the server settings.

    Perl derives such a pref default once at init: ``Slim/Utils/Prefs.pm:161``
    maps ``'language' => \\&defaultLanguage``, ``:676-678`` resolves that to
    ``Slim::Utils::OSDetect->getOS->getSystemLanguage`` →
    ``POSIX::setlocale(LC_CTYPE)`` → ``_parseLanguage``
    (``Slim/Utils/OS.pm:399-414``).  The rule here is the same two sources the
    task names, in that order: the region of the locale
    (:func:`_locale_region`), else the country of the timezone
    (:func:`_region_of_timezone`), else :data:`DEFAULT_COUNTRY`.
    """
    return _locale_region() or _region_of_timezone() or DEFAULT_COUNTRY


def _stored_country() -> Optional[str]:
    """Raw value of :data:`COUNTRY_PREF`, ``None`` when it was never set."""
    try:
        from lyrion.config import get_prefs
        value = get_prefs().get(COUNTRY_PREF)
    except Exception:  # noqa: BLE001 — pref store not available
        return None
    return None if value is None else str(value)


def _as_country_code(value: Any) -> str:
    """ISO-3166-1 alpha-2 of a stored pref value (``""`` = all countries)."""
    text = str(value or "").strip().upper()
    return text[:2] if len(text) >= 2 and text[:2].isalpha() else ""


def local_country() -> str:
    """The country of the ``local`` node: the pref, else its derived default.

    The stored value wins in *every* case — including the empty string, which
    means "all countries".  Only when the pref was never set does the derived
    first-start value answer; Perl's read-side counterpart of that fallback is
    ``Slim/Utils/Light.pm:95`` ``$language ||= getPref('language') ||
    $os->getSystemLanguage();`` (the server path writes the default once
    instead — see :func:`ensure_country_pref`).
    """
    raw = _stored_country()
    if raw is None:
        return derived_country()
    return _as_country_code(raw)


async def ensure_country_pref() -> str:
    """Store the derived first-start value once, like Perl's pref ``init``.

    ``Slim/Utils/Prefs/Base.pm:178-234``: for every pref of the defaults table
    that does not exist yet, a ``CODE`` default is *called*
    (:204-206 ``$value = $hash->{ $pref }->( $class->_obj );``), put into the
    prefs hash (:226) and written to disk (:233 ``$class->_root->save if
    $changed;``) — the derivation runs exactly once, from then on the value is
    an ordinary pref a web page can change.

    ``radiobrowser_country`` has no ``%defaults`` entry yet (``config.py`` is
    outside this change's file scope), so the same code runs when the settings
    page is opened: an *unset* pref is filled once, a stored one — the empty
    "all countries" value included — is never touched again.  Returns the
    effective country (``""`` = all).

    The feed path deliberately does **not** call this: browsing Radio must not
    write settings, and until the pref is stored :func:`local_country` derives
    the value live anyway (Perl's ``Light.pm:95`` read-side order).
    """
    from lyrion.config import get_prefs
    prefs = get_prefs()
    if COUNTRY_PREF in prefs:                 # set (any value, "" included)
        return local_country()
    value = derived_country()
    try:
        await prefs.set(COUNTRY_PREF, value)
    except Exception as exc:  # noqa: BLE001 — never break a page/feed read
        logger.debug("radio country: storing %s failed: %s", COUNTRY_PREF, exc)
    return value


# ── the node model ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RadioNode:
    """One addressable feed level of the Radio directory.

    ``kind`` decides what the level contains:

    * ``index``    — link rows (Perl's ``type: "link"`` drill-downs): the
      music/news/sports/talk tag index, the country list (``location``), the
      language list (``language``) and the ``local`` entry point.
    * ``stations`` — station rows for ``arg`` (radio-browser tag, ISO country
      code, language name or search term).
    * ``presets``  — our favourites (Perl's TuneIn "My Presets" feed); rendered
      by :mod:`lyrion.web.api` because it needs the favourites manager.
    * ``empty``    — Perl's ``Leer`` placeholder (``XMLBrowser.pm:837-846``),
      used for the feeds radio-browser has no data for (podcast/sounds).
    """

    kind: str
    feed: str
    arg: str = ""
    title: str = ""


def node_sid(node: RadioNode) -> str:
    """The 8-hex browse handle of a node (Perl ``getSID``, ``XMLBrowser.pm``).

    Perl mints a random handle per browse session (``createUUID``,
    ``Slim/Utils/Misc.pm:1557-1560``) and caches the fetched feed under it
    (:353-371, ``CACHE_TIME`` 3600).  Because radio-browser results are stable
    inside :data:`CACHE_TTL`, a *deterministic* handle per node does the same
    job and survives a client that answers with an id from an earlier request.
    """
    digest = hashlib.sha1(
        f"{node.kind}|{node.feed}|{node.arg}|{node.title}".encode("utf-8"))
    sid = digest.hexdigest()[:8]
    if sid not in _sids and len(_sids) >= _MAX_SIDS:
        _sids.clear()
    _sids[sid] = node
    return sid


def node_for_sid(sid: str) -> Optional[RadioNode]:
    return _sids.get(str(sid or ""))


def parse_item_id(item_id: Any) -> tuple[Optional[RadioNode], list[int], str]:
    """Split Perl's ``<sid>[<_search>][.<index>…]`` item id.

    Perl builds the crumb as ``$sid`` plus one entry per drill-down
    (``XMLBrowser.pm:353``/``:387``), rendered as ``join('.', @crumbIndex, '')``
    plus the index (:1022).  A *search* feed replaces the sid by
    ``"$sid\_".uri_escape_utf8($search)`` (:356-358, :392-395) and recovers the
    term from the crumb at :445-447 — the same encoding is used here so clients
    see Perl-identical ids (``1a2b3c4d_rock.0``).
    """
    text = str(item_id or "")
    if not text:
        return None, [], ""
    head, _, tail = text.partition(".")
    sid, _, search = head.partition("_")
    node = node_for_sid(sid)
    indices: list[int] = []
    if tail:
        for part in tail.split("."):
            if not part.isdigit():
                return node, indices, urllib.parse.unquote(search)
            indices.append(int(part))
    return node, indices, urllib.parse.unquote(search)


def _label(tag: str) -> str:
    return tag[:1].upper() + tag[1:] if tag else tag


async def index_rows(node: RadioNode) -> list[tuple[str, RadioNode]]:
    """``(label, child)`` of every drill-down row of an ``index`` level.

    Rebuilt from the same (cached) sources every time, which is what Perl's
    per-session feed cache does for the item ids.
    """
    if node.feed == "local":
        # The pref is read here, never written: a browse must not change the
        # server's settings.  An unset pref answers with the derived default
        # (``local_country`` → ``derived_country``); Perl's read-side rule is
        # the same (``Slim/Utils/Light.pm:95``), its write happens at init
        # (``Slim/Utils/Prefs/Base.pm:178-234``) — see
        # :func:`ensure_country_pref` for the port's equivalent call site.
        code = local_country()
        if not code:
            # ``radiobrowser_country`` empty = all countries.  Perl's second
            # "Alle <Land>" row (the country-name variant of the same stations)
            # has no country to name here, so the level carries the one row
            # whose list is unfiltered (:func:`top_stations`).
            return [(STATIONS_TITLE, RadioNode("stations", "local", "",
                                               STATIONS_TITLE))]
        name = (await country_name(code)).removeprefix("The ")
        return [(STATIONS_TITLE, RadioNode("stations", "local", code,
                                           STATIONS_TITLE)),
                (f"Alle {name}", RadioNode("stations", "local", code,
                                           f"Alle {name}"))]
    if node.feed in TAG_INDEX:
        return [(_label(tag), RadioNode("stations", node.feed, tag, _label(tag)))
                for tag in TAG_INDEX[node.feed]]
    if node.feed == "location":
        return [(c["name"], RadioNode("stations", "location", c["code"],
                                      c["name"]))
                for c in await countries()]
    if node.feed == "language":
        return [(l["name"], RadioNode("stations", "language", l["name"],
                                      l["name"]))
                for l in await languages()]
    return []


async def index_child(node: RadioNode, index: int) -> Optional[RadioNode]:
    """The node behind the ``index``-th link row (``None`` = out of range)."""
    if node.kind != "index":
        return None
    rows = await index_rows(node)
    if 0 <= index < len(rows):
        return rows[index][1]
    return None


async def stations_for(node: RadioNode, *, limit: int, offset: int) -> list[Station]:
    """The station list of a leaf node (dispatch by feed)."""
    if node.feed == "search":
        return await search_stations(node.arg, limit=limit, offset=offset)
    if node.feed == "local" and not node.arg.strip():
        # Radiobrowser_country empty ("all countries"): no country filter at all.
        return await top_stations(limit=limit, offset=offset)
    if node.feed in ("location", "local"):
        return await stations_by_country(node.arg, limit=limit, offset=offset)
    if node.feed == "language":
        return await stations_by_language(node.arg, limit=limit, offset=offset)
    return await stations_by_tag(node.arg, limit=limit, offset=offset)


async def resolve(node: RadioNode, indices: list[int]
                  ) -> tuple[Optional[RadioNode], Optional[Station], int]:
    """Walk a Perl ``item_id`` path.

    Returns ``(node, station, index)``:

    * ``station`` set → the path ended on a station row of ``node``;
    * ``station`` ``None`` and ``node`` set → the path ended on a drill-down,
      i.e. ``node`` is the level to render;
    * both ``None`` → the path does not exist (out of range / expired session).
    """
    current: Optional[RadioNode] = node
    for depth, index in enumerate(indices):
        if current is None:
            return None, None, index
        if current.kind == "index":
            current = await index_child(current, index)
            if depth == len(indices) - 1:
                return current, None, index
            continue
        if current.kind == "stations":
            if depth != len(indices) - 1:
                return None, None, index
            stations = await stations_for(current, limit=index + 1, offset=index)
            return current, (stations[0] if stations else None), index
        return None, None, index
    return current, None, (indices[-1] if indices else 0)


# ── item builders (Perl's XMLBrowser item shapes) ─────────────────────────

def proxied_image(url: str) -> str:
    """``Slim::Web::ImageProxy::proxiedImage`` (``ImageProxy.pm:407-425``).

    Delegates to the port's implementation so radio logos are byte-identical
    to every other ``/imageproxy`` URL this server hands out (``web/app.py``
    serves the endpoint).
    """
    from lyrion.web.api import _perl_proxied_image

    return str(_perl_proxied_image(url))


def station_icon(station: Station) -> tuple[str, str]:
    """``(key, url)`` of a station row's image — Perl's rule, ``XMLBrowser.pm:1171``.

    ``$hash{'icon' . ($item->{icon} =~ /^https?:/ ? '' : '-id')}``: an external
    logo becomes ``icon``, a skin-relative one ``icon-id``.  No logo ⇒ no key
    at all (Perl sets none), so ``windowStyle`` stays ``text_list`` for such a
    feed (``:1434-1441``).
    """
    if station.favicon and station.favicon.lower().startswith(("http://", "https://")):
        return "icon", proxied_image(station.favicon)
    if station.favicon:
        return "icon-id", proxied_image(station.favicon)
    return "", ""


def station_item(station: Station, item_id: str, *, index: int = 0,
                 use_play_control: bool = False) -> dict[str, Any]:
    """A station row — Perl's ``type: "audio"`` item (``XMLBrowser.pm:1053-1270``).

    Field set measured live on Perl 9.1.1 (2026-09-14,
    ``local items 0 4 menu:local item_id:<sid>.0``)::

        {"type":"audio", "text":…, "presetParams":{favorites_url,
         favorites_title, favorites_type:"audio", icon},
         "icon":"/imageproxy/…/image.png", "style":"itemplay",
         "goAction":"play", "params":{item_id, isContextMenu:1,
         touchToPlay, touchToPlaySingle}}

    ``presetParams`` is ``_favoritesParams($item)`` (``:1925-1944``) — the
    source of the client's "add to favourites" call
    (``favorites add url:<favorites_url> title:<favorites_title>``).  The
    ``play``/``playControl`` branch is Perl's
    ``_defeatDestructiveTouchToPlay`` (``:1259-1272``, ``:1951-1983``).
    """
    key, icon = station_icon(station)
    params: dict[str, Any] = {"item_id": str(item_id), "isContextMenu": 1}
    item: dict[str, Any] = {
        "type": "audio",
        "text": station.title,
        "presetParams": {
            "favorites_url": station.url,
            "favorites_title": station.title,
            "favorites_type": "audio",
            "icon": station.favicon,
        },
    }
    if key:
        item[key] = icon
    if use_play_control:
        # XMLBrowser.pm:1268-1272 — the defeated branch carries neither
        # ``style`` nor the touchToPlay pair; the tap opens the
        # play/add/insert menu.
        item["goAction"] = "playControl"
        item["playControlParams"] = {"xmlbrowserPlayControl": str(index)}
    else:
        params["touchToPlay"] = str(item_id)
        params["touchToPlaySingle"] = 1
        item["goAction"] = "play"
        item["style"] = "itemplay"
    item["params"] = params
    return item


def link_item(text: str, item_id: str, *, feed: str) -> dict[str, Any]:
    """A drill-down row — Perl's ``type: "link"`` item (``XMLBrowser.pm:1233-1265``).

    Only ``actions.go`` + ``addAction: "go"``; the tap re-enters the *same*
    feed tag with the child's ``item_id`` (live Perl ``music items`` and
    ``location items`` rows).
    """
    return {
        "text": text,
        "type": "link",
        "addAction": "go",
        "actions": {
            "go": {"cmd": [feed, "items"],
                   "params": {"menu": feed, "item_id": str(item_id)}},
        },
    }


def root_menu_item(tag: str, text: str, weight: int, icon: str,
                   *, search: bool = False, **texts: str) -> dict[str, Any]:
    """One row of the ``radios`` menu — ``OPMLBased.pm:200-247``.

    With a ``menu:`` param Perl answers the jive item form: ``text``,
    ``weight``, ``icon-id`` (``proxiedImage`` of the plugin icon),
    ``actions.go`` = ``[tag,'items']`` with ``params {menu:tag}`` (plus
    ``search: '__TAGGEDINPUT__'`` for the ``type=search`` entry, :231-233) and
    ``window {titleStyle:'album'}``; a search entry also carries the ``input``
    block (:235-246) that makes the controller open its text input.
    """
    params: dict[str, Any] = {"menu": tag}
    if search:
        params["search"] = "__TAGGEDINPUT__"
    item: dict[str, Any] = {
        "text": text,
        "weight": weight,
        "icon-id": proxied_image(icon) if icon else proxied_image(
            "html/images/radio.png"),
        "actions": {"go": {"cmd": [tag, "items"], "params": params}},
        "window": {"titleStyle": "album"},
    }
    if search:
        item["input"] = {
            "len": 3,
            "help": {"text": texts.get("help") or ""},
            "softbutton1": texts.get("softbutton1") or "",
            "softbutton2": texts.get("softbutton2") or "",
        }
    return item


def root_plain_item(tag: str, text: str, weight: int, icon: str,
                    *, search: bool = False) -> dict[str, Any]:
    """One row of the plain ``radioss_loop`` — ``OPMLBased.pm:249-265``.

    ``type`` is mapped ``link → xmlbrowser`` and ``search →
    xmlbrowser_search`` (:250-256); the row carries ``cmd``/``name``/``type``/
    ``icon``/``weight`` with the proxied plugin icon.
    """
    return {
        "cmd": tag,
        "name": text,
        "type": "xmlbrowser_search" if search else "xmlbrowser",
        "icon": proxied_image(icon) if icon else proxied_image(
            "html/images/radio.png"),
        "weight": weight,
    }


# ── rendering a level ─────────────────────────────────────────────────────

@dataclass
class FeedLevel:
    """A rendered feed level, before the XMLBrowser envelope wraps it."""

    title: str = ""
    items: list[dict] = field(default_factory=list)
    total: int = 0
    feed: str = ""


async def level_for(node: RadioNode, *, start: int = 0, qty: int = 0,
                    use_play_control: bool = False) -> FeedLevel:
    """Render any radio feed level.

    ``qty <= 0`` means "as much as the level has" (no client quantity: Perl's
    ``normalize()`` uses the feed's own ``$count`` then, ``Request.pm:1815``).

    A station level's ``FeedLevel.total`` is the **feed's total**, not the size
    of the answered window: Perl's ``$subFeed->{'total'}``
    (``XMLBrowser.pm:792-793``) — see the ``stations`` branch below.  The
    play-control branch is decided by the caller and applied to the station
    rows here.
    """
    feed = node.feed
    if node.kind == "empty" or feed in ("podcast", "sounds"):
        # Perl has no station data here either: the TuneIn podcast feed needs a
        # PodcastIndex/GPodder account and the Sounds app ships audio files with
        # its plugin.  Perl's placeholder (count 1, "Leer", XMLBrowser.pm:837-846)
        # is the honest, client-usable answer — never a fabricated list.
        return FeedLevel(title=node.title or FEED_TITLES.get(feed, ""), total=0,
                         feed=feed)
    if node.kind == "index":
        rows = await index_rows(node)
        total = len(rows)
        window = rows[start:start + qty] if qty > 0 else rows[start:]
        if qty > 0 and start > total - 1:
            window = []
        sid = node_sid(node)
        items = [link_item(label, f"{sid}.{start + offset}", feed=feed)
                 for offset, (label, _child) in enumerate(window)]
        return FeedLevel(title=node.title or FEED_TITLES.get(feed, ""),
                         items=items, total=total, feed=feed)
    if node.kind == "stations":
        # ``count`` is the feed's **total**, exactly as Perl's
        # ``$subFeed->{'total'}`` (``XMLBrowser.pm:792-793``) is: Jive's list
        # stores it as the length of the whole long list (``DB.lua:63``
        # ``count = 0, -- =last_chunk.count, the total number of items in the
        # long list``), pages in 200-row chunks towards it (``DB.lua:272-300``
        # ``missing``) and **drops and refetches everything** whenever a chunk
        # reports a different count (``DB.lua:125-136``).  The former
        # ``start + len(items) + 1`` marker therefore had two effects: the
        # first answer already looked like the end of the list (100 of 6001
        # country stations) and every following page invalidated the client's
        # cache.  Squeezer/Squeeze Client read ``count`` the same way.
        total = await node_total(node)
        if total is None:
            # No published total (``search``, unfiltered ``local``): the list
            # itself is the feed and every page is a slice of it.
            whole = await full_stations(node)
            total = len(whole)
            window = whole[start:start + qty] if qty > 0 else whole[start:]
        else:
            # Perl's ``_quantity`` page of a feed whose total is known
            # (``Request.pm:1805-1839``).  A request without a quantity gets
            # the whole (bounded) list — Perl's ``normalize`` defaults
            # ``$numofitems`` to ``$count`` in that case.
            limit = qty if qty > 0 else _MAX_LIMIT
            window = await stations_for(node, limit=limit, offset=start)
        sid = node_sid(node)
        items = [station_item(s, f"{sid}.{start + index}", index=start + index,
                              use_play_control=use_play_control)
                 for index, s in enumerate(window)]
        if feed == "search":
            title = SEARCH_TITLE.format(term=node.arg)
        else:
            title = node.title or STATIONS_TITLE
        return FeedLevel(title=title, items=items, total=total, feed=feed)
    return FeedLevel(title=node.title, total=0, feed=feed)


def empty_placeholder() -> dict[str, Any]:
    """Perl's ``Leer`` row (``XMLBrowser.pm:837-846`` / :1183-1185)."""
    from lyrion.web import menus

    return menus.empty_placeholder_item()


def root_level(*, title_only: bool = False) -> list[dict[str, Any]]:
    """``ROOT_NODES`` as they appear in the ``radios`` query.

    ``title_only`` (the plain, non-menu form) keeps ``ROOT_NODES``' labels —
    the menu form gets its labels from the same table.
    """
    return [dict(n) for n in ROOT_NODES]


def home_menu_items() -> list[dict[str, Any]]:
    """The radio sub-nodes as **home-menu** rows — Perl's ``@pluginMenus``.

    Perl registers one dynamic OPMLBased plugin per directory item
    (``Slim/Plugin/InternetRadio/Plugin.pm:150-153``::

        $class->SUPER::initPlugin(
            tag    => '$tag',
            menu   => 'radios',
            weight => $weight,
            type   => '$type',
        );

    and ``initPlugin`` hands each plugin's jive menu to the controller
    registry (``Slim/Plugin/OPMLBased.pm:48-52``)::

        if ( my $menu = $class->initJive( %args ) ) {
            ...
            Slim::Control::Jive::registerPluginMenu($menu);
        }

    ``OPMLBased::initJive`` (:66-91) builds exactly one item per sub-node::

        id             => 'opml' . $args{tag},
        uuid           => $class->_pluginDataFor('id'),
        node           => $args{node} || $args{menu} || 'plugins',
        weight         => $class->weight,
        displayWhenOff => 0,
        window         => { 'icon-id' => $icon, titleStyle => 'album' },
        actions        => { go => { player => 0, cmd => [ $args{tag}, 'items' ],
                                    params => { menu => $args{tag} } } },

    and, for ``type eq 'search'``, the ``input`` block (:93-106).  With
    ``menu => 'radios'`` the item's ``node`` is therefore ``'radios'`` —
    these rows are the **children of the Radio node**, not extra home rows.

    Why they must be sent (measured against SqueezePlay's own Lua, read-only):

    * the client creates the Radio entry *itself* —
      ``share/jive/jive/JiveMain.lua:464``::

          jiveMain:addNode( { id = 'radios', iconStyle = 'hm_radio',
                              node = 'home', text = str("INTERNET_RADIO"),
                              weight = 20 } )

      and **drops** the server's identical node:
      ``applets/SlimMenus/SlimMenusApplet.lua:569-570``
      (``elseif item.id == "radios" then --ignore, shown locally``);
    * ``share/jive/jive/ui/HomeMenu.lua:567-580`` (``addItem``) adds a node to
      its parent menu only once it has a child::

          local nodeEntry = self.nodeTable[item.node]
          if nodeEntry and nodeEntry.item then
              local hasEntry = pairs(nodeEntry.items)(nodeEntry.items)
              if hasEntry then self:addItem(nodeEntry.item) end
          end

      so without a row whose ``node`` is ``'radios'`` the locally created
      Radio node never reaches the home menu — the reported
      "SqueezePlay hat keinen Radio-Eintrag im Home-Menü".

    Perl's live ``menu 0 100 direct:1`` (192.168.1.90, read-only 2026-09-14)
    carries these ten rows (indices 16-25 of 54): ``opmlpresets``,
    ``opmllocal``, ``opmlmusic``, ``opmlnews``, ``opmlsports``, ``opmltalk``,
    ``opmllocation``, ``opmllanguage``, ``opmlsearch``, ``opmlpodcast`` (plus
    ``opmlsounds``, a My-Apps app item, and ``opmlmyapps`` — both outside the
    Radio node and therefore not emitted here).
    """
    from lyrion.web.menus import _search_input

    items: list[dict[str, Any]] = []
    for node in ROOT_NODES:
        tag = str(node["tag"])
        icon = str(node["icon"] or "html/images/radio.png")
        go_params: dict[str, Any] = {"menu": tag}
        item: dict[str, Any] = {
            "text": node["text"],
            # OPMLBased.pm:69 ``'opml' . $args{tag}``
            "id": f"opml{tag}",
            # OPMLBased.pm:70 — a generated plugin has no manifest id: null.
            "uuid": None,
            # OPMLBased.pm:71 ``$args{node} || $args{menu}`` = 'radios'.
            "node": "radios",
            "weight": node["weight"],
            "displayWhenOff": 0,                       # OPMLBased.pm:73
            "window": {"icon-id": proxied_image(icon),  # :74-77
                       "titleStyle": "album"},
            "actions": {"go": {"player": 0,           # :78-85
                               "cmd": [tag, "items"],
                               "params": go_params}},
        }
        if node.get("type") == "search":
            # OPMLBased.pm:93-106 (Bug 12336).
            go_params["search"] = "__TAGGEDINPUT__"
            item["input"] = _search_input(str(node["text"]))
        items.append(item)
    return items


__all__ = [
    "CACHE_TTL",
    "COUNTRY_PREF",
    "DEFAULT_COUNTRY",
    "FEED_TITLES",
    "HTTP_TIMEOUT",
    "RADIO_BROWSER_HOSTS",
    "RADIO_FEEDS",
    "ROOT_NODES",
    "SEARCH_TITLE",
    "STATIONS_TITLE",
    "TAG_INDEX",
    "USER_AGENT",
    "FeedLevel",
    "RadioNode",
    "Station",
    "countries",
    "country_name",
    "country_total",
    "derived_country",
    "empty_placeholder",
    "ensure_country_pref",
    "full_stations",
    "home_menu_items",
    "index_child",
    "index_rows",
    "language_total",
    "languages",
    "level_for",
    "link_item",
    "local_country",
    "node_for_sid",
    "node_sid",
    "node_total",
    "parse_item_id",
    "proxied_image",
    "resolve",
    "root_level",
    "root_menu_item",
    "root_plain_item",
    "search_stations",
    "station_icon",
    "station_item",
    "stations_by_country",
    "stations_by_language",
    "stations_by_tag",
    "stations_for",
    "tag_total",
    "top_stations",
]
