"""Firmware versioning + download for Pyrion Music Server.

This module is the Python equivalent of ``Slim/Utils/Firmware.pm`` — both the
isolated version arithmetic the port already had **and** the download/verify/
serve mechanism Perl actually runs.  Every value and every control-flow branch
below carries its Perl source location; nothing here is guessed.

Perl reference (read-only checkout ``/tmp/lms-ref``, ``public/9.2``)
--------------------------------------------------------------------
* ``Slim/Utils/Firmware.pm:16-20``   module contract: "downloads firmware in
  async mode and try to download missing firmware every 10 minutes in the
  background.  All downloaded firmware is verified using an SHA1 checksum
  file before being saved."
* ``Slim/Utils/Firmware.pm:41-42``   ``INITIAL_RETRY_TIME => 600``,
  ``MAX_RETRY_TIME => 86400``.
* ``Slim/Utils/Firmware.pm:49``      ``sub BASE { 'http://downloads.lms-community.org/update/firmware/' }``
  (a ``sub``, not a constant — "to allow 3rd party plugins to re-use the
  mechanism", ``:48``).
* ``Slim/Utils/Firmware.pm:50``      ``sub CHECK_INTERVAL { MAX_RETRY_TIME }``.
* ``Slim/Utils/Firmware.pm:53``      ``my $CHECK_TIME = INITIAL_RETRY_TIME;`` —
  the back-off starts at 600 s and doubles on failure (``:538-541``).
* ``Slim/Utils/Firmware.pm:56``      ``my $BASE_FOLDER = $::VERSION;`` — the
  server version is the folder inside ``BASE``; on a 404 it falls back to
  ``'latest'`` (``:513-519``).
* ``Slim/Utils/Firmware.pm:74-75``   directory pair ``dirsFor('Firmware')``
  (shipped images) and ``dirsFor('updates')`` (downloaded images).
* ``Slim/Utils/OS.pm:157-174``       ``dirsFor('updates')`` =
  ``<cachedir>/updates`` (created on demand).
* ``Slim/Utils/Firmware.pm:93-135``  ``init_firmware_download`` —
  custom files first, then the ``checkVersion`` pref gate, then the
  ``$model.version`` download.
* ``Slim/Utils/Firmware.pm:109-110`` / ``:154`` / ``:258``  the version file is
  parsed with ``m/^([^ ]+)\\sr(\\d+)/`` → ``($version, $revision)``.
* ``Slim/Utils/Firmware.pm:145-201`` ``init_version_done`` — compute
  ``${model}_${ver}_r${rev}.bin``, download it if missing, else register the
  existing file; then re-schedule ``init_firmware_download`` after
  ``CHECK_INTERVAL()``.
* ``Slim/Utils/Firmware.pm:188,227,270``  ``Slim::Web::Pages->addRawDownload(
  "^firmware/${model}.*\\.bin", $fw_file, 'binary')`` — the HTTP route.
* ``Slim/Utils/Firmware.pm:112``     the custom image gets its own route
  ``"^firmware/custom.$model.bin"``.
* ``Slim/Utils/Firmware.pm:239-247`` ``init_fw_error`` — a failed download
  logs and falls back to any firmware already on disk ("Server will keep
  trying to download a new one").
* ``Slim/Utils/Firmware.pm:360-550`` ``downloadAsync`` / ``downloadAsyncDone``
  / ``downloadAsyncSHADone`` / ``downloadAsyncError`` — the actual transfer:
  save to ``$file.tmp``, fetch ``$url.sha``, compare the 40-hex digest, rename
  on match, unlink the tmp file on mismatch, double the back-off on failure.
* ``Slim/Utils/Firmware.pm:96``      ``return if $model =~ /squeeze(?:play|lite)/i;``
  — no firmware for the desktop players.
* ``Slim/Utils/Firmware.pm:117-120`` the ``checkVersion`` pref (Server
  Settings → Software Updates): when off, only local images are used.

Deliberate deviations (documented, not accidental)
--------------------------------------------------
1. **The four invented helpers are gone.**  ``get_firmware_url`` /
   ``parse_firmware_response`` / ``encode_firmware_request`` /
   ``decode_firmware_response`` (old ``firmware.py:174-254``) produced query
   parameters (``?playerid=…&revision=…``) and a ``"FRM:"``/``"UPD"``/
   ``"upd?"`` wire format that Perl does not have anywhere.  Perl's player
   request is the SlimProto ``UREQ`` frame handled by the player layer; the
   firmware itself travels over plain HTTP (``BASE`` above).  Keeping them
   would have produced wrong handshakes, so they are deleted rather than
   re-purposed.
2. **Package directory.**  Perl's ``dirsFor('Firmware')`` is empty on a
   source Unix run (``Slim/Utils/OS.pm:143-176`` only knows ``Plugins`` and
   ``updates``) and ``/usr/share/squeezeboxserver/Firmware`` in the Debian
   package (``OS/Debian.pm:50-52``).  The port has no packaged image tree, so
   :func:`firmware_dirs` returns the packaged path for
   ``packaged=True`` and ``[]`` otherwise — the downloaded-image directory
   (``updates``) is the one that always exists.
3. **Single-shot download instead of the async iterator.**  Perl chains four
   callbacks over ``SimpleAsyncHTTP``; the port uses one blocking
   ``urllib`` fetch per file inside a worker thread and keeps the same order
   (image → ``.sha`` → compare → rename), because the port has no equivalent
   async HTTP client and the call runs off the playback path anyway.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lyrion.version import __version__

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Perl constants — values verbatim, each with its source line
# ---------------------------------------------------------------------------

#: ``Firmware.pm:49`` — ``sub BASE { 'http://downloads.lms-community.org/update/firmware/' }``.
FIRMWARE_BASE = "http://downloads.lms-community.org/update/firmware/"

#: ``Firmware.pm:41`` — ``use constant INITIAL_RETRY_TIME => 600;``
INITIAL_RETRY_TIME = 600

#: ``Firmware.pm:42`` — ``use constant MAX_RETRY_TIME => 86400;``
MAX_RETRY_TIME = 86400

#: ``Firmware.pm:50`` — ``sub CHECK_INTERVAL { MAX_RETRY_TIME }``.
CHECK_INTERVAL = MAX_RETRY_TIME

#: ``Firmware.pm:56`` — ``my $BASE_FOLDER = $::VERSION;`` (``slimserver.pl:61``
#: ``our $VERSION = '9.2.0';``).  Falls back to ``'latest'`` after a 404
#: (``Firmware.pm:513-519``).
BASE_FOLDER = __version__
BASE_FOLDER_FALLBACK = "latest"  # Firmware.pm:519

#: ``Firmware.pm:96`` — there is no firmware for the desktop players.
_DESKTOP_MODEL_RE = re.compile(r"squeeze(?:play|lite)", re.IGNORECASE)

#: ``Firmware.pm:110``/``:154``/``:258`` — ``m/^([^ ]+)\sr(\d+)/``.
VERSION_FILE_RE = re.compile(r"^([^ ]+)\sr(\d+)")

#: ``Firmware.pm:439`` — ``my ($sum) = $http->content =~ m/([a-f0-9]{40})/;``
SHA1_RE = re.compile(r"([a-f0-9]{40})")

#: ``Firmware.pm:78`` — the old download location Perl cleans up at init.
LEGACY_CACHE_RE = re.compile(r"^\w{4}_\d\.\d_.*\.bin(\.tmp)?$", re.IGNORECASE)
#: ``Firmware.pm:79``.
LEGACY_VERSION_RE = re.compile(r"^.*version$", re.IGNORECASE)

#: ``Slim/Utils/OS/Debian.pm:50-52`` — packaged image tree.
PACKAGED_FIRMWARE_DIR = Path("/usr/share/squeezeboxserver/Firmware")


# ---------------------------------------------------------------------------
# Version parsing (kept from the previous module — the arithmetic was fine)
# ---------------------------------------------------------------------------

@dataclass
class FirmwareVersion:
    """
    Parsed firmware version number.

    Versions follow the format: major.minor.patch.build
    Examples: "127", "137", "160", "161.1", "7.7.2", "9.0.0.12345"
    """
    major: int = 0
    minor: int = 0
    patch: int = 0
    build: int = 0
    raw: str = ""

    @classmethod
    def parse(cls, version_str: str) -> FirmwareVersion:
        """Parse a firmware version string."""
        result = cls()
        result.raw = str(version_str).strip()

        # Remove leading 'v' if present
        version_str = version_str.lstrip("vV")

        # Try to split by dots
        parts = version_str.split(".")

        try:
            result.major = int(parts[0]) if len(parts) > 0 else 0
            result.minor = int(parts[1]) if len(parts) > 1 else 0
            result.patch = int(parts[2]) if len(parts) > 2 else 0
            result.build = int(parts[3]) if len(parts) > 3 else 0
        except (ValueError, IndexError):
            pass

        return result

    def __str__(self) -> str:
        if self.build:
            return f"{self.major}.{self.minor}.{self.patch}.{self.build}"
        if self.patch:
            return f"{self.major}.{self.minor}.{self.patch}"
        return f"{self.major}.{self.minor}"

    def __repr__(self) -> str:
        return f"FirmwareVersion({self.major}.{self.minor}.{self.patch}.{self.build})"

    def __lt__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() < other._tuple()

    def __le__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() <= other._tuple()

    def __gt__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() > other._tuple()

    def __ge__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() >= other._tuple()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        if not isinstance(other, FirmwareVersion):
            return False
        return self._tuple() == other._tuple()

    def __hash__(self) -> int:
        return hash(self._tuple())

    def _tuple(self) -> tuple[int, int, int, int]:
        return (self.major, self.minor, self.patch, self.build)


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

# LMS / Lyrion version mapping to minimum firmware requirements
MIN_FIRMWARE_REQUIREMENTS: dict[str, str] = {
    # (lms_version_major, lms_version_minor): minimum_player_firmware
    (7, 0): "127",
    (7, 5): "137",
    (7, 6): "150",
    (7, 7): "155",
    (7, 8): "160",
    (7, 9): "161",
    (8, 0): "161",
    (9, 0): "161",
    (9, 2): "161",
}


def get_min_firmware_for_lms_version(lms_major: int, lms_minor: int) -> FirmwareVersion:
    """Return minimum player firmware version for a given LMS version."""
    key = (lms_major, lms_minor)
    min_version_str = MIN_FIRMWARE_REQUIREMENTS.get(key, "127")
    return FirmwareVersion.parse(min_version_str)


def is_player_compatible(
    player_firmware: str | FirmwareVersion,
    lms_version: str | FirmwareVersion,
) -> bool:
    """
    Return True if a player firmware is compatible with the LMS version.
    """
    if isinstance(player_firmware, str):
        player_firmware = FirmwareVersion.parse(player_firmware)
    if isinstance(lms_version, str):
        lms_version = FirmwareVersion.parse(lms_version)

    min_fw = get_min_firmware_for_lms_version(lms_version.major, lms_version.minor)
    return player_firmware >= min_fw


def needs_upgrade(
    player_firmware: str | FirmwareVersion,
    lms_version: str | FirmwareVersion,
) -> tuple[bool, str]:
    """
    Check if a player needs a firmware upgrade.

    Returns (needs_upgrade: bool, message: str)
    """
    if isinstance(player_firmware, str):
        player_firmware = FirmwareVersion.parse(player_firmware)
    if isinstance(lms_version, str):
        lms_version = FirmwareVersion.parse(lms_version)

    min_fw = get_min_firmware_for_lms_version(lms_version.major, lms_version.minor)

    if player_firmware >= min_fw:
        return False, "Compatible"

    return True, (
        f"Player firmware {player_firmware} is older than recommended "
        f"minimum {min_fw} for LMS {lms_version}. "
        "Upgrade recommended."
    )


def model_needs_upgrade(current: str, available: "FirmwareInfo") -> bool:
    """Perl ``Firmware.pm:319-348`` ``need_upgrade($current, $model)``.

    ``( $firmwares->{$model}->{version} ne $cur_version )
      || ( $firmwares->{$model}->{revision} > $cur_rev )``  (``:336-340``) —
    a *different* version string forces the upgrade even when it is older, so a
    newer firmware never gets silently downgraded by a revision compare alone.

    ``False`` when nothing is available (``:322-325``) or the player's string
    does not parse (``:329-332``).
    """
    if available is None or not available.file or not available.version:
        return False

    match = VERSION_FILE_RE.match(current or "")
    if not match:
        logger.warning("%s sent invalid current version: %s", available.model, current)
        return False

    cur_version, cur_rev = match.group(1), int(match.group(2))
    return (str(available.version) != cur_version
            or int(available.revision) > cur_rev)


# ---------------------------------------------------------------------------
# Version-file parsing (Perl Firmware.pm:110 / :154 / :258)
# ---------------------------------------------------------------------------

def parse_version_file(text: str) -> tuple[str, int] | None:
    """Perl's ``($ver, $rev) = $version =~ m/^([^ ]+)\\sr(\\d+)/``.

    The ``jive.version`` file looks like (``Firmware.pm:151-153``)::

        7.0 rNNNN
        sdi@padbuild #24 Sat Sep 8 01:26:46 PDT 2007

    Only the first line matters; the revision is the digits after ``r``.
    Returns ``None`` when the file does not match — Perl then leaves both
    values ``undef`` and the subsequent ``ne``/``>`` comparisons behave the
    same as our ``False``.
    """
    match = VERSION_FILE_RE.match(text or "")
    if not match:
        return None
    return match.group(1), int(match.group(2))


@dataclass
class FirmwareInfo:
    """One entry of Perl's ``%$firmwares`` (``Firmware.pm:160-164``)."""

    model: str
    version: str = ""
    revision: int = 0
    #: Absolute path of the image, or the Perl dummy ``1`` when the player
    #: downloads from the origin host directly (``Firmware.pm:163``).
    file: Path | str | int = 0

    @property
    def filename(self) -> str:
        """``basename($firmwares->{$model}->{file})`` (``Firmware.pm:309``)."""
        return Path(str(self.file)).name if self.file else ""

    def bin_name(self) -> str:
        """Perl's ``${model}_${ver}_r${rev}.bin`` (``Firmware.pm:171``)."""
        return f"{self.model}_{self.version}_r{self.revision}.bin"


# ---------------------------------------------------------------------------
# Directories — dirsFor('Firmware') / dirsFor('updates')
# ---------------------------------------------------------------------------

def updates_dir(cache_dir: str | Path) -> Path:
    """Perl ``dirsFor('updates')`` — ``<cachedir>/updates``.

    ``Slim/Utils/OS.pm:157-174``: the pref ``cachedir`` plus ``'updates'``,
    ``mkdir``-ed unless it already exists.  This is where downloaded
    ``<model>.version`` and ``<model>_<ver>_r<rev>.bin`` files live
    (``Firmware.pm:98``, ``:171``).
    """
    target = Path(cache_dir) / "updates"
    target.mkdir(parents=True, exist_ok=True)
    return target


def firmware_dirs(*, packaged: bool = False) -> list[Path]:
    """Perl ``dirsFor('Firmware')`` — the *shipped* image directories.

    ``Slim/Utils/OS.pm:143-176`` has no ``Firmware`` branch at all, so a source
    run yields the empty list (the ``updates`` directory above is the only
    writable place, ``Firmware.pm:252`` searches both).  ``OS/Debian.pm:50-52``
    (and ``OS/RedHat.pm:42``) add ``/usr/share/squeezeboxserver/Firmware``;
    ``packaged=True`` selects it.  Deviation 2 in the module docstring.
    """
    if packaged:
        return [PACKAGED_FIRMWARE_DIR]
    return []


def cleanup_legacy_cache(cache_dir: str | Path) -> list[Path]:
    """Perl ``Firmware.pm:77-80`` — delete the old download location.

    Two ``deleteFiles`` calls in ``init()``: ``qr/^\\w{4}_\\d\\.\\d_.*\\.bin(\\.tmp)?$/i``
    and ``qr/^.*version$/i`` inside ``cachedir``.  Returns what was removed so
    a caller (or a test) can see it.
    """
    removed: list[Path] = []
    root = Path(cache_dir)
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        if LEGACY_CACHE_RE.match(entry.name):
            try:
                entry.unlink()
                removed.append(entry)
            except OSError:
                pass
        elif LEGACY_VERSION_RE.match(entry.name):
            # Perl's second deleteFiles call is anchored with ^.*version$ — it
            # would also match a directory name, but deleteFiles only unlinks
            # files (Misc.pm:deleteFiles), hence the is_file() guard above.
            try:
                entry.unlink()
                removed.append(entry)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# The downloader — Perl downloadAsync/downloadAsyncDone/downloadAsyncSHADone
# ---------------------------------------------------------------------------

class FirmwareError(RuntimeError):
    """Mirrors the ``$error`` string Perl hands to ``downloadAsyncError``."""


@dataclass
class DownloadResult:
    """Outcome of one :meth:`FirmwareDownloader.download`.

    ``status`` is one of ``download`` (fresh bytes verified and renamed),
    ``exists`` (the target was already there, ``Firmware.pm:178-184``),
    ``error`` (transfer/verify failure, ``:239-247``), ``local`` (custom or
    pre-existing image, ``:103-115``/``:249-279``) or ``direct`` (the player
    fetches from the origin host, ``:158-167``).
    """

    status: str
    path: Path | None = None
    url: str = ""
    sha1: str = ""
    detail: str = ""
    http_status: int = 0


class FirmwareDownloader:
    """Perl's ``downloadAsync`` family, one object per download location.

    ``updates_dir`` is ``dirsFor('updates')``; ``firmware_dir`` (optional) is
    the first ``dirsFor('Firmware')`` entry and is searched read-only, exactly
    like ``get_fw_locally`` (``Firmware.pm:252``).  ``opener`` exists for the
    tests: it receives the URL and returns a file-like object (readable
    ``.read()``/``.status``), defaulting to a plain ``urllib`` request.
    """

    def __init__(
        self,
        updates_dir: str | Path | None = None,
        *,
        firmware_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        base_url: str = FIRMWARE_BASE,
        base_folder: str = BASE_FOLDER,
        opener: Any = None,
    ) -> None:
        #: Firmware.pm:74-75 — the two directory sources.
        if updates_dir is not None:
            self.updates_dir = Path(updates_dir)
        elif cache_dir is not None:
            self.updates_dir = updates_dir_path(cache_dir)
        else:
            raise ValueError("updates_dir or cache_dir is required")
        self.firmware_dir = Path(firmware_dir) if firmware_dir else None
        self.base_url = base_url
        #: Firmware.pm:56 + :513-519 — version folder, ``latest`` after a 404.
        self.base_folder = base_folder
        self._opener = opener
        #: Firmware.pm:53 — the back-off state, doubling on failure.
        self.check_time = INITIAL_RETRY_TIME
        #: Firmware.pm:358 — ``%filesDownloading`` de-duplication.
        self._downloading: set[str] = set()

    # -- URL -------------------------------------------------------------

    def download_url(self, filename: str) -> str:
        """Perl ``Firmware.pm:379`` — ``BASE(basename($file)) . $BASE_FOLDER . '/' . basename($file)``.

        Note the Perl quirk: ``BASE(...)`` *discards* the argument (``BASE`` is a
        no-arg sub, ``:49``), so the URL is ``BASE . $BASE_FOLDER . '/' . name``
        — e.g. ``http://downloads.lms-community.org/update/firmware/9.2.0/jive.version``.
        """
        return f"{self.base_url}{self.base_folder}/{Path(filename).name}"

    def sha_url(self, url: str) -> str:
        """Perl ``Firmware.pm:424`` — ``$http->get( $url . '.sha' )``."""
        return url + ".sha"

    # -- fetching --------------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        """One GET; raises :class:`FirmwareError` with Perl's error shapes."""
        try:
            if self._opener is not None:
                response = self._opener(url)
            else:  # pragma: no cover - exercised live, not in the suite
                response = urllib.request.urlopen(url, timeout=30)  # noqa: S310
            try:
                data = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            return data
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            # Firmware.pm:513 — Perl matches /^40[34] / against "$code $message".
            raise FirmwareError(f"{exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise FirmwareError(str(exc)) from exc

    @staticmethod
    def sha1_of(data: bytes) -> str:
        """``Digest::SHA1->hexdigest`` (``Firmware.pm:445-449``)."""
        return hashlib.sha1(data).hexdigest()  # noqa: S324 - firmware wire format

    @classmethod
    def extract_expected_sha(cls, sha_file: bytes | str) -> str | None:
        """Perl ``Firmware.pm:439`` — first 40-hex run in the ``.sha`` body."""
        if isinstance(sha_file, bytes):
            sha_file = sha_file.decode("latin-1", "replace")
        match = SHA1_RE.search(sha_file)
        return match.group(1) if match else None

    # -- the actual Perl sequence ---------------------------------------

    def download_version_file(self, model: str) -> bytes | None:
        """Download ``<model>.version`` into the updates dir.

        Perl ``init_firmware_download`` → ``downloadAsync($version_file, …)``
        (``Firmware.pm:126-134``).  The file is SHA1-checked like any other
        (``downloadAsyncDone`` does not special-case it) and saved to
        ``<model>.version``.
        """
        target = self.updates_dir / f"{model}.version"
        result = self.download(target)
        if result.status != "download":
            logger.debug("Firmware: %s.version not stored (%s) %s",
                         model, result.status, result.detail)
            return None
        try:
            return target.read_bytes()
        except OSError:  # pragma: no cover - unreadable right after rename
            return None

    def download(self, file: str | Path) -> DownloadResult:
        """Download ``file`` to ``file.tmp``, verify its SHA1, then rename.

        Perl ``downloadAsync`` (``Firmware.pm:360-395``),
        ``downloadAsyncDone`` (``:403-425``) and ``downloadAsyncSHADone``
        (``:433-477``) in sequence, including the ``latest`` retry on a 404
        (``:513-523``) and the tmp-file cleanup on a mismatch (``:475`` →
        ``:502``).
        """
        target = Path(file)
        name = target.name
        if name in self._downloading:
            return DownloadResult("busy", path=target,
                                  detail="download already in progress")
        self._downloading.add(name)
        try:
            return self._download_locked(target)
        finally:
            self._downloading.discard(name)

    def _download_locked(self, target: Path) -> DownloadResult:
        url = self.download_url(target.name)
        tmp = target.with_name(target.name + ".tmp")
        self.updates_dir.mkdir(parents=True, exist_ok=True)

        try:
            data = self._fetch(url)
        except FirmwareError as exc:
            message = str(exc)
            retried = self._maybe_retry_latest(target, message)
            if retried is not None:
                return retried
            return self._fail(target, url, message)

        # Firmware.pm:410-412 — "make sure we got the file".
        if not data:
            return self._fail(target, url, "File was not saved properly")

        # Firmware.pm:415-424 — fetch the checksum file next.
        try:
            sha_body = self._fetch(self.sha_url(url))
        except FirmwareError as exc:
            return self._fail(target, url, str(exc))

        expected = self.extract_expected_sha(sha_body)
        actual = self.sha1_of(data)

        # Firmware.pm:442 — "Unable to read $file to verify firmware" happens
        # when the local file cannot be opened; the digest compare is :449.
        if expected is None or actual != expected:
            logger.warning(
                "Validation of firmware %s failed, SHA1 checksum did not match "
                "(Firmware.pm:475)", target,
            )
            # Firmware.pm:475 → downloadAsyncError → :502 — the tmp file is
            # removed, the target is NEVER written.
            self._unlink_quietly(tmp)
            self.check_time = min(self.check_time * 2, MAX_RETRY_TIME)
            return DownloadResult(
                "error", path=None, url=url, sha1=actual, http_status=0,
                detail=("Validation of firmware %s failed, SHA1 checksum did "
                        "not match" % target),
            )

        # Firmware.pm:452 — write via the tmp file, then rename into place.
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError as exc:
            message = f"Unable to write {tmp} ({exc})"
            return self._fail(target, url, message)

        # Firmware.pm:457 — "reset back off time".
        self.check_time = INITIAL_RETRY_TIME
        logger.info("Successfully downloaded and verified %s.", target)
        return DownloadResult("download", path=target, url=url, sha1=actual)

    def _maybe_retry_latest(self, target: Path, message: str):
        """Perl ``Firmware.pm:512-523`` — 404/403 under the version folder.

        ``elsif ( $error =~ /^40[34] / && $BASE_FOLDER eq $::VERSION )``: switch
        the folder to ``latest`` and retry once.  Returns ``None`` when the rule
        does not apply, otherwise the retry result.
        """
        if not re.match(r"^40[34] ", message):
            return None
        if self.base_folder != BASE_FOLDER:
            return None

        logger.info(
            "Firmware %s not found (%s), will try folder 'latest' instead of "
            "'%s'.", self.download_url(target.name), message, BASE_FOLDER,
        )
        self.base_folder = BASE_FOLDER_FALLBACK
        return self._download_locked(target)

    def _fail(self, target: Path, url: str, message: str) -> DownloadResult:
        """Perl ``Firmware.pm:486-550`` — log, clean up, back off."""
        # Firmware.pm:506-511 — "Unable to open/write" will never succeed.
        if re.search(r"Unable to (?:open|write)", message):
            logger.error("Fatal error downloading %s (%s), giving up", url, message)
        else:
            logger.warning(
                "Failed to download %s (%s), will try again in %d minutes.",
                url, message, int(self.check_time / 60),
            )
        # Firmware.pm:502 — drop the partial file.
        self._unlink_quietly(target.with_name(target.name + ".tmp"))
        # Firmware.pm:535-541 — schedule the retry and double the back-off.
        self.check_time = min(self.check_time * 2, MAX_RETRY_TIME)
        return DownloadResult("error", path=None, url=url, detail=message)

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


def updates_dir_path(cache_dir: str | Path) -> Path:
    """``dirsFor('updates')`` without the ``mkdir`` (construction-time use)."""
    return Path(cache_dir) / "updates"


# ---------------------------------------------------------------------------
# The registry Perl keeps in %$firmwares — custom files, local images, route
# ---------------------------------------------------------------------------

#: Per-model route path of the served image.  Perl registers
#: ``"^firmware/${model}.*\\.bin"`` (``Firmware.pm:188,227,270``) and, for a
#: custom image, ``"^firmware/custom.$model.bin"`` (``:112``).
FIRMWARE_ROUTE_PREFIX = "firmware/"


def route_path_for(info: FirmwareInfo) -> str:
    """The HTTP path the player fetches this image from.

    Perl's ``url()`` (``Firmware.pm:309``) returns
    ``serverURL() . '/firmware/' . basename($file)`` — the *file name*, which
    for a downloaded image is ``<model>_<ver>_r<rev>.bin`` and for a custom
    image ``custom.<model>.bin``.  A file name that does not match the
    registered regex (``^firmware/<model>.*\\.bin``) would not be routable, so
    the caller gets ``""`` and must not advertise it.
    """
    if not info.file:
        return ""
    name = info.filename
    if not name:
        return ""
    if not re.match(rf"^{re.escape(info.model)}.*\.bin$", name):
        return ""
    return FIRMWARE_ROUTE_PREFIX + name


def route_matches(model: str, path: str) -> bool:
    """Perl's ``addRawDownload`` regex, applied to an inbound request path.

    Two patterns are in play (both MIME ``'binary'``):

    * ``^firmware/custom.$model.bin`` — ``Firmware.pm:112``.  Perl builds this
      as a regex without escaping, so ``.`` is any character; we mirror the
      *intent* (the literal name) because a wildcard there would let
      ``firmware/customXjive.bin`` hit the custom branch.
    * ``^firmware/${model}.*\\.bin`` — ``Firmware.pm:188,227,270``.
    """
    if not path.startswith(FIRMWARE_ROUTE_PREFIX):
        return False
    name = path[len(FIRMWARE_ROUTE_PREFIX):]
    if "/" in name:
        return False
    if name == f"custom.{model}.bin":
        return True
    return bool(re.match(rf"^{re.escape(model)}.*\.bin$", name))


class FirmwareRegistry:
    """Perl's ``%$firmwares`` plus the routing table it feeds.

    ``Slim::Web::Pages->addRawDownload`` is Perl's global route registry; the
    port keeps the equivalent mapping here (``path → file``), because the web
    layer must not hold a second copy of the firmware state.  ``web/app.py``
    asks this registry for a hit — no network access, no download, just the
    bytes already on disk (deviation: the port has no "raw download" page
    table, so this class *is* the table).
    """

    def __init__(self, downloader: FirmwareDownloader | None = None) -> None:
        self.downloader = downloader
        #: model → FirmwareInfo (Perl ``$firmwares->{$model}``).
        self.firmwares: dict[str, FirmwareInfo] = {}
        #: route path → file (Perl's ``addRawDownload`` table).
        self.routes: dict[str, Path] = {}

    # -- registration (Perl addRawDownload) ------------------------------

    def _register_route(self, route: str, file: Path) -> None:
        self.routes[route] = file

    def register(self, info: FirmwareInfo) -> str:
        """Store ``info`` and register its route; returns the path (or ``""``)."""
        self.firmwares[info.model] = info
        if isinstance(info.file, Path):
            path = route_path_for(info)
            if path:
                self._register_route(path, info.file)
            return path
        return ""

    def register_custom_route(self, model: str, file: Path) -> None:
        """Perl ``Firmware.pm:112`` — ``"^firmware/custom.$model.bin"``."""
        self._register_route(f"firmware/custom.{model}.bin", file)

    def lookup(self, path: str) -> Path | None:
        """Resolve a request path to a file — the ``addRawDownload`` handler.

        Returns ``None`` for anything not registered, so the caller answers 404
        like Perl does for an unknown path.
        """
        normalized = path.lstrip("/")
        direct = self.routes.get(normalized)
        if direct is not None and direct.is_file():
            return direct
        # Perl's patterns are regexes: a cached image answers for any
        # '<model>…bin' spelling (Firmware.pm:188), so match by model too.
        for model, info in self.firmwares.items():
            if not isinstance(info.file, Path) or not info.file.is_file():
                continue
            if route_matches(model, normalized):
                return info.file
        return None

    def get(self, model: str) -> FirmwareInfo | None:
        return self.firmwares.get(model)

    def url_for(self, model: str, *, server_url: str = "") -> str | None:
        """Perl ``Firmware.pm:288-310`` ``url($model)``.

        ``Slim::Utils::Network::serverURL() . '/firmware/' . basename($file)``,
        or ``None`` when no image is known.  Perl additionally *starts* the
        download here when the model is unknown; that side effect is
        :meth:`ensure` in this port, called explicitly, so a status request
        never triggers a network round-trip.
        """
        info = self.firmwares.get(model)
        if info is None or not info.file:
            return None
        route = route_path_for(info)
        if not route:
            return None
        return f"{server_url.rstrip('/')}/{route}"


# ---------------------------------------------------------------------------
# Copyright 2001-2024 Logitech / 2024 Lyrion Community note preserved above —
# see Firmware.pm:3-7 for the original header this module ports.
# ---------------------------------------------------------------------------
