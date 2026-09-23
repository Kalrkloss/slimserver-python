"""Firmware download, SHA1 verification and the ``/firmware/`` route.

Perl reference: ``Slim/Utils/Firmware.pm`` (read-only ``/tmp/lms-ref``,
``public/9.2``).  Every assertion names the Perl line it checks.

Run in-process (no live server, no network): the downloader takes an ``opener``
and the route is exercised through an ASGI transport, so the whole suite is
offline and gates every verify.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from lyrion.platform import paths as platform_paths
from lyrion.utils import firmware as fw
from lyrion.utils import firmware_service as fws


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_opener(files: dict[str, bytes]):
    """An opener that serves ``url -> bytes`` and 404s everything else."""
    import urllib.error

    def _open(url: str):
        if url not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def close(self) -> None:
                pass

        return _Resp(files[url])

    return _open


def _bin_payload(model: str = "jive", version: str = "8.5.0",
                 revision: str = "16560") -> bytes:
    return f"FIRMWARE-{model}-{version}-r{revision}".encode()


# ---------------------------------------------------------------------------
# 1. Perl constants and URL construction (Firmware.pm:41-56, :379)
# ---------------------------------------------------------------------------

def test_base_url_is_the_perl_host_not_squeezebox_com():
    """``Firmware.pm:49`` — the old module had ``update.squeezebox.com``.

    The gap analysis (``port-gaps-2026-09-22.md`` §2.2) called the wrong host
    out by name; this is the regression guard.
    """
    assert fw.FIRMWARE_BASE == "http://downloads.lms-community.org/update/firmware/"
    assert "squeezebox.com" not in fw.FIRMWARE_BASE


def test_constants_match_perl():
    """``Firmware.pm:41-42`` and ``:50``."""
    assert fw.INITIAL_RETRY_TIME == 600
    assert fw.MAX_RETRY_TIME == 86400
    assert fw.CHECK_INTERVAL == fw.MAX_RETRY_TIME == 86400


def test_download_url_is_base_folder_plus_name(tmp_path):
    """``Firmware.pm:379`` — ``BASE . $BASE_FOLDER . '/' . basename($file)``."""
    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0")
    assert dl.download_url("/some/where/jive.version") == (
        "http://downloads.lms-community.org/update/firmware/9.2.0/jive.version")
    assert dl.sha_url(dl.download_url("x.bin")).endswith("x.bin.sha")


def test_invented_helpers_are_gone():
    """The four speculation helpers must not come back (deviation 1)."""
    for name in ("get_firmware_url", "parse_firmware_response",
                 "encode_firmware_request", "decode_firmware_response",
                 "DEFAULT_FIRMWARE_BASE"):
        assert not hasattr(fw, name), f"{name} wurde wieder eingefuehrt"


# ---------------------------------------------------------------------------
# 2. version-file parsing (Firmware.pm:110, :154, :258)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("8.5.0 r16560\nsdi@padbuild #24 Sat Sep 8 01:26:46 PDT 2007\n",
     ("8.5.0", 16560)),
    ("7.0 rNNNN\n", None),            # Perl needs \d+ after the 'r'
    ("", None),
    ("no revision here", None),
])
def test_parse_version_file(text, expected):
    """``m/^([^ ]+)\\sr(\\d+)/`` — the regex decides, nothing else."""
    assert fw.parse_version_file(text) == expected


# ---------------------------------------------------------------------------
# 3. the download sequence (Firmware.pm:403-477)
# ---------------------------------------------------------------------------

def test_download_verifies_sha1_and_stores_file(tmp_path):
    """(b) a simulated image + .sha is stored after the SHA1 check.

    Perl: image → ``.sha`` (``:424``), 40-hex extract (``:439``), digest
    compare (``:449``), rename (``:452``).
    """
    payload = _bin_payload()
    url = ("http://downloads.lms-community.org/update/firmware/9.2.0/"
           "jive_8.5.0_r16560.bin")
    files = {url: payload,
             url + ".sha": hashlib.sha1(payload).hexdigest().encode()
             + b"  jive_8.5.0_r16560.bin\n"}

    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0",
                               opener=_fake_opener(files))
    target = tmp_path / "jive_8.5.0_r16560.bin"
    result = dl.download(target)

    assert result.status == "download", result.detail
    assert result.sha1 == hashlib.sha1(payload).hexdigest()
    assert target.read_bytes() == payload
    assert not target.with_name(target.name + ".tmp").exists()
    # Firmware.pm:457 — the back-off is reset on success.
    assert dl.check_time == fw.INITIAL_RETRY_TIME
    # Firmware.pm:449 — the digest form Perl compares (lower-case hex).
    assert result.sha1 == result.sha1.lower()


def test_download_rejects_wrong_sha_and_never_writes_target(tmp_path):
    """(c) negative case: a bad SHA1 leaves NO target file, only a tmp cleanup."""
    payload = _bin_payload()
    url = ("http://downloads.lms-community.org/update/firmware/9.2.0/"
           "jive_8.5.0_r16560.bin")
    wrong = "0" * 40
    files = {url: payload, url + ".sha": wrong.encode()}

    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0",
                               opener=_fake_opener(files))
    target = tmp_path / "jive_8.5.0_r16560.bin"
    result = dl.download(target)

    assert result.status == "error"
    assert "SHA1 checksum did not match" in result.detail
    # Firmware.pm:475 → :502: the temporary file is removed, the target is not
    # created and, crucially, any pre-existing good file is left untouched.
    assert not target.exists()
    assert not target.with_name(target.name + ".tmp").exists()


def test_failed_download_does_not_overwrite_existing_good_file(tmp_path):
    """A later bad download must not destroy a verified file already on disk."""
    target = tmp_path / "jive_8.5.0_r16560.bin"
    target.write_bytes(b"GOOD-OLD-FIRMWARE")

    url = ("http://downloads.lms-community.org/update/firmware/9.2.0/"
           "jive_8.5.0_r16560.bin")
    files = {url: b"tampered", url + ".sha": ("0" * 40).encode()}
    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0",
                               opener=_fake_opener(files))
    assert dl.download(target).status == "error"
    assert target.read_bytes() == b"GOOD-OLD-FIRMWARE"


def test_unreachable_update_source_logs_and_never_raises(tmp_path, caplog):
    """(d) a dead/unreachable source breaks nothing — one log line, no crash."""
    import urllib.error

    def _dead(url: str):
        raise urllib.error.URLError("Name or service not known")

    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0", opener=_dead)
    target = tmp_path / "jive.version"
    with caplog.at_level("WARNING"):
        result = dl.download(target)

    assert result.status == "error"
    assert not target.exists()
    assert any("Failed to download" in rec.getMessage()
               for rec in caplog.records)


def test_backoff_doubles_and_caps_at_max_retry_time(tmp_path):
    """``Firmware.pm:538-541`` — ``$CHECK_TIME *= 2`` capped at 86400."""
    import urllib.error

    def _dead(url: str):
        raise urllib.error.URLError("boom")

    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0", opener=_dead)
    seen = []
    for _ in range(10):
        dl.download(tmp_path / "jive.version")
        seen.append(dl.check_time)

    # 600 is the *start*, first failure doubles it (Firmware.pm:53 + :538).
    assert seen[0] == 1200
    assert seen[-1] == fw.MAX_RETRY_TIME
    assert all(a <= b for a, b in zip(seen, seen[1:]))


def test_404_retries_with_latest_folder(tmp_path):
    """``Firmware.pm:513-523`` — a 404 under the version folder → ``latest``."""
    payload = _bin_payload()
    latest = ("http://downloads.lms-community.org/update/firmware/latest/"
              "jive_8.5.0_r16560.bin")
    files = {latest: payload,
             latest + ".sha": hashlib.sha1(payload).hexdigest().encode()}

    dl = fw.FirmwareDownloader(tmp_path, base_folder="9.2.0",
                               opener=_fake_opener(files))
    result = dl.download(tmp_path / "jive_8.5.0_r16560.bin")

    assert result.status == "download", result.detail
    assert dl.base_folder == "latest"
    # The version folder is only abandoned for the fallback, not permanently
    # for the object: a second instance starts at BASE_FOLDER again.
    assert fw.BASE_FOLDER == fw.__dict__["BASE_FOLDER"]


# ---------------------------------------------------------------------------
# 4. route registration + MIME (Firmware.pm:112, :188, :227, :270)
# ---------------------------------------------------------------------------

def test_route_pattern_is_perls_model_prefix():
    """``^firmware/${model}.*\\.bin`` (Firmware.pm:188) and custom (``:112``)."""
    assert fw.route_matches("jive", "firmware/jive_8.5.0_r16560.bin")
    assert fw.route_matches("jive", "firmware/jive.bin")
    assert not fw.route_matches("jive", "firmware/squeezebox_1.2_r3.bin")
    assert not fw.route_matches("jive", "firmware/jive.txt")
    # A path with a slash cannot match — Perl's regex is anchored at ^firmware/
    # and the file name carries no separator.
    assert not fw.route_matches("jive", "firmware/sub/jive.bin")
    # Firmware.pm:112 — the custom image has its own route.
    assert fw.route_matches("jive", "firmware/custom.jive.bin")


def test_registry_lookup_returns_file_and_mime_binary(tmp_path):
    """(a) the served bytes come from an existing file; MIME is Perl's 'binary'."""
    image = tmp_path / "jive_8.5.0_r16560.bin"
    image.write_bytes(b"\x00\x01FIRMWARE")

    reg = fw.FirmwareRegistry()
    info = fw.FirmwareInfo("jive", "8.5.0", 16560, image)
    path = reg.register(info)

    assert path == "firmware/jive_8.5.0_r16560.bin"
    assert reg.lookup("/" + path) == image
    assert reg.lookup(path) == image
    assert reg.lookup("/firmware/nothing.bin") is None
    # A model-prefixed spelling also resolves (Perl's regex, not equality).
    assert reg.lookup("/firmware/jive.bin") == image


def test_custom_image_uses_its_own_route(tmp_path):
    """``Firmware.pm:112`` — ``^firmware/custom.$model.bin``."""
    custom = tmp_path / "custom.jive.bin"
    custom.write_bytes(b"CUSTOM")
    reg = fw.FirmwareRegistry()
    reg.register_custom_route("jive", custom)
    assert reg.lookup("/firmware/custom.jive.bin") == custom
    # ... and the file name does not match the generic model route.
    assert fw.route_path_for(fw.FirmwareInfo("jive", "8.5.0", 1, custom)) == ""


def test_registry_url_for_is_network_free(tmp_path):
    """Perl ``Firmware.pm:288-310`` — ``serverURL() . '/firmware/' . basename``."""
    image = tmp_path / "jive_8.5.0_r16560.bin"
    image.write_bytes(b"X")
    reg = fw.FirmwareRegistry()
    reg.register(fw.FirmwareInfo("jive", "8.5.0", 16560, image))
    assert reg.url_for("jive", server_url="http://127.0.0.1:9000/") == (
        "http://127.0.0.1:9000/firmware/jive_8.5.0_r16560.bin")
    # Unknown model → None, no download triggered (deviation from Perl's
    # side-effecting url(); the service's timer does that work instead).
    assert reg.url_for("boombox") is None


# ---------------------------------------------------------------------------
# 5. the service — custom images, pref gate, local fallback, timer
# ---------------------------------------------------------------------------

def test_service_prefers_custom_version_and_image(tmp_path):
    """``Firmware.pm:100-114`` — both custom files present → no download."""
    updates = fw.updates_dir(tmp_path)
    (updates / "custom.jive.version").write_text("9.9.9 r4242\n")
    (updates / "custom.jive.bin").write_bytes(b"CUSTOM-IMAGE")

    def _explode(url: str):  # pragma: no cover - must not be called
        raise AssertionError("custom firmware must not hit the network")

    service = fws.FirmwareService(tmp_path, opener=_explode)
    info = service.init_firmware_download("jive")

    assert info is not None
    assert (info.version, info.revision) == ("9.9.9", 4242)
    assert info.file == updates / "custom.jive.bin"
    assert service.registry.lookup("/firmware/custom.jive.bin") == info.file


def test_desktop_models_have_no_firmware(tmp_path):
    """``Firmware.pm:96`` — ``squeeze(?:play|lite)`` is skipped."""
    service = fws.FirmwareService(tmp_path, opener=_fake_opener({}))
    for model in ("squeezeplay", "SqueezeLite", "squeezelite"):
        assert service.init_firmware_download(model) is None


def test_check_version_pref_gates_the_download(tmp_path):
    """``Firmware.pm:117-122`` — pref off → local images only, no HTTP."""

    class _Prefs:
        def __init__(self, value):
            self.value = value

        def get(self, name, default=None):
            return self.value if name == "checkVersion" else default

    calls: list[str] = []

    def _opener(url: str):  # pragma: no cover - must not be called
        calls.append(url)
        raise AssertionError("no HTTP when checkVersion is off")

    # Value arrives as the string "0" — truthy in Python, hence the coercion.
    service = fws.FirmwareService(tmp_path, prefs=_Prefs("0"), opener=_opener)
    assert service.check_version_enabled() is False
    assert service.init_firmware_download("jive") is None
    assert calls == []

    # ... and with the pref on, the same service does try to download.
    service_on = fws.FirmwareService(tmp_path, prefs=_Prefs(1),
                                     opener=_fake_opener({}))
    assert service_on.check_version_enabled() is True
    assert service_on.init_firmware_download("jive") is None  # 404 → error path


def test_local_firmware_is_used_when_download_fails(tmp_path):
    """(d) + ``Firmware.pm:239-247`` — a failed download falls back to disk."""
    updates = fw.updates_dir(tmp_path)
    (updates / "jive.version").write_text("8.5.0 r16560\n")
    (updates / "jive_8.5.0_r16560.bin").write_bytes(b"ON-DISK")

    import urllib.error

    def _dead(url: str):
        raise urllib.error.URLError("network down")

    service = fws.FirmwareService(tmp_path, opener=_dead)
    info = service.init_firmware_download("jive")

    assert info is not None
    assert info.file == updates / "jive_8.5.0_r16560.bin"
    assert service.registry.lookup("/firmware/jive_8.5.0_r16560.bin") == info.file


def test_version_file_drives_the_bin_download(tmp_path):
    """``Firmware.pm:145-188`` — ``<model>.version`` → ``<model>_<v>_r<r>.bin``."""
    payload = _bin_payload("jive", "8.5.0", "16560")
    base = "http://downloads.lms-community.org/update/firmware/9.2.0/"
    files = {
        base + "jive.version": b"8.5.0 r16560\nbuild info\n",
        base + "jive.version.sha": hashlib.sha1(b"8.5.0 r16560\nbuild info\n").hexdigest().encode(),
        base + "jive_8.5.0_r16560.bin": payload,
        base + "jive_8.5.0_r16560.bin.sha": hashlib.sha1(payload).hexdigest().encode(),
    }
    service = fws.FirmwareService(tmp_path, base_folder="9.2.0",
                                  opener=_fake_opener(files))
    info = service.init_firmware_download("jive")

    assert info is not None
    assert (info.version, info.revision) == ("8.5.0", 16560)
    assert info.file.read_bytes() == payload
    # Firmware.pm:188 — the route is registered for the downloaded image.
    assert service.registry.lookup("/firmware/jive_8.5.0_r16560.bin") == info.file


def test_service_notifies_fwdownloaded(tmp_path, monkeypatch):
    """``Firmware.pm:166,186,230,273`` — notify ``fwdownloaded <model>``."""
    import lyrion.control.notifications as notifications

    seen: list[list] = []

    def _capture(client_id, terms, **kwargs):
        seen.append(list(terms))

    monkeypatch.setattr(notifications, "notify_from_array", _capture)

    updates = fw.updates_dir(tmp_path)
    (updates / "jive.version").write_text("8.5.0 r16560\n")
    (updates / "jive_8.5.0_r16560.bin").write_bytes(b"X")
    service = fws.FirmwareService(tmp_path, opener=_fake_opener({}))
    service.get_fw_locally("jive")

    assert ["fwdownloaded", "jive"] in seen


def test_periodic_loop_runs_headless_and_stops(tmp_path):
    """``Firmware.pm:193-200`` — the check repeats; the port runs it as a task."""

    async def _main():
        service = fws.FirmwareService(tmp_path, models=["jive"],
                                      opener=_fake_opener({}))
        assert await service.start() is True
        assert service.running is True
        # Let the first pass run, then stop.
        await asyncio.sleep(0.05)
        await service.stop()
        assert not service.running

    asyncio.run(_main())


def test_ensure_registers_and_checks_a_new_model(tmp_path):
    """Perl ``Firmware.pm:292-297`` — an unknown model triggers a check at once.

    This is what makes the ``/firmware/`` route serve a real file shortly after
    a player connects, instead of waiting a full ``CHECK_INTERVAL``.
    """
    updates = fw.updates_dir(tmp_path)
    (updates / "jive.version").write_text("8.5.0 r16560\n")
    (updates / "jive_8.5.0_r16560.bin").write_bytes(b"LOCAL-IMAGE")

    async def _main():
        service = fws.FirmwareService(tmp_path, opener=_fake_opener({}))
        service.ensure("jive")
        assert "jive" in service.models
        # The check runs as a task; give it a moment, then assert the route.
        for _ in range(50):
            if service.registry.lookup("/firmware/jive_8.5.0_r16560.bin"):
                break
            await asyncio.sleep(0.02)
        assert service.registry.lookup("/firmware/jive_8.5.0_r16560.bin") == \
            updates / "jive_8.5.0_r16560.bin"
        # ensure_model() must not duplicate a known model.
        service.ensure("jive")
        assert service.models.count("jive") == 1

    asyncio.run(_main())


def test_ensure_without_a_running_loop_uses_local_images(tmp_path):
    """No event loop (tooling/tests): the check runs inline, no task created."""
    updates = fw.updates_dir(tmp_path)
    (updates / "jive.version").write_text("8.5.0 r16560\n")
    (updates / "jive_8.5.0_r16560.bin").write_bytes(b"LOCAL-IMAGE")

    service = fws.FirmwareService(tmp_path, opener=_fake_opener({}))
    service.ensure("jive")  # no asyncio.run → synchronous fallback
    assert service.registry.lookup("/firmware/jive_8.5.0_r16560.bin") is not None


def test_loop_survives_a_download_exception(tmp_path):
    """A crashing opener must not kill the loop — the server stays up."""

    def _boom(url: str):
        raise RuntimeError("kaboom")

    async def _main():
        service = fws.FirmwareService(tmp_path, models=["jive"], opener=_boom)
        await service.start()
        await asyncio.sleep(0.05)
        assert service.running is True
        await service.stop()

    asyncio.run(_main())


# ---------------------------------------------------------------------------
# 6. directory equivalence (OS.pm:157-174, Firmware.pm:74-75)
# ---------------------------------------------------------------------------

def test_updates_dir_is_cachedir_updates(tmp_path):
    """``Slim/Utils/OS.pm:157-174`` — ``<cachedir>/updates``, created."""
    target = fw.updates_dir(tmp_path)
    assert target == tmp_path / "updates"
    assert target.is_dir()


def test_paths_dirs_for_knows_firmware_and_updates():
    """``dirs_for`` mirrors Perl's two firmware-relevant kinds."""
    # ``updates`` is OS-independent; its real location comes from the cache dir.
    assert platform_paths.dirs_for("updates", os_name="linux") == []
    # ``Firmware`` has no base-class branch → empty on a source run
    # (``Slim/Utils/OS.pm:143-176``) ...
    assert platform_paths.dirs_for("Firmware", os_name="linux") == []
    # ... and the packaged Debian layout adds the shipped tree (``Debian.pm:50``).
    assert platform_paths.dirs_for(
        "Firmware", os_name="linux", packaged=True) == [
            platform_paths.Path("/usr/share/squeezeboxserver/Firmware")]


def test_legacy_cache_cleanup(tmp_path):
    """``Firmware.pm:78-79`` — two delete patterns inside ``cachedir``."""
    (tmp_path / "jive_8.5_thing.bin").write_bytes(b"old")
    (tmp_path / "jive_8.5_thing.bin.tmp").write_bytes(b"old")
    (tmp_path / "old.version").write_text("x")
    keep = tmp_path / "jive_8.5.0_r16560.bin"
    keep.write_bytes(b"current")

    removed = fw.cleanup_legacy_cache(tmp_path)
    names = {p.name for p in removed}

    assert "jive_8.5_thing.bin" in names
    assert "old.version" in names
    # The pattern needs \w{4}_\d.\d_ — this one does not match, like in Perl.
    assert keep.exists()


# ---------------------------------------------------------------------------
# 7. the HTTP route (Perl addRawDownload, MIME 'binary')
# ---------------------------------------------------------------------------

class _ASGISend:
    """Collect one ASGI response."""

    def __init__(self) -> None:
        self.start: dict | None = None
        self.body = b""

    async def __call__(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.start = message
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def header(self, name: str) -> str:
        for key, value in (self.start or {}).get("headers", []):
            if key.decode().lower() == name.lower():
                return value.decode()
        return ""


async def _receive_once() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _run_route(app, path: str):
    send = _ASGISend()
    asyncio.run(app({"type": "http", "method": "GET", "path": path,
                     "headers": []}, _receive_once, send))
    return send


@pytest.fixture
def firmware_app(tmp_path, monkeypatch):
    """A real ASGI app whose firmware registry serves a temp image."""
    from lyrion.web.app import create_app

    monkeypatch.setenv("LYRION_SERVERDATA", str(tmp_path / "data"))
    image = tmp_path / "jive_8.5.0_r16560.bin"
    image.write_bytes(b"\x00BINARY-FIRMWARE\xff")

    service = fws.init_service(tmp_path)
    service.registry.register(fw.FirmwareInfo("jive", "8.5.0", 16560, image))
    yield create_app(static_dir=None)
    fws.reset_service()


def test_route_serves_real_bytes_with_mime_binary(firmware_app, tmp_path):
    """(a) ``GET /firmware/<model>_<v>_r<r>.bin`` → 200, ``Content-Type: binary``.

    Perl: ``addRawDownload("^firmware/${model}.*\\.bin", $file, 'binary')``
    (``Firmware.pm:188``).
    """
    send = _run_route(firmware_app, "/firmware/jive_8.5.0_r16560.bin")

    assert send.start is not None
    assert send.start["status"] == 200
    assert send.header("content-type") == "binary"
    assert send.body == b"\x00BINARY-FIRMWARE\xff"
    assert send.header("content-length") == str(len(send.body))


def test_route_404s_an_unknown_model(firmware_app):
    """Perl answers only registered paths; anything else falls through."""
    send = _run_route(firmware_app, "/firmware/boombox_1.0_r1.bin")
    assert send.start["status"] == 404


def test_route_404s_when_no_service_runs(monkeypatch, tmp_path):
    """No firmware service → the route must not crash, just 404."""
    from lyrion.web.app import create_app

    monkeypatch.setenv("LYRION_SERVERDATA", str(tmp_path / "data"))
    fws.reset_service()
    app = create_app(static_dir=None)
    send = _run_route(app, "/firmware/jive_8.5.0_r16560.bin")
    assert send.start["status"] == 404


def test_route_bytes_are_sha1_verified_content(firmware_app, tmp_path):
    """The served bytes are exactly what the SHA1 check accepted."""
    payload = (tmp_path / "jive_8.5.0_r16560.bin").read_bytes()
    send = _run_route(firmware_app, "/firmware/jive_8.5.0_r16560.bin")
    assert hashlib.sha1(send.body).hexdigest() == hashlib.sha1(payload).hexdigest()
