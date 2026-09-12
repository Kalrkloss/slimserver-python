"""LMS file-type tables (Perl ``types.conf`` + ``stream_s`` format bytes).

Single source of truth for "which format is this file" and "which slimproto
format byte / player capability does it map to". Everything here is copied
from the Perl LMS — never invented:

* ``types.conf`` (LMS root, loaded by ``Slim/Music/Info.pm:92``
  ``loadTypesConfig``): type -> suffixes + mime types.
* ``Slim/Player/Squeezebox.pm:546-770`` ``stream_s``: type -> ``formatbyte``.
* ``Slim/Player/SqueezePlay.pm:170-200`` ``updateCapabilities``: a player
  declares its codecs as bare capability tokens; ``SqueezePlay.pm:59`` is the
  static fallback list.

Why this module exists: deriving the format from a MIME type is wrong for
real libraries. The live DB stores a ``.opus`` file as ``audio/ogg`` and a
Musepack ``.mpc`` as ``chemical/x-mopac-input``; Perl decides by SUFFIX
first (``types.conf``), which is what this module does.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

# ── types.conf: Perl type -> (suffixes, mime types) ───────────────────────
# Verbatim from /tmp/lms-ref/types.conf (columns: type suffixes mime-types).
TYPES_CONF: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "aif": (("aif", "aiff"), ("audio/x-aiff",)),
    "alc": ((), ("audio/x-m4a-lossless",)),
    "dff": (("dff",), ("audio/dff",)),
    "dsf": (("dsf",), ("audio/dsf",)),
    "flc": (("flac", "flc", "fla"), ("audio/x-flac", "audio/flac")),
    "aac": (("aac",), ("audio/aac", "audio/aacp")),
    "mp4": (("m4a", "mp4", "m4b"),
            ("audio/m4a", "audio/x-m4a", "audio/mp4")),
    "mp3": (("mp2", "mp3"),
            ("audio/mpeg", "audio/mp3", "audio/mp3s", "audio/x-mpeg",
             "audio/mpeg3", "audio/mpg")),
    "mpc": (("mpc", "mp+"), ("audio/x-musepack",)),
    "ogg": (("ogg", "oga"),
            ("audio/x-ogg", "application/ogg", "audio/ogg",
             "application/x-ogg")),
    "ogf": (("ogf",), ("audio/ogg;codecs=flac",)),
    "ops": (("opus",), ("audio/opus", "audio/ogg;codecs=opus")),
    "pcm": (("pcm",), ()),
    "wav": (("wav", "wave"),
            ("audio/x-wav", "audio/wav", "audio/vnd.wave", "audio/wave")),
    "wma": (("wma",),
            ("audio/x-ms-wma", "application/vnd.ms.wms-hdr.asfv1",
             "application/x-mms-framed", "audio/asf")),
}

_SUFFIX_TO_TYPE: dict[str, str] = {
    suffix: perl_type
    for perl_type, (suffixes, _mimes) in TYPES_CONF.items()
    for suffix in suffixes
}

# Mime lookup strips parameters, but types.conf has one entry WITH a
# parameter ("audio/ogg;codecs=flac" -> ogf / ";codecs=opus" -> ops), so keep
# the full lowercased string first and fall back to the stripped form.
_MIME_TO_TYPE: dict[str, str] = {
    mime: perl_type
    for perl_type, (_suffixes, mimes) in TYPES_CONF.items()
    for mime in mimes
}

# ── stream_s format byte (Slim/Player/Squeezebox.pm:560-770) ──────────────
FORMAT_TO_BYTE: dict[str, str] = {
    "pcm": "p", "wav": "p", "aif": "p",      # :590-:622
    "flc": "f", "ogf": "f",                  # :633-:655
    "wma": "w", "wmv": "w", "asx": "w",      # :660
    "ogg": "o",                              # :687
    "ops": "u",                              # :698
    "alc": "l",                              # :709
    "mp4": "a", "aac": "a",                  # :712
    "dff": "d", "dsf": "d",                  # :735
    "test": "n",                             # :755
}
# stream_s else-branch: "assume MP3" (:762), with a logBacktrace warning when
# the format is not really mp3.
DEFAULT_FORMAT_BYTE = "m"

# ── Perl type -> the "extension" used in player capability sets ───────────
# Names match lyrion.player.manager._formats_for_model / stream._codec_to_
# extension so that a capability set and a codec byte compare consistently.
FORMAT_TO_EXTENSION: dict[str, str] = {
    "flc": "flac", "ogf": "flac",
    "alc": "alac",
    "aif": "aiff",
    "pcm": "pcm",
    "wav": "wav",
    "ogg": "ogg",
    "ops": "opus",
    "mp3": "mp3",
    "aac": "aac", "mp4": "aac",
    "wma": "wma", "wmv": "wma", "asx": "wma",
    "dsf": "dsd", "dff": "dsd",
    "mpc": "musepack",
    "test": "test",
}


def type_from_suffix(path_or_url: str) -> str | None:
    """Perl type from the file suffix (``Slim/Music/Info.pm`` typeFromSuffix)."""
    path = path_or_url
    if "://" in path:
        path = urlparse(path).path
    path = unquote(path)
    if "/" in path:
        path = path.rsplit("/", 1)[-1]
    if "." not in path:
        return None
    return _SUFFIX_TO_TYPE.get(path.rsplit(".", 1)[-1].lower())


def type_from_mime(mime: str | None) -> str | None:
    """Perl type from a MIME type (``types.conf`` mime column)."""
    if not mime:
        return None
    m = mime.strip().lower()
    if m in _MIME_TO_TYPE:
        return _MIME_TO_TYPE[m]
    return _MIME_TO_TYPE.get(m.split(";")[0].strip())


def describe_type(path_or_url: str = "", mime: str | None = None) -> str | None:
    """Perl file type for a track: suffix first, then MIME.

    Perl uses the scanned content type; the suffix is the reliable proxy in
    our library DB (whose ``content_type`` column is often wrong: ``.opus``
    stored as ``audio/ogg``, ``.mpc`` as ``chemical/x-mopac-input``).
    """
    return type_from_suffix(path_or_url) or type_from_mime(mime)


def format_byte(perl_type: str | None) -> str:
    """The slimproto strm format byte for a Perl type (default: mp3)."""
    if not perl_type:
        return DEFAULT_FORMAT_BYTE
    return FORMAT_TO_BYTE.get(perl_type, DEFAULT_FORMAT_BYTE)


def format_extension(perl_type: str | None) -> str:
    """The player-capability extension for a Perl type."""
    if not perl_type:
        return "mp3"
    return FORMAT_TO_EXTENSION.get(perl_type, perl_type)


def pcm_samplesize_for(perl_type: str | None) -> str:
    """The ``pcmsamplesize`` field for aac/mp4 sources.

    Squeezebox.pm:712-717: the field carries the AAC container type —
    '5' (mp4ff) for anything that is not raw ADTS, '2' (adts) for a real
    ``aac`` stream. Other formats use '?' (unknown).
    """
    if perl_type == "aac":
        return "2"
    if perl_type == "mp4":
        return "5"
    return "?"
