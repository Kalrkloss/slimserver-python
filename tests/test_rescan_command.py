"""``rescan`` wiring: the command reaches the importer (Perl rescanCommand).

Regression (2026-09-18): ``rescan`` answered its Perl echo on both ports while
the library never changed — the importer call was there, but every failure
inside it was swallowed (``logger.warning("Rescan failed: %s", exc)`` in
``cli_commands.cmd_rescan`` and a fire-and-forget task in
``JSONRPCAPI._rescan``), so a failed scan looked exactly like "nothing
happened".  These tests pin the wiring itself with a fake importer — no scan
of a real library, no DB write:

* ``rescan``  → the importer runs (Perl ``rescanCommand``,
  ``Slim/Control/Commands.pm:2679-2830``);
* ``rescan ?`` → ``_rescan`` 1/0 (Perl ``rescanQuery``,
  ``Slim/Control/Queries.pm:3214-3229``; Dispatch ``Request.pm:606``);
* a second ``rescan`` while a scan runs is *queued*, not run in parallel
  (``Slim/Control/Commands.pm:2712-2720`` ``queueScanTask``/``nextScanTask``,
  ``Slim/Music/Import.pm:829-860``);
* ``rescanprogress`` reports the scan and its percentage
  (``Queries.pm:3231-3285``) and a leftover failure as ``lastscanfailed``
  (:3291-3296, ``_scanFailed`` :6247-6258).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyrion.control import cli_commands, rescan
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.media.scan_state import SCAN_STATE


@pytest.fixture(autouse=True)
def _clean_scan_state():
    """Each test gets idle scan state and an empty scan queue."""
    rescan.reset()
    SCAN_STATE.reset()
    yield
    rescan.reset()
    SCAN_STATE.reset()


class FakeImporter:
    """Stand-in for ``MusicImporter`` — records calls, never touches a disk."""

    instances: list = []
    finished = 0
    hold: asyncio.Event | None = None
    fail_with: BaseException | None = None

    def __init__(self, config) -> None:
        self.config = config
        FakeImporter.instances.append(config)

    async def import_music(self):
        if FakeImporter.hold is not None:
            await FakeImporter.hold.wait()
        if FakeImporter.fail_with is not None:
            raise FakeImporter.fail_with
        FakeImporter.finished += 1
        return SimpleNamespace(imported_files=1, updated_files=0,
                               skipped_files=0, error_files=0,
                               deleted_files=0)


@pytest.fixture
def fake_importer(monkeypatch):
    FakeImporter.instances = []
    FakeImporter.finished = 0
    FakeImporter.hold = None
    FakeImporter.fail_with = None
    monkeypatch.setattr("lyrion.media.importer.MusicImporter", FakeImporter)
    music = Path("/tmp/lyrion-test-music")
    monkeypatch.setattr("lyrion.media.music_dir.resolve_music_dir",
                        lambda: music)
    yield FakeImporter
    FakeImporter.hold = None
    FakeImporter.fail_with = None


async def _cli(cmd: str, args: list[str]) -> list[str]:
    return await CLIHandler().dispatch(CLIContext(), (cmd, args))


async def _settle(rounds: int = 6) -> None:
    """Let the background scan task and its callbacks run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# rescan <mode> — the command starts the scan
# ---------------------------------------------------------------------------


def test_rescan_starts_the_importer_and_echoes(fake_importer):
    async def body():
        line = await _cli("rescan", [])
        await _settle()
        return line

    assert asyncio.run(body()) == ["rescan  "]
    assert fake_importer.finished == 1, "rescan must reach the importer"
    assert fake_importer.instances[0].mode == "full"
    assert fake_importer.instances[0].source_path == Path("/tmp/lyrion-test-music")


def test_rescan_full_and_once_are_the_full_scan(fake_importer):
    async def body():
        for args in (["full"], ["1"], ["once"]):
            await _cli("rescan", list(args))
            await _settle()

    asyncio.run(body())
    assert [c.mode for c in fake_importer.instances] == ["full", "full", "full"]


def test_rescan_query_reports_the_running_scan(fake_importer):
    held = asyncio.Event()

    async def body():
        fake_importer.hold = held
        assert await _cli("rescan", ["?"]) == ["rescan 0"]
        await _cli("rescan", ["full"])
        await _settle()
        running = await _cli("rescan", ["?"])
        held.set()
        await _settle(20)
        idle = await _cli("rescan", ["?"])
        return running, idle

    running, idle = asyncio.run(body())
    assert running == ["rescan 1"], "a running scan must report _rescan 1"
    assert idle == ["rescan 0"]


def test_second_rescan_while_scanning_is_queued_not_parallel(fake_importer):
    """Perl queues scan tasks (Commands.pm:2712-2720) — no parallel scan."""
    held = asyncio.Event()
    started: list = []

    async def body():
        fake_importer.hold = held
        await _cli("rescan", ["full"])
        await _settle()
        started.append(len(fake_importer.instances))
        # second request while the first scan is still running
        await _cli("rescan", ["full"])
        await _settle()
        queued = len(fake_importer.instances)
        held.set()
        await _settle(30)
        return queued

    queued = asyncio.run(body())
    assert started == [1]
    assert queued == 1, "a running scan must not be joined by a second one"
    assert fake_importer.finished == 2, "the queued scan runs after the first"
    assert not rescan.has_scan_task(), "the queue must be drained"


def test_rescan_album_without_target_is_rejected(fake_importer):
    async def body():
        return await _cli("rescan", ["album"])

    assert asyncio.run(body()) == ["rescan album "]
    assert fake_importer.instances == [], "Perl needs an album/track id"


def test_rescan_singledir_scans_only_that_folder_without_deleting(fake_importer):
    """``rescan _mode _target`` is Perl's ``$singledir`` (Commands.pm:2700-2708).

    A one-folder walk must not reconcile deletions — the importer would drop
    every track it did not see (``ImportConfig.delete_missing``); Perl
    reconciles the scanned folder only.
    """
    async def body():
        line = await _cli("rescan", ["full", "/srv/music/AlbumA"])
        await _settle()
        return line

    assert asyncio.run(body()) == ["rescan full %2Fsrv%2Fmusic%2FAlbumA"]
    cfg = fake_importer.instances[0]
    assert cfg.source_path == Path("/srv/music/AlbumA")
    assert cfg.delete_missing is False, "a partial walk must never delete"


def test_failed_scan_is_logged_with_traceback_and_reported(fake_importer, caplog):
    """A failure must not be silent (Perl logs it and stores a failure row)."""
    fake_importer.fail_with = AttributeError("boom")

    async def body():
        await _cli("rescan", ["full"])
        await _settle(10)
        progress = await _cli("rescanprogress", [])
        return progress

    with caplog.at_level(logging.ERROR, logger="lyrion.control.rescan"):
        progress = asyncio.run(body())

    assert "Library scan failed" in caplog.text
    assert "Traceback" in caplog.text or "boom" in caplog.text
    assert progress == ["rescanprogress rescan%3A0 "
                        "lastscanfailed%3AAttributeError%3A%20boom"]


def test_rescan_without_a_usable_folder_skips_the_scan(fake_importer, monkeypatch, caplog):
    """Perl skips the scan when no media dir is defined (MediaFolderScan.pm:47-52)."""
    monkeypatch.setattr("lyrion.media.music_dir.resolve_music_dir", lambda: None)

    async def body():
        line = await _cli("rescan", ["full"])
        await _settle()
        return line

    with caplog.at_level(logging.ERROR, logger="lyrion.control.rescan"):
        line = asyncio.run(body())

    assert line == ["rescan full "]
    assert fake_importer.instances == [], "never scan a folder nobody configured"
    assert "no folders defined" in caplog.text
    assert rescan.is_scanning() is False


# ---------------------------------------------------------------------------
# rescanprogress — Perl's per-step percentage
# ---------------------------------------------------------------------------


def test_rescanprogress_reports_percent_while_scanning(fake_importer):
    async def body():
        SCAN_STATE.start(total=4)
        SCAN_STATE.update(done=2, total=4)
        return await _cli("rescanprogress", [])

    line = asyncio.run(body())[0]
    assert line.startswith("rescanprogress rescan%3A1 ")
    assert "importer%3A50" in line, "done/total must reach the answer"
    assert "steps%3Aimporter" in line and "totaltime%3A" in line


def test_rescanprogress_is_idle_shape_without_a_failure(fake_importer):
    async def body():
        return await _cli("rescanprogress", [])

    assert asyncio.run(body()) == ["rescanprogress rescan%3A0"]


# ---------------------------------------------------------------------------
# JSON-RPC — same command, same query (Perl: one dispatch table)
# ---------------------------------------------------------------------------


def _jsonrpc(command: list) -> object:
    return asyncio.run(_jsonrpc_async(command))


async def _jsonrpc_async(command: list, api=None) -> object:
    from lyrion.web.api import JSONRPCAPI

    api = api if api is not None else JSONRPCAPI()
    return await api._slim_request("", list(command))


def test_jsonrpc_rescan_query_matches_rescanquery(fake_importer):
    """``_rescan`` is the bare result of rescanQuery — never ``""``."""
    held = asyncio.Event()

    async def body():
        assert await _jsonrpc_async(["rescan", "?"]) == {"_rescan": 0}
        fake_importer.hold = held
        from lyrion.web.api import JSONRPCAPI

        api = JSONRPCAPI()
        await _jsonrpc_async(["rescan", "full"], api)
        await _settle()
        running = await _jsonrpc_async(["rescan", "?"], api)
        held.set()
        await _settle(20)
        return running

    assert asyncio.run(body()) == {"_rescan": 1}


def test_jsonrpc_rescan_command_starts_the_scan(fake_importer):
    async def body():
        out = await _jsonrpc_async(["rescan", "refresh"])
        await _settle()
        return out

    asyncio.run(body())
    assert fake_importer.instances[0].mode == "refresh"


def test_jsonrpc_rescanprogress_uses_the_shared_shape(fake_importer):
    assert _jsonrpc(["rescanprogress"]) == {"rescan": 0}


def test_jsonrpc_rescan_method_queues_like_perl(fake_importer):
    """``{"method":"rescan"}`` must not race a running scan either."""
    from lyrion.web.api import JSONRPCAPI

    held = asyncio.Event()

    async def body():
        api = JSONRPCAPI()
        fake_importer.hold = held
        first = await api._rescan("full")
        await _settle()
        second = await api._rescan("full")
        held.set()
        await _settle(30)
        return first, second

    first, second = asyncio.run(body())
    assert first["status"] == "rescan started"
    assert second["status"] == "rescan queued"
    assert fake_importer.finished == 2
