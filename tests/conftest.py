"""Pytest config for the slimserver-python controller-compat suite.

When ``LMS_TEST_SPAWN=1`` is set, a session-scoped fixture boots a fresh
server on the test ports (``test-ports.conf``) into a throwaway data dir
and registers the contract test player via a minimal SlimProto HELO, so
the live-server contract tests exercise real code instead of skipping.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Real-shaped ``tracks`` DDL for tests that need a library DB.  It mirrors
#: ``lyrion.database.schema.Track`` (NOT NULL columns included — the folder
#: layer's INSERT must satisfy them) without pulling SQLAlchemy into the test.
TRACKS_SCHEMA_SQL = """
CREATE TABLE tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titlesort VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    url VARCHAR(1000) NOT NULL UNIQUE,
    content_type VARCHAR(50),
    modtime BIGINT,
    filesize BIGINT,
    duration FLOAT NOT NULL DEFAULT 0,
    playcount INTEGER NOT NULL DEFAULT 0,
    lastplayed DATETIME,
    lastscanned DATETIME,
    year INTEGER,
    remote INTEGER NOT NULL DEFAULT 0,
    audio INTEGER NOT NULL DEFAULT 1,
    video INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    genre VARCHAR(255),
    type VARCHAR(100),
    tracknum INTEGER,
    disc INTEGER,
    compilation INTEGER NOT NULL DEFAULT 0,
    artflow_flag INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.fixture
def tracks_schema_sql() -> str:
    """The ``tracks`` DDL above (fixture so tests can request it by name)."""
    return TRACKS_SCHEMA_SQL


@pytest.fixture
def library_db(tmp_path, tracks_schema_sql) -> str:
    """A writable temp library DB with the port's ``tracks`` schema."""
    db = tmp_path / "lyrion.db"
    con = sqlite3.connect(db)
    con.executescript(tracks_schema_sql)
    con.commit()
    con.close()
    return str(db)


@pytest.fixture(autouse=True)
def _no_library_db_writes(monkeypatch, tmp_path):
    """Point every unstubbed library-DB accessor at an empty throwaway DB.

    Folder browsing creates Perl's ``content_type='dir'`` rows on demand
    (``lyrion.media.dir_rows``), so a test that calls a browse function
    without pointing the library DB at a temp file would otherwise insert
    directory rows into ``~/.lyrion/Lyrion/Prefs/lyrion.db`` — and a test that
    merely *reads* a count would silently measure the developer's real
    library.  The throwaway DB carries the port's ``tracks`` schema and is
    created on first use, so tests that stub nothing see an empty library; a
    test that wants directory rows stubs the accessor itself (its own
    ``monkeypatch.setattr`` runs after this fixture and wins).  An explicitly
    passed ``db_path`` is untouched — that is how the tests that do exercise
    the rows point at their temp DB.
    """
    from lyrion.media import dir_rows, folders

    db = tmp_path / "no-library.db"
    state = {"created": False}

    def _path(explicit: str | None = None) -> str:
        if explicit:
            return str(explicit)
        if not state["created"]:
            con = sqlite3.connect(db)
            con.executescript(TRACKS_SCHEMA_SQL)
            con.commit()
            con.close()
            state["created"] = True
        return str(db)

    monkeypatch.setattr(dir_rows, "config_db_path", lambda: _path())
    monkeypatch.setattr("lyrion.web.api._library_db_path", lambda: _path())
    monkeypatch.setattr(folders, "_rw_connection", lambda db_path=None: None)
    yield

TEST_WEB_PORT = 9002
TEST_CLI_PORT = 9091
TEST_SLIMPROTO_PORT = 3484
TEST_PLAYER = "02:11:22:33:44:55"


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _register_player(mac: str, attempts: int = 20):
    """Register ``mac`` via a minimal SlimProto HELO over TCP 3484.

    Matches the squeezelite wire format parsed in
    ``networking/protocol.py`` (HELO + length + deviceid + revision +
    mac + uuid + wlan + brH + brL + lang + capabilities).

    Returns the still-open socket: the server unregisters a player when
    its last SlimProto connection closes, so the HELO socket must stay
    open for the whole session (mirrors how squeezelite keeps its
    control connection up).
    """
    caps = b"Model=squeezelite,ModelName=TestPlayerSL,CanHTTPS=0"
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    body = (
        struct.pack(">I", 36 + len(caps))
        + bytes([4, 0])          # deviceid=4 (squeezelite), revision=0
        + mac_bytes              # 6 bytes
        + b"\x00" * 16           # uuid
        + struct.pack(">H", 0)   # wlan channellist
        + struct.pack(">I", 0)   # bytes_received_H
        + struct.pack(">I", 0)   # bytes_received_L
        + b"en"                  # lang
        + caps
    )
    frame = b"HELO" + body
    for _ in range(attempts):
        try:
            s = socket.create_connection(("127.0.0.1", TEST_SLIMPROTO_PORT), timeout=2)
            s.sendall(frame)
            time.sleep(0.3)
            return s
        except OSError:
            time.sleep(0.2)
    pytest.fail("konnte Test-Player nicht über SlimProto registrieren (Port 3484 nicht bereit)")


@pytest.fixture(scope="session", autouse=True)
def _spawn_server(tmp_path_factory):
    """Boot a throwaway server on the test ports when LMS_TEST_SPAWN=1."""
    if os.environ.get("LMS_TEST_SPAWN") != "1":
        yield
        return

    if _port_open("127.0.0.1", TEST_WEB_PORT):
        pytest.skip(
            "auf Test-Port 9002 läuft bereits ein Server — für LMS_TEST_SPAWN "
            "zuerst stoppen, damit kein fremder Zustand getestet wird"
        )

    serverdata = tmp_path_factory.mktemp("lyrion-data")
    env = dict(os.environ, LYRION_SERVERDATA=str(serverdata))
    proc = subprocess.Popen(
        [sys.executable, "-m", "lyrion", "--localfile", "test-ports.conf",
         "--loglevel", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the web listener (startup order: CLI → slimproto → web).
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"Serverprozess endete frühzeitig (rc={proc.returncode})")
        if _port_open("127.0.0.1", TEST_WEB_PORT):
            break
        time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("Server wurde nicht rechtzeitig bereit (Web-Port 9002)")

    # Register the contract test player once the SlimProto listener is up.
    # Keep the HELO socket open for the session (closing it would make the
    # server unregister the player).
    player_socket = _register_player(TEST_PLAYER)

    yield

    try:
        player_socket.close()
    except OSError:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(autouse=True)
def _isolate_scan_channels(tmp_path_factory, monkeypatch):
    """Keep every test out of the real scan state and off a real scan process.

    ``lyrion.media.scan_state`` keeps the shared progress/abort file paths in
    module state and ``lyrion.control.rescan`` keeps the running-scan flag and
    starts the scan **process** (``lyrion.media.scan_process``).  Without this
    isolation a test that merely dispatches ``rescan`` would

    * write/delete the running server's progress and abort files in the real
      cache dir, and
    * start a scanner that imports the *developer's* library,

    and a leaked running-scan flag would make the next test's ``rescan ?``
    answer ``1``.  Tests that need a real child override the stub with the
    function captured at import time (``tests/test_scan_process.py``).
    """
    # Outside the test's own tmp_path: a test may use that directory as a music
    # folder and list it (test_musicfolder_primitive_lists_a_real_directory).
    cache = tmp_path_factory.mktemp("scan-channels")
    monkeypatch.setattr(
        "lyrion.media.scan_state.scan_channel_paths",
        lambda: (cache / "scan-progress.json", cache / "scan-abort"))

    from lyrion.control import rescan
    from lyrion.media import scan_process, scan_state
    from lyrion.media.scan_state import SCAN_STATE

    def _stub_spawn(source, mode, **kwargs):  # pragma: no cover - trivial
        return FinishedScanProcess()

    monkeypatch.setattr(scan_process, "spawn_scan_worker", _stub_spawn)

    def _reset() -> None:
        scan_state.clear_channels()
        scan_state.reset_published_reap_flag()
        SCAN_STATE.reset()
        rescan.reset()

    _reset()
    yield
    _reset()


class FinishedScanProcess:
    """A scan process stand-in that is already done (tests, no real child)."""

    def __init__(self, pid: int = 4242, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode: int | None = returncode

    def poll(self):
        return self.returncode


@pytest.fixture(autouse=True)
def _isolate_streaming_controllers():
    """Give every test the controller state of a fresh player.

    Perl keeps a StreamingController per player and drops it with the player
    (``Slim/Player/StreamingController.pm``); the port mirrors that per-player
    state, but tests share one process, so a controller left behind by an
    earlier test answers for the same MAC. Measured: ``test_strm_idempotency``
    only fails when the file runs twice in a row (the later test saw the
    earlier test's ``mode``). Reset on both sides of each test.
    """
    from lyrion.player import streaming

    streaming.reset()
    yield
    streaming.reset()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "contract: live-server controller-compat contract tests "
        "(require an LMS server up on LMS_HTTP/LMS_CLI)",
    )
