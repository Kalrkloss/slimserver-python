"""Remote-Stream-Format wie Perl bestimmen (HTTP-Header statt URL-Endung).

Warum dieses Modul: das Format nur aus der URL-Endung abzuleiten ist falsch.
Ein AAC-Sender ohne ``.aac`` in der URL
(``https://stream02.pcradio.app/Garik_Sukachov-hi`` → ``Content-Type:
audio/aac``) wurde als MP3 angesagt; der Client dekodierte AAC-Daten mit dem
MP3-Decoder — Symptom des Users: „AAC radio stream geht nicht" (2026-09-12).

Perl liest stattdessen die Antwort-Header des Senders und leitet daraus den
Content-Type des Tracks ab; erst daraus entsteht das Format-Byte des
strm-Frames: ``Slim/Utils/Scanner/Remote.pm:333-390`` (scanURL-Callback):

    my $type = $http->response->content_type;
    $type = Slim::Music::Info::mimeToType($type) || $type;
    # Bug 3396: .aac-URL, aber audio/mpeg  -> aac
    if ( $url =~ /aac$/i && ($type eq 'mp3' || $type eq 'txt') ) { $type = 'aac' }
    elsif ( $url =~ /(?:m4a|mp4)$/i && ($type eq 'mp3' || $type eq 'txt') ) { $type = 'mp4' }
    elsif ( $type =~ /(?:htm|txt)/ && $url =~ /\\.(asx|m3u|pls|wpl|wma)$/i ) { $type = $1 }
    elsif ( $type eq 'wma' && $url =~ /\\.(m3u)$/i ) { $type = $1 }
    elsif ( $type =~ /(?:htm|txt)/ ) { $type = 'm3u' }
    elsif ( $type =~ /octet-stream/ ) { $type = $1 if $url =~ /\\.(<valid>)\\b/ }
    if ( !$type && $http->response->header('icy-name') ) { $type = 'mp3' }

Das Ergebnis wird dort als Content-Type des Tracks zwischengespeichert
(``Slim::Music::Info::setContentType``), damit der nächste Start nicht erneut
proben muss — hier als kleiner In-Process-Cache.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping, Optional

from .lms_types import format_byte, type_from_mime, type_from_suffix

logger = logging.getLogger(__name__)

# Perl vergleicht mit regulären Ausdrücken, nicht auf Gleichheit
# (Remote.pm:353/:357/:366/:370/:375/:381).
_RE_HTM_TXT = re.compile(r"htm|txt")
_RE_ICY_FALLBACK_TYPE = "mp3"          # Remote.pm:381-383 (Server ohne content-type)
_RE_OCTET = re.compile(r"octet-stream")  # Remote.pm:375
_RE_PLAYLIST_EXT = re.compile(r"\.(asx|m3u|pls|wpl|wma)$")
_RE_AAC_TAIL = re.compile(r"aac$")
_RE_MP4_TAIL = re.compile(r"(?:m4a|mp4)$")

# Cache des ermittelten Typs pro URL (Perl: Slim::Music::Info::setContentType).
_TYPE_CACHE: dict[str, str] = {}
# Cache der Fehlschläge, damit ein toter Sender nicht jeden Start ausbremst.
_TYPE_FAIL_CACHE: set[str] = set()


def perl_type_from_response(
    url: str, content_type: Optional[str], headers: Mapping[str, str]
) -> Optional[str]:
    """Perl-Typ eines Remote-Streams aus den HTTP-Antwort-Headern.

    Regelreihenfolge wörtlich nach ``Slim/Utils/Scanner/Remote.pm:333-390``.
    ``headers`` muss die Namen in Kleinschreibung führen (``icy-name``).
    Gibt ``None`` zurück, wenn nichts bestimmbar ist (Aufrufer fällt dann auf
    die URL-Endung zurück).
    """
    raw = (content_type or "").split(";")[0].strip().lower()
    type_ = type_from_mime(content_type) or raw
    u = (url or "").lower()

    if _RE_AAC_TAIL.search(u) and type_ in ("mp3", "txt"):
        # Bug 3396 — manche m4a/AAC-Streams werden als audio/mpeg ausgeliefert
        type_ = "aac"
    elif _RE_MP4_TAIL.search(u) and type_ in ("mp3", "txt"):
        type_ = "mp4"
    elif _RE_HTM_TXT.search(type_) and (pl := _RE_PLAYLIST_EXT.search(u)):
        type_ = pl.group(1)                               # type = $1
    elif type_ == "wma" and u.endswith(".m3u"):
        type_ = "m3u"
    elif _RE_HTM_TXT.search(type_):
        type_ = "m3u"                                     # Playlist hinter HTML
    elif _RE_OCTET.search(type_):
        suffix_type = type_from_suffix(u)
        if suffix_type:
            type_ = suffix_type                           # gültige Endung gewinnt

    if not type_ and (headers or {}).get("icy-name"):
        type_ = _RE_ICY_FALLBACK_TYPE                     # Shoutcast/Icecast ohne MIME

    return type_ or None


async def probe_remote_type(url: str, timeout: float = 5.0) -> Optional[str]:
    """HTTP-Header eines Streams lesen und den Perl-Typ bestimmen.

    Perl nutzt einen GET (scanURL) und wertet nur die Header aus; wir probieren
    zuerst HEAD und fallen auf einen abgebrochenen GET zurück, weil manche
    Icecast-Server HEAD ablehnen. Fehler/Timeouts sind kein Grund, die
    Wiedergabe zu verhindern — dann gilt die URL-Endung (Rückgabewert None).
    """
    if url in _TYPE_CACHE:
        return _TYPE_CACHE[url]
    if url in _TYPE_FAIL_CACHE:
        return None
    try:
        import httpx

        headers: Mapping[str, str] = {}
        content_type: Optional[str] = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout,
                                  pool=timeout),
            follow_redirects=True,
        ) as client:
            try:
                resp = await client.head(url)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                content_type = resp.headers.get("content-type")
            except Exception:  # noqa: BLE001 — HEAD nicht unterstützt
                headers = {}
            if not headers:
                # GET, aber nur die Header: der Body wird sofort verworfen.
                async with client.stream("GET", url) as resp:
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    content_type = resp.headers.get("content-type")
        type_ = perl_type_from_response(url, content_type, headers)
        if type_:
            _TYPE_CACHE[url] = type_
            logger.debug("Stream-Format für %s: %s (content-type %r)",
                         url[:70], type_, content_type)
            return type_
        logger.debug("Stream-Format für %s unbestimmt (content-type %r)",
                     url[:70], content_type)
    except Exception as exc:  # noqa: BLE001 — Netzfehler nie fatal
        logger.debug("Stream-Format-Probe für %s fehlgeschlagen: %s", url[:70], exc)
    _TYPE_FAIL_CACHE.add(url)
    return None


async def codec_for_stream_url(url: str, fallback: str = "m",
                               timeout: float = 5.0) -> str:
    """strm-Codec-Byte für eine Remote-URL — Header-Probe, sonst ``fallback``.

    Der Rückgabewert ist das Format-Byte des strm-Frames
    (``lms_types.format_byte``): AAC → ``'a'``, MP3 → ``'m'``, Ogg → ``'o'`` …
    """
    type_ = await probe_remote_type(url, timeout=timeout)
    if type_:
        byte = format_byte(type_)
        if byte:
            return byte
    return fallback


def clear_stream_type_cache() -> None:
    """Cache leeren (Tests / Konfigurationswechsel)."""
    _TYPE_CACHE.clear()
    _TYPE_FAIL_CACHE.clear()
