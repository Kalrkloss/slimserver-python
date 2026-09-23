"""Player-side firmware upgrade logic — Perl's ``needsUpgrade``/``upgradeFirmware``.

This is the PLAYER half of the firmware mechanism: which image a given player
class wants, whether the player's current revision needs one, and which file
``upgradeFirmware`` then picks.  The download/verify/serve half lives in
``lyrion.utils.firmware`` (Perl ``Slim/Utils/Firmware.pm``).

Perl reference (read-only checkout ``/tmp/lms-ref``, ``public/9.2``, commit
``d1d0a68``)
---------------------------------------------------------------------------
* ``Slim/Player/Squeezebox2.pm:323-388``   ``upgradeFirmware`` — the SDK5 path
  (Squeezebox2/Transporter/Boom; ``Squeezebox1.pm:354-410`` is the SDK4 twin).
* ``Slim/Player/Squeezebox.pm:241-341``    ``needsUpgrade`` — reads the
  ``<model>.version`` file, maps the player's revision through the range
  table, returns the target revision (or 0).
* ``Slim/Player/Squeezebox.pm:332-342``    the ``FIRMWARE_MISSING`` branch of
  ``_checkFirmwareUpgrade`` (showBriefly, no ``updn`` — that is
  ``Squeezebox2.pm:340-360``).
* ``Slim/Player/Squeezebox.pm:395-437``    ``upgradeFirmware_SDK5`` — the
  ``upda`` chunked transfer and the final ``updn``.
* ``Slim/Player/Client.pm:645-647``        base ``needsUpgrade`` — ``return 0``
  (a class that cannot upgrade).
* ``Slim/Player/SqueezePlay.pm:144``       ``sub needsUpgrade {}`` — undef.
* ``Slim/Player/SqueezeSlave.pm:161``      its own ``needsUpgrade``.
* ``Slim/Utils/OSDetect.pm`` ``dirsFor('Firmware')``/``('updates')`` — the two
  search directories (see :func:`lyrion.utils.firmware.firmware_dirs`).
* ``Slim/Utils/Firmware.pm:88-114``        ``custom.<model>.version`` /
  ``custom.<model>.bin`` take precedence over the downloaded pair.

Every value below is a Perl literal or a direct port of a Perl branch; where a
thing is NOT read from Perl it is called out in the docstring.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``Slim/Player/Squeezebox.pm:258`` — the version file is ``<model>.version``
#: inside ``dirsFor('Firmware')``; the custom twin is
#: ``custom.<model>.version`` (``Slim/Utils/Firmware.pm:100``).
VERSION_FILE_SUFFIX = ".version"
CUSTOM_VERSION_PREFIX = "custom."

#: ``Squeezebox.pm:275-289`` — the three line shapes of a ``<model>.version``
#: file.  ``N..M T`` = "revisions N through M upgrade to T";
#: ``N T`` = "revision N upgrades to T"; ``* T`` = the default target.
_RE_RANGE = re.compile(r"^(\d+)\s*\.\.\s*(\d+)\s+(\d+)\s*$")   # :275
_RE_EXACT = re.compile(r"^(\d+)\s+(\d+)\s*$")                  # :279
_RE_DEFAULT = re.compile(r"^\*\s+(\d+)\s*$")                   # :283

#: ``Slim/Player/Squeezebox.pm:376`` — ``<model>_<to_version>.bin``.
def image_name(model: str, to_version: str | int) -> str:
    """Perl's ``$client->model . "_$to_version.bin"`` (``Squeezebox2.pm:334``)."""
    return f"{model}_{to_version}.bin"


@dataclass
class VersionTable:
    """Parsed ``<model>.version`` file (``Squeezebox.pm:266-300``)."""

    #: revision → target revision, from the range and exact lines.
    mapping: dict[int, int]
    #: ``* T`` — the fallback used when no rule matches (``:290-295``).
    default: int | None = None

    def target_for(self, revision: int) -> int | None:
        """``$to`` for a revision, else the default, else ``None``.

        Perl walks the file and takes the LAST matching line (it ``last``s out
        of the loop on the first match, ``:287``) — but it collects a single
        ``$to``/``$default``, so a file whose rules overlap keeps the first hit.
        The port keeps the same first-match-wins behaviour by returning the
        first rule that covers the revision.
        """
        if revision in self.mapping:
            return self.mapping[revision]
        return self.default


def parse_version_table(text: str, *, source: str = "") -> VersionTable:
    """Port of the ``while (<$versionFile>)`` loop (``Squeezebox.pm:268-297``).

    Blank lines and comments (``^\\s*(#.*)?$``) are skipped (:273); an
    unrecognised line logs ``Garbage in $versionFilePath at line $.: $_``
    (:293-295).  Range lines map every revision in ``[lo, hi]`` to ``to``;
    exact lines map one revision; ``*`` sets the default.  Perl keeps the
    first matching rule, so a later rule for the same revision does not win.
    """
    table = VersionTable(mapping={})
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.rstrip("\n").rstrip()
        if re.match(r"^\s*(#.*)?$", raw):
            continue
        match = _RE_RANGE.match(line)
        if match:
            lo, hi, to = (int(match.group(1)), int(match.group(2)),
                          int(match.group(3)))
            for revision in range(lo, hi + 1):
                table.mapping.setdefault(revision, to)
            continue
        match = _RE_EXACT.match(line)
        if match:
            table.mapping.setdefault(int(match.group(1)), int(match.group(2)))
            continue
        match = _RE_DEFAULT.match(line)
        if match:
            table.default = int(match.group(1))
            continue
        location = f"{source}:{lineno}" if source else f"line {lineno}"
        logger.error("Garbage in firmware version file %s: %s", location, line)
    return table


def read_version_file(path: Path) -> str | None:
    """Read a version file, or ``None`` when it cannot be opened.

    Perl ``open($versionFile, "<$versionFilePath")`` failing logs
    ``can't open $versionFilePath`` and returns 0 (``Squeezebox.pm:258-261``).
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("can't open %s", path)
        return None


def resolve_version_paths(model: str, directories: list[Path]) -> list[Path]:
    """The candidate version files for ``model``, in Perl's precedence order.

    ``Slim/Utils/Firmware.pm:100-114``: ``custom.<model>.version`` wins over
    ``<model>.version``; ``Firmware.pm:252-256`` searches every directory it is
    given (``updatesDir`` first, then the shipped ``Firmware`` tree).
    Returns every existing candidate, custom first — the caller uses the first
    one whose image is actually present.
    """
    candidates: list[Path] = []
    for directory in directories:
        candidates.append(Path(directory) / f"{CUSTOM_VERSION_PREFIX}{model}{VERSION_FILE_SUFFIX}")
    for directory in directories:
        candidates.append(Path(directory) / f"{model}{VERSION_FILE_SUFFIX}")
    return [path for path in candidates if path.is_file()]


@dataclass
class UpgradeDecision:
    """What ``needsUpgrade`` concluded for one player."""

    #: The target revision, or 0 when no upgrade is wanted
    #: (``Squeezebox.pm:308``/``:315`` ``$client->_needsUpgrade(0)``).
    target: int = 0
    #: The version file that produced the decision (``None`` if unreadable).
    version_file: Path | None = None
    #: True when the revision maps to itself: "up to date" but Perl still
    #: checks for a firmware file (``$okButCheckFirmware``, ``:311``).
    up_to_date: bool = False
    #: Human-readable reason, for the log line Perl writes.
    reason: str = ""


def needs_upgrade(
    revision: str | int,
    model: str,
    directories: list[Path],
) -> UpgradeDecision:
    """Port of ``Slim/Player/Squeezebox.pm:241-341`` ``needsUpgrade``.

    ``$from`` is the player's own revision (HELO/handshake number, ``:250``
    ``$client->revision``); a missing/zero revision returns 0 immediately
    (:250 ``|| return 0``).  The result is the TARGET revision when the table
    maps the current one to something else, or 0 when it maps to itself.

    The SIDE EFFECTS of Perl (writing ``_needsUpgrade``, starting a background
    download via ``Slim::Utils::Firmware::downloadAsync`` at ``:327`` and
    showing ``FIRMWARE_MISSING``) are NOT performed here — this function only
    decides.  ``upgrade_firmware`` in :mod:`lyrion.player.manager` owns them,
    because the download belongs to ``utils.firmware`` (Perl's
    ``Slim/Utils/Firmware.pm``) and the message to the display layer.
    """
    try:
        current = int(revision)
    except (TypeError, ValueError):
        return UpgradeDecision(0, None, False, "no revision")
    if not current:
        return UpgradeDecision(0, None, False, "no revision")

    paths = resolve_version_paths(model, directories)
    if not paths:
        logger.info("No firmware version file for %s", model)
        return UpgradeDecision(0, None, False, "no version file")

    version_file = paths[0]
    text = read_version_file(version_file)
    if text is None:
        return UpgradeDecision(0, version_file, False, "version file unreadable")

    table = parse_version_table(text, source=str(version_file))
    target = table.target_for(current)
    if target is None:
        logger.info("No upgrades found for %s v. %d", model, current)
        return UpgradeDecision(0, version_file, False, "no rule matches")

    if target == current:
        logger.info("%s firmware is up-to-date, v. %d", model, current)
        # Perl marks this as "ok but check the firmware file anyway" (:311).
        return UpgradeDecision(0, version_file, True, "up-to-date")

    logger.info("%s v. %d requires upgrade to %d", model, current, target)
    return UpgradeDecision(target, version_file, False, "upgrade")


def choose_image(
    model: str,
    target: int,
    directories: list[Path],
) -> Path | None:
    """Port of the file selection in ``Squeezebox2.pm:332-362``.

    ``$file`` is ``<Firmware>/<model>_<to>.bin`` and ``$file2`` is
    ``<updates>/<model>_<to>.bin`` (:334-335); when the first is missing but
    the second exists, ``$file = $file2`` (:362).  ``None`` means neither
    exists — the caller then shows ``FIRMWARE_MISSING`` and sends the ``updn``
    that lets the player reconnect (``:340-360``).

    ``directories`` is Perl's ordered ``(dirsFor('Firmware'),
    dirsFor('updates'))`` pair; the port passes the same two element order so
    the "shipped image wins over a downloaded one" behaviour is kept.
    """
    if not directories:
        return None
    if not target:
        return None
    primary = Path(directories[0]) / image_name(model, target)
    if primary.is_file():
        return primary
    if len(directories) > 1:
        fallback = Path(directories[1]) / image_name(model, target)
        if fallback.is_file():
            return fallback
    for directory in directories[2:]:
        candidate = Path(directory) / image_name(model, target)
        if candidate.is_file():
            return candidate
    return None


def upgrade_line(model: str, to_version: int, from_revision: int | None = None,
                 device_id: int | None = None) -> str:
    """The display line for an in-progress upgrade — ``Squeezebox.pm:420-436``.

    ``$client->string('UPDATING_FIRMWARE_' . uc($client->model()))`` (:420).
    The Boom (``deviceid == 10``, :423) appends ``" (1 of 2)"`` / ``"(2 of 2)"``
    for its two-stage upgrade (:425-434).  The translation table maps the token
    through :func:`lyrion.i18n.get_string`; a missing token falls back to the
    token itself (Perl ``Display.pm:884-886``).
    """
    from lyrion.i18n import get_string

    token = f"UPDATING_FIRMWARE_{model.upper()}"
    line = get_string(token, default="")
    if not line:
        # No model-specific token: Perl's own string() would return the token.
        line = get_string("UPDATING_FIRMWARE", default=token)
    if device_id == 10 and to_version and from_revision is not None:
        out_of = get_string("OUT_OF", default="of")
        if to_version == 30:
            line += f" (1 {out_of} 2)"       # :427-430
        elif from_revision == 30:
            line += f" (2 {out_of} 2)"       # :431-434
    return line


__all__ = [
    "CUSTOM_VERSION_PREFIX",
    "VERSION_FILE_SUFFIX",
    "UpgradeDecision",
    "VersionTable",
    "choose_image",
    "image_name",
    "needs_upgrade",
    "parse_version_table",
    "read_version_file",
    "resolve_version_paths",
    "upgrade_line",
]
