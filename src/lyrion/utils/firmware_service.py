"""Periodic firmware check — Perl's ``init_firmware_download`` scheduling.

Why this is a separate module
-----------------------------
``utils/firmware.py`` mirrors ``Slim/Utils/Firmware.pm`` mechanically (URLs,
regexes, the SHA1 sequence, the route table).  Perl also *starts* that work
from a few places: at server start (``Slim/Player/Player.pm`` after HELO), from
``Firmware::url()`` when an unknown model is asked for (``Firmware.pm:292-297``)
and — the part that keeps a missing download self-healing — from a re-scheduled
timer (``Firmware.pm:193-200``).  The port had none of it, so this module owns
the timer and the prefs; the parity mechanics stay in ``firmware.py``.

Perl reference (read-only ``/tmp/lms-ref``, ``public/9.2``)
-----------------------------------------------------------
* ``Slim/Utils/Firmware.pm:16-17``  "downloads firmware in async mode and try to
  download missing firmware **every 10 minutes** in the background".
* ``Slim/Utils/Firmware.pm:50``     ``sub CHECK_INTERVAL { MAX_RETRY_TIME }``
  = 86400 s — the *success* path re-checks after a day, not 10 minutes; the 10
  minutes come from the failure back-off below.
* ``Slim/Utils/Firmware.pm:53``     ``my $CHECK_TIME = INITIAL_RETRY_TIME;``
* ``Slim/Utils/Firmware.pm:535-541`` on failure
  ``setTimer($file, time() + $CHECK_TIME, \\&downloadAsync, $checkArgs)`` and
  ``$CHECK_TIME *= 2`` capped at ``MAX_RETRY_TIME`` — that is where "every 10
  minutes" comes from (600 s, then 1200, 2400 … 86400).
* ``Slim/Utils/Firmware.pm:117-122`` ``if ( !$prefs->get('checkVersion') )`` —
  with the pref off, only local images are used, no download is started.
* ``Slim/Utils/Firmware.pm:193-200`` after a successful pass,
  ``setTimer(undef, time() + CHECK_INTERVAL(), sub { init_firmware_download($model) })``.

Rule this module keeps: **no network access on the playback path.**  The timer
is the only trigger; ``FirmwareRegistry.url_for`` reads the registry and never
starts a download, so a status/menu request cannot block on HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from lyrion.utils import firmware as fw

logger = logging.getLogger(__name__)


class FirmwareService:
    """Owns the ``updates`` directory, the registry and the check timer.

    ``cache_dir`` is the port's cache directory — the equivalent of Perl's
    ``cachedir`` pref, i.e. ``dirsFor('updates')``'s parent
    (``Slim/Utils/OS.pm:157-174``).  ``prefs`` is any object with a
    ``get(name, default)`` method; only ``checkVersion`` is read
    (``Firmware.pm:118``).
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        prefs: Any = None,
        base_url: str = fw.FIRMWARE_BASE,
        base_folder: str = fw.BASE_FOLDER,
        opener: Any = None,
        models: list[str] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.prefs = prefs
        self.base_url = base_url
        self.base_folder = base_folder
        self.opener = opener
        #: Models to keep checking.  Perl learns them as players appear (HELO);
        #: the port accepts an explicit list and grows it via :meth:`ensure`.
        self.models: list[str] = list(models or [])

        self.downloader = fw.FirmwareDownloader(
            fw.updates_dir(self.cache_dir),
            base_url=base_url,
            base_folder=base_folder,
            opener=opener,
        )
        self.registry = fw.FirmwareRegistry(self.downloader)
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        #: Set when a custom image was adopted (``Firmware.pm:103-115``).
        self._custom_models: set[str] = set()

    # -- pref gate -------------------------------------------------------

    def check_version_enabled(self) -> bool:
        """Perl ``Firmware.pm:118`` ``$prefs->get('checkVersion')``.

        Default ``1``: Perl registers ``checkVersion`` with default ``1``
        (``Slim/Web/Settings/Server/Software.pm:23-27`` renders the checkbox),
        so a missing pref means "check".  The value may arrive as the string
        ``"0"``, which is truthy in Python — hence the explicit coercion.
        """
        if self.prefs is None:
            return True
        value = self.prefs.get("checkVersion", 1)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "", "false", "no", "off")
        return bool(value)

    # -- one pass (Perl init_firmware_download) --------------------------

    def init_firmware_download(self, model: str) -> fw.FirmwareInfo | None:
        """Perl ``Firmware.pm:93-135`` verbatim order.

        1. Desktop players have no firmware (``:96``).
        2. ``custom.<model>.version`` + ``custom.<model>.bin`` win when both
           exist (``:103-115``) — parsed and registered, no network.
        3. ``checkVersion`` off → local images only (``:118-122``).
        4. Otherwise download ``<model>.version`` (``:126-134``) and, in
           :meth:`init_version_done`, the ``.bin`` it names.
        """
        if fw._DESKTOP_MODEL_RE.search(model):
            logger.debug("Keine Firmware fuer %s (Firmware.pm:96)", model)
            return None

        custom_version = self.downloader.updates_dir / f"custom.{model}.version"
        custom_image = self.downloader.updates_dir / f"custom.{model}.bin"
        if custom_version.is_file() and custom_image.is_file():
            parse = fw.parse_version_file(
                custom_version.read_text(encoding="latin-1", errors="replace"))
            if parse is None:
                logger.warning("custom %s.version unlesbar: %s", model,
                               custom_version)
            else:
                info = fw.FirmwareInfo(model, parse[0], parse[1], custom_image)
                self._custom_models.add(model)
                self.registry.register_custom_route(model, custom_image)
                self.registry.register(info)
                logger.info("Using custom %s firmware %s %s (Firmware.pm:104)",
                            model, custom_version, custom_image)
                self._notify_fwdownloaded(model)
                return info

        if not self.check_version_enabled():
            logger.info(
                "Not downloading firmware for %s - update check has been "
                "disabled in Settings/Advanced/Software Updates (Firmware.pm:119)",
                model,
            )
            return self.get_fw_locally(model)

        body = self.downloader.download_version_file(model)
        if body is None:
            # Firmware.pm:239-247 — the failure path falls back to a locally
            # present image ("Server will keep trying to download a new one").
            return self.get_fw_locally(model, always=True)

        return self.init_version_done(model, body)

    def init_version_done(
        self, model: str, version_body: bytes | str,
    ) -> fw.FirmwareInfo | None:
        """Perl ``Firmware.pm:145-201`` — parse the version file, fetch the bin.

        The version-file download is asynchronous in Perl and re-schedules the
        next check after ``CHECK_INTERVAL()`` (``:193-200``); the recurring
        timer lives in :meth:`start`, so this method only does the file work.
        """
        if isinstance(version_body, bytes):
            version_body = version_body.decode("latin-1", "replace")
        parse = fw.parse_version_file(version_body)
        if parse is None:
            logger.warning("%s.version unlesbar (Firmware.pm:154): %r",
                           model, (version_body or "")[:80])
            return self.get_fw_locally(model, always=True)

        version, revision = parse
        fw_file = self.downloader.updates_dir / f"{model}_{version}_r{revision}.bin"

        if not fw_file.exists():
            logger.info("Downloading %s firmware to: %s (Firmware.pm:174)",
                        model, fw_file)
            result = self.downloader.download(fw_file)
            if result.status != "download":
                return self.get_fw_locally(model, always=True)

        info = fw.FirmwareInfo(model, version, revision, fw_file)
        self.registry.register(info)
        self._notify_fwdownloaded(model)
        return info

    def get_fw_locally(
        self, model: str, *, always: bool = False,
    ) -> fw.FirmwareInfo | None:
        """Perl ``Firmware.pm:249-279`` ``get_fw_locally($model)``.

        Searches ``updates`` first, then the shipped ``Firmware`` directory
        (``:252``), reads ``<model>.version`` from whichever holds it and
        registers ``<model>_<ver>_r<rev>.bin`` when that exists.

        ``always`` mirrors Perl's unconditional call from ``init_fw_error``
        (``:244``): a re-check must not leave a stale entry behind, so the same
        search runs even when a (failed) attempt already happened.
        """
        search: list[Path] = [self.downloader.updates_dir]
        if self.downloader.firmware_dir is not None:
            search.append(self.downloader.firmware_dir)

        for path in search:
            version_file = path / f"{model}.version"
            if not version_file.is_file():
                continue
            parse = fw.parse_version_file(
                version_file.read_text(encoding="latin-1", errors="replace"))
            if parse is None:
                continue
            version, revision = parse
            fw_file = path / f"{model}_{version}_r{revision}.bin"
            if not fw_file.is_file():
                continue
            logger.info("Using existing firmware for %s: %s (Firmware.pm:263)",
                        model, fw_file)
            info = fw.FirmwareInfo(model, version, revision, fw_file)
            self.registry.register(info)
            self._notify_fwdownloaded(model)
            return info

        if not always:
            logger.debug("Keine lokale Firmware fuer %s", model)
        return None

    # -- notification (Perl Firmware.pm:166,186,230,273) -----------------

    def _notify_fwdownloaded(self, model: str) -> None:
        """Perl ``Slim::Control::Request->new(undef, ['fwdownloaded', $model])
        ->notify('firmwareupgrade')``.

        The notification is what the player layer turns into an upgrade offer
        (``Slim/Player/Squeezebox2.pm:323-388``).  A missing/failed notify
        module must not break the download, exactly like the port's other
        fire-and-forget notifications.
        """
        try:
            from lyrion.control.notifications import notify_from_array

            notify_from_array(None, ["fwdownloaded", model])
        except Exception as exc:  # noqa: BLE001 - notification is best effort
            logger.debug("fwdownloaded-Notify fuer %s nicht gesendet: %s",
                         model, exc)

    # -- periodic check (Perl Firmware.pm:193-200 + :535-541) ------------

    async def start(self) -> bool:
        """Start the background check loop (Perl's re-scheduled timer).

        Returns ``True`` when a new task was started.  Never raises: a firmware
        check must not be able to break server startup, the same rule the
        artwork downloader follows.
        """
        if self._task is not None and not self._task.done():
            return False
        self._stop = asyncio.Event()
        # First pass promptly (Perl starts it at time(), Firmware.pm:128-134),
        # then CHECK_INTERVAL() apart.
        self._task = asyncio.ensure_future(self._loop(first_delay=0))
        logger.info(
            "Firmware-Check gestartet (Intervall %s s, Firmware.pm:50)",
            fw.CHECK_INTERVAL,
        )
        return True

    def ensure(self, model: str, *, check_now: bool = True) -> None:
        """Register ``model`` and run its check right away.

        Perl's ``url()`` calls ``init_firmware_download`` on an unknown model
        (``Firmware.pm:292-297``), so a newly seen player triggers a check at
        once instead of waiting for the next timer tick — that is what makes
        the ``/firmware/`` route serve a real file minutes after a player
        connects.  The work happens on the running loop (never in the caller's
        thread), and no HTTP is done for a model that already has an image.
        """
        if not model:
            return
        if model not in self.models:
            self.models.append(model)
        if not check_now:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (tests/tooling): run the (local-image only) check inline.
            try:
                self.init_firmware_download(model)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Firmware-Check fuer %s inline fehlgeschlagen: %s",
                             model, exc)
            return
        loop.create_task(self._check_one(model))

    async def _check_one(self, model: str) -> None:
        """One model's check, off the caller's stack (Perl's timer callback)."""
        try:
            # ``to_thread``: the downloader uses blocking urllib, and this must
            # never stall the event loop that also serves players.
            await asyncio.to_thread(self.init_firmware_download, model)
        except Exception as exc:  # noqa: BLE001 - a check must never crash
            logger.warning("Firmware-Check fuer %s fehlgeschlagen: %s", model, exc)

    async def stop(self) -> None:
        """Stop the loop (server shutdown / test teardown)."""
        task, self._task = self._task, None
        self._stop.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self, *, first_delay: float | None = None) -> None:
        """``init_firmware_download`` for every known model, then sleep.

        ``CHECK_INTERVAL`` is the *success* interval (``Firmware.pm:50``); the
        downloader's own ``check_time`` grows on failure (``:538-541``), which
        is what Perl's "every 10 minutes" refers to.  The loop waits on the
        smaller of the two so a failure is retried sooner.
        """
        try:
            if first_delay:
                await asyncio.sleep(first_delay)
            while not self._stop.is_set():
                for model in list(self.models):
                    try:
                        self.init_firmware_download(model)
                    except Exception as exc:  # noqa: BLE001 - never die here
                        logger.warning("Firmware-Check fuer %s fehlgeschlagen: %s",
                                       model, exc)
                delay = min(fw.CHECK_INTERVAL, self.downloader.check_time)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, delay))
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise


# ---------------------------------------------------------------------------
# Module-level singleton — the server wires it once at startup
# ---------------------------------------------------------------------------

_service: FirmwareService | None = None


def get_service() -> FirmwareService | None:
    """The running service, or ``None`` before startup (web route uses this)."""
    return _service


def init_service(cache_dir: str | Path, *, prefs: Any = None,
                 opener: Any = None, models: list[str] | None = None) -> FirmwareService:
    """Create (and return) the process-wide service.

    Perl's ``Firmware::init()`` (``Firmware.pm:72-80``) resolves its two
    directories and cleans the legacy cache; the same happens here, and the
    cleanup is logged so an operator sees what was removed.
    """
    global _service
    _service = FirmwareService(cache_dir, prefs=prefs, opener=opener, models=models)
    removed = fw.cleanup_legacy_cache(cache_dir)
    if removed:
        logger.info("Firmware: %d alte Cache-Dateien entfernt (Firmware.pm:78-79)",
                    len(removed))
    return _service


def reset_service() -> None:
    """Drop the singleton (tests)."""
    global _service
    _service = None


async def start_service() -> bool:
    """Start the singleton's check loop; ``False`` when none is configured."""
    service = _service
    if service is None:
        return False
    return await service.start()


async def stop_service() -> None:
    """Stop the singleton's check loop (idempotent)."""
    service = _service
    if service is None:
        return
    await service.stop()


def ensure_model(model: str) -> None:
    """Register ``model`` with the running service (Perl ``Firmware.pm:292-297``)."""
    service = _service
    if service is not None:
        service.ensure(model)


def serve_path(path: str) -> tuple[Path, str] | None:
    """Resolve a ``/firmware/…`` request to ``(file, mime)`` — Perl ``'binary'``.

    ``Firmware.pm:188,227,270`` register the handler with MIME ``'binary'``;
    ``Slim/Web/Pages.pm`` answers with exactly that Content-Type.  Returns
    ``None`` when no service runs or nothing matches (the caller answers 404).
    """
    service = _service
    if service is None:
        return None
    target = service.registry.lookup(path)
    if target is None:
        return None
    return target, "binary"


def version_file_name(model: str) -> str:
    """``<model>.version`` — the name Perl looks for (``Firmware.pm:98``)."""
    return f"{model}.version"


def now() -> float:
    """``time()`` — indirection so tests can reason about the timer."""
    return time.time()
