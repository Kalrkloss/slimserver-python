"""Player-driven firmware upgrade (Perl ``UREQ`` / ``upgradeFirmware``).

Perl sources (read-only pinned tree ``/tmp/lms-ref``, ``public/9.2`` @ d1d0a68;
every assertion carries a file:line):

* ``Slim/Networking/Slimproto.pm:69``    ``'UREQ' => \\&_update_request_handler``
* ``Slim/Networking/Slimproto.pm:887-897`` the handler: logs
  ``Client requests firmware update.``, ``$client->unblock()``, deletes the
  heartbeat (Bug 3881), calls ``$client->upgradeFirmware()``.
* ``Slim/Player/Squeezebox2.pm:323-388``  ``upgradeFirmware``: pick the target
  (``needsUpgrade``), the ``<model>_<ver>.bin`` in Firmware/updates, the
  ``FIRMWARE_MISSING`` branch + ``updn`` callback (:340-360), ``stop()``
  (:364), ``isUpgrading(1)`` (:369), ``notifyFromArray ['firmware_upgrade']``
  (:372), ``upgradeFirmware_SDK5`` (:374).
* ``Slim/Player/Squeezebox.pm:241-341``   ``needsUpgrade`` — the
  ``<model>.version`` range table; ``:395-500`` ``upgradeFirmware_SDK5`` —
  1024-byte ``upda`` blocks (:453) and the final ``updn`` (:488).
* ``Slim/Player/Client.pm:645-647``       base ``needsUpgrade`` returns 0.
* ``Slim/Utils/Firmware.pm:88-114``       ``custom.<model>.version/.bin`` wins.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import lyrion.control.notifications as notifications
from lyrion.networking.protocol import SlimProtoClient
from lyrion.player import firmware as pf
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:EE"
KEY = MAC.replace(":", "").upper()
# ``Model=squeezebox2`` → a real Squeezebox2 (SDK5 upgrade path); the revision
# is the HELO's 8-byte revision field.
CAPS = "Model=squeezebox2,ModelName=Living Room,Firmware=9.0.0-r50,alc,mp3"


def _helo_frame(mac: str, caps: str, revision: int = 50) -> bytes:
    body = (
        bytes([4, 0])
        + bytes.fromhex(mac.replace(":", ""))
        + bytes(16)
        + struct.pack(">H", 0)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"en"
        + caps.encode()
    )
    return b"HELO" + struct.pack(">I", len(body)) + body


def _client_frame(opcode: str, payload: bytes = b"") -> bytes:
    return opcode.encode("ascii") + struct.pack(">I", len(payload)) + payload


async def _frame_from_server(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(2)
    length = int.from_bytes(header, "big")
    return await reader.readexactly(length)


async def _collect_frames(reader: asyncio.StreamReader, limit: int = 40):
    """Every server frame currently queued, up to ``limit`` (non-blocking-ish)."""
    frames = []
    for _ in range(limit):
        try:
            frames.append(await asyncio.wait_for(_frame_from_server(reader),
                                                 timeout=0.4))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            break
    return frames


def _cleanup() -> None:
    PlayerManager().players.clear()
    notifications.reset()


async def _server_with_player(caps: str = CAPS, revision: int = 50):
    client = SlimProtoClient()
    # The manager is a singleton and sends through ``_protocol_handler``; a
    # unit-test server must wire it (the app does this in bootstrap).
    PlayerManager().set_protocol_handler(client)
    server = await asyncio.start_server(client._handle_player, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_helo_frame(MAC, caps, revision))
    await writer.drain()
    await asyncio.sleep(0.25)
    return client, server, reader, writer


# ── needsUpgrade (Squeezebox.pm:241-341) ───────────────────────────────────


def _write_version(tmp_path, model: str, text: str, custom: bool = False):
    name = f"custom.{model}.version" if custom else f"{model}.version"
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_version_table_range_exact_and_default(tmp_path):
    """``N..M T`` / ``N T`` / ``* T`` → the three line shapes (:275-289)."""
    root = tmp_path
    _write_version(root, "squeezebox2", "40..60 137\n70 138\n* 139\n")
    decision = pf.needs_upgrade(50, "squeezebox2", [root])
    assert decision.target == 137
    assert decision.up_to_date is False

    assert pf.needs_upgrade(70, "squeezebox2", [root]).target == 138
    # No rule for 99 → the ``*`` default (:290-295).
    assert pf.needs_upgrade(99, "squeezebox2", [root]).target == 139


def test_up_to_date_revision_returns_zero(tmp_path):
    """``$to == $from`` → ``_needsUpgrade(0)`` + ``$okButCheckFirmware`` (:307-311)."""
    _write_version(tmp_path, "squeezebox2", "130..140 137\n")
    decision = pf.needs_upgrade(137, "squeezebox2", [tmp_path])
    assert decision.target == 0
    assert decision.up_to_date is True


def test_custom_version_file_wins(tmp_path):
    """``custom.<model>.version`` outranks ``<model>.version`` (Firmware.pm:100)."""
    _write_version(tmp_path, "squeezebox2", "40..60 111\n")
    custom = _write_version(tmp_path, "squeezebox2", "40..60 222\n", custom=True)
    paths = pf.resolve_version_paths("squeezebox2", [tmp_path])
    assert paths[0] == custom
    assert pf.needs_upgrade(50, "squeezebox2", [tmp_path]).target == 222


def test_no_version_file_is_no_upgrade(tmp_path):
    """``open`` fails → ``can't open`` + return 0 (Squeezebox.pm:258-261)."""
    decision = pf.needs_upgrade(50, "squeezebox2", [tmp_path])
    assert decision.target == 0
    assert decision.version_file is None


def test_zero_or_missing_revision_is_no_upgrade(tmp_path):
    """``$client->revision || return 0`` (:250)."""
    _write_version(tmp_path, "squeezebox2", "40..60 137\n")
    assert pf.needs_upgrade(0, "squeezebox2", [tmp_path]).target == 0
    assert pf.needs_upgrade("", "squeezebox2", [tmp_path]).target == 0
    assert pf.needs_upgrade("junk", "squeezebox2", [tmp_path]).target == 0


def test_choose_image_prefers_shipped_then_updates(tmp_path):
    """``$file`` (Firmware) wins; else ``$file = $file2`` (updates)
    (Squeezebox2.pm:334-335/:362)."""
    firm = tmp_path / "Firmware"
    updates = tmp_path / "updates"
    firm.mkdir()
    updates.mkdir()
    assert pf.choose_image("squeezebox2", 137, [firm, updates]) is None

    (updates / "squeezebox2_137.bin").write_bytes(b"UP")
    assert pf.choose_image("squeezebox2", 137, [firm, updates]) == \
        updates / "squeezebox2_137.bin"

    (firm / "squeezebox2_137.bin").write_bytes(b"SHIPPED")
    assert pf.choose_image("squeezebox2", 137, [firm, updates]) == \
        firm / "squeezebox2_137.bin"


def test_choose_image_unknown_model_is_none(tmp_path):
    """An unknown model finds no file — no crash (``!-f $file && !-f $file2``)."""
    assert pf.choose_image("nosuchmodel", 999, [tmp_path]) is None


# ── UREQ end-to-end over a real socket (Slimproto.pm:887-897) ──────────────


def test_ureq_without_image_shows_firmware_missing_and_sends_updn(
    tmp_path, caplog,
):
    """UREQ, no image on disk → ``FIRMWARE_MISSING`` + ``updn``.

    Perl: no ``<model>_<to>.bin`` → showBriefly with the two strings, whose
    callback sends ``updn`` (Squeezebox2.pm:340-360).  The manager falls back to
    sending ``updn`` directly when no display layer is bound (headless), which
    is what this test exercises — the player then reconnects.
    """
    async def run():
        client, server, reader, writer = await _server_with_player()
        # A version table that maps 50 → 137; no 137 image exists anywhere.
        (tmp_path / "squeezebox2.version").write_text("40..60 137\n")

        pm = PlayerManager()
        player = pm.get_player(MAC)

        # Point the manager at the tmp searches (no cachedir pref in a test).
        import lyrion.player.manager as mgr_mod
        original = mgr_mod.PlayerManager._firmware_search_dirs
        mgr_mod.PlayerManager._firmware_search_dirs = lambda self: [tmp_path]
        try:
            # Drain the HELO ack/vers/setd frames.
            await _collect_frames(reader)
            with caplog.at_level("INFO"):
                writer.write(_client_frame("UREQ", b""))
                await writer.drain()
                await asyncio.sleep(1.4)
            frames = await _collect_frames(reader)
        finally:
            mgr_mod.PlayerManager._firmware_search_dirs = original

        opcodes = [f[:4] for f in frames]
        assert b"updn" in opcodes, (
            f"missing image must end in an updn frame (Squeezebox2.pm:354); "
            f"got {opcodes}")
        assert b"upda" not in opcodes, "no image → no upda frames"
        assert any("Client requests firmware update." in r.message % r.args
                   if r.args else "Client requests firmware update." in r.message
                   for r in caplog.records), "Perl logs this verbatim (:891)"

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_ureq_with_image_pushes_upda_and_updn(tmp_path, caplog):
    """A real image → ``stop`` + ``is_upgrading`` + notify + ``upda``s + ``updn``."""
    async def run():
        client, server, reader, writer = await _server_with_player()
        (tmp_path / "squeezebox2.version").write_text("40..60 137\n")
        image = tmp_path / "squeezebox2_137.bin"
        image.write_bytes(b"F" * 2500)          # 2500 bytes → 3 blocks of 1024

        pm = PlayerManager()
        player = pm.get_player(MAC)

        import lyrion.player.manager as mgr_mod
        original = mgr_mod.PlayerManager._firmware_search_dirs
        mgr_mod.PlayerManager._firmware_search_dirs = lambda self: [tmp_path]
        try:
            await _collect_frames(reader)
            writer.write(_client_frame("UREQ", b""))
            await writer.drain()
            await asyncio.sleep(1.4)
            frames = await _collect_frames(reader, limit=80)
        finally:
            mgr_mod.PlayerManager._firmware_search_dirs = original

        opcodes = [f[:4] for f in frames]
        # Squeezebox2.pm:364 stop → strm 'q'; :369 isUpgrading(1).
        assert b"strm" in opcodes, f"stop() before the push, got {opcodes}"
        assert player.is_upgrading is True, "isUpgrading(1) (Squeezebox2.pm:369)"
        upda = [f for f in frames if f[:4] == b"upda"]
        assert len(upda) == 3, f"1024-byte blocks → 3 upda frames, got {len(upda)}"
        assert b"".join(f[4:] for f in upda) == b"F" * 2500
        assert opcodes[-1] == b"updn", "the last frame is the upgrade-done (:488)"

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_ureq_fires_firmware_upgrade_notification(tmp_path):
    """``notifyFromArray($client, ['firmware_upgrade'])`` (Squeezebox2.pm:372)."""
    async def run():
        client, server, reader, writer = await _server_with_player()
        (tmp_path / "squeezebox2.version").write_text("40..60 137\n")
        (tmp_path / "squeezebox2_137.bin").write_bytes(b"X")

        pm = PlayerManager()
        player = pm.get_player(MAC)

        seen: list[str] = []
        notifications.subscribe(lambda n: seen.append(n.request_string))
        import lyrion.player.manager as mgr_mod
        original = mgr_mod.PlayerManager._firmware_search_dirs
        mgr_mod.PlayerManager._firmware_search_dirs = lambda self: [tmp_path]
        try:
            await _collect_frames(reader)
            writer.write(_client_frame("UREQ", b""))
            await writer.drain()
            await asyncio.sleep(1.4)
        finally:
            mgr_mod.PlayerManager._firmware_search_dirs = original

        assert "firmware_upgrade" in seen, (
            f"Perl's literal notify verb; got {seen}")

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_ureq_unknown_model_does_not_crash(tmp_path):
    """An unknown model: no version table → no upgrade, no exception."""
    async def run():
        client, server, reader, writer = await _server_with_player(
            caps="Model=weirdbox,ModelName=Weird,Firmware=1.0")
        pm = PlayerManager()
        player = pm.get_player(MAC)

        import lyrion.player.manager as mgr_mod
        original = mgr_mod.PlayerManager._firmware_search_dirs
        mgr_mod.PlayerManager._firmware_search_dirs = lambda self: [tmp_path]
        try:
            await _collect_frames(reader)
            writer.write(_client_frame("UREQ", b""))
            await writer.drain()
            await asyncio.sleep(1.4)
            frames = await _collect_frames(reader)
        finally:
            mgr_mod.PlayerManager._firmware_search_dirs = original

        # No image → the FIRMWARE_MISSING path still ends in an updn.
        assert [f[:4] for f in frames].count(b"updn") == 1
        # The connection stays open (Perl never closes on UREQ).
        assert client._player_writers.get(KEY) is not None

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_bye_chr1_also_requests_the_upgrade(tmp_path):
    """``BYE!`` with ``chr(1)`` calls ``upgradeFirmware`` too (Slimproto.pm:921-937)."""
    async def run():
        client, server, reader, writer = await _server_with_player()
        (tmp_path / "squeezebox2.version").write_text("40..60 137\n")
        (tmp_path / "squeezebox2_137.bin").write_bytes(b"Y" * 10)

        pm = PlayerManager()
        player = pm.get_player(MAC)

        import lyrion.player.manager as mgr_mod
        original = mgr_mod.PlayerManager._firmware_search_dirs
        mgr_mod.PlayerManager._firmware_search_dirs = lambda self: [tmp_path]
        try:
            await _collect_frames(reader)
            writer.write(_client_frame("BYE!", b"\x01"))
            await writer.drain()
            await asyncio.sleep(1.4)
            frames = await _collect_frames(reader)
        finally:
            mgr_mod.PlayerManager._firmware_search_dirs = original

        opcodes = [f[:4] for f in frames]
        assert b"upda" in opcodes and opcodes[-1] == b"updn", opcodes
        assert player.is_upgrading is True

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


# ── Regression: the new opcodes leave ordinary frames alone ────────────────


def test_normal_play_and_status_frames_unaffected(caplog):
    """A plain HELO + STAT still works; UREQ handling must not disturb it."""
    async def run():
        client, server, reader, writer = await _server_with_player()
        await _collect_frames(reader)

        # A STAT frame is still processed (heartbeat refresh, no crash).
        stat = bytes([0x43]) + b"\x00" * 59
        writer.write(_client_frame("STAT", stat))
        await writer.drain()
        await asyncio.sleep(0.2)

        # A non-firmware unknown opcode must not be mistaken for UREQ.
        writer.write(_client_frame("DBUG", b"\x00\x01"))
        await writer.drain()
        await asyncio.sleep(0.2)

        frames = await _collect_frames(reader)
        assert not [f for f in frames if f[:4] in (b"updn", b"upda")], (
            "a plain STAT/DBUG session sends no firmware frames")

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_no_player_for_update_request_is_a_noop():
    """``upgrade_firmware`` for an unknown MAC returns False, no exception."""
    async def run():
        pm = PlayerManager()
        pm._protocol_handler = object()     # non-None, but no such player
        assert await pm.upgrade_firmware("AA:BB:CC:DD:EE:FF") is False

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()


def test_upgrade_firmware_without_handler_returns_false():
    """No protocol handler → nothing to send, no crash."""
    async def run():
        pm = PlayerManager()
        pm._protocol_handler = None
        pm.players[KEY] = PlayerState(mac=MAC, name="x", ip="", port=0)
        assert await pm.upgrade_firmware(MAC) is False

    _cleanup()
    try:
        asyncio.run(run())
    finally:
        _cleanup()
