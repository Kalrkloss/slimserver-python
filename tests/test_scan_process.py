"""The library scan runs in **its own process** (Perl's ``scanner.pl``).

Perl scans outside the server: ``Slim::Music::Import->launchScan``
(``Slim/Music/Import.pm:106-240``) starts ``scanner.pl``
(``Slim/Utils/OS.pm:233-235``) with its own database handle
(``scanner.pl:228-231``), its own process priority (``scanner.pl:213-218`` →
``Slim/Utils/OS.pm:409-421``) and its own log
(``Slim/Music/Import.pm:203-212``).  Progress and the abort flag travel through
shared state (``Slim/Utils/Progress.pm:298-341``; ``Slim/Utils/SQLiteHelper.pm:
404-460``), and the server keeps answering requests while the scan runs.

These tests use the **real** child process (a temp library of tiny but valid
WAV files, a temp library DB, temp serverdata) and measure the thing the user
feels: the round-trip latency of ``status`` / ``rescan ?`` on the control path
while the scan is running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from lyrion.control import rescan
from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.database import sqlite_helper
from lyrion.media import scan_process, scan_state
from lyrion.media.scan_process import spawn_scan_worker as _real_spawn_scan_worker
from lyrion.media.scan_state import SCAN_STATE, reset_published_reap_flag

#: The bar from the task: control requests during a scan stay well below this.
LATENCY_BAR_MS = 100.0


def _write_wav(path: Path) -> None:
    """A real (tiny) WAV file — mutagen parses it, so the scan does real work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)


def _audio_titles(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT title FROM tracks WHERE content_type != 'dir'").fetchall()
    except sqlite3.OperationalError:  # no schema yet → empty library
        return set()
    finally:
        con.close()
    return {r[0] for r in rows}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Temp library + temp serverdata whose scan channels are temp files."""
    music = tmp_path / "music" / "Album"
    for i in range(4):
        _write_wav(music / f"t{i}.wav")

    serverdata = tmp_path / "data"
    (serverdata / "Cache").mkdir(parents=True)
    (serverdata / "Prefs").mkdir(parents=True)
    db_path = serverdata / "Prefs" / "lyrion.db"
    progress = serverdata / "Cache" / "scan-progress.json"
    abort = serverdata / "Cache" / "scan-abort"

    # Both sides must resolve the same files: the parent through these
    # monkeypatched helpers, the child through its --serverdata argument.
    monkeypatch.setattr("lyrion.media.scan_state.scan_channel_paths",
                        lambda: (progress, abort))
    monkeypatch.setattr("lyrion.control.rescan._child_paths",
                        lambda: (db_path, serverdata))
    monkeypatch.setattr("lyrion.control.rescan._source_path",
                        lambda mode, target: music)
    # The conftest isolation stubs the spawner (so no test can accidentally
    # import the developer's library); this file wants the *real* child
    # process, so it restores the function captured at import time.
    monkeypatch.setattr("lyrion.media.scan_process.spawn_scan_worker",
                        _real_spawn_scan_worker)
    monkeypatch.setenv("LYRION_SERVERDATA", str(serverdata))

    rescan.reset()
    SCAN_STATE.reset()
    scan_state.clear_channels()
    yield SimpleNamespace(music=music, serverdata=serverdata, db_path=db_path,
                          progress=progress, abort=abort)
    rescan.reset()
    SCAN_STATE.reset()
    scan_state.clear_channels()


async def _cli(cmd: str, args: list[str]) -> list[str]:
    return await CLIHandler().dispatch(CLIContext(), (cmd, args))


# ---------------------------------------------------------------------------
# the child process itself
# ---------------------------------------------------------------------------


def test_scan_worker_is_a_separate_process_with_its_own_db(rig):
    """``scanner.pl`` parity: own pid, own DB handle, own log, own priority."""
    proc = scan_process.spawn_scan_worker(
        rig.music, "full", db_path=rig.db_path, serverdata=rig.serverdata,
        priority=10, parent_pid=os.getpid())
    try:
        assert proc.pid != os.getpid(), "the scan must not run in the server"
        assert proc.wait(timeout=180) == 0
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()

    # 1. it wrote into the *same* library DB, from its own process
    assert _audio_titles(rig.db_path) == {"t0", "t1", "t2", "t3"}

    # 2. it published its progress for the server (Perl: Progress rows + JSON)
    published = json.loads(rig.progress.read_text())
    assert published["pid"] == proc.pid
    assert published["scanning"] is False
    assert published["progress"] == 100
    assert published["done_files"] >= 4

    # 3. its own log file (Perl: Slim/Music/Import.pm:203-212), with the
    #    priority it applied (scanner.pl:213-218)
    log = (rig.serverdata / "Cache" / "scanner.log").read_text()
    assert "Starting scan worker" in log
    assert "nice=10" in log, f"priority must be applied in the child: {log[:400]}"

    # 4. WAL on that DB file (Perl on_connect_do,
    #    Slim/Utils/SQLiteHelper.pm:98-111)
    con = sqlite3.connect(rig.db_path)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    assert mode.lower() == "wal"


def test_worker_refuses_to_start_when_a_scan_is_running(rig, caplog):
    """``scanner.pl:233-239``: "There appears to be an existing scanner running"."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        rig.progress.write_text(json.dumps(
            {"scanning": True, "progress": 1, "pid": live.pid}))
        code = scan_process.spawn_scan_worker(
            rig.music, "full", db_path=rig.db_path,
            serverdata=rig.serverdata).wait(timeout=120)
        assert code == 0  # Perl exits 0 after the message (scanner.pl:238)
        assert _audio_titles(rig.db_path) == set(), "no second concurrent scan"
        # the child's own log carries the refusal (Perl: scanner.log)
        log = (rig.serverdata / "Cache" / "scanner.log").read_text()
        assert "existing scanner" in log
    finally:
        live.kill()
        live.wait()


def test_abort_marker_makes_the_worker_stop_before_importing(rig):
    """The server's abort flag reaches the scan process (Perl ``$ABORT``)."""
    rig.abort.write_text("abort\n")  # what rescan.request_abort() writes
    code = scan_process.spawn_scan_worker(
        rig.music, "full", db_path=rig.db_path,
        serverdata=rig.serverdata).wait(timeout=180)
    assert code == 0
    assert _audio_titles(rig.db_path) == set(), "an aborted scan imports nothing"


# ---------------------------------------------------------------------------
# progress / status while the scan process runs
# ---------------------------------------------------------------------------


def test_rescan_query_and_progress_come_from_the_scan_process(rig, monkeypatch):
    """``rescan ?`` is 1 and progress is the child's, then both go back to 0."""
    music = rig.music
    for i in range(300):  # long enough to be observed while running
        _write_wav(music / f"t{i:04d}.wav")

    async def body():
        assert rescan.request_scan("full") == "started"
        proc = rescan._PROC
        assert proc is not None and proc.pid != os.getpid()
        proc_pid = proc.pid

        seen_running = False
        published_pids: list[int] = []
        progress_seen: list[str] = []
        latencies: list[float] = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            t0 = time.perf_counter()
            rescan_line = (await _cli("rescan", ["?"]))[0]
            latency = time.perf_counter() - t0
            # 3 control-path round trips per round, all while the scan writes
            t0 = time.perf_counter()
            await _cli("status", ["0", "20"])
            latency = max(latency, time.perf_counter() - t0)
            t0 = time.perf_counter()
            progress_seen.append((await _cli("rescanprogress", []))[0])
            latency = max(latency, time.perf_counter() - t0)
            latencies.append(latency)
            if rescan_line.endswith(" 1"):
                seen_running = True
                try:  # the child's own publication (Perl: Progress rows)
                    published = json.loads(rig.progress.read_text())
                except (OSError, ValueError):
                    published = {}
                if published.get("scanning"):
                    published_pids.append(int(published.get("pid") or 0))
            else:
                break
            await asyncio.sleep(0.02)

        # wait for the watcher to reap the process
        while rescan.is_scanning() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        idle_line = (await _cli("rescan", ["?"]))[0]
        return (seen_running, latencies, progress_seen, idle_line,
                published_pids, proc_pid)

    (seen_running, latencies, progress_seen, idle_line,
     published_pids, proc_pid) = asyncio.run(body())
    assert seen_running, "rescan ? must report the running scan process"
    assert idle_line.endswith(" 0"), "after the process ends, rescan ? is 0"
    assert any("rescan%3A1" in line for line in progress_seen), \
        "rescanprogress must report the scan while the child runs"
    assert rescan._PROC is None and not rescan.is_scanning(), \
        "the ended process must not leave the rescan flag set"
    assert proc_pid in published_pids, \
        "the scan process must publish its own progress (Perl: Progress rows)"
    titles = _audio_titles(rig.db_path)
    assert {f"t{i:04d}" for i in range(300)} <= titles
    assert {"t0", "t1", "t2", "t3"} <= titles

    worst_ms = max(latencies) * 1000
    assert worst_ms < LATENCY_BAR_MS, (
        f"control requests stalled {worst_ms:.1f} ms (> {LATENCY_BAR_MS} ms) "
        f"while the scan process ran")


def test_status_requests_stay_fast_while_a_scan_process_runs(rig):
    """Three ``status`` round trips during a running scan, each < 100 ms.

    This is the *parallel responsiveness* the user asked for: the scan owns
    another process, so the request path never waits for a scan batch.  The
    measurement is taken while the child is demonstrably running
    (``rescan ?`` = 1) and a slow scan is forced by a sizeable library.
    """
    for i in range(400):
        _write_wav(rig.music / f"s{i:04d}.wav")

    async def body() -> list[float]:
        rescan.request_scan("full")
        proc = rescan._PROC
        assert proc is not None and proc.pid != os.getpid()
        lat: list[float] = []
        deadline = time.monotonic() + 120
        while len(lat) < 3 and time.monotonic() < deadline:
            if rescan.is_scanning():
                t0 = time.perf_counter()
                await _cli("status", ["0", "20"])
                lat.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0)
        while rescan.is_scanning() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return lat

    lat = asyncio.run(body())
    assert len(lat) == 3, "three status requests must have been measured"
    assert max(lat) < LATENCY_BAR_MS, f"status stalled: {lat}"


def test_service_is_not_blocked_by_the_scan_loop(rig, monkeypatch):
    """Logic half of the latency proof: the loop keeps ticking during a scan.

    A watchdog task measures the *event-loop* gaps while the scan process runs.
    With the scan inside the server these gaps are the scan's own work; with the
    scan in its own process they stay tiny (this is the assertable core of the
    measured request latency above).
    """
    for i in range(200):
        _write_wav(rig.music / f"w{i:04d}.wav")

    async def body() -> float:
        gaps: list[float] = []
        stop = False

        async def watchdog() -> None:
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(watchdog())
        rescan.request_scan("full")
        deadline = time.monotonic() + 120
        while rescan.is_scanning() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stop = True
        await task
        return max(gaps) * 1000

    worst_ms = asyncio.run(body())
    assert worst_ms < LATENCY_BAR_MS, f"event loop stalled {worst_ms:.1f} ms during the scan"


# ---------------------------------------------------------------------------
# cross-process state: publication, liveness, pragmas
# ---------------------------------------------------------------------------


def test_live_published_state_is_read_and_stale_state_is_dropped(rig, caplog):
    """Perl ``stillScanning`` parity: a dead scanner's flag never sticks."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        rig.progress.write_text(json.dumps(
            {"scanning": True, "progress": 42, "total_files": 10,
             "done_files": 4, "pid": sleeper.pid}))
        scan_state.set_channels(rig.progress, rig.abort)
        live = SCAN_STATE.snapshot()
        assert live["scanning"] is True and live["progress"] == 42
        assert rescan.progress_results()[0] == ("rescan", 1)
    finally:
        sleeper.kill()
        sleeper.wait()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    rig.progress.write_text(json.dumps({"scanning": True, "pid": dead.pid}))
    scan_state.set_channels(rig.progress, rig.abort)
    reset_published_reap_flag()
    with caplog.at_level(logging.ERROR, logger="lyrion.media.scan_state"):
        assert SCAN_STATE.snapshot()["scanning"] is False
    assert "External scanner exited without completing the scan" in caplog.text


def test_abort_request_writes_the_marker_for_the_scan_process(rig):
    """``rescan.request_abort`` must reach a scan that lives elsewhere."""
    rescan._RUNNING = "full"  # a scan is under way (no loop needed for this)
    try:
        rescan.request_abort()
    finally:
        rescan._RUNNING = None
    assert rig.abort.exists(), "the scan process polls this marker"
    assert SCAN_STATE.abort_requested is True


def test_worker_argv_and_priority_are_perl_shaped(monkeypatch):
    """``scanner.pl`` flags: ``--priority`` (-20..20), ``--prefsdir``, log."""
    argv = scan_process.worker_argv(
        Path("/music"), "full", db_path=Path("/d/lyrion.db"),
        serverdata="/sd", priority=10, parent_pid=4242)
    assert argv[1:3] == ["-m", "lyrion.media.scan_worker"]
    assert argv[argv.index("--source") + 1] == "/music"
    assert argv[argv.index("--mode") + 1] == "full"
    assert argv[argv.index("--priority") + 1] == "10"
    assert argv[argv.index("--parent-pid") + 1] == "4242"
    assert argv[argv.index("--serverdata") + 1] == "/sd"

    # Pref default 0 (Slim/Utils/Prefs.pm:220), clamped to Perl's -20..20
    monkeypatch.setattr("lyrion.config.get_config",
                        lambda: SimpleNamespace(get=lambda *a, **k: 99))
    assert scan_process.scanner_priority() == 20
    monkeypatch.setattr("lyrion.config.get_config",
                        lambda: SimpleNamespace(get=lambda *a, **k: "nope"))
    assert scan_process.scanner_priority() == 0

    # No setpriority on Windows (Perl Win32.pm:617 is a no-op) — the helper
    # must not explode there either way.
    assert isinstance(scan_process._PRIORITY_SUPPORTED, bool)


def test_db_pragmas_for_the_scan_process(monkeypatch):
    """busy_timeout + ``wal_autocheckpoint`` 200/10000 (Perl :104)."""
    assert sqlite_helper.BUSY_TIMEOUT_MS == 30000
    monkeypatch.delenv("LYRION_SCANNER", raising=False)
    assert sqlite_helper.wal_autocheckpoint() == sqlite_helper.DEFAULT_WAL_AUTOCHECKPOINT == 200
    monkeypatch.setenv("LYRION_SCANNER", "1")
    assert sqlite_helper.wal_autocheckpoint() == sqlite_helper.SCANNER_WAL_AUTOCHECKPOINT == 10000


def test_scan_state_publish_roundtrip(tmp_path):
    """The published snapshot is what another process reads (Perl Progress JSON)."""
    progress = tmp_path / "p.json"
    abort = tmp_path / "a"
    scan_state.set_channels(progress, abort)
    try:
        SCAN_STATE.start(total=10)
        SCAN_STATE.update(done=4, total=10)
        published = json.loads(progress.read_text())
        assert published["scanning"] is True
        assert published["done_files"] == 4 and published["progress"] == 40
        assert published["pid"] == os.getpid()
        SCAN_STATE.finish()
        assert json.loads(progress.read_text())["scanning"] is False
    finally:
        scan_state.clear_channels()
