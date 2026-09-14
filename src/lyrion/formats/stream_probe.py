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

Die Probe selbst ist EIN ``GET`` (``scanURL``), nicht HEAD:

    my $request = HTTP::Request->new( GET => $url );
    ...
    my $timeout = preferences('server')->get('remotestreamtimeout');
    $http->send_request( { request => $request, onRedirect => \\&handleRedirect,
        onHeaders => \\&readRemoteHeaders, onError => sub {...}, Timeout => $timeout } )

(Slim/Utils/Scanner/Remote.pm:205-233; Timeout-Default 15 s =
Slim/Utils/Prefs.pm:209). Ein Fehlerstatus ist ein FEHLER, kein Header-Satz:
Async/HTTP.pm:434-435 ruft bei ``$code !~ /[23]\\d\\d/`` ``onError`` mit der
Statuszeile, Remote.pm:228-238 macht daraus ``$cb->(undef, $error, ...)``.
Der Aufrufer (Play-Pfad) bricht damit ab — Perl hat keinen Proxy-Fallback:

    Slim/Player/Song.pm:302-312  (scanUrl-Callback, Fehlerzweig)
        Slim::Control::Request::notifyFromArray( $client,
            [ 'playlist', 'cant_open', $url, $error ] );
        $error ||= 'PROBLEM_OPENING_REMOTE_URL';
        $failCb->($error, $url);

Genau der gemeldete 1.FM-Fall (live 2026-09-14): der Sender antwortete der
Probe mit ``503 Service Unavailable``; die alte Fassung verwarf das (HEAD-Probe
ohne Statusprüfung, Fallback auf die URL-Endung) und schickte den Stream
trotzdem DIRECT → Player still.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .lms_types import type_from_mime, type_from_suffix

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
# Cache der ICY-Bitrate pro URL — Perl liest sie aus DERSELBEN Scan-Antwort
# (Remote.pm:530-545) und legt sie über setBitrate am Track ab.
_BITRATE_CACHE: dict[str, float] = {}

# Perl: ``preferences('server')->get('remotestreamtimeout')`` — Default 15
# (Slim/Utils/Prefs.pm:209), benutzt als ``Timeout`` der Scan-Anfrage
# (Slim/Utils/Scanner/Remote.pm:214/:232).
REMOTE_STREAM_TIMEOUT = 15.0

# Perl liest den Playlist-Body bounded ein: ``readLimit => 128 * 1024``
# (Slim/Utils/Scanner/Remote.pm:1179-1206, parsePlaylist).
PLAYLIST_READ_LIMIT = 128 * 1024

# Playlist-Typen, deren Body Perl als URL-Liste liest (Remote.pm:416 →
# else-Zweig :587-597; die Endungsliste steht in Remote.pm:357).
PLAYLIST_TYPES = ("m3u", "pls", "asx", "wpl")


@dataclass
class StreamScan:
    """Ergebnis EINER Perl-gleichen Probe (``scanURL``).

    ``error`` ist gesetzt, wenn Perl an dieser Stelle abbrechen würde
    (Remote.pm:228-238 → Song.pm:302-312); ``type``/``bitrate``/``url`` sind
    dann nicht verwertbar.
    """

    url: str
    status: int = 0
    type: Optional[str] = None
    bitrate: float = 0.0
    error: Optional[str] = None
    headers: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error is not None


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


def bitrate_from_headers(headers: Mapping[str, str]) -> float:
    """ICY-Bitrate aus den Scan-Headern (``Remote.pm:530-560``).

    Perl liest sie in ``readRemoteHeaders`` aus der Antwort des Scans::

        # Look for bitrate information in header indicating it's an Icy stream
        elsif ( $bitrate = ( $http->response->header('icy-br')
                || $http->response->header('x-audiocast-bitrate') || 0 ) * 1000 ) { ... }
        if ( $bitrate ) {
            if ( $bitrate < 1000 ) { $bitrate *= 1000; }
            Slim::Music::Info::setBitrate( $track, $bitrate, $vbr );
        }
    """
    value = (headers or {}).get("icy-br") or (headers or {}).get("x-audiocast-bitrate")
    if not value:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not match:
        return 0.0
    try:
        bitrate = float(match.group(1)) * 1000            # Remote.pm:543 `* 1000`
    except ValueError:                                    # pragma: no cover
        return 0.0
    if bitrate and bitrate < 1000:                        # Remote.pm:551-552
        bitrate *= 1000
    return bitrate


def _playlist_entry_urls(body: bytes) -> list[str]:
    """Spielbare URL-Zeilen eines M3U/PLS-Bodys (Perl ``parsePlaylist``).

    Perl übergibt den Body der Format-Klasse (``Slim/Formats/Playlists/
    M3U.pm`` / ``PLS.pm``) und scannt die gefundenen Einträge danach erneut
    (Remote.pm:1195-1214). ``File1=``-Zeilen eines PLS liefert diese Liste
    mit, weil das Suffix-Schema dieselbe Zeilenform benutzt.
    """
    out: list[str] = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if "=" in line and not line.lower().startswith(("http://", "https://")):
            # PLS: ``File1=http://...``
            line = line.split("=", 1)[1].strip()
        if line.startswith(("http://", "https://")):
            out.append(line)
    return out


async def scan_stream_url(
    url: str, timeout: float = REMOTE_STREAM_TIMEOUT, _depth: int = 0
) -> StreamScan:
    """EINE Probe wie Perls ``scanURL``: GET, Statuszeile, Header, Redirects.

    Perl öffnet die URL mit ``GET`` (Slim/Utils/Scanner/Remote.pm:205) und
    einem ``Timeout`` von ``remotestreamtimeout`` (:214/:232). Redirects folgen
    über ``handleRedirect`` (:285-300, Async/HTTP.pm:433-475) — die maßgebliche
    URL ist die FINALE (``$http->request->uri``, :309). Ein Status außerhalb
    2xx/3xx ist ein Fehler (Async/HTTP.pm:434-435 → Remote.pm:228-238).
    Ist das Ergebnis eine Playlist, wird sie wie in Perl gelesen und der erste
    Eintrag erneut gescannt (:587-597 → :1195-1214).

    Der Rückgabewert ist NIE ``None``: ``StreamScan.error`` trägt den
    Perl-Fehlercode, damit der Aufrufer wie ``Song.pm:302-312`` abbrechen kann.
    """
    if _TYPE_CACHE.get(url) and url in _BITRATE_CACHE:
        return StreamScan(url=url, status=200, type=_TYPE_CACHE[url],
                          bitrate=_BITRATE_CACHE[url])

    import httpx

    scan = StreamScan(url=url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=timeout, read=timeout,
                                  write=timeout, pool=timeout),
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as resp:
                scan.status = resp.status_code
                scan.headers = {k.lower(): v for k, v in resp.headers.items()}
                scan.url = str(resp.url)                  # finale URL nach Redirect
                if not (200 <= resp.status_code < 400):
                    # Async/HTTP.pm:434-435 — onError mit der Statuszeile
                    # ("503 Service Unavailable").
                    scan.error = f"{resp.status_code} {resp.reason_phrase}".strip()
                    logger.warning("Stream-Scan %s: %s", url[:70], scan.error)
                    return scan

                scan.type = perl_type_from_response(
                    scan.url, resp.headers.get("content-type"), scan.headers)
                scan.bitrate = bitrate_from_headers(scan.headers)

                if scan.type in PLAYLIST_TYPES and _depth < 2:
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > PLAYLIST_READ_LIMIT:
                            break
                    entries = _playlist_entry_urls(body)
                    if not entries:
                        # Remote.pm:1172-1179 — ``!scalar @results``
                        scan.error = "PLAYLIST_NO_ITEMS_FOUND"
                        logger.warning("Stream-Scan %s: Playlist ohne Einträge (%s)",
                                       url[:70], scan.type)
                        return scan
                    # Perl scannt ALLE Einträge und nimmt den ersten
                    # brauchbaren (Remote.pm:1195-1214). Wir bleiben bei einer
                    # kleinen, sequenziellen Auswahl, damit ein toter Eintrag
                    # nicht die Summe aller Timeouts kostet.
                    last = scan
                    for entry in entries[:3]:
                        sub = await scan_stream_url(entry, timeout=timeout,
                                                    _depth=_depth + 1)
                        if not sub.failed:
                            return sub
                        last = sub
                    return last
    except Exception as exc:  # noqa: BLE001 — Netzfehler sind Fehler, nicht "unklar"
        scan.error = str(exc) or "PROBLEM_OPENING_REMOTE_URL"
        logger.warning("Stream-Scan %s fehlgeschlagen: %s", url[:70], exc)
        return scan

    if scan.type:
        _TYPE_CACHE[url] = scan.type                       # Perl setContentType
        if scan.bitrate:
            _BITRATE_CACHE[url] = scan.bitrate
    else:
        # Perl: ``isSong($track, undef)`` ist falsch → der Body wird als
        # Playlist gelesen → keine Einträge → Remote.pm:1172-1179.
        scan.error = "PLAYLIST_NO_ITEMS_FOUND"
        logger.warning("Stream-Scan %s: kein Format erkennbar (Header: %s)",
                       url[:70], ",".join(sorted(scan.headers))[:160])
    return scan


async def probe_remote_type(url: str,
                            timeout: float = REMOTE_STREAM_TIMEOUT) -> Optional[str]:
    """Perl-Typ einer Remote-URL — ``None``, wenn der Scan scheitert."""
    scan = await scan_stream_url(url, timeout=timeout)
    return None if scan.failed else scan.type


async def codec_for_stream_url(url: str, fallback: str = "m",
                               timeout: float = REMOTE_STREAM_TIMEOUT) -> str:
    """strm-Codec-Byte für eine Remote-URL — Header-Probe, sonst ``fallback``.

    Der Rückgabewert ist das Format-Byte des strm-Frames
    (``lms_types.format_byte``): AAC → ``'a'``, MP3 → ``'m'``, Ogg → ``'o'`` …
    """
    type_ = await probe_remote_type(url, timeout=timeout)
    if type_:
        from .lms_types import format_byte
        byte = format_byte(type_)
        if byte:
            return byte
    return fallback


def clear_stream_type_cache() -> None:
    """Cache leeren (Tests / Konfigurationswechsel)."""
    _TYPE_CACHE.clear()
    _BITRATE_CACHE.clear()
