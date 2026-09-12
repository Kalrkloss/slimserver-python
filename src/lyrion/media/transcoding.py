"""Transcoding rules: ``convert.conf``/``types.conf`` parsing and selection.

This is the Python counterpart of Perl's
``Slim/Player/TranscodingHelper.pm`` (rule table + capability resolution) and
the types half of ``Slim/Music/Info.pm`` (``loadTypesConfig``).

The two shipped configuration files are byte copies of the Perl LMS
``public/9.2`` originals (md5 ``51c63deb…`` / ``068464fd…``) so the rule table
is the real 80-rule table, not a hand-maintained subset.

Perl citations
--------------
* ``Slim/Player/TranscodingHelper.pm:51-135``  ``loadConversionTables``
* ``Slim/Player/TranscodingHelper.pm:193-225`` ``_getCapabilities``
* ``Slim/Player/TranscodingHelper.pm:263-307`` ``checkBin`` (``[bin]`` lookup)
* ``Slim/Player/TranscodingHelper.pm:309-500`` ``getConvertCommand2``
* ``Slim/Player/TranscodingHelper.pm:519-673`` ``tokenizeConvertCommand2``
* ``Slim/Music/Info.pm:92-159``               ``loadTypesConfig``
* ``Slim/Utils/Prefs.pm:176-178``             ``searchSubString`` / ``splitList``
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent

#: Shipped defaults (verbatim LMS copies, ``Slim/Utils/OSDetect.pm:dirsFor``).
CONVERT_CONF = _PKG_DIR / "convert.conf"
TYPES_CONF = _PKG_DIR / "types.conf"

_RE_PROFILE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")
_RE_PROXY = re.compile(r"^proxy\s+(\S+)\s+(\S+)", re.IGNORECASE)
_RE_CAP_COMMENT = re.compile(r"^\s+#\s+(\S.*)")
_RE_CAP_SPEC = re.compile(r"([A-Z](:\{\w+=[^}]+\})?)+$")
_RE_CAP_ARG = re.compile(r"^:\{(\w+=[^}]+)\}")
_RE_BIN = re.compile(r"\[([^]]+)\]")
_RE_VAR = re.compile(r"%(\w)")
_RE_SUB = re.compile(r"\$(\w+)\$")
_RE_PREF = re.compile(r"\$\{(.*?)\}\$")
_RE_LEFTOVER = re.compile(r"\s+\$\w+\$")


# ---------------------------------------------------------------------------
# convert.conf
# ---------------------------------------------------------------------------

@dataclass
class ConversionTables:
    """The three hashes ``loadConversionTables`` fills (Perl :34-37)."""

    command_table: dict[str, str] = field(default_factory=dict)   # %commandTable
    capabilities: dict[str, dict] = field(default_factory=dict)   # %capabilities
    proxies: dict[str, str] = field(default_factory=dict)         # %proxies


def parse_capabilities(spec: str) -> dict:
    """Parse one ``# IFT:{START=--skip=%t}U:{END=--until=%v}`` capability line.

    Perl ``_getCapabilities`` (TranscodingHelper.pm:193-225): a run of
    ``<LETTER>`` or ``<LETTER>:{key=value,key=value}``, terminated by an
    always-present ``E`` extension hash.  ``OTUDB`` without arguments is an
    error in Perl; we keep the same shape but do not raise.
    """
    caps: dict = {}
    if not _RE_CAP_SPEC.fullmatch(spec or ""):
        logger.error("Capabilities syntax error: %s", spec)
        return caps

    rest = spec
    while rest:
        can, rest = rest[0], rest[1:]
        m = _RE_CAP_ARG.match(rest)
        if m:
            rest = rest[m.end():]
            if can == "E":
                # TranscodingHelper.pm:209 — `{ split /[=,]/, $1 }`
                parts = re.split(r"\s*[=,]\s*", m.group(1))
                args: object = dict(zip(parts[0::2], parts[1::2]))
            else:
                args = m.group(1)
        else:
            args = "noArgs"
        caps[can] = args

    # TranscodingHelper.pm:224 — "for convenience, always define an extension field"
    caps.setdefault("E", {})
    return caps


def load_conversion_tables(
    paths: Iterable[Path | str] = (),
) -> ConversionTables:
    """Read ``convert.conf`` files into a :class:`ConversionTables`.

    Mirrors ``loadConversionTables`` (TranscodingHelper.pm:51-135): comment and
    blank lines are skipped, a rule is a four-field profile line followed by an
    optional ``#`` capability line and the command line.  Duplicate profiles
    get a ``-N`` suffix (:101-105).
    """
    tables = ConversionTables()
    files = list(paths) or [CONVERT_CONF]

    for path in files:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:  # pragma: no cover - unreadable file
            logger.warning("Cannot read %s: %s", p, exc)
            continue

        i = 0
        while i < len(lines):
            line = lines[i]
            if re.match(r"^\s*#", line) or re.match(r"^\s*$", line):
                i += 1
                continue
            line = re.sub(r"#.*$", "", line).strip()

            proxy = _RE_PROXY.match(line)
            if proxy:
                tables.proxies[proxy.group(1)] = proxy.group(2)
                i += 1
                continue

            m = _RE_PROFILE.match(line)
            if not m:
                i += 1
                continue

            inputtype, outputtype, clienttype, clientid = (
                m.group(1), m.group(2), m.group(3), m.group(4).lower())
            profile = f"{inputtype}-{outputtype}-{clienttype}-{clientid}"

            # duplicate profile → unique instance suffix (:101-105)
            if profile in tables.command_table:
                idx = sorted(k for k in tables.command_table
                             if k.startswith(profile))
                last = idx[-1]
                tail = re.search(r"-(\d+)$", last)
                profile = f"{profile}-{int(tail.group(1)) + 1 if tail else 1}"

            i += 1
            cap_line = lines[i] if i < len(lines) else ""
            cap_match = _RE_CAP_COMMENT.match(cap_line)
            if cap_match:
                tables.capabilities[profile] = parse_capabilities(
                    cap_match.group(1))
                i += 1
            else:
                # TranscodingHelper.pm:112 — default capabilities
                tables.capabilities[profile] = {"I": "noArgs", "F": "noArgs"}

            command = (lines[i] if i < len(lines) else "").strip()
            i += 1
            if not command:
                continue
            tables.command_table[profile] = command

    return tables


# ---------------------------------------------------------------------------
# types.conf
# ---------------------------------------------------------------------------

@dataclass
class TypesConfig:
    """The four hashes ``loadTypesConfig`` fills (Info.pm:43-52)."""

    types: dict[str, str] = field(default_factory=dict)        # %types
    mime_types: dict[str, str] = field(default_factory=dict)   # %mimeTypes
    suffixes: dict[str, str] = field(default_factory=dict)     # %suffixes
    slim_types: dict[str, str] = field(default_factory=dict)   # %slimTypes


def load_types_config(paths: Iterable[Path | str] = ()) -> TypesConfig:
    """Parse ``types.conf`` (Info.pm:92-159).  ``-`` means "no value"."""
    cfg = TypesConfig()
    files = list(paths) or [TYPES_CONF]

    for path in files:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:  # pragma: no cover
            logger.warning("Cannot read %s: %s", p, exc)
            continue

        for raw in lines:
            line = re.sub(r"#.*$", "", raw)
            line = re.sub(r"^\s", "", line)
            line = re.sub(r"\s$", "", line)
            m = re.match(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
            if not m:
                continue
            ctype = m.group(1)
            for suffix in m.group(2).split(","):
                if suffix != "-":
                    cfg.suffixes[suffix] = ctype
            for mime in m.group(3).split(","):
                if mime != "-":
                    cfg.mime_types[mime.lower()] = ctype
            for slim in m.group(4).split(","):
                if slim != "-":
                    cfg.slim_types[ctype] = slim
            # Info.pm:142-145 — the first mime column is the default
            first = m.group(3).split(",")[0]
            if first != "-":
                cfg.types[ctype] = first

    return cfg


# ---------------------------------------------------------------------------
# cached singletons
# ---------------------------------------------------------------------------

_TABLES: ConversionTables | None = None
_TYPES: TypesConfig | None = None


def get_conversion_tables() -> ConversionTables:
    global _TABLES
    if _TABLES is None:
        _TABLES = load_conversion_tables()
    return _TABLES


def get_types_config() -> TypesConfig:
    global _TYPES
    if _TYPES is None:
        _TYPES = load_types_config()
    return _TYPES


# ---------------------------------------------------------------------------
# rule selection — getConvertCommand2
# ---------------------------------------------------------------------------

def check_bin(
    command: str,
    find_bin: Callable[[str], str | None] = shutil.which,
    cache: dict[str, str] | None = None,
) -> str | None:
    """Return ``command`` only when every ``[bin]`` resolves (Perl :263-307)."""
    cache = cache if cache is not None else {}
    for name in _RE_BIN.findall(command):
        if name not in cache:
            found = find_bin(name)
            if found:
                cache[name] = found
        if name not in cache:
            logger.warning("couldn't find binary for: %s", name)
            return None
    return command


def get_convert_command(
    source_type: str,
    supported_formats: Sequence[str],
    *,
    player: str | None = None,
    clientid: str | None = None,
    force_transcode: bool = False,
    stream_modes: Sequence[str] = ("I",),
    need: Sequence[str] = (),
    want: Sequence[str] = (),
    command_table: dict[str, str] | None = None,
    capabilities: dict[str, dict] | None = None,
    find_bin: Callable[[str], str | None] = shutil.which,
    rate_limit: int = 320,
) -> dict | None:
    """Pick the conversion profile Perl's ``getConvertCommand2`` would pick.

    ``supported_formats`` are the output formats the client advertised in its
    HELO caps (``Slim::Player::CapabilitiesHelper::supportedFormats``);
    ``stream_modes`` are the modes the source protocol offers (``I``/``F``/``R``);
    ``need``/``want`` are the mandatory/optional capabilities.

    Returns a transcoder dict (same keys as Perl :438-461) or ``None``.
    """
    tables = get_conversion_tables()
    ct = command_table if command_table is not None else tables.command_table
    cap_tbl = capabilities if capabilities is not None else tables.capabilities
    bin_cache: dict[str, str] = {}

    profiles: list[str] = []
    if force_transcode:
        profiles.append(f"{source_type}-{source_type}-transcode-*")   # :369

    for check_format in supported_formats:                            # :371-386
        if clientid and player:
            profiles.extend((
                f"{source_type}-{check_format}-{player}-{clientid}",
                f"{source_type}-{check_format}-*-{clientid}",
                f"{source_type}-{check_format}-{player}-*",
            ))
        profiles.append(f"{source_type}-{check_format}-*-*")
        if check_format == source_type and not force_transcode:
            profiles.append(f"{source_type}-{check_format}-transcode-*")

    backup: dict | None = None
    backup_wanted: int | None = None

    for base in profiles:
        instance = 0
        while True:
            profile = f"{base}-{instance}" if instance else base
            instance += 1
            if profile not in ct:
                break
            command = check_bin(ct[profile], find_bin, bin_cache)
            if not command:
                if instance > 1:
                    break
                continue
            caps = cap_tbl.get(profile, {})

            stream_mode = next((m for m in stream_modes if m in caps), None)
            if not stream_mode:                                        # :403-414
                if instance > 1:
                    break
                continue
            if any(cap not in caps for cap in need):                   # :417-426
                if instance > 1:
                    break
                continue

            streamformat = profile.split("-")[1]

            transcoder = {
                "command": command,
                "profile": profile,
                "usedCapabilities": list(need) + list(want),
                "streamMode": stream_mode,
                "streamformat": streamformat,
                "rateLimit": rate_limit,
                "clientid": clientid or "undefined",
                "name": "undefined",
                "player": player or "undefined",
                "channels": 2,
                "sampleSize": 16,
                "sampleRate": 44100,
                "outputChannels": 2,
                "extensions": caps.get("E", {}),
                "groupid": 0,
            }

            # optional capabilities → keep as backup (:464-483)
            missing = [cap for cap in want if cap not in caps]
            if missing:
                if backup is None or (backup_wanted or 0) > len(missing):
                    backup = transcoder
                    backup_wanted = len(missing)
                if instance > 1:
                    break
                continue

            return transcoder

    return backup


# ---------------------------------------------------------------------------
# command tokenizing — tokenizeConvertCommand2
# ---------------------------------------------------------------------------

def tokenize_convert_command(
    transcoder: dict,
    filepath: str,
    fullpath: str | None = None,
    *,
    quality: str = "5",
    binaries: dict[str, str] | None = None,
    capabilities: dict[str, dict] | None = None,
    find_bin: Callable[[str], str | None] = shutil.which,
) -> str:
    """Substitute one conversion command's placeholders.

    Perl ``tokenizeConvertCommand2`` (TranscodingHelper.pm:519-673):
    ``[bin]`` → the quoted binary path (:530-533), ``$FILE$``/``$URL$`` and the
    ``$…$`` capability literals (:587-597), ``%x`` variable capabilities
    (:599-633), then the leftover ``$…$`` cleanup (:663).
    """
    command = transcoder.get("command", "")
    if command == "-":
        return "-"

    binaries = dict(binaries or {})
    cap_tbl = capabilities if capabilities is not None else (
        get_conversion_tables().capabilities.get(transcoder.get("profile", ""), {}))

    def _bin(name: str) -> str | None:
        if name not in binaries:
            found = find_bin(name)
            if found:
                binaries[name] = found
        return binaries.get(name)

    # [bin] → "path" (:530-533)
    command = _RE_BIN.sub(lambda m: '"%s"' % (_bin(m.group(1)) or m.group(1)),
                          command)

    subs: dict[str, str] = {}
    variables: set[str] = set()

    # capability args carry the variable templates (e.g. BITRATE=--abr %B)
    for cap in [transcoder.get("streamMode"), *transcoder.get("usedCapabilities", [])]:
        if not cap:
            continue
        arg = cap_tbl.get(cap)
        if not isinstance(arg, str):
            continue
        m = re.match(r"(\w+)=(.+)", arg)
        if not m:
            continue
        subs[m.group(1)] = m.group(2)
        variables.update(_RE_VAR.findall(m.group(2)))

    file_quoted = filepath if filepath == "-" else f'"{filepath}"'
    subs["FILE"] = file_quoted
    subs["URL"] = f'"{fullpath or filepath}"'
    subs["QUALITY"] = quality
    subs["CHANNELS"] = str(transcoder.get("channels", 2))
    subs["SAMPLESIZE"] = str(transcoder.get("sampleSize", 16))
    subs["SAMPLERATE"] = str(transcoder.get("sampleRate", 44100))
    subs["OCHANNELS"] = str(transcoder.get("outputChannels", 2))
    subs["CLIENTID"] = re.sub(r"[.:]", "-", str(transcoder.get("clientid", "")))
    subs["PLAYER"] = re.sub(r'[" ]', "_", str(transcoder.get("player", "")))
    subs["NAME"] = re.sub(r'[" ]', "_", str(transcoder.get("name", "")))

    start = transcoder.get("start")
    end = transcoder.get("end")

    def _var_value(v: str) -> str:
        if v == "s":
            return str(start)
        if v == "u":
            return str(end)
        if v == "w" and start is not None and end is not None:
            return str(end - start)
        if v == "b":
            return str(transcoder.get("rateLimit", 320) * 1000)
        if v == "B":
            return str(transcoder.get("rateLimit", 320))
        if v == "f":
            return subs["FILE"]
        if v == "F":
            return subs["URL"]
        if v == "i":
            return str(transcoder.get("clientid", ""))
        if v == "I":
            return subs["CLIENTID"]
        if v == "p":
            return str(transcoder.get("player", ""))
        if v == "P":
            return subs["PLAYER"]
        if v == "q":
            return quality
        if v == "C":
            return subs["CHANNELS"]
        if v == "c":
            return subs["OCHANNELS"]
        return ""

    for var in variables:
        value = _var_value(var)
        for key in list(subs):
            subs[key] = subs[key].replace(f"%{var}", value)

    # $KEY$ substitution
    command = _RE_SUB.sub(lambda m: subs.get(m.group(1), m.group(0)), command)

    # ${PREF.KEY}$ placeholders (Perl :641-660) — no prefs wired here
    for m in list(_RE_PREF.finditer(command)):
        placeholder = m.group(1)
        resolved = binaries.get(placeholder, "")
        command = command.replace(f"${{{placeholder}}}$", resolved)

    # clean all remaining '$*$' (:663)
    command = _RE_LEFTOVER.sub("", command)

    return command
