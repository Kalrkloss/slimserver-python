"""Online-Albumcover-Suche — Vorbild: Kodi „Universal Album Scraper“ (UAS).

Der Perl-LMS kennt **keinen** Online-Cover-Anbieter.  Seine Kette endet bei
``Slim/Music/Artwork.pm:388-400`` (``readCoverArt``): eingebettetes Bild aus den
Tags (``_readCoverArtTags``, ``:480-516``) → Bilddatei im Ordner
(``_readCoverArtFiles``, ``:517-637``) → gar nichts, und dann liefert
``Slim/Web/Graphics.pm:275-291`` das generische ``html/images/cover_*.png``
(live geprüft: ``/html/images/cover_200x200.png`` → 200, 10697 Bytes, image/png).
Kein ``http://``-Anbieter, kein MusicBrainz/Cover-Art-Archive, kein Cache für
remote Cover — im ganzen Perl-Baum gibt es kein ``coverartarchive`` /
``theaudiodb`` / ``fanart`` (``grep -rn`` über ``*.pm``: keine Treffer).

Deshalb ist dieser Anbieterteil eine **Zutat dieses Ports**, strukturell aber
genau wie Perl: die Vorrang-Regel (Tag-Bild → Ordnerbild → hier) und der
Plattencache bleiben Perls Struktur; nur *welche* Quelle gefragt wird, ist am
Kodi-UAS orientiert.

Kodi-UAS als Vorlage (``metadata.album.universal``, v3.1.20, Zip von
``https://mirrors.kodi.tv/addons/omega/metadata.album.universal/``):

* ``addon.xml:7-12`` — der Scraper hängt an ``metadata.common.musicbrainz.org``,
  ``metadata.common.fanart.tv``, ``metadata.common.theaudiodb.com`` und
  ``metadata.common.allmusic.com``.
* ``albumuniversal.xml:6-10`` (``CreateAlbumSearchUrl``) — der **Album-Match**
  läuft über MusicBrainz: ``$INFO[mbsite]/ws/2/release/?fmt=xml&query=
  release:"<album>" AND (artistname:"<artist>" OR artist:"<artist>")``;
  ``mbsite`` hat den Default ``https://musicbrainz.org``
  (``resources/settings.xml:24``).
* ``albumuniversal.xml:42-49`` (``GetAlbumDetails``) — aus dem Treffer werden
  **Release-**MBID (dest 3) und **Release-Group-**MBID (dest 4) gelesen; die
  Release-Group-MBID ist der Schlüssel für die Bild-Anbieter.
* ``albumuniversal.xml:122-137`` — die Bild-Kette in dieser Reihenfolge:
  ``GetFanartTvAlbumThumbsByMBID`` (conditional ``fanarttvalbumthumbs``),
  ``GetFanartTvAlbumDiscartByMBID``, ``GetTADBAlbumThumbsByMBID``
  (conditional ``tadbalbumthumbs``), ``GetTADBAlbumDiscartByMBID``,
  ``GetTADBAlbumBackByMBID``, ``GetTADBAlbumSpineByMBID``; danach
  ``allmusicalbumthumbs`` (``resources/settings.xml:15-21``; Defaults:
  fanart.tv an, TheAudioDB an, allmusic aus).
* ``tadb.xml:676-680`` — TheAudioDB wird per MBID gefragt:
  ``https://www.theaudiodb.com/api/v1/json/<key>/album-mb.php?i=<release-group>``;
  der Key steckt im Addon, nicht in einer Nutzer-Einstellung
  (``resources/settings.xml`` kennt nur ``tadbalbumlanguage``, Default ``en``,
  ``:12``).
* ``fanarttv.xml:5`` — fanart.tv wird mit ``webservice.fanart.tv/v3/...?api_key=…``
  gefragt.

Unser Anbieterteil übernimmt von UAS: MusicBrainz als **Matcher** (Titel +
Interpret, Treffer-Score), die **Release-Group**-MBID als gemeinsamen Schlüssel
für die Bildquellen, die abgestufte Kette, Sprache aus der Konfiguration und
sauberes Verhalten bei 0 Treffern (kein Fallback ins Blaue).  Der **Interpret
gehört immer zum Treffer**: er steht in jeder Suchstufe (UAS' Phrase,
``albumuniversal.xml:6-10``) *und* wird am Kandidaten geprüft
(:func:`candidate_matches`) — bei einem mehrdeutigen Albumnamen wie „Greatest
Hits“ ist er der einzige Unterschied zwischen den Katalog-Einträgen.  Passt
kein Kandidat mit dem Album-Interpreten zusammen oder widersprechen sich die
Kandidaten darin, gibt es **kein** Cover statt eines fremden
(:func:`select_candidate`).  Abweichend:

* **Cover Art Archive zuerst.**  Kodi holt das Vorder-Cover bei fanart.tv /
  TheAudioDB; das CAA (``coverartarchive.org/release-group/<mbid>/front-500``)
  ist die MusicBrainz-eigene, **schlüsselfreie** Quelle und steht daher vor den
  beiden Anbietern, die einen API-Key brauchen.  Damit funktioniert die Kette
  „ohne Key“ (Vorgabe der Aufgabe) — ``audiodb``/``fanart`` werden ohne Key nur
  geloggt übersprungen, nie mit einem im Code hinterlegten Schlüssel gefragt.
* **Keys nur aus der Konfiguration** (``web/settings.py``), nie im Code; der
  Nutzer kann eigene Keys eintragen.
* **Plattencache + DB**, damit ein einmal gefundenes Cover nie erneut gesucht
  wird und offline funktioniert (UAS hat nur einen flüchtigen XML-``cache``-Tag).
* **Kein Netz im Wiedergabepfad**: ``read_cached_cover`` liest nur Platte/DB;
  Suchen laufen über die Queue (``request_lookup``) bzw. im Scan nachgelagert.

Der Cache liegt absichtlich in einer **eigenen** SQLite-Datei
(``<cache_dir>/art_online.db``, Tabelle ``artwork_online_cache``).  Damit fasst
diese Suche die Bibliotheks-DB nicht an; wer die Treffer direkt am Album
braucht, schreibt ``albums.artwork`` (Verdrahtungspunkt, siehe Modulende).
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import io
import json
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pref-Namen (gelesen/geschrieben von lyrion.web.settings)
# ---------------------------------------------------------------------------

PREF_ENABLED = "artworkOnlineSearch"
PREF_PROVIDERS = "artworkOnlineProviders"
PREF_LANGUAGE = "artworkOnlineLanguage"
PREF_COUNTRY = "artworkOnlineCountry"
PREF_CACHE_DIR = "artworkOnlineCacheDir"
PREF_RETRY_DAYS = "artworkOnlineRetryDays"
PREF_TIMEOUT = "artworkOnlineTimeout"
PREF_AUDIODB_KEY = "artworkOnlineAudioDbKey"
PREF_FANART_KEY = "artworkOnlineFanartKey"

#: Alle Prefs dieser Suche mit ihrem Vorbelegungswert (Vorbild: UAS-Defaults
#: ``resources/settings.xml:11-21``).  Die Reihenfolge ist die Anzeige-Reihenfolge
#: der Settings-Seite.
PREF_DEFAULTS: dict[str, str] = {
    PREF_ENABLED: "1",
    PREF_PROVIDERS: "musicbrainz,coverartarchive,audiodb,fanart",
    PREF_LANGUAGE: "en",          # UAS: tadbalbumlanguage, settings.xml:12
    PREF_COUNTRY: "",             # UAS kennt kein Land; freies Feld für TADB/Sprache
    PREF_CACHE_DIR: "",           # leer = <serverdata>/artwork-online
    PREF_RETRY_DAYS: "30",
    PREF_TIMEOUT: "8",
    PREF_AUDIODB_KEY: "",
    PREF_FANART_KEY: "",
}

# ---------------------------------------------------------------------------
# Anbieter
# ---------------------------------------------------------------------------

#: ``musicbrainz`` = Matcher (Release-/Release-Group-Suche), ``coverartarchive``
#: = Bild vom CAA, ``audiodb`` = TheAudioDB, ``fanart`` = fanart.tv.
DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "musicbrainz", "coverartarchive", "audiodb", "fanart",
)

#: Anbieter, die ohne API-Key arbeiten (MusicBrainz bittet nur um einen
#: aussagekräftigen User-Agent, ``https://musicbrainz.org/doc/MusicBrainz_API``).
KEYLESS_PROVIDERS: tuple[str, ...] = ("musicbrainz", "coverartarchive")

#: Anbieter → Pref mit dem API-Key.  Ohne Wert in der Konfiguration wird der
#: Anbieter übersprungen und geloggt („ohne Key funktioniert es“).
PROVIDER_KEY_PREFS: dict[str, str] = {
    "audiodb": PREF_AUDIODB_KEY,
    "fanart": PREF_FANART_KEY,
}

#: Anbieter, die eine Release-Group-MBID brauchen (UAS: MBID dest 4).
PROVIDER_NEEDS_MBID: tuple[str, ...] = ("coverartarchive", "audiodb", "fanart")

#: Schreibweisen, die die Konfiguration zulässt (die Liste bleibt kurz, damit
#: ein Tippfehler nicht stillschweigend einen anderen Anbieter aktiviert).
PROVIDER_ALIASES: dict[str, str] = {
    "musicbrainz": "musicbrainz",
    "mb": "musicbrainz",
    "coverartarchive": "coverartarchive",
    "caa": "coverartarchive",
    "coverart": "coverartarchive",
    "audiodb": "audiodb",
    "theaudiodb": "audiodb",
    "tadb": "audiodb",
    "fanart": "fanart",
    "fanarttv": "fanart",
    "fanart.tv": "fanart",
}

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
AUDIODB_BASE = "https://theaudiodb.com/api/v1/json"
FANART_BASE = "https://webservice.fanart.tv/v3/music"

#: MusicBrainz verlangt einen erkennbaren User-Agent
#: (``https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting``).  Kein Secret.
USER_AGENT = "LyrionMusicServer/9.0 (https://lyrion.org)"

#: Bild-Prüfung (Treffer nur mit echtem Bild, nicht mit HTML/JSON-Resten).
MIN_IMAGE_EDGE = 200
MAX_IMAGE_BYTES = 12 * 1024 * 1024

#: Treffer-Schwellen der Namensprüfung.  UAS verlässt sich auf MusicBrainz'
#: ``score``; wir prüfen zusätzlich den Titel selbst, weil MBs Suche gerade bei
#: Zusätzen wie „(Deluxe Edition)“ sehr grosszügig ist.
TITLE_MIN_RATIO = 0.72
ARTIST_MIN_RATIO = 0.66
YEAR_TOLERANCE = 1

#: CAA-Spezifikationen in dieser Reihenfolge („front“ = Original).
CAA_SPECS: tuple[str, ...] = ("front-500", "front-250", "front")

_SPECIALS_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
#: UAS kürzt den Interpreten an dieser Stelle (``albumuniversal.xml:11-20``).
_ARTIST_SPLIT_RE = re.compile(r"\s+(?:ft\.?|feat\.?|featuring|and|&|/)\s+", re.IGNORECASE)
#: Klammer-Zusätze („(MMXXIII Version)“, „[Deluxe]“) — UAS sucht mit dem
#: vollen Titel; die zweite Suchstufe nimmt den Zusatz weg, weil MusicBrainz'
#: Lucene-Suche Klammern im Phrasen-Ausdruck nicht auflöst (live geprüft:
#: ``release:"Point Blank (MMXXIII Version)"`` → ``count: 0``).
_BRACKET_RE = re.compile(r"[\(\[]\s*[^\)\]]*[\)\]]")


class ProviderError(RuntimeError):
    """Ein Anbieter ist fehlgeschlagen (Netz, HTTP-Status, kaputtes JSON)."""


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtOnlineSettings:
    """Laufzeit-Konfiguration der Online-Suche (Quelle: ``web/settings.py``)."""

    enabled: bool = True
    providers: tuple[str, ...] = DEFAULT_PROVIDER_ORDER
    language: str = "en"
    country: str = ""
    cache_dir: Path = field(default_factory=lambda: default_cache_dir())
    retry_days: int = 30
    timeout: float = 8.0
    audiodb_key: str = ""
    fanart_key: str = ""
    min_edge: int = MIN_IMAGE_EDGE
    user_agent: str = USER_AGENT

    def effective_providers(self) -> tuple[str, ...]:
        """Anbieter-Reihenfolge ohne die, deren Key fehlt.

        Die Reihenfolge der Konfiguration bleibt erhalten — nur keylose
        Anbieter ohne Key fallen weg.  Wird das geloggt, damit ein leeres
        Ergebnis nachvollziehbar ist („ohne Key funktioniert es trotzdem“
        heisst: CAA bleibt).
        """
        active: list[str] = []
        for name in self.providers:
            key_pref = PROVIDER_KEY_PREFS.get(name)
            if key_pref and not self.key_for(name):
                logger.debug(
                    "art_online: Anbieter %r übersprungen — kein API-Key "
                    "konfiguriert (%s)", name, key_pref)
                continue
            active.append(name)
        return tuple(active)

    def key_for(self, provider: str) -> str:
        """API-Key eines Anbieters — NUR aus der Konfiguration, nie aus dem Code."""
        if provider == "audiodb":
            return (self.audiodb_key or "").strip()
        if provider == "fanart":
            return (self.fanart_key or "").strip()
        return ""

    def fingerprint(self) -> str:
        """Kennung der **suchrelevanten** Einstellungen (für die wanted-Liste).

        Ändert sich diese Kennung, ist ein früherer Fehlschlag nicht mehr
        bindend: ein neuer API-Key (``PROVIDER_KEY_PREFS``), ein anderer
        Anbieter, eine andere Sprache/ein anderes Land oder ein anderer
        Schalter — ``media/art_online_wanted.py`` arbeitet die Liste dann erneut
        ab (ausdrückliche Anforderung „neuer API-Key hinzugekommen“).

        Die Keys selbst werden **nicht** gespeichert, nur ihr SHA1-Abdruck:
        eine Kennung darf keinen Geheimwert in eine Datei schreiben.  Die
        Reihenfolge kommt aus :meth:`effective_providers`, damit ein Key, der
        einen Anbieter erst aktiviert, die Kennung ebenfalls ändert.
        """
        def _key_digest(value: str) -> str:
            value = (value or "").strip()
            if not value:
                return ""
            return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]

        parts = (
            "1" if self.enabled else "0",
            ",".join(self.effective_providers()),
            str(self.providers),
            self.language.strip().casefold(),
            self.country.strip().casefold(),
            _key_digest(self.audiodb_key),
            _key_digest(self.fanart_key),
        )
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ArtOnlineSettings":
        """Einstellungen aus gelesenen Pref-Werten bauen (``web/settings.py``)."""
        merged = dict(PREF_DEFAULTS)
        merged.update({k: v for k, v in values.items() if v is not None})
        cache_raw = str(merged.get(PREF_CACHE_DIR) or "").strip()
        cache_dir = Path(cache_raw).expanduser() if cache_raw else default_cache_dir()
        return cls(
            enabled=_as_bool(merged.get(PREF_ENABLED), default=True),
            providers=normalize_provider_list(str(merged.get(PREF_PROVIDERS) or "")),
            language=str(merged.get(PREF_LANGUAGE) or "en").strip() or "en",
            country=str(merged.get(PREF_COUNTRY) or "").strip(),
            cache_dir=cache_dir,
            retry_days=_as_int(merged.get(PREF_RETRY_DAYS), 30),
            timeout=_as_float(merged.get(PREF_TIMEOUT), 8.0),
            audiodb_key=str(merged.get(PREF_AUDIODB_KEY) or ""),
            fanart_key=str(merged.get(PREF_FANART_KEY) or ""),
        )


def _as_bool(value: Any, *, default: bool) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "ja"):
        return True
    if text in ("0", "false", "no", "off", "nein", ""):
        return False if text else default
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_provider_list(text: str) -> tuple[str, ...]:
    """Anbieter-Liste aus der Konfiguration normalisieren.

    Akzeptiert Komma- und/oder Leerzeichen-getrennte Namen, erhält die
    eingegebene Reihenfolge (= Priorität), kennt nur die vier bekannten Namen
    und fällt bei leerer/unbekannter Eingabe auf :data:`DEFAULT_PROVIDER_ORDER`
    zurück.  Ein Tippfehler darf die Kette nicht leer machen.
    """
    raw = re.split(r"[,\s;]+", text.strip()) if text else []
    known = [item.strip().lower() for item in raw if item.strip()]
    out: list[str] = []
    for name in known:
        resolved = PROVIDER_ALIASES.get(name)
        if resolved is None:
            logger.warning("art_online: unbekannter Anbieter %r ignoriert", name)
            continue
        if resolved not in out:
            out.append(resolved)
    if not out:
        return DEFAULT_PROVIDER_ORDER
    return tuple(out)


def default_cache_dir() -> Path:
    """Vorbelegtes Cache-Verzeichnis: ``<serverdata>/artwork-online``.

    Perl legt seinen Artwork-Cache parallel zu den Bibliotheksdaten ab
    (``Slim/Utils/ArtworkCache.pm:44-47``: ``librarycachedir``); wir nehmen das
    Serverdata-Verzeichnis unseres Ports.  Ohne initialisierte Config (Tests,
    Einzelskripte) bleibt ``~/.lyrion/artwork-online``.
    """
    try:
        from lyrion.config import get_config

        return Path(get_config().serverdata_dir) / "artwork-online"
    except Exception:  # noqa: BLE001 - Start ohne Library/Config ist erlaubt
        return Path.home() / ".lyrion" / "artwork-online"


# ---------------------------------------------------------------------------
# Anfrage + Ergebnis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlbumQuery:
    """Ein Album, für das ein Cover gesucht werden soll."""

    album: str
    artist: str = ""
    year: int | None = None
    #: MusicBrainz-Release-Group-MBID, falls die Bibliothek sie schon kennt
    #: (``albums.musicbrainz_id``) — dann entfällt die Suche.
    mbid: str | None = None

    def key(self) -> str:
        """Stabiler Cache-Schlüssel (normalisiert, also tag-schreibweisen-tolerant)."""
        parts = "|".join((
            normalize(self.album),
            normalize(self.artist),
            str(self.year or ""),
        ))
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class Candidate:
    """Ein MusicBrainz-Treffer (UAS' „dest 3/4“-Paar aus ``albumuniversal.xml:42-49``)."""

    release_id: str = ""
    release_group_id: str = ""
    title: str = ""
    group_title: str = ""
    artist: str = ""
    year: int | None = None
    score: int = 0
    primary_type: str = ""


@dataclass(frozen=True)
class CoverResult:
    """Ein gefundenes (oder aus dem Cache geladenes) Album-Cover."""

    path: Path
    provider: str
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    mbid: str | None = None
    source_url: str = ""
    from_cache: bool = False


@dataclass(frozen=True)
class CacheEntry:
    """Eine Zeile der Cache-Tabelle (Treffer *oder* Fehlschlag)."""

    album_key: str
    status: str            # "hit" | "miss"
    provider: str = ""
    mbid: str | None = None
    source_url: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    created_at: int = 0
    reason: str = ""
    file_name: str = ""

    def cover_path(self, cache_dir: Path) -> Path | None:
        if self.status != "hit" or not self.file_name:
            return None
        path = cache_dir / self.file_name
        return path if path.is_file() else None


# ---------------------------------------------------------------------------
# Namensvergleich (Trefferprüfung)
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Kleinschreibung, Akzente weg, Sonderzeichen weg (Vergleichsform)."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_ish = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_ish.casefold()
    stripped = _SPECIALS_RE.sub(" ", lowered)
    return _SPACES_RE.sub(" ", stripped).strip()


def similarity(a: str, b: str) -> float:
    """Ähnlichkeit zweier Namen (0..1) in Vergleichsform."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


#: Führende Artikel, die der Import-Sortierschlüssel abschneidet
#: (``media/importer.py:91-98``: „The Beatles“ → ``beatles``).
_ARTICLES = ("the ", "a ", "an ", "der ", "die ", "das ", "le ", "la ", "les ")


def artist_key(text: str) -> str:
    """Vergleichsform des **Interpreten**: normalisiert, ohne führenden Artikel,
    ohne Leerzeichen.

    Dieselbe Bildung wie der Sortierschlüssel des Imports
    (``media/importer.py:91-98``), nur zusätzlich ohne Leerzeichen, damit
    Schreibvarianten desselben Namens zusammenfallen: „The Police“ →
    ``police``, „BlutEngel“ = „blut engel`` → ``blutengel``.
    """
    key = normalize(text)
    for article in _ARTICLES:
        if key.startswith(article):
            key = key[len(article):]
            break
    return key.replace(" ", "")


def _artist_matches(candidate_artist: str, query_artist: str, *,
                    artist_min: float = ARTIST_MIN_RATIO) -> bool:
    """Trägt der Kandidat den gesuchten Interpreten?  (Pflichtprüfung)

    Zuerst die Identität (:func:`artist_key`), dann eine Ähnlichkeitsstufe, die
    **nur** für Varianten desselben Namens gilt (einer der beiden Schlüssel
    enthält den anderen: „Nina Hagen“ ⊂ „Nina Hagen Band“).  Reine Ähnlichkeit
    genügt absichtlich nicht: ``difflib`` hält „The Police“ und „The Cure“ für
    75 % gleich (``ARTIST_MIN_RATIO`` = 0,66) — und genau solche Verwechslungen
    sollen nicht mehr passieren.  Ohne gesuchten Interpreten gibt es keinen
    Treffer: der Titel allein ist bei mehrdeutigen Albumnamen kein Schlüssel.
    """
    if not str(candidate_artist or "").strip() or not str(query_artist or "").strip():
        return False
    a, b = artist_key(candidate_artist), artist_key(query_artist)
    if a == b:
        return True
    if similarity(candidate_artist, query_artist) < artist_min:
        return False
    return bool(a) and bool(b) and (a in b or b in a)


def _year_of(text: Any) -> int | None:
    """Jahreszahl aus ``strAlbumThumb``-Umfeld bzw. ``date``-Feldern (``1989-12-06``)."""
    match = re.match(r"\s*(\d{4})", str(text or ""))
    return int(match.group(1)) if match else None


def candidate_matches(
    candidate: Candidate,
    query: AlbumQuery,
    *,
    title_min: float = TITLE_MIN_RATIO,
    artist_min: float = ARTIST_MIN_RATIO,
) -> bool:
    """Prüft Treffer wie UAS' Suche-plus-``score``: Titel, Interpret, Jahr.

    **Der Interpret ist Pflicht** — UAS' Suchphrase verlangt ihn
    (``albumuniversal.xml:6-10``: ``release:"<album>" AND (artistname:"<artist>"
    OR artist:"<artist>")``), und bei mehrdeutigen Albumnamen entscheidet allein
    er: „Greatest Hits“ gibt es in dieser Sammlung von Fleetwood Mac, Barry
    Manilow, Janis Joplin, ZZ Top, The Police und Bob Marley.  Ein Album ohne
    Interpreten hat damit **keinen** prüfbaren Treffer (lieber kein Cover als ein
    falsches; Perl zeigt dann den generischen Platzhalter,
    ``Slim/Web/Graphics.pm:275-291``).

    Der Titel darf um Klammer-Zusätze abweichen („Point Blank (MMXXIII
    Version)“ vs. „Point Blank MMXXIII“), das Jahr aber höchstens um
    :data:`YEAR_TOLERANCE` — sonst zieht eine Reissue von 1989 das Cover einer
    2023er-Ausgabe (oder umgekehrt).
    """
    if not normalize(query.artist):
        return False
    title = max(
        similarity(candidate.title, query.album),
        similarity(candidate.group_title, query.album),
    )
    if title < title_min:
        return False
    if not _artist_matches(candidate.artist, query.artist, artist_min=artist_min):
        return False
    if query.year and candidate.year:
        if abs(query.year - candidate.year) > YEAR_TOLERANCE:
            return False
    return True


def _candidate_rank(candidate: Candidate, query: AlbumQuery) -> tuple[int, float, int]:
    title = max(
        similarity(candidate.title, query.album),
        similarity(candidate.group_title, query.album),
    )
    year_hit = 1 if (query.year and candidate.year == query.year) else 0
    return (year_hit, title, candidate.score)


def select_candidate(
    candidates: Iterable[Candidate],
    query: AlbumQuery,
    *,
    title_min: float = TITLE_MIN_RATIO,
    artist_min: float = ARTIST_MIN_RATIO,
) -> tuple[Candidate | None, str]:
    """Besten passenden Treffer wählen — oder sagen, **warum** keiner passt.

    Rückgabe ``(treffer, grund)``; ``grund`` ist leer, wenn ein Treffer gefunden
    wurde, sonst der Klartext für den ``miss``-Eintrag.

    Mehrdeutigkeit ist ein Fehlschlag **mit Grund**, kein Raten: passen mehrere
    Kandidaten zum Titel und tragen dabei *verschiedene* Interpreten, von denen
    keiner genau der gesuchte ist, gibt es keinen Treffer.  UAS nimmt dort den
    ``score``-besten Treffer seiner Phrase (``albumuniversal.xml:6-10``); unsere
    Leiter darf aber nur den *Titel* entspannen (:func:`mb_query_variants`), also
    muss der Interpret den Ausschlag geben — lieber kein Cover als ein fremdes.
    """
    valid = [c for c in candidates
             if candidate_matches(c, query, title_min=title_min, artist_min=artist_min)]
    if not valid:
        return None, "kein passender Treffer"
    wanted = artist_key(query.artist)
    exact = [c for c in valid if artist_key(c.artist) == wanted]
    if exact:
        valid = exact
    else:
        artists = {artist_key(c.artist) for c in valid}
        if len(artists) > 1:
            names = ", ".join(sorted(a for a in artists if a)[:3])
            return None, (f"mehrdeutig: {len(artists)} Interpreten zu "
                          f"{query.album!r} ({names})")
    valid.sort(key=lambda c: _candidate_rank(c, query), reverse=True)
    return valid[0], ""


def pick_candidate(
    candidates: Iterable[Candidate],
    query: AlbumQuery,
    *,
    title_min: float = TITLE_MIN_RATIO,
    artist_min: float = ARTIST_MIN_RATIO,
) -> Candidate | None:
    """Besten passenden Treffer wählen (Jahr, dann Titelähnlichkeit, dann MB-Score).

    Dünne Hülle um :func:`select_candidate`; der Grund bleibt dort.
    """
    return select_candidate(candidates, query, title_min=title_min,
                            artist_min=artist_min)[0]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass
class FetchResponse:
    """Antwort eines ``GET`` (bewusst schmal, damit Tests einen Fake einsetzen)."""

    status: int
    content: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return str(value).split(";")[0].strip().lower()
        return ""

    def json(self) -> Any:
        try:
            return json.loads(self.content.decode("utf-8", "replace"))
        except ValueError as exc:  # pragma: no cover - defensive
            raise ProviderError(f"ungültiges JSON von {self.url}") from exc


#: MusicBrainz bittet um höchstens **eine Anfrage pro Sekunde**
#: (``https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting``).  Ohne
#: Drossel antwortet der Dienst mit 503 — live belegt am 2026-09-19: zwei
#: Suchen 0,5 s auseinander (Album-Queue) → ``MusicBrainz 503 (Rate-Limit)``.
#: Die Drossel sitzt im echten HTTP-Pfad (:meth:`HttpFetcher.get`), nicht in
#: der Suchfunktion — Tests mit einem Fake-Fetcher warten also nicht.
MB_MIN_INTERVAL = 1.0

_mb_lock: asyncio.Lock = asyncio.Lock()
_mb_last_request = 0.0


async def _throttle_musicbrainz() -> None:
    """Nächste MusicBrainz-Anfrage auf ``MB_MIN_INTERVAL`` Abstand schieben."""
    global _mb_last_request
    async with _mb_lock:
        wait = MB_MIN_INTERVAL - (time.monotonic() - _mb_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _mb_last_request = time.monotonic()


class HttpFetcher:
    """Dünner ``httpx``-Wrapper: User-Agent, Umleitungen, Timeout, Fehlerlog.

    ``httpx`` ist Projektabhängigkeit (``pyproject.toml:11``; so auch
    ``lyrion.music.radio`` und ``lyrion.web.stream``).  Jeder Fehler wird als
    :class:`ProviderError` geworfen — der Aufrufer protokolliert ihn und macht
    mit dem nächsten Anbieter weiter.
    """

    def __init__(self, settings: ArtOnlineSettings) -> None:
        self.settings = settings
        self.headers = {
            "User-Agent": settings.user_agent or USER_AGENT,
            "Accept": "application/json, image/*;q=0.9, */*;q=0.5",
            "Accept-Language": settings.language or "en",
        }
        self._client: Any = None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout,
                follow_redirects=True,   # CAA antwortet 307 auf archive.org
                headers=self.headers,
            )
        return self._client

    async def get(self, url: str, params: Mapping[str, Any] | None = None) -> FetchResponse:
        # MusicBrainz' Rate-Limit gilt pro Client — die Drossel hier hält es ein.
        if "musicbrainz.org" in url:
            await _throttle_musicbrainz()
        client = await self._ensure_client()
        try:
            response = await client.get(url, params=params)
        except Exception as exc:  # noqa: BLE001 - Netzfehler jeder Art
            raise ProviderError(f"{url}: {exc}") from exc
        return FetchResponse(
            status=response.status_code,
            content=response.content,
            headers=dict(response.headers),
            url=str(response.url),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Bildprüfung
# ---------------------------------------------------------------------------


def sniff_mime(data: bytes) -> str | None:
    """Bildtyp an den Magic Bytes erkennen — wie Perl ``_imageContentType``
    (``Slim/Music/Artwork.pm:437-478``; Perl prüft PNG/GIF/JPEG/BMP, wir zusätzlich WEBP).
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int]:
    """Pixelmasse eines Bildes; ``(0, 0)`` wenn nicht lesbar (kein Absturz)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001 - ein kaputtes Bild ist kein Fehlerfall
        return 0, 0


def validate_image(data: bytes, *, min_edge: int = MIN_IMAGE_EDGE) -> tuple[str, int, int] | None:
    """Bilddaten prüfen: echtes Bildformat + Mindestgrösse.

    Liefert ``(mime, breite, hoehe)`` oder ``None``.  Ein HTML-Fehlerseite
    (CAA/Fanart liefern im Fehlerfall Text) fällt hier heraus.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    mime = sniff_mime(data)
    if mime is None:
        return None
    width, height = image_dimensions(data)
    if min_edge and (width < min_edge or height < min_edge):
        # Masse unbekannt (PIL fehlt) → Format reicht als Nachweis.
        if width and height:
            return None
    return mime, width, height


def _ext_for(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, ".jpg")


# ---------------------------------------------------------------------------
# Anbieter-Schritte
# ---------------------------------------------------------------------------


def _parse_mb_payload(payload: Any) -> list[Candidate]:
    """MusicBrainz-Antwort lesen — Release- **und** Release-Group-Suche.

    UAS liest XML-RegExp aus derselben Antwort (``albumuniversal.xml:29-40``);
    wir nutzen ``fmt=json`` und dieselben Felder (Titel, ``artist-credit``,
    ``date``, ``release-group.id``, ``score``).
    """
    if not isinstance(payload, Mapping):
        return []
    out: list[Candidate] = []

    for release in payload.get("releases") or []:
        if not isinstance(release, Mapping):
            continue
        group = release.get("release-group") or {}
        out.append(Candidate(
            release_id=str(release.get("id") or ""),
            release_group_id=str(group.get("id") or ""),
            title=str(release.get("title") or ""),
            group_title=str(group.get("title") or ""),
            artist=_credit_artist(release),
            year=_year_of(release.get("date")) or _year_of(group.get("first-release-date")),
            score=int(release.get("score") or 0),
            primary_type=str(group.get("primary-type") or ""),
        ))

    for group in payload.get("release-groups") or []:
        if not isinstance(group, Mapping):
            continue
        out.append(Candidate(
            release_group_id=str(group.get("id") or ""),
            title=str(group.get("title") or ""),
            group_title=str(group.get("title") or ""),
            artist=_credit_artist(group),
            year=_year_of(group.get("first-release-date")),
            score=int(group.get("score") or 0),
            primary_type=str(group.get("primary-type") or ""),
        ))
    return out


def _credit_artist(entity: Mapping[str, Any]) -> str:
    names = []
    for credit in entity.get("artist-credit") or []:
        if not isinstance(credit, Mapping):
            continue
        name = credit.get("name") or (credit.get("artist") or {}).get("name")
        if name:
            names.append(str(name))
    return " & ".join(names)


def mb_query_variants(query: AlbumQuery) -> list[str]:
    """Suchterme in Stufen — UAS' ``CreateAlbumSearchUrl`` (``albumuniversal.xml:6-10``)
    mit ``release:"<album>" AND (artistname:"<artist>" OR artist:"<artist>")``.

    **Nur der Titel wird entspannt, nie der Interpret.**  Jede Stufe trägt die
    Interpreten-Klausel, solange das Album einen Interpreten hat; ohne sie wäre
    bei mehrdeutigen Albumnamen („Greatest Hits“) der Titel allein der Schlüssel.
    Stufe 2 nimmt Klammer-Zusätze aus dem Albumtitel (MusicBrainz' Lucene-Suche
    findet ``release:"Point Blank (MMXXIII Version)"`` nicht, obwohl die Ausgabe
    als „Point Blank MMXXIII“ vorhanden ist — live geprüft).  Stufe 3 kürzt
    zusätzlich den Interpreten an ``Ft.``/``Feat.``/``and``/``/``
    (``albumuniversal.xml:11-20``).  Stufe 4 nimmt Sonderzeichen aus dem Titel,
    die Lucene sonst zerlegt („Sex, Sex, Sex“ → „Sex Sex Sex“).

    Ein Album **ohne** Interpreten bekommt nur die Titel-Stufe; ein Treffer daraus
    wird nicht akzeptiert (:func:`candidate_matches` verlangt den Interpreten),
    die Stufe erspart also nur den Fehlversuch ohne Term.
    """
    album = _sanitize_query_term(query.album)
    artist = _sanitize_query_term(query.artist)
    # Stufe 1 mit dem *vollen* Titel (auch Klammer-Zusatz): MusicBrainz'
    # Phrasensuche verträgt ihn nicht immer, findet aber manche Ausgaben nur so.
    full_album = _quote_phrase(query.album)
    plain_album = _sanitize_query_term(_BRACKET_RE.sub(" ", query.album))
    plain_artist = _sanitize_query_term(_short_artist(query.artist))

    def with_artist(title: str, who: str) -> str:
        """Eine Suchstufe — mit Interpreten-Klausel, wie UAS sie immer stellt."""
        if not who:
            return f'release:"{title}"'
        return f'release:"{title}" AND (artistname:"{who}" OR artist:"{who}")'

    variants: list[str] = []
    if full_album:
        variants.append(with_artist(full_album, artist))
    if plain_album and plain_album != full_album:
        variants.append(with_artist(plain_album, artist))
    if plain_album and plain_artist and plain_artist != artist:
        variants.append(with_artist(plain_album, plain_artist))
    if album and album != full_album and album != plain_album:
        variants.append(with_artist(album, artist))
    if not variants and album:
        variants.append(with_artist(album, artist))
    # Duplikate raus, Reihenfolge behalten.
    seen: list[str] = []
    for variant in variants:
        if variant not in seen:
            seen.append(variant)
    return seen


def _short_artist(artist: str) -> str:
    """Interpret bis zum ersten „feat.“/„and“/„&“/„/“ (UAS ``albumuniversal.xml:11-20``)."""
    if not artist:
        return ""
    head = _ARTIST_SPLIT_RE.split(artist, maxsplit=1)[0]
    return head.strip()


def _sanitize_query_term(text: str) -> str:
    """Anführungszeichen/Sonderzeichen entfernen, die Lucene sonst zerlegen."""
    cleaned = re.sub(r'["\\:\[\]\{\}\^~\*\(\)\|]', " ", text or "")
    return _SPACES_RE.sub(" ", cleaned).strip()


def _quote_phrase(text: str) -> str:
    """Titel für die Lucene-Phrase vorbereiten — Klammern bleiben erhalten.

    MusicBrainz lehnt manche Phrasen mit Klammer-Zusatz still ab (live geprüft:
    ``release:"Point Blank (MMXXIII Version)"`` → ``count: 0``); deshalb folgen
    die weiteren Suchstufen mit gekürztem Titel.
    """
    cleaned = re.sub(r'["\\]', " ", text or "")
    return _SPACES_RE.sub(" ", cleaned).strip()


def search_musicbrainz_url(query: str) -> str:
    return f"{MB_BASE}/release"


async def search_musicbrainz_detailed(fetch: Any, settings: ArtOnlineSettings,
                                      query: AlbumQuery
                                      ) -> tuple[Candidate | None, int, str]:
    """Release-Suche über die Stufen aus :func:`mb_query_variants`.

    Liefert ``(bester_treffer, anzahl_treffer, grund)``; ``grund`` ist leer bei
    einem Treffer, sonst der Klartext für den ``miss``-Eintrag („kein passender
    Treffer“ bzw. „mehrdeutig: …“).  Nur die *erste* Stufe, die überhaupt
    MusicBrainz-Treffer liefert, wird bewertet — sonst zöge ein unspezifischer
    Suchterm einen falschen Katalog-Treffer herein.
    """
    attempts = 0
    reason = "kein passender Treffer"
    for term in mb_query_variants(query):
        attempts += 1
        response = await fetch.get(
            search_musicbrainz_url(term),
            params={"query": term, "fmt": "json", "limit": 10},
        )
        if response.status == 503:
            raise ProviderError("MusicBrainz 503 (Rate-Limit)")
        if response.status != 200:
            # Eine abgelehnte Suchphrase (400) oder ein Serverfehler darf die
            # Leiter nicht beenden — nächste Stufe probieren.
            logger.debug("art_online: MusicBrainz HTTP %s für Term %s",
                         response.status, term)
            continue
        candidates = _parse_mb_payload(response.json())
        if not candidates:
            logger.debug("art_online: MusicBrainz-Suchterm ohne Treffer: %s", term)
            continue
        match, reason = select_candidate(candidates, query)
        if match is not None:
            return match, len(candidates), ""
        logger.info(
            "art_online: MusicBrainz lieferte %d Treffer, keiner passte zu "
            "%r / %r — %s (%s)", len(candidates), query.album, query.artist,
            reason, _describe_candidates(candidates))
        return None, len(candidates), reason
    return None, 0, reason


async def search_musicbrainz(fetch: Any, settings: ArtOnlineSettings,
                             query: AlbumQuery) -> tuple[Candidate | None, int]:
    """Wie :func:`search_musicbrainz_detailed`, aber ohne den Grund."""
    match, hits, _reason = await search_musicbrainz_detailed(fetch, settings, query)
    return match, hits


def _failure_is_definitive(text: str) -> bool:
    """Sagt der Anbieter „es gibt kein Bild“ — oder war er nur gestört?

    Nur eine **definitive** Absage darf als Fehlschlag in den Cache: er
    verhindert weitere Versuche für ``artworkOnlineRetryDays`` (Vorgabe 30
    Tage).  Ein Rate-Limit (503), eine Zeitüberschreitung oder ein fehlender
    MusicBrainz-Treffer nach einem Anbieterfehler ist dagegen vorübergehend —
    live belegt am 2026-09-19: zwei Suchen 0,5 s auseinander → MusicBrainz 503,
    und mit der alten Regel stand der Sampler danach 30 Tage als „kein Cover“
    im Cache.  Diese Regel ist eine Zutat des Ports (Perl hat keine
    Online-Suche), sie ändert nur die Cache-Politik, nicht die Kette.
    """
    definitive = (
        "kein passender treffer",        # MusicBrainz kennt das Album nicht
        "mehrdeutig",                    # Titel mehrdeutig, Interpret nicht eindeutig
        "ohne vorder-cover",             # CAA hat kein Vorder-Cover (404)
        "kennt kein cover",              # TheAudioDB: Treffer ohne Bild
        "kein albumtreffer",             # TheAudioDB: nichts gefunden
        "treffer ohne cover",
        "kennt die release-group nicht",  # fanart.tv 404
        "kein album-cover in der antwort",
        "api-key abgelehnt",             # falscher Key: retry bringt nichts
    )
    lowered = text.casefold()
    return any(marker in lowered for marker in definitive)


def _failure_is_transient(text: str) -> bool:
    """Vorübergehender Fehler (Netz, 5xx, Zeitüberschreitung, Rate-Limit)?"""
    lowered = text.casefold()
    return any(marker in lowered for marker in (
        "503", "rate-limit", "zeitüberschreitung", "timed out", "timeout",
        "connection", "http 5", "unerwartet", "keine musicbrainz-id",
        "keine release-group-mbid", "kein api-key",
    ))


#: Fehlschläge mit diesem Präfix sind **vorübergehend** (Rate-Limit, Netz weg,
#: Zeitüberschreitung) und gelten nur :data:`TRANSIENT_RETRY_SECONDS` lang —
#: nicht ``artworkOnlineRetryDays``.  Grund (live, 2026-09-19): zwei Suchen im
#: Abstand von 0,5 s → MusicBrainz ``503 (Rate-Limit)``; mit der vollen
#: Wiederholungsfrist hätte dieses Album einen Monat lang kein Cover bekommen,
#: obwohl der Katalog es kennt.
TRANSIENT_REASON_PREFIX = "vorübergehend: "
TRANSIENT_RETRY_SECONDS = 6 * 3600.0


def _describe_candidates(candidates: Sequence[Candidate]) -> str:
    parts = []
    for cand in candidates[:3]:
        parts.append(f"{cand.group_title or cand.title} ({cand.artist}, {cand.year})")
    return "; ".join(parts)


async def fetch_coverartarchive(fetch: Any, settings: ArtOnlineSettings,
                                query: AlbumQuery,
                                candidate: Candidate | None) -> tuple[bytes, str, str]:
    """Cover Art Archive: ``release-group/<mbid>/front-*`` (keyless).

    Das CAA leitet mit 307 auf archive.org um (live geprüft), der Fetcher folgt
    Umleitungen.  Ohne ``front``-Bild gibt es 404 → nächster Anbieter.
    """
    mbid = (candidate.release_group_id if candidate else "") or (query.mbid or "")
    if not mbid:
        raise ProviderError("CAA ohne Release-Group-MBID")
    last = 404
    for spec in CAA_SPECS:
        url = f"{CAA_BASE}/release-group/{mbid}/{spec}"
        response = await fetch.get(url)
        last = response.status
        if response.status == 200:
            # Auch die CAA-Antwort prüfen: im Fehlerfall kommt Text statt Bild
            # (Perl erkennt den Typ an den Magic Bytes, ``Slim/Music/Artwork.pm:437-478``).
            checked = validate_image(response.content, min_edge=settings.min_edge)
            if checked is None:
                raise ProviderError(
                    f"{url}: keine brauchbaren Bilddaten "
                    f"({len(response.content)} Bytes, {response.content_type or 'ohne Typ'})")
            mime, _width, _height = checked
            return response.content, mime, url
        if response.status == 404:
            continue
        raise ProviderError(f"CAA HTTP {response.status} ({url})")
    raise ProviderError(f"CAA ohne Vorder-Cover (HTTP {last})")


async def fetch_audiodb(fetch: Any, settings: ArtOnlineSettings,
                        query: AlbumQuery,
                        candidate: Candidate | None) -> tuple[bytes, str, str]:
    """TheAudioDB — wie UAS per MBID (``tadb.xml:676-680``), sonst per Suche.

    Ohne konfigurierten Key wirft der Schritt — der Aufrufer überspringt ihn
    schon vorher (siehe :meth:`ArtOnlineSettings.effective_providers`), das hier
    ist nur der letzte Schutz.
    """
    key = settings.key_for("audiodb")
    if not key:
        raise ProviderError("TheAudioDB ohne API-Key in der Konfiguration")
    mbid = (candidate.release_group_id if candidate else "") or (query.mbid or "")
    if mbid:
        url = f"{AUDIODB_BASE}/{key}/album-mb.php"
        response = await fetch.get(url, params={"i": mbid})
        albums = _audiodb_albums(response.json())
        thumb = _audiodb_thumb(albums[0]) if albums else ""
        if thumb:
            return await _download_image(fetch, settings, thumb)
        raise ProviderError(f"TheAudioDB kennt kein Cover zu MBID {mbid}")
    # Namenssuche als Ersatz, wenn MusicBrainz nichts liefert.  UAS kennt diesen
    # Weg nicht (dort ist die MBID Pflicht) — hier nur, damit die Kette auch
    # ohne MusicBrainz-Treffer eine Chance hat.
    response = await fetch.get(
        f"{AUDIODB_BASE}/{key}/searchalbum.php",
        params={"s": query.artist, "a": query.album},
    )
    best = None
    for album in _audiodb_albums(response.json()):
        cand = Candidate(
            title=str(album.get("strAlbum") or ""),
            group_title=str(album.get("strAlbum") or ""),
            artist=str(album.get("strArtist") or ""),
            year=_year_of(album.get("intYearReleased")),
        )
        if candidate_matches(cand, query):
            best = album
            break
    if best is None:
        raise ProviderError("TheAudioDB: kein passender Albumtreffer")
    thumb = _audiodb_thumb(best)
    if not thumb:
        raise ProviderError("TheAudioDB: Treffer ohne Cover")
    return await _download_image(fetch, settings, thumb)


def _audiodb_albums(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    albums = payload.get("album") or payload.get("albums") or []
    return [a for a in albums if isinstance(a, Mapping)]


def _audiodb_thumb(album: Mapping[str, Any]) -> str:
    for key in ("strAlbumThumbHQ", "strAlbumThumb", "strAlbumThumbBack", "strAlbum3DThumb"):
        value = album.get(key)
        if value:
            return str(value)
    return ""


async def fetch_fanart(fetch: Any, settings: ArtOnlineSettings,
                       query: AlbumQuery,
                       candidate: Candidate | None) -> tuple[bytes, str, str]:
    """fanart.tv Musik-API: ``webservice.fanart.tv/v3/music/<release-group-mbid>``.

    Wie UAS ``GetFanartTvAlbumThumbsByMBID`` (``albumuniversal.xml:122``) an der
    Release-Group-MBID; der Key kommt ausschliesslich aus der Konfiguration
    (``fanarttv.xml:5`` zeigt das ``api_key``-Abfragefeld).
    """
    key = settings.key_for("fanart")
    if not key:
        raise ProviderError("fanart.tv ohne API-Key in der Konfiguration")
    mbid = (candidate.release_group_id if candidate else "") or (query.mbid or "")
    if not mbid:
        raise ProviderError("fanart.tv ohne Release-Group-MBID")
    response = await fetch.get(f"{FANART_BASE}/{mbid}", params={"api_key": key})
    if response.status in (401, 403):
        raise ProviderError("fanart.tv: API-Key abgelehnt")
    if response.status == 404:
        raise ProviderError("fanart.tv kennt die Release-Group nicht")
    if response.status != 200:
        raise ProviderError(f"fanart.tv HTTP {response.status}")
    payload = response.json()
    url = _fanart_album_url(payload, mbid)
    if not url:
        raise ProviderError("fanart.tv: kein Album-Cover in der Antwort")
    return await _download_image(fetch, settings, url)


def _fanart_album_url(payload: Any, mbid: str) -> str:
    """Cover-URL aus der fanart.tv-Antwort (``albums[<rg-mbid>][].url``)."""
    if not isinstance(payload, Mapping):
        return ""
    albums = payload.get("albums")
    if not isinstance(albums, Mapping):
        return ""
    entry = albums.get(mbid)
    if entry is None:
        for value in albums.values():
            entry = value
            break
    if isinstance(entry, Mapping):
        entry = [entry]
    for item in entry or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("url", "image", "albumcover"):
            if item.get(key):
                return str(item[key])
    return ""


async def _download_image(fetch: Any, settings: ArtOnlineSettings,
                          url: str) -> tuple[bytes, str, str]:
    """Bild von einem Anbieter holen und prüfen (Format + Mindestgrösse)."""
    response = await fetch.get(url)
    if response.status != 200:
        raise ProviderError(f"Bild HTTP {response.status} ({url})")
    checked = validate_image(response.content, min_edge=settings.min_edge)
    if checked is None:
        raise ProviderError(
            f"{url}: keine brauchbaren Bilddaten "
            f"({len(response.content)} Bytes, {response.content_type or 'ohne Typ'})")
    mime, _width, _height = checked
    return response.content, mime, url


# ---------------------------------------------------------------------------
# Plattencache (+ DB)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artwork_online_cache (
    album_key   TEXT PRIMARY KEY,
    album       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    year        INTEGER,
    status      TEXT NOT NULL,                 -- 'hit' | 'miss'
    provider    TEXT NOT NULL DEFAULT '',
    mbid        TEXT,
    source_url  TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL DEFAULT '',
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    file_name   TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
)
"""


class ArtOnlineCache:
    """Plattencache + Index in SQLite.

    Ein Treffer wird als Datei ``<cache_dir>/<key><ext>`` abgelegt und mit einer
    Zeile ``status='hit'`` indexiert; ein Fehlschlag bekommt ``status='miss'``
    mit ``reason`` (und wird erst nach ``retry_days`` erneut versucht).  Beides
    zusammen macht ``find_cover`` nach dem ersten Lauf netzfrei —
    Perl cacht Artwork genauso dauerhaft (``Slim/Utils/ArtworkCache.pm:44-66``).
    """

    def __init__(self, cache_dir: Path, db_path: Path | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.cache_dir / "art_online.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    # ---- Schema / Verbindung ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        if not self._schema_ready:
            conn.execute(_SCHEMA)
            conn.commit()
            self._schema_ready = True
        return conn

    # ---- Lesen ----

    def read_sync(self, album_key: str) -> CacheEntry | None:
        """Synchron lesen — der Wiedergabepfad darf nicht auf den Loop warten."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM artwork_online_cache WHERE album_key = ?",
                    (album_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.debug("art_online: Cache-Lesefehler (%s)", exc)
            return None
        return _entry_from_row(row) if row else None

    def find_by_album_sync(self, album: str, artist: str = "",
                           year: int | None = None) -> CacheEntry | None:
        """Trefferzeile über die **Album-Spalten** finden (ohne Hash-Schlüssel).

        Siehe :meth:`ArtOnlineService.read_cached_album` für den Grund: der
        Aufrufer kennt nur die Bibliothekszeile (Titel + Sortierschlüssel +
        Jahr).

        **Der Interpret ist Pflicht** (UAS-Vorbild ``albumuniversal.xml:6-10``:
        ``release:"<album>" AND (artistname:"<artist>" OR artist:"<artist>")``).
        Ohne ihn wäre der Titel allein der Schlüssel — und „Greatest Hits“ gehört
        in dieser Sammlung zu Fleetwood Mac, Barry Manilow, Janis Joplin, ZZ Top,
        The Police und Bob Marley.  Live belegt: vor dieser Regel bekam
        ``albums.id=6794`` (Björk, „Greatest Hits“, 2002) die Datei
        ``f87a0f981d73159ae138.jpg`` des Bob-Marley-Eintrags
        (MBID ``035d9663-6997-32d5-abcb-3ba834f6d823``), weil hier der *erste*
        Treffer mit passendem Titel ohne Interpretenprüfung gewann (Rang 0).
        Es gilt: lieber **kein** Cover — der Aufrufer fällt dann auf den
        Perl-Platzhalter zurück (``Slim/Web/Graphics.pm:275-291``) — als ein
        fremdes.

        Bewertung: Titel muss passen (in Vergleichsform), der Interpret ebenfalls
        (Vergleichsform oder :data:`ARTIST_MIN_RATIO`-Ähnlichkeit, weil
        ``albums.albumartist_sort`` der Sortierschlüssel ist: „The Police“ →
        ``police``); dann zählt Interpretengleichheit (2) und Jahresgleichheit
        (1); der beste Treffer gewinnt, bei Gleichstand der erste.  Ohne
        Album-Interpreten gibt es **keinen** Treffer.
        """
        wanted_album = normalize(album)
        wanted_artist = normalize(artist)
        if not wanted_album or not wanted_artist:
            # Ohne Interpreten ist der Titel allein kein Schlüssel (UAS sucht
            # immer mit Interpret) — kein Treffer statt eines fremden Bildes.
            logger.debug("art_online: Album-Suche ohne Interpreten für %r — kein "
                         "Treffer (Interpret ist Pflicht)", album)
            return None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM artwork_online_cache WHERE status = 'hit'"
                ).fetchall()
        except sqlite3.Error as exc:
            logger.debug("art_online: Cache-Lesefehler (%s)", exc)
            return None

        best: sqlite3.Row | None = None
        best_rank = -1
        for row in rows:
            if normalize(str(row["album"] or "")) != wanted_album:
                continue
            row_artist = str(row["artist"] or "")
            if not _artist_matches(row_artist, artist):
                # Fremder Interpret zum gleichen Titel: nie ausliefern.
                continue
            rank = 2 if artist_key(row_artist) == artist_key(artist) else 1
            row_year = row["year"]
            if year is not None and row_year is not None and int(row_year) == int(year):
                rank += 1
            if rank > best_rank:
                best, best_rank = row, rank
        if best is None:
            return None
        entry = _entry_from_row(best)
        return entry if entry.cover_path(self.cache_dir) is not None else None

    async def read(self, album_key: str) -> CacheEntry | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.read_sync, album_key)

    # ---- Schreiben ----

    def write_cover_sync(self, entry_key: str, query: AlbumQuery, data: bytes, *,
                         provider: str, mime: str, source_url: str,
                         mbid: str | None, width: int, height: int) -> Path:
        file_name = f"{entry_key}{_ext_for(mime)}"
        target = self.cache_dir / file_name
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        self._store_sync(CacheEntry(
            album_key=entry_key, status="hit", provider=provider, mbid=mbid,
            source_url=source_url, mime=mime, width=width, height=height,
            created_at=int(time.time()), file_name=file_name,
        ), query)
        return target

    async def write_cover(self, *args: Any, **kwargs: Any) -> Path:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.write_cover_sync(*args, **kwargs))

    def write_miss_sync(self, entry_key: str, query: AlbumQuery, reason: str) -> None:
        self._store_sync(CacheEntry(
            album_key=entry_key, status="miss", provider="", created_at=int(time.time()),
            reason=reason[:200],
        ), query)

    async def write_miss(self, *args: Any, **kwargs: Any) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.write_miss_sync(*args, **kwargs))

    def _store_sync(self, entry: CacheEntry, query: AlbumQuery) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO artwork_online_cache
                    (album_key, album, artist, year, status, provider, mbid,
                     source_url, mime, width, height, file_name, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (entry.album_key, query.album, query.artist, query.year,
                     entry.status, entry.provider, entry.mbid, entry.source_url,
                     entry.mime, entry.width, entry.height, entry.file_name,
                     entry.reason, entry.created_at),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("art_online: Cache-Schreibfehler (%s)", exc)

    # ---- Statistik ----

    def stats_sync(self) -> dict[str, int]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) FROM artwork_online_cache GROUP BY status"
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {str(row[0]): int(row[1]) for row in rows}


def _entry_from_row(row: sqlite3.Row) -> CacheEntry:
    return CacheEntry(
        album_key=str(row["album_key"]),
        status=str(row["status"]),
        provider=str(row["provider"] or ""),
        mbid=row["mbid"],
        source_url=str(row["source_url"] or ""),
        mime=str(row["mime"] or ""),
        width=int(row["width"] or 0),
        height=int(row["height"] or 0),
        created_at=int(row["created_at"] or 0),
        reason=str(row["reason"] or ""),
        file_name=str(row["file_name"] or ""),
    )


# ---------------------------------------------------------------------------
# Dienst
# ---------------------------------------------------------------------------

#: Anbieter-Funktionen der Kette (Signatur der drei Bildschritte).
PROVIDER_FUNCS: dict[str, Callable[..., Awaitable[tuple[bytes, str, str]]]] = {
    "coverartarchive": fetch_coverartarchive,
    "audiodb": fetch_audiodb,
    "fanart": fetch_fanart,
}


class ArtOnlineService:
    """Sucht Album-Cover online — nachgelagert, gecacht, nie im Wiedergabepfad.

    ``find_cover`` ist die Suchfunktion (Cache → Anbieter-Kette).  ``request_lookup``
    legt eine Suche in eine beschränkte Queue, damit weder Scan noch Wiedergabe
    auf das Netz warten; ``read_cached_cover``/``read_cached`` lesen ausschliesslich
    Platte und DB.  ``on_cover`` wird nach einem neuen Treffer aufgerufen — das ist
    die Stelle, an der die Verdrahtung ``albums.artwork`` schreibt.
    """

    def __init__(
        self,
        settings: ArtOnlineSettings | None = None,
        *,
        fetcher: Any | None = None,
        cache: ArtOnlineCache | None = None,
        db_path: Path | None = None,
        on_cover: Callable[[AlbumQuery, CoverResult], Awaitable[None] | None] | None = None,
        queue_size: int = 64,
    ) -> None:
        self.settings = settings or ArtOnlineSettings()
        self.fetcher = fetcher or HttpFetcher(self.settings)
        self.cache = cache or ArtOnlineCache(self.settings.cache_dir, db_path)
        self.on_cover = on_cover
        self._queue: asyncio.Queue[tuple[AlbumQuery, bool]] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._inflight: dict[str, asyncio.Task[CoverResult | None]] = {}
        #: Album-Schlüssel, die in der Queue stehen (eine Anfrage je Album).
        self._queued: set[str] = set()

    # ---- Suche (cache-first) ----

    async def find_cover(self, query: AlbumQuery, *, force: bool = False) -> CoverResult | None:
        """Cover für ein Album finden: Cache → Anbieter-Kette.

        Der Ergebnis-Pfad liegt im Plattencache; ein Treffer wird nie erneut
        gesucht (``force=True`` für einen manuellen Neulauf).
        """
        if not self.settings.enabled:
            logger.debug("art_online: Online-Suche ist aus (%s=0)", PREF_ENABLED)
            return None

        key = query.key()
        cached = await self.cache.read(key)
        if cached is not None and not force:
            if cached.status == "hit":
                path = cached.cover_path(self.cache.cache_dir)
                if path is not None:
                    logger.debug("art_online: Cache-Treffer für %r (%s)",
                                 query.album, path.name)
                    return CoverResult(
                        path=path, provider=cached.provider, mime=cached.mime or "image/jpeg",
                        width=cached.width, height=cached.height, mbid=cached.mbid,
                        source_url=cached.source_url, from_cache=True,
                    )
            elif cached.status == "miss":
                age_days = (time.time() - cached.created_at) / 86400.0
                window = float(self.settings.retry_days)
                if (cached.reason or "").startswith(TRANSIENT_REASON_PREFIX):
                    # Vorübergehender Fehlschlag: nur kurz liegen lassen.
                    window = min(window, TRANSIENT_RETRY_SECONDS / 86400.0)
                if age_days < window:
                    logger.debug(
                        "art_online: Fehlschlag im Cache für %r (%.1f Tage alt, %s)",
                        query.album, age_days, cached.reason or "ohne Grund")
                    return None

        task = self._inflight.get(key)
        if task is not None:
            # Dieselbe Album-Anfrage läuft schon → nicht doppelt ins Netz.
            return await asyncio.shield(task)

        task = asyncio.ensure_future(self._search(query, key))
        self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)

    async def _search(self, query: AlbumQuery, key: str) -> CoverResult | None:
        providers = self.settings.effective_providers()
        if not providers:
            logger.info("art_online: keine Anbieter aktiv (%s)", PREF_PROVIDERS)
            return None

        logger.info("art_online: Suche Cover für %r / %r (%s) über %s",
                    query.album, query.artist, query.year or "ohne Jahr",
                    ",".join(providers))
        candidate: Candidate | None = None
        failures: list[str] = []

        # Schritt 1: MusicBrainz-Match (UAS' Suche, albumuniversal.xml:6-40).
        if "musicbrainz" in providers:
            mb_reason = ""
            try:
                candidate, _hits, mb_reason = await asyncio.wait_for(
                    search_musicbrainz_detailed(self.fetcher, self.settings, query),
                    timeout=self.settings.timeout * 2,
                )
            except asyncio.TimeoutError:
                failures.append("musicbrainz: Zeitüberschreitung")
            except ProviderError as exc:
                failures.append(f"musicbrainz: {exc}")
            except Exception as exc:  # noqa: BLE001 - nie den Scan abbrechen
                failures.append(f"musicbrainz: unerwartet {exc}")
            if candidate is not None:
                logger.debug("art_online: MusicBrainz-Treffer %s (%s, %s)",
                             candidate.release_group_id, candidate.group_title,
                             candidate.year)
            elif not failures:
                # Der Grund der Ablehnung gehört in den Cache: „mehrdeutig“ ist
                # eine definitive Absage (kein Blindflug, kein Ratetreffer).
                failures.append(
                    f"musicbrainz: {mb_reason or 'kein passender Treffer'}")

        if query.mbid and candidate is None:
            candidate = Candidate(release_group_id=query.mbid, group_title=query.album,
                                  artist=query.artist, year=query.year)

        # Schritt 2: Bild-Anbieter in der konfigurierten Reihenfolge.
        for name in providers:
            func = PROVIDER_FUNCS.get(name)
            if func is None:
                continue
            if name in PROVIDER_NEEDS_MBID and not (
                    (candidate.release_group_id if candidate else "") or query.mbid):
                failures.append(f"{name}: keine MusicBrainz-ID")
                continue
            try:
                data, mime, source_url = await asyncio.wait_for(
                    func(self.fetcher, self.settings, query, candidate),
                    timeout=self.settings.timeout,
                )
            except asyncio.TimeoutError:
                failures.append(f"{name}: Zeitüberschreitung")
                continue
            except ProviderError as exc:
                failures.append(f"{name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - Anbieterfehler sind normal
                failures.append(f"{name}: unerwartet {exc}")
                continue

            width, height = image_dimensions(data)
            mbid = (candidate.release_group_id if candidate else "") or query.mbid
            path = await self.cache.write_cover(
                key, query, data, provider=name, mime=mime,
                source_url=source_url, mbid=mbid, width=width, height=height,
            )
            result = CoverResult(path=path, provider=name, mime=mime, width=width,
                                 height=height, mbid=mbid, source_url=source_url)
            logger.info("art_online: Cover von %s für %r: %s (%s, %d Bytes, %dx%d)",
                        name, query.album, path.name, mime, len(data), width, height)
            if self.on_cover is not None:
                try:
                    outcome = self.on_cover(query, result)
                    if asyncio.iscoroutine(outcome):
                        await outcome
                except Exception as exc:  # noqa: BLE001 - Rückruf darf nichts brechen
                    logger.warning("art_online: on_cover-Rückruf scheiterte (%s)", exc)
            return result

        reason = "; ".join(failures) or "keine Anbieter"
        if failures and not any(_failure_is_definitive(f) for f in failures):
            # Kein Anbieter hat definitiv „nein“ gesagt: den Fehlschlag als
            # vorübergehend markieren, damit er nur kurz liegen bleibt
            # (``TRANSIENT_RETRY_SECONDS`` statt ``retry_days``).
            reason = TRANSIENT_REASON_PREFIX + reason
        await self.cache.write_miss(key, query, reason)
        logger.info("art_online: kein Online-Cover für %r / %r — %s",
                    query.album, query.artist, reason)
        return None

    # ---- Wiedergabepfad: nur Platte/DB, kein Netz ----

    def read_cached(self, query: AlbumQuery) -> CoverResult | None:
        """Gecachtes Cover ohne jede Netzabfrage (auch synchron nutzbar)."""
        entry = self.cache.read_sync(query.key())
        return self._result_from_entry(entry)

    def read_cached_album(self, album: str, artist: str = "",
                          year: int | None = None) -> CoverResult | None:
        """Gecachtes Cover über die **Album-Zeile** finden (Platte/DB, kein Netz).

        Für den Auslieferungspfad (``web/app.py`` ``/music/<id>/cover…``): dort
        ist nur die Zeile aus ``albums`` bekannt — ``title``,
        ``albumartist_sort`` und ``year``.  Der Cache-Schlüssel entsteht
        dagegen aus den *Tag*-Schreibweisen des Scans (:meth:`AlbumQuery.key`),
        und ``albums.albumartist_sort`` ist der Sortierschlüssel
        (``media/importer.py:91-98``: kleingeschrieben, ohne führenden Artikel),
        also nicht immer gleich ``normalize(<Albumartist-Tag>)`` („The
        Beatles“ → ``beatles`` gegen ``the beatles``).  Deshalb vergleicht
        diese Suche die gespeicherten Album-Spalten statt den Schlüssel — **und
        verlangt den Interpreten**: ohne ihn wäre der Titel allein der Schlüssel
        (siehe :meth:`ArtOnlineCache.find_by_album_sync`).  Der Aufrufer übergibt
        deshalb den Album-Contributor bzw. ``albumartist_sort``
        (``web/app.py`` ``_online_cover_for_album``,
        ``media/importer.py`` ``online_cover_path``); ohne Interpreten gibt es
        ``None`` und der Aufrufer zeigt den Platzhalter.
        """
        entry = self.cache.find_by_album_sync(album, artist, year)
        return self._result_from_entry(entry)

    def _result_from_entry(self, entry: CacheEntry | None) -> CoverResult | None:
        """``CacheEntry`` → :class:`CoverResult` (nur bei ``status='hit'`` + Datei)."""
        if entry is None or entry.status != "hit":
            return None
        path = entry.cover_path(self.cache.cache_dir)
        if path is None:
            return None
        return CoverResult(
            path=path, provider=entry.provider, mime=entry.mime or "image/jpeg",
            width=entry.width, height=entry.height, mbid=entry.mbid,
            source_url=entry.source_url, from_cache=True,
        )

    async def read_cached_async(self, query: AlbumQuery) -> CoverResult | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.read_cached, query)

    # ---- Nachgelagerte Suche ----

    def request_lookup(self, query: AlbumQuery) -> bool:
        """Suche anstossen, ohne zu warten (Scan/Wiedergabe blockieren nicht).

        Rückgabe ``False`` heisst: Queue voll — die Anfrage wird verworfen und
        geloggt (kein Aufstauen von Netzlast bei riesigen Sammlungen).

        Ein Album wird nur **einmal** eingereiht: der Scan fragt pro *Datei* an,
        ein Album mit zwölf Titeln würde die Queue sonst zwölfmal füllen und
        echte Anfragen anderer Alben verdrängen (``maxsize``).

        Perl-Beleg fürs Zusammenfassen: LMS sucht/cacht Artwork pro Album, nicht
        pro Datei (``Slim/Utils/ArtworkCache.pm:44-66`` legt einen Eintrag je
        Album-Cache-Schlüssel an).
        """
        if not self.settings.enabled:
            return False
        key = query.key()
        if key in self._inflight or key in self._queued:
            logger.debug("art_online: %r ist schon in Arbeit — nicht erneut eingereiht",
                         query.album)
            return True
        self._ensure_worker()
        try:
            self._queue.put_nowait((query, False))
        except asyncio.QueueFull:
            logger.warning("art_online: Queue voll (%d) — %r verworfen",
                           self._queue.maxsize, query.album)
            return False
        self._queued.add(key)
        return True

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            query, force = await self._queue.get()
            self._queued.discard(query.key())
            try:
                await self.find_cover(query, force=force)
            except Exception as exc:  # noqa: BLE001 - Worker darf nie sterben
                logger.warning("art_online: Suche für %r scheiterte (%s)",
                               query.album, exc)
            finally:
                self._queue.task_done()

    async def drain(self, timeout: float | None = None) -> int:
        """Offene Suchen abarbeiten (Scan-Ende, Tests).  Liefert die Restlänge."""
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("art_online: Queue nicht leer nach %.1fs (%d offen)",
                               timeout or 0.0, self._queue.qsize())
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        """Wie viele Album-Anfragen gleichzeitig in der Queue stehen dürfen."""
        return int(self._queue.maxsize)

    @property
    def queue_pending(self) -> int:
        """Wie viele Anfragen gerade in der Queue stehen (Statistik/Log)."""
        return int(self._queue.qsize())

    async def close(self) -> None:
        """Worker beenden und HTTP-Client schliessen."""
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None
        close = getattr(self.fetcher, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Bequeme Modul-Schnittstellen für die Verdrahtung
# ---------------------------------------------------------------------------

_service: ArtOnlineService | None = None


def get_service(settings: ArtOnlineSettings | None = None) -> ArtOnlineService:
    """Prozessweiter Dienst (eigener Anbieter-Client, eigener Cache)."""
    global _service
    if _service is None or settings is not None:
        _service = ArtOnlineService(settings)
    return _service


def configured_service() -> ArtOnlineService:
    """Dienst dieses Prozesses, beim ersten Aufruf mit den Prefs konfiguriert.

    Die Verdrahtung (``media/scanner.py``, ``media/scan_worker.py``,
    ``web/app.py``) ruft **nur** diese Funktion: so entsteht pro Prozess genau
    ein Dienst mit einer Queue, und ein zweiter Aufruf ersetzt ihn nicht (sonst
    gingen eingereihte Suchen verloren).  Die Prefs kommen über
    ``web/settings.load_art_online_settings`` (Lazy-Import: ``media`` darf
    ``web`` nicht auf Modulebene importieren); ohne lesbare Prefs gelten die
    Defaults aus :data:`PREF_DEFAULTS` — die Suche läuft also auch im
    Scan-Prozess ohne Server-Kontext.
    """
    global _service
    if _service is None:
        settings: ArtOnlineSettings | None = None
        try:
            from lyrion.web.settings import load_art_online_settings

            settings = load_art_online_settings()
        except Exception as exc:  # noqa: BLE001 - ohne Prefs gelten die Defaults
            logger.debug("art_online: Prefs nicht lesbar (%s) — Defaults", exc)
        _service = ArtOnlineService(settings)
    return _service


def reset_service() -> None:
    """Dienst vergessen (Tests, Konfigurationswechsel)."""
    global _service
    _service = None


def current_service() -> ArtOnlineService | None:
    """Der in diesem Prozess schon angelegte Dienst (oder ``None``).

    Für Aufrufer, die einen vorhandenen Dienst *benutzen*, aber keinen neuen
    anlegen wollen (z. B. das Scan-Ende in ``media/scanner.py``).
    """
    return _service


async def lookup_cover(
    album: str,
    artist: str = "",
    year: int | None = None,
    mbid: str | None = None,
    *,
    settings: ArtOnlineSettings | None = None,
) -> CoverResult | None:
    """Bequeme Suchfunktion für den nachgelagerten Scan-Lauf."""
    query = AlbumQuery(album=album, artist=artist, year=year, mbid=mbid)
    return await get_service(settings).find_cover(query)


def read_cached_cover(
    album: str,
    artist: str = "",
    year: int | None = None,
    *,
    settings: ArtOnlineSettings | None = None,
) -> Path | None:
    """Gecachtes Cover ohne Netzabfrage — für den Auslieferungs-/Wiedergabepfad."""
    query = AlbumQuery(album=album, artist=artist, year=year)
    result = get_service(settings).read_cached(query)
    return result.path if result else None


__all__ = [
    "AlbumQuery", "ArtOnlineCache", "ArtOnlineService", "ArtOnlineSettings",
    "CacheEntry", "Candidate", "CoverResult", "DEFAULT_PROVIDER_ORDER",
    "FetchResponse", "HttpFetcher", "KEYLESS_PROVIDERS", "MIN_IMAGE_EDGE",
    "PREF_AUDIODB_KEY", "PREF_CACHE_DIR", "PREF_COUNTRY", "PREF_DEFAULTS",
    "PREF_ENABLED", "PREF_FANART_KEY", "PREF_LANGUAGE", "PREF_PROVIDERS",
    "PREF_RETRY_DAYS", "PREF_TIMEOUT", "PROVIDER_ALIASES", "PROVIDER_KEY_PREFS",
    "PROVIDER_NEEDS_MBID",
    "ProviderError", "artist_key", "candidate_matches", "configured_service",
    "current_service",
    "default_cache_dir", "get_service",
    "image_dimensions", "lookup_cover", "mb_query_variants",
    "normalize_provider_list", "pick_candidate", "read_cached_cover",
    "reset_service", "search_musicbrainz", "search_musicbrainz_detailed",
    "select_candidate", "sniff_mime", "validate_image",
]
