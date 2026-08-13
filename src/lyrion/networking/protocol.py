"""
Slimproto v8 client — player ↔ server TCP protocol on port 3483.

Frame format (binary, big-endian):
    4-byte header:  [1-byte command ID][3-byte big-endian payload length]
    N-byte payload

Command IDs (hex):
    0x00  HELO   — handshake (player → server)
    0x02  BYE    — disconnect notification
    0x03  STAT   — player status update (player → server)
    0x04  RESP   — response to CLI query (server → player)
    0x05  EVNT   — server → player event / notification
    0x06  QUER   — server → player query
    0x07  BODY   — binary body follows (chunked)
    0x08  STMU   — stream metadata update
    0x09  ANIC   — artwork / now-playing image
    0x0a  GRFB   — global radio feedback (?) — usually not needed
    0x0b  ?      — reserved
    0x0c  ?      — reserved
    0x0d  ?      — reserved
    0x0e  ?      — reserved
    0x0f  ?      — reserved
    0x10  ?      — reserved

Player capabilities bitfield (HELO payload, 4 bytes):
    bit 0  (0x0001)  — can receive PCM
    bit 1  (0x0002)  — can receive FLAC
    bit 2  (0x0004)  — needs 16-bit samples
    bit 3  (0x0008)  — supports DSD over DoP
    bit 4  (0x0010)  — has digital output
    bit 5  (0x0020)  — has IR receiver
    bit 6  (0x0040)  — can receive Ogg Vorbis
    bit 7  (0x0080)  — has cursor keys
    bit 8  (0x0100)  — has keyboard
    bit 9  (0x0200)  — has volume control
    bit 10 (0x0400)  — has infra-red
    bit 11 (0x0800)  — can decode MP3
    bit 12 (0x1000)  — can decode AAC
    bit 13 (0x2000)  — can decode ALAC
    bit 14 (0x4000)  — can decode MP4
    bit 15 (0x8000)  — supports compressed transport

HELO payload (player → server):
    0-7   device-id  (8 bytes ASCII / binary)
    8-15  revision   (8 bytes, firmware rev)
    16    MAC[0]
    17    MAC[1]
    18    MAC[2]
    19    MAC[3]
    20    MAC[4]
    21    MAC[5]
    22-25  capabilities (little-endian u32 bitfield)
    26-29  4-bytes language code (e.g. "EN")
    30-37  8-bytes UUID (unique player ID)
    38-    optional tail (extensions)

HELO response (server → player):
    0     0x00 (OK ack)
    1     num-extensions (u8)
    2-3   buffer-size (u16, in samples)
    4-7   max-output-channels (u32)
    8-11  supported-commands bitfield (u32)
    12-   extension tuples: (code, length, value)
          "J"   — JSON capabilities blob
          "Diac" — digital input adapter caps
          "TUNE" — initial server URL (e.g. http://my.squeeze.center:9000)
          "wrnm" — ?

STAT payload (player → server, sent periodically):
    0-3   crlf_ref (u32)
    4-7   wallclock (u32 seconds)
    8-11  stream buffers (u32)
    12-15  decoded buffers (u32)
    16-19  output buffers (u32)
    20-23  CPU (0..100)
    24-27  dac (0..100)
    28-31  jive (u32)
    32-35  flags
    36-39  server timestamp (u32)
    40-43  elapsed (u32 ms)
    44-47  current output sample rate (u32)
    48-51  current output bit depth (u32)
    52-55  decoder (u32)
    56-59  player IP (u32, network order)
    60-63  wifi mode / strength
    64-67  wifi error rate
    68-71  wifi noise floor
    72-73  power state (u16: 0=on, 1=off)
    74     player type (ascii)
    75-    model name / uuid etc.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
from collections.abc import Coroutine
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Any, Callable

# Install uvloop as the default event loop policy at import time.
try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _notify_cometd_server_status() -> None:
    """Wake Cometd /slim/serverstatus subscribers (player list changed).

    Called after player register/unregister; schedules the fresh
    serverstatus push on the running event loop.
    """
    try:
        from lyrion.web.cometd import get_manager
        mgr = get_manager()
        if mgr is not None:
            asyncio.create_task(mgr.notify_server_status())
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SLIMPROTO_PORT = 3483

# Command IDs
CMD_HELO = 0x00
CMD_BYE = 0x02
CMD_STAT = 0x03
CMD_RESP = 0x04
CMD_EVNT = 0x05
CMD_QUER = 0x06
CMD_BODY = 0x07
CMD_STMU = 0x08
CMD_ANIC = 0x09
CMD_GRFB = 0x0A


# ---------------------------------------------------------------------------
# Enums / flags
# ---------------------------------------------------------------------------
class PlayerCapabilities(IntFlag):
    """HELO capabilities bitfield."""

    PCM = 0x0001
    FLAC = 0x0002
    NEEDS_16BIT = 0x0004
    DSD_DOP = 0x0008
    DIGITAL_OUT = 0x0010
    IR_RECEIVER = 0x0020
    VORBIS = 0x0040
    CURSOR_KEYS = 0x0080
    KEYBOARD = 0x0100
    VOLUME = 0x0200
    INFRA_RED = 0x0400
    MP3_DECODE = 0x0800
    AAC_DECODE = 0x1000
    ALAC_DECODE = 0x2000
    MP4_DECODE = 0x4000
    COMPRESSED_TRANSPORT = 0x8000


# Reasonable default caps: PCM, FLAC, 16-bit, digital-out, volume, IR,
# MP3+AAC+ALAC+MP4 decode, compressed transport
DEFAULT_CAPABILITIES: int = int(
    PlayerCapabilities.PCM
    | PlayerCapabilities.FLAC
    | PlayerCapabilities.NEEDS_16BIT
    | PlayerCapabilities.DIGITAL_OUT
    | PlayerCapabilities.VOLUME
    | PlayerCapabilities.IR_RECEIVER
    | PlayerCapabilities.MP3_DECODE
    | PlayerCapabilities.AAC_DECODE
    | PlayerCapabilities.ALAC_DECODE
    | PlayerCapabilities.MP4_DECODE
    | PlayerCapabilities.COMPRESSED_TRANSPORT
)


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------
def pack_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Pack a slimproto binary frame.

    Header: 1 byte command ID + 3 bytes big-endian payload length.
    """
    length = len(payload)
    if length > 0xFFFFFF:
        raise ValueError(f"Payload too large for slimproto frame: {length}")
    header = bytes([cmd]) + length.to_bytes(3, "big")
    return header + payload


def unpack_header(data: bytes) -> tuple[int, int]:
    """Unpack a 4-byte slimproto header. Returns (command_id, payload_length)."""
    if len(data) < 4:
        raise ValueError(f"Incomplete header: {len(data)} bytes")
    cmd = data[0]
    length = int.from_bytes(data[1:4], "big")
    return cmd, length


# ---------------------------------------------------------------------------
# Dataclasses for protocol messages
# ---------------------------------------------------------------------------
@dataclass
class HelloMessage:
    """HELO handshake message sent from player to server."""

    device_id: str  # 8 bytes ASCII, padded with spaces
    revision: str  # 8 bytes ASCII firmware revision
    mac: tuple[int, int, int, int, int, int]  # 6 MAC bytes
    capabilities: int  # 4-byte little-endian bitfield
    lang: str  # 4-byte language code, space-padded
    uuid: str  # 8-byte unique ID

    def to_bytes(self) -> bytes:
        dev_id = self.device_id.encode("ascii")[:8].ljust(8)
        rev = self.revision.encode("ascii")[:8].ljust(8)
        mac_bytes = bytes(self.mac)
        # Capabilities: 4-byte little-endian
        cap = struct.pack("<I", self.capabilities)
        lang = self.lang.encode("ascii")[:4].ljust(4)
        uuid = self.uuid.encode("ascii")[:8].ljust(8)
        return dev_id + rev + mac_bytes + cap + lang + uuid


@dataclass
class HelloAck:
    """ACK response from server after HELO."""

    num_extensions: int
    buffer_size: int  # samples
    max_output_channels: int
    supported_commands: int  # bitfield u32
    extensions: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def from_bytes(cls, data: bytes) -> HelloAck:
        if len(data) < 1:
            raise ValueError("HELO response too short")
        num_ext = data[0]
        # Layout: byte(1) + ushort(2) + uint(4) + uint(4) = 11 bytes
        # Offsets: 0        1-2            3-6            7-10
        buf_size = struct.unpack_from("<H", data, 1)[0] if len(data) >= 3 else 0
        max_ch = struct.unpack_from("<I", data, 3)[0] if len(data) >= 7 else 0
        supp_cmds = struct.unpack_from("<I", data, 7)[0] if len(data) >= 11 else 0
        extensions: dict[str, bytes] = {}
        offset = 11  # byte(1) + ushort(2) + uint(4) + uint(4) = 11 bytes
        for _ in range(num_ext):
            if offset + 1 >= len(data):  # need code + length = 2 bytes
                break
            code = chr(data[offset]) if data[offset] < 128 else "?"
            length = data[offset + 1]
            offset += 2
            if offset + length > len(data):  # need 'length' more bytes for value
                break
            value = data[offset : offset + length]
            extensions[code] = value
            offset += length
        return cls(num_ext, buf_size, max_ch, supp_cmds, extensions)


@dataclass
class StatMessage:
    """STAT message — player status update sent periodically to server."""

    crlf_ref: int = 0
    wallclock: int = 0
    stream_buffers: int = 0
    decoded_buffers: int = 0
    output_buffers: int = 0
    cpu: int = 0
    dac: int = 0
    jive: int = 0
    flags: int = 0
    server_timestamp: int = 0
    elapsed_ms: int = 0
    sample_rate: int = 44100
    bit_depth: int = 16
    decoder: int = 0
    player_ip: int = 0
    wifi_mode: int = 0
    wifi_error_rate: int = 0
    wifi_noise: int = 0
    power_state: int = 0
    player_type: str = "T"
    model: str = ""

    def to_bytes(self) -> bytes:
        base = struct.pack(
            "<IIIIIIIIIII",
            self.crlf_ref,
            self.wallclock,
            self.stream_buffers,
            self.decoded_buffers,
            self.output_buffers,
            self.cpu,
            self.dac,
            self.jive,
            self.flags,
            self.server_timestamp,
            self.elapsed_ms,
        )
        extra = struct.pack(
            "<IIIII",
            self.sample_rate,
            self.bit_depth,
            self.decoder,
            self.player_ip,
            self.wifi_mode,
        )
        extra2 = struct.pack(
            "<IIH",
            self.wifi_error_rate,
            self.wifi_noise,
            self.power_state,
        )
        tail = self.player_type.encode("ascii")[:1] + self.model.encode("ascii")
        return base + extra + extra2 + tail

    def to_frame(self) -> bytes:
        return pack_frame(CMD_STAT, self.to_bytes())


@dataclass
class EventMessage:
    """EVNT message — server → player event/notification."""

    code: int  # event subtype code
    payload: bytes = b""

    def to_frame(self) -> bytes:
        return pack_frame(CMD_EVNT, bytes([self.code]) + self.payload)


# ---------------------------------------------------------------------------
# SlimProtoClient
# ---------------------------------------------------------------------------


class SlimProtoClient:
    """Async slimproto client — connects a player (or emulated player) to LMS.

    Supports both player-mode (connects to server) and server-mode
    (accepts connections from players).

    Events are dispatched via callback handlers registered per command ID.
    """

    def __init__(
        self,
        device_id: str = "Lyrion  ",
        revision: str = "9.2.0  ",
        mac: tuple[int, int, int, int, int, int] = (0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
        capabilities: int = DEFAULT_CAPABILITIES,
        lang: str = "EN  ",
        uuid: str = "LYR00001",
        web_port: int = 9000,
    ):
        self.device_id = device_id
        self.revision = revision
        self.mac = mac
        self.capabilities = capabilities
        self.lang = lang
        self.uuid = uuid
        # HTTP port of THIS server's web/stream endpoint. The strm frame
        # tells the player where to fetch /stream.mp3 — LMS sends its own
        # httpport here (Squeezebox.pm stream_s: $server_port = $prefs->get('httpport')).
        self.web_port = web_port

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

        # Callback handlers: command_id -> Callable[[bytes], None | Awaitable[None]]
        self._handlers: dict[int, Callable[[bytes], None | Coroutine[Any, Any, None]]] = {}

        # Internal response futures for synchronous-style calls
        self._resp_futures: dict[int, asyncio.Future[bytes]] = {}

        # Server-side: player MAC -> StreamWriter (for sending frames to players)
        self._player_writers: dict[str, asyncio.StreamWriter] = {}

        # Direct-stream RESP waiters: MAC -> Future resolved with icy-metaint
        # when the player reports the source's response headers (RESP frame).
        self._resp_waiters: dict[str, asyncio.Future[int]] = {}

        # Open TCP connections per player MAC. Squeezelite keeps TWO
        # connections (control + data); closing one must NOT mark the
        # player disconnected while the other is alive (that broke
        # stop/play: "Cannot send command to disconnected player").
        self._player_connections: dict[str, int] = {}

        # Server-side fields (populated after HELO)
        self._server_buffer_size: int = 0
        self._server_max_channels: int = 0
        self._server_supported_commands: int = 0
        self._server_extensions: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(
        self,
        host: str,
        port: int = SLIMPROTO_PORT,
        timeout: float = 10.0,
    ) -> None:
        """Connect as a player to an LMS server."""
        logger.info("Connecting to LMS at %s:%d", host, port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        self._reader = reader
        self._writer = writer
        self._connected = True
        logger.info("TCP connection established")

        # Send HELO
        hello = HelloMessage(
            device_id=self.device_id,
            revision=self.revision,
            mac=self.mac,
            capabilities=self.capabilities,
            lang=self.lang,
            uuid=self.uuid,
        )
        await self._send_frame(CMD_HELO, hello.to_bytes())
        logger.info("HELO sent")

        # Read HELO ACK
        ack_data = await self._read_frame()
        ack_cmd, ack_payload = unpack_header(ack_data)
        if ack_cmd != 0:
            raise RuntimeError(f"Expected HELO ACK (0x00), got 0x{ack_cmd:02X}")

        ack = HelloAck.from_bytes(ack_payload)
        self._server_buffer_size = ack.buffer_size
        self._server_max_channels = ack.max_output_channels
        self._server_supported_commands = ack.supported_commands
        self._server_extensions = ack.extensions
        logger.info(
            "HELO ack: buffer=%d max_ch=%d supp_cmds=0x%08X",
            ack.buffer_size,
            ack.max_output_channels,
            ack.supported_commands,
        )

        # Start reader loop
        self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Send BYE and close the connection gracefully."""
        if self._connected:
            try:
                await self._send_frame(CMD_BYE, b"")
            except Exception as exc:
                logger.warning("Error sending BYE: %s", exc)
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), timeout=3.0)
        self._reader = None
        self._writer = None
        logger.info("Disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
  # Framing
    # ------------------------------------------------------------------

    async def _send_frame(self, cmd: int, payload: bytes = b"") -> None:
        """Send a binary frame. Thread-safe via lock."""
        if self._writer is None:
            raise RuntimeError("Not connected")
        frame = pack_frame(cmd, payload)
        async with self._lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def _read_frame(self) -> bytes:
        """Read exactly one binary frame (header + payload)."""
        if self._reader is None:
            raise RuntimeError("Not connected")

        header = await self._reader.readexactly(4)
        cmd, length = unpack_header(header)

        if length == 0:
            return header

        payload = b""
        while len(payload) < length:
            chunk = await self._reader.readexactly(length - len(payload))
            payload += chunk

        return header + payload

    async def _read_loop(self) -> None:
        """Background task: read frames and dispatch to handlers."""
        while self._connected:
            try:
                frame = await self._read_frame()
            except asyncio.CancelledError:
                break
            except ConnectionResetError:
                logger.info("Connection reset by peer")
                break
            except Exception as exc:
                logger.error("Frame read error: %s", exc)
                break

            try:
                cmd, length = unpack_header(frame[0:4])
                payload = frame[4:]
            except Exception as exc:
                logger.error("Malformed frame: %s", exc)
                continue

            # Handle built-in responses first
            if cmd == CMD_RESP:
                # CLI response — resolve pending futures
                future_id = 0  # TODO: use request ID from RESP
                for fid, fut in list(self._resp_futures.items()):
                    if not fut.done():
                        fut.set_result(payload)
                        del self._resp_futures[fid]
                    break
                continue

            # Dispatch to registered handlers
            handler = self._handlers.get(cmd)
            if handler:
                try:
                    result = handler(payload)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Handler for cmd 0x%02X raised: %s", cmd, exc)
            else:
                logger.debug("Unhandled slimproto cmd 0x%02X len=%d", cmd, length)

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on(self, cmd: int, handler: Callable[[bytes], None | Coroutine[Any, Any, None]]) -> None:
        """Register a handler for a command ID."""
        self._handlers[cmd] = handler

    def off(self, cmd: int) -> None:
        """Remove handler for a command ID."""
        self._handlers.pop(cmd, None)

    # ------------------------------------------------------------------
    # CLI commands — send text commands over the binary channel
    # ------------------------------------------------------------------

    async def cli(
        self,
        *args: str,
        timeout: float = 5.0,
    ) -> str:
        """Send a CLI command over the slimproto channel.

        Format: "COMMAND arg1 arg2 ...\n"
        Returns the decoded text response.
        """
        if not self._connected:
            raise RuntimeError("Not connected")

        cmd_line = " ".join(str(a) for a in args) + "\r\n"
        payload = cmd_line.encode("utf-8")
        await self._send_frame(CMD_EVNT, payload)  # EVNT used for CLI in practice

        # Wait for RESP
        fut: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
        # Simple: just read one RESP frame (blocking for this call)
        # We handle this in the read loop instead
        try:
            resp_payload = await asyncio.wait_for(
                self._wait_for_resp(), timeout=timeout
            )
            return resp_payload.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            raise TimeoutError(f"CLI command timed out: {' '.join(args)}")

    async def _wait_for_resp(self) -> bytes:
        """Wait for the next RESP frame."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        fid = id(future)
        self._resp_futures[fid] = future
        try:
            return await future
        finally:
            self._resp_futures.pop(fid, None)

    # ------------------------------------------------------------------
    # Server-side (accepting player connections)
    # ------------------------------------------------------------------

    async def serve(
        self,
        host: str = "0.0.0.0",
        port: int = SLIMPROTO_PORT,
    ) -> None:
        """Server-mode: accept connections from players."""
        server = await asyncio.start_server(
            self._handle_player,
            host,
            port,
            reuse_address=True,
        )
        logger.info("Slimproto server listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    async def _handle_player(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming player connection (server-side)."""
        peer = writer.get_extra_info("peername")
        logger.info("Player connected from %s", peer)
        buf = b""

        # Track the parsed HELO for cleanup
        hello: HelloMessage | None = None
        keepalive_task: asyncio.Task | None = None
        # Set when the player sent 'dsco' (end-of-stream): the player keeps
        # its identity/playlist across the reconnect that follows (LMS
        # behaviour). Only a real disconnect (bye/TCP-close) unregisters.
        keep_registered = False

        try:
            # Read first byte to detect protocol
            first_byte = await reader.readexactly(1)
            
            # ── Squeezelite / SB Player HELO (Ralph Irving fork format) ──
            # Wire format: "HELO" (4B) + length (u32 BE) + deviceid (u8) + revision (u8)
            # + mac (6B) + uuid (16B) + wlan_channellist (u16 BE)
            # + bytes_received_H (u32 BE) + bytes_received_L (u32 BE) + lang (2B)
            # + capabilities text (remaining bytes up to `length`)
            if first_byte[0] == 0x48:  # 'H' = "HELO" opcode
                # Read: "ELO" (3B) + length (4B) + deviceid+revision (2B) + mac (6B)
                # + uuid (16B) + wlan (2B) + brH (4B) + brL (4B) + lang (2B)
                # = 3 + 4 + 2 + 6 + 16 + 2 + 4 + 4 + 2 = 43 bytes of fixed header remaining
                fixed_header = await reader.readexactly(43)
                # Verify "ELO" prefix
                if fixed_header[0:3] != b"ELO":
                    logger.warning("Bad HELO header from %s: expected ELO, got %s", peer, fixed_header[0:3].hex())
                    writer.close()
                    return
                length_be = int.from_bytes(fixed_header[3:7], "big")  # 4-byte big-endian length
                # Parse remaining fixed fields
                offset = 7  # after opcode+length (=4+4=8) minus the 'H' we already read (=1) → 7
                deviceid = fixed_header[offset]; offset += 1
                revision = fixed_header[offset]; offset += 1
                mac_raw = fixed_header[offset:offset + 6]; offset += 6
                uuid_raw = fixed_header[offset:offset + 16]; offset += 16
                wlan = int.from_bytes(fixed_header[offset:offset + 2], "big"); offset += 2
                br_h = int.from_bytes(fixed_header[offset:offset + 4], "big"); offset += 4
                br_l = int.from_bytes(fixed_header[offset:offset + 4], "big"); offset += 4
                lang_raw = fixed_header[offset:offset + 2].decode("ascii", errors="replace"); offset += 2
                # Capabilities = remaining bytes: length - header_size
                header_size = 44  # 4(opcode)+4(length)+1(dev)+1(rev)+6(mac)+16(uuid)+2(wlan)+4(brH)+4(brL)+2(lang)
                cap_bytes_len = length_be - (header_size - 4 - 4)  # length includes opcode+length fields
                # Fix: length in squeezelite includes the whole packet size MINUS 8 (opcode+length)
                # Actually: "length" = sizeof(packet) - 8 (from squeezelite source)
                # So: cap_len = length - (header_size - 8) = length - 36
                cap_bytes_len = length_be - (header_size - 8)
                cap_text = ""
                if cap_bytes_len > 0 and cap_bytes_len < 65536:  # sanity check
                    cap_text = (await reader.readexactly(cap_bytes_len)).decode("ascii", errors="replace")
                mac_str = ":".join(f"{b:02X}" for b in mac_raw)
                # Parse capabilities: Model=<type> is the device type
                # (squeezeplay/squeezelite/...), ModelName=<display> is the
                # client-assigned display name (some clients, e.g. the
                # Taverne SqueezePlay, put their player name there).
                model = "squeezelite"
                display_name = ""
                can_https = False
                for part in cap_text.split(","):
                    part = part.strip()
                    if part.startswith("Model="):
                        model = part[6:]
                    elif part.startswith("ModelName="):
                        display_name = part[10:]
                    elif part == "CanHTTPS=1":
                        # Player can do TLS itself (SqueezeLite/ESP32 builds
                        # with OpenSSL, SqueezePlay). https radio streams may
                        # then be streamed DIRECTLY with the SSL flag; without
                        # this cap they are proxied by the server (Perl LMS:
                        # HTTP.pm canDirectStream + HTTPS.pm slimprotoFlags).
                        can_https = True

                logger.info(
                    "HELO from %s: model=%s display=%s mac=%s len=%d caps=%s",
                    peer, model, display_name, mac_str, length_be, cap_text[:80]
                )

                # ── Server response: binary 'vers' frame (NOT text!) ──
                # Server → player framing (from LMS Slim/Player/Squeezebox.pm sendFrame):
                #   pack('n', len(payload)+4) + 4-byte ASCII opcode + payload
                from lyrion import __version__
                vers_payload = __version__.encode("ascii", errors="replace")
                server_frame = struct.pack(">H", len(vers_payload) + 4) + b"vers" + vers_payload
                writer.write(server_frame)
                await writer.drain()
                logger.info("Sent 'vers' frame to %s (%s)", model, mac_str)

                # ── Ask the player for its name (SETD query, id=0) ──
                # Squeezelite/SqueezeESP32 only send their assigned name in
                # response to a SETD name query. Payload = pack('C', 0) →
                # single id byte (client sees len==5 → replies with
                # SETD(id=0, name\0)). Matches LMS getPlayerSetting:
                #   $data = pack('C', firmwareid=0); sendFrame('setd', \$data)
                try:
                    setd_payload = bytes([0])  # id=0, 1 byte
                    setd_frame = struct.pack(">H", 4 + len(setd_payload)) + b"setd" + setd_payload
                    writer.write(setd_frame)
                    await writer.drain()
                    logger.info("Sent SETD name query to %s", mac_str)
                except Exception as exc:
                    logger.warning("SETD query failed for %s: %s", mac_str, exc)

                # Register writer so play/stop commands can reach this player
                # (key normalized: uppercase, no colons — same as PlayerManager)
                mac_key = mac_str.replace(":", "").upper()
                self._player_writers[mac_key] = writer
                # Track connection count per MAC (Squeezelite uses 2 TCP
                # connections; only the last close unregisters).
                self._player_connections[mac_key] = (
                    self._player_connections.get(mac_key, 0) + 1
                )

                # Start keepalive: Squeezelite declares the connection dead after
                # ~35s without any server message ("No messages from server -
                # connection dead"). Real LMS sends periodic frames. A 'setd'
                # frame with display id > 0 is ignored by squeezelite (no
                # display support) but resets its receive timeout.
                keepalive_task = asyncio.create_task(
                    self._keepalive_loop(writer, mac_str)
                )

                # Track for disconnect
                class TempHello:
                    def __init__(self, m, d, r):
                        self.mac = tuple(int(b, 16) for b in m.split(":"))
                        self.device_id = d
                        self.revision = r
                hello = TempHello(mac_str, model[:8], str(revision)[:8])

                # Register with PlayerManager
                try:
                    from lyrion.player.manager import PlayerManager
                    peer_ip = peer[0] if peer else "unknown"
                    reg_name = display_name or model
                    # A ModelName that merely repeats the device type
                    # (e.g. ModelName=SqueezePlay with Model=squeezeplay) is
                    # device identity, not a real player name — rank it low
                    # so a second connection carrying the real name wins.
                    if display_name and display_name.lower() == model.lower():
                        src = "device"
                    else:
                        src = "display" if display_name else "device"
                    PlayerManager().register_player(
                        mac=mac_str, name=reg_name, ip=peer_ip,
                        port=peer[1] if peer else 0, model=model, firmware="2.0.0",
                        name_source=src, can_https=can_https,
                    )
                    logger.info("Squeezelite player registered: %s (%s) model=%s src=%s", reg_name, mac_str, model, src)
                except Exception as exc:
                    logger.warning("Squeezelite register failed: %s", exc)

                # Wake /slim/serverstatus Cometd subscribers (player list changed)
                _notify_cometd_server_status()

                # ── Sync volume like the real LMS (audg frame) ──
                # Squeezelite zero-initialises its internal gain; until an
                # audg frame arrives all audio is multiplied by 0 → the
                # player decodes but outputs silence. The Perl LMS pushes
                # the current volume to every newly connected player.
                try:
                    from lyrion.player.manager import PlayerManager
                    pstate = PlayerManager().get_player(mac_str)
                    if pstate is not None:
                        await self.send_volume_to_player(mac_str, pstate.volume)
                        logger.info("Sent audg volume=%d to %s on connect", pstate.volume, mac_str)
                except Exception as exc:
                    logger.warning("Volume sync failed for %s: %s", mac_str, exc)

                # ── Read loop: binary slimproto frames from player ──
                # Player → server framing (from LMS Slim/Networking/Slimproto.pm
                # client_readable): 4-byte ASCII opcode + 4-byte BE length + payload.
                # NOTE: this differs from the server → player framing (2-byte length
                # including opcode). The protocol is asymmetric.
                while True:
                    try:
                        header = await reader.readexactly(8)
                    except asyncio.IncompleteReadError:
                        logger.info("Player %s closed connection", mac_str)
                        break
                    opcode_raw = header[0:4]
                    plen = int.from_bytes(header[4:8], "big")
                    if plen > 0xFFFFFF:
                        logger.warning("Oversized frame from %s: op=%r len=%d", peer, opcode_raw, plen)
                        break
                    payload = await reader.readexactly(plen) if plen else b""
                    op = opcode_raw.decode("ascii", errors="replace").lower()
                    if op == "stat":
                        self._handle_stat_frame(mac_str, payload)
                    elif op == "resp":
                        # Direct stream: player forwards the source's HTTP
                        # response headers — extract icy-metaint and send
                        # the 'cont' frame that starts the decoder.
                        self._handle_resp_frame(mac_str, payload)
                    elif op == "setd":
                        # SETD frame — id 0 carries the player's assigned name
                        if payload:
                            setd_id = payload[0]
                            if setd_id == 0 and len(payload) > 1:
                                raw = payload[1:].split(b"\x00")[0]
                                try:
                                    new_name = raw.decode("utf-8").strip()
                                except UnicodeDecodeError:
                                    # SqueezePlay sends the name in latin-1
                                    # (e.g. "Küche" -> b'K\xfcche'); utf-8
                                    # would mangle it to "K�che".
                                    new_name = raw.decode("latin-1").strip()
                                if new_name:
                                    logger.info("Player %s sends name via SETD: %r", mac_str, new_name)
                                    try:
                                        from lyrion.player.manager import PlayerManager
                                        PlayerManager().rename_player(mac_str, new_name)
                                    except Exception as exc:
                                        logger.warning("SETD rename failed for %s: %s", mac_str, exc)
                    elif op in ("bye", "dsco", "quit"):
                        logger.info("Player %s sent '%s' — closing connection", mac_str, op)
                        if op == "dsco":
                            # DSCO = end-of-stream notification: Squeezelite
                            # sends it whenever the current stream disconnects
                            # (e.g. after EOF — for fast local files that is
                            # right after the strm, because the whole file is
                            # buffered instantly) and then IMMEDIATELY opens a
                            # new connection with a fresh HELO. The real LMS
                            # treats DSCO as end-of-stream and KEEPS the
                            # player (playlist, volume, mode survive the
                            # reconnect). Unregistering here would destroy the
                            # playlist before the track even finished playing.
                            keep_registered = True
                        break
                    elif op == "helo":
                        # Player re-sent HELO (reconnect after control drop) — reply again
                        writer.write(server_frame)
                        await writer.drain()
                    else:
                        logger.debug("Frame '%s' from %s (%d bytes)", op, mac_str, plen)
                return
            
            # ── Binary SlimProto HELO ──
            header = first_byte + await reader.readexactly(3)
            cmd, length = unpack_header(header)
            if cmd != CMD_HELO:
                logger.warning("Expected HELO from player, got 0x%02X", cmd)
                return

            payload = await reader.readexactly(length)
            hello = HelloMessage(
                device_id=payload[0:8].decode("ascii", errors="replace").strip(),
                revision=payload[8:16].decode("ascii", errors="replace").strip(),
                mac=tuple(payload[16:22]),
                capabilities=struct.unpack_from("<I", payload, 22)[0],
                lang=payload[26:30].decode("ascii", errors="replace").strip(),
                uuid=payload[30:38].decode("ascii", errors="replace").strip(),
            )
            logger.info(
                "HELO from player: device=%s rev=%s mac=%s uuid=%s caps=0x%08X",
                hello.device_id,
                hello.revision,
                ":".join(f"{b:02X}" for b in hello.mac),
                hello.uuid,
                hello.capabilities,
            )

            # Send HELO ACK — num_ext(1) + buffer_size(2) + max_channels(4)
            # + supported_commands(4) = 11 bytes. The spec-complete layout
            # lets strict players parse the u32 at offset 7 (previously the
            # field was missing entirely).
            supported_commands = 0x00000007  # strm|audg|aude (audio frames)
            ack_payload = struct.pack(
                "<BHII",
                0,  # no extensions for now
                8192,  # buffer size
                2,  # stereo
                supported_commands,
            )
            writer.write(pack_frame(0, ack_payload))
            await writer.drain()

            # Jive controllers (SqueezeControl, iPeng, SqueezePlay) expect
            # the same greeting as players: 'vers' + SETD name query —
            # mirror the text-HELO path below.
            try:
                from lyrion import __version__
                vers_payload = __version__.encode("ascii", errors="replace")
                writer.write(struct.pack(">H", len(vers_payload) + 4) + b"vers" + vers_payload)
                setd_frame = struct.pack(">H", 5) + b"setd" + bytes([0])
                writer.write(setd_frame)
                await writer.drain()
                logger.info("Sent vers + SETD to %s (binary HELO)", hello.device_id)
            except Exception as exc:
                logger.warning("vers/SETD send failed for %s: %s", hello.device_id, exc)

            # Register this player with the PlayerManager
            mac_formatted = ":".join(f"{b:02X}" for b in hello.mac)
            # The binary HELO path must populate the same writer registry as
            # the Squeezelite/ASCII-HELO path. Playback commands use this map.
            mac_key = mac_formatted.replace(":", "").upper()
            self._player_writers[mac_key] = writer
            self._player_connections[mac_key] = (
                self._player_connections.get(mac_key, 0) + 1
            )
            model_name = hello.device_id.strip() or "squeezebox"
            player_ip = peer[0] if peer else "unknown"
            player_port = peer[1] if peer else 0
            try:
                from lyrion.player.manager import PlayerManager
                PlayerManager().register_player(
                    mac=mac_formatted,
                    name=model_name,
                    ip=player_ip,
                    port=player_port,
                    model=model_name,
                    firmware=hello.revision.strip(),
                )
                logger.info("Player registered via SlimProto: %s (%s)", mac_formatted, player_ip)
            except Exception as exc:
                logger.warning("Could not register player %s: %s", mac_formatted, exc)
            # Wake /slim/serverstatus Cometd subscribers (player list changed)
            _notify_cometd_server_status()

            # ── Sync volume like the real LMS (audg frame) ──
            # Same as the text-HELO path above: without an audg frame the
            # player's internal gain stays 0 and everything is silent.
            try:
                pstate = PlayerManager().get_player(mac_formatted)
                if pstate is not None:
                    await self.send_volume_to_player(mac_formatted, pstate.volume)
                    logger.info("Sent audg volume=%d to %s on connect (binary HELO)",
                                pstate.volume, mac_formatted)
            except Exception as exc:
                logger.warning("Volume sync failed for %s: %s", mac_formatted, exc)

            # Read loop for this player
            while True:
                frame = await self._read_single_frame_from_reader(reader)
                if not frame:
                    break
                cmd, length = unpack_header(frame[0:4])
                payload_data = frame[4:]
                handler = self._handlers.get(cmd)
                if handler:
                    try:
                        result = handler(payload_data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.error("Server handler for cmd 0x%02X: %s", cmd, exc)
                else:
                    logger.debug("Server received cmd 0x%02X len=%d", cmd, length)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Player handler error (%s): %s", peer, exc)
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
            # Deregister writer + decrement connection count for this player.
            # Squeezelite keeps TWO connections; only when the LAST one
            # closes (and it was not a DSCO end-of-stream) the player is
            # unregistered. Otherwise stop/play would break mid-stream
            # ("Cannot send command to disconnected player").
            try:
                if hello is not None:
                    mac_clean = ":".join(f"{b:02X}" for b in hello.mac)
                    key = mac_clean.replace(":", "").upper()
                    count = self._player_connections.get(key, 1) - 1
                    if count <= 0:
                        self._player_connections.pop(key, None)
                        if self._player_writers.get(key) is writer:
                            self._player_writers.pop(key, None)
                        from lyrion.player.manager import PlayerManager
                        if keep_registered:
                            # DSCO end-of-stream: the player reconnects
                            # immediately. Mark offline but KEEP the state
                            # (playlist/volume) so the reconnect restores it.
                            p = PlayerManager().get_player(mac_clean)
                            if p is not None:
                                p.connected = False
                        else:
                            PlayerManager().unregister_player(mac_clean)
                            logger.info("Player unregistered: %s", mac_clean)
                            # Wake /slim/serverstatus Cometd subscribers
                            _notify_cometd_server_status()
                    else:
                        self._player_connections[key] = count
            except Exception:
                pass
            writer.close()
            await writer.wait_closed()
            logger.info("Player disconnected: %s", peer)

    async def _read_single_frame_from_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> bytes:
        """Read one frame from a StreamReader."""
        header = await reader.readexactly(4)
        cmd, length = unpack_header(header)
        if length == 0:
            return header
        payload = b""
        while len(payload) < length:
            chunk = await reader.readexactly(length - len(payload))
            payload += chunk
        return header + payload

    async def _keepalive_loop(
        self,
        writer: asyncio.StreamWriter,
        mac_str: str,
    ) -> None:
        """Send a 'setd' keepalive frame every 10s.

        Squeezelite's slimproto_run declares the connection dead after ~35s
        without any message from the server ("No messages from server -
        connection dead") and reconnects. Real LMS sends periodic frames.
        A 'setd' frame with display id > 0 is ignored by squeezelite builds
        without display support, but every received message resets its
        timeout counter — exactly what we need.
        """
        # Frame: pack('n', len+4) + "setd" + id(1) + data(1).
        # NOTE: squeezelite's setd_packet is { opcode[4]; u8 id; data[] } —
        # there is NO 4-byte length field inside the payload (unlike HELO).
        # Sending a length field would shift the id to 0 → player name query.
        keepalive_frame = struct.pack(">H", 4 + 2) + b"setd" + b"\x01\x00"
        try:
            while True:
                await asyncio.sleep(10)
                if writer.is_closing():
                    break
                writer.write(keepalive_frame)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError, RuntimeError):
            pass
        logger.debug("Keepalive loop for %s stopped", mac_str)

    # ------------------------------------------------------------------
    # Streaming control (server -> player)
    # ------------------------------------------------------------------

    @staticmethod
    def _codec_char(mime: str | None) -> str:
        """Map a MIME type to the slimproto codec character."""
        if not mime:
            return "m"
        m = mime.lower()
        if "flac" in m:
            return "f"
        if "ogg" in m or "opus" in m or "vorbis" in m:
            return "o"
        if "aac" in m or "mp4" in m or "m4a" in m:
            return "a"
        if "wav" in m or "pcm" in m or "aiff" in m or "aif" in m:
            return "p"
        return "m"  # mp3 and everything else

    @staticmethod
    def _build_stream_frame(
        *,
        request: bytes,
        codec: str = "m",
        autostart: int = 1,
        server_port: int = 9000,
        server_ip: int = 0,
        flags: int = 0,
        threshold: int = 50,
    ) -> bytes:
        """Build the 24-byte LMS ``strm`` packet plus its HTTP request.

        The important distinction is ``autostart``:

        * 1: normal LMS proxy stream.  Squeezelite starts decoding after
          receiving the HTTP headers; no ``cont`` frame is expected.
        * 3: direct/header-managed stream.  The player waits for a later
          ``cont`` frame after sending RESP to the server.

        The Perl LMS uses ``?`` for unknown PCM fields and output threshold
        1 for MP3.  Numeric zeroes here are not equivalent: they describe
        an 8-bit/0Hz/0-channel stream to some clients.
        """
        if autostart not in (0, 1, 2, 3):
            raise ValueError(f"invalid strm autostart: {autostart}")
        payload = b"".join([
            b"strm",
            b"s",
            str(autostart).encode("ascii"),  # LMS/squeezelite: '0'..'3'
            codec.encode("ascii"),
            b"?",                    # pcm_sample_size: unknown for MP3
            b"?",                    # pcm_sample_rate: unknown for MP3
            b"?",                    # pcm_channels: unknown for MP3
            b"?",                    # pcm_endianness: unknown for MP3
            bytes([max(0, min(255, threshold))]),
            bytes([0]),               # SPDIF auto
            bytes([0]),               # transition period
            b"0",                    # transition type: none
            bytes([flags & 0xFF]),
            bytes([1 if codec == "m" else 0]),  # output threshold
            bytes([0]),               # proxy slaves
            struct.pack(">I", 0),
            struct.pack(">H", server_port),
            struct.pack(">I", server_ip),
            request,
        ])
        return struct.pack(">H", len(payload)) + payload

    async def send_flush_to_player(self, mac: str) -> bool:
        """Send a 'strm' flush command ('f') to a player.

        The Perl LMS sends this before switching streams while the player
        is playing (Squeezebox2.pm flush -> stream('f'), triggered by
        StreamingController::_FlushGetNext). Squeezelite's 'f' handler
        does decode_flush + output_flush + buf_flush(streambuf) — without
        it the player keeps playing out its old buffers, so a radio
        switch takes as long as the old buffer lasts (and the old stream
        keeps being audible).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"".join([
            b"strm", b"f", b"0", b"?", b"0", b"0", b"0", b"l",
            bytes(7),                       # threshold..slaves
            struct.pack(">I", 0),           # replay_gain
            struct.pack(">H", 0),           # server_port
            struct.pack(">I", 0),           # server_ip
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent strm 'f' (flush) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def _flush_if_playing(self, mac: str) -> None:
        """Flush the player's buffers before a stream switch, like the
        Perl LMS does (stream('f') before the new strm when playing)."""
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is not None and player.mode in ("play", "loading"):
                await self.send_flush_to_player(mac)
        except Exception:
            pass

    async def send_strm_to_player(self, mac: str, track_id: int) -> bool:
        """Send a 'strm' (stream) frame to a player so it fetches the track
        over HTTP from this server's /stream.mp3 endpoint.

        Squeezelite opens its own TCP connection to the server (ip from the
        slimproto connection when server_ip=0) and issues the HTTP request
        string we embed in the frame.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        # Load track metadata for codec
        mime = None
        try:
            from sqlalchemy import select
            from lyrion.database.schema import Track
            from lyrion.database.sqlite_helper import db_session
            async with db_session() as session:
                track = (await session.execute(
                    select(Track).where(Track.id == track_id)
                )).scalar_one_or_none()
                if track is not None:
                    mime = track.content_type
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load track %d for codec: %s", track_id, exc)

        codec = self._codec_char(mime)

        # HTTP request string Squeezelite will send to our web server.
        # LMS format (Slim/Player/Squeezebox.pm stream_s): the request is
        # exactly "GET /stream.mp3?player=<MAC> HTTP/1.0\r\n\r\n" — no track
        # id in the URL, no Host header. The /stream endpoint resolves the
        # current track from the player's playlist via the player= param.
        request = (
            f"GET /stream.mp3?player={mac} HTTP/1.0\r\n"
            f"\r\n"
        ).encode("ascii")

        # Normal LMS proxy stream: autostart=1.  The player starts after
        # HTTP headers; it must not wait for a cont frame.
        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=1,
            server_port=self.web_port,
        )
        # Flush old stream buffers first (Perl LMS behaviour) so the
        # switch is immediate instead of playing out the old buffer.
        await self._flush_if_playing(mac)
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent strm to %s: track=%d codec=%s", mac, track_id, codec)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send strm to %s: %s", mac, exc)
            return False

    @staticmethod
    def _pick_playlist_url(body: bytes) -> list[str]:
        """Playable URL lines from an M3U/PLS body (http/https), skipping
        TuneIn/Radiotime ad 'bump' stub URLs — in playlist order."""
        ad_hints = ("bump", "preview", "pre_", "advert",
                    "promo", "teaser", "sample")
        return [
            ln.strip()
            for ln in body.decode("utf-8", errors="replace").splitlines()
            if ln.strip().startswith(("http://", "https://"))
            and not any(h in ln.lower() for h in ad_hints)
        ]

    async def _first_reachable(self, candidates: list[str]) -> str | None:
        """First playlist candidate that answers HEAD < 400, trying an
        https→http fallback per candidate. Stream ports (8000/8060/…)
        are often plain HTTP even when the playlist advertises https —
        e.g. 1Mix: TuneIn lists https://fr2.1mix.co.uk:8060/320h, the
        working stream is http://fr2.1mix.co.uk:8060/320.

        All HEADs run in parallel (asyncio.gather) — with a dead station
        a sequential scan would block the play request for the sum of
        all timeouts (60 s+); parallel it is just one timeout.
        """
        import httpx
        headers = {"User-Agent": "LyrionMusicServer/9.2.0"}
        timeout = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            variants: list[str] = []
            for cand in candidates:
                variants.append(cand)
                if cand.startswith("https://"):
                    variants.append("http://" + cand[len("https://"):])

            async def test(v: str) -> str | None:
                try:
                    r = await client.head(v, headers=headers)
                    if r.status_code < 400:
                        return str(r.url)
                except Exception:
                    pass
                return None

            results = await asyncio.gather(*(test(v) for v in variants))
            for r in results:
                if r:
                    return r
        return None

    # Cache for resolved stream URLs (redirects / playlist expansion).
    # Short TTL so a station's current stream URL can change.
    _url_resolve_cache: dict[str, tuple[float, str]] = {}
    _URL_RESOLVE_TTL = 600.0  # 10 min

    async def _resolve_stream_url(self, url: str) -> str:
        """Resolve a radio URL the way the Perl LMS does before sending a
        direct stream: follow HTTP redirects and expand M3U/PLS playlists
        to the first playable audio URL. Squeezelite can do neither —
        without this, redirect URLs (SWR3) or playlist URLs (1Mix/TuneIn)
        produce silence on direct streams.
        """
        import time as _time

        now = _time.time()
        cached = self._url_resolve_cache.get(url)
        if cached and now - cached[0] < self._URL_RESOLVE_TTL:
            return cached[1]

        import httpx

        result = url
        try:
            headers = {"User-Agent": "LyrionMusicServer/9.2.0"}
            timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
            async with httpx.AsyncClient(timeout=timeout,
                                         follow_redirects=True) as client:
                # HEAD first: follows redirects, cheap, no audio body.
                resp = await client.head(url, headers=headers)
                final = str(resp.url)
                ctype = resp.headers.get("content-type", "").lower()
                is_playlist = ("playlist" in ctype or "mpegurl" in ctype
                               or final.lower().endswith((".pls", ".m3u", ".m3u8")))
                if is_playlist:
                    # Read the playlist body (bounded), then pick the first
                    # REACHABLE URL (HEAD test, https→http fallback) — the
                    # playlist often lists dead/mis-schemed URLs first
                    # (1Mix: TuneIn lists https://…:8000, working stream
                    # is http://…:8060/…).
                    try:
                        resp = await client.get(url, headers=headers)
                        body = b""
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) > 128 * 1024:
                                break
                        candidates = self._pick_playlist_url(body)
                        if candidates:
                            reachable = await self._first_reachable(candidates)
                            result = reachable or candidates[0]
                    except Exception as exc:
                        logger.warning("Playlist resolve failed for %s: %s",
                                       url[:60], exc)
                elif final != url and resp.status_code < 400:
                    result = final
        except Exception as exc:
            logger.warning("Stream URL resolve failed for %s (%s) — using as-is",
                           url[:60], exc)

        if result != url:
            logger.info("Resolved stream URL: %s -> %s", url[:60], result[:90])
        self._url_resolve_cache[url] = (now, result)
        return result

    async def send_remote_stream(self, mac: str, url: str, codec: str = "m") -> bool:
        """Send a strm frame for an EXTERNAL stream URL (radio/favorites).

        LMS behaviour (Squeezebox.pm stream_s, $isDirect branch): the player
        streams DIRECTLY from the source. server_ip/server_port in the strm
        frame point at the source, the request string is the source request
        (Slim/Player/Protocols/HTTP.pm requestString), autostart is 3
        (direct = proxy-autostart 1 + 2). After the player connects it
        forwards the source's HTTP response headers as a RESP frame; the
        server replies with a 'cont' frame carrying the Icecast metaint so
        the player can de-interleave metadata itself. The stream keeps
        playing when this server goes away — exactly like the real LMS.

        Falls back to the server-side proxy (/stream.mp3?player=MAC) when
        the URL cannot be resolved to a direct connection.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        # Flush old stream buffers first (Perl LMS behaviour) so the
        # switch is immediate instead of playing out the old buffer.
        await self._flush_if_playing(mac)

        # Resolve redirects / M3U/PLS playlists server-side (Squeezelite
        # can do neither) — like the Perl LMS does before direct streams.
        url = await self._resolve_stream_url(url)

        # Keep the player playlist in sync: the proxy endpoint resolves
        # the track from player.playlist, so it must hold the RESOLVED
        # URL — otherwise it would re-fetch the raw M3U/redirect URL and
        # stream playlist text as audio (silence).
        try:
            from lyrion.player.manager import PlayerManager
            player = PlayerManager().get_player(mac)
            if player is not None and player.playlist:
                pos = player.playlist_position or 0
                if 0 <= pos < len(player.playlist) and isinstance(player.playlist[pos], str):
                    player.playlist[pos] = url
            # Keep state consistent regardless of the calling path
            # (play_url vs _play_playlist_item): this is a live stream.
            if player is not None:
                player.current_url = url
                player.current_track_id = None
        except Exception:
            pass

        from urllib.parse import urlparse
        import socket as _socket

        # Player without CanHTTPS=1 cannot do TLS itself → https streams
        # must be proxied by the server (Perl LMS: canDirectStream returns
        # 0 unless CanHTTPS; HTTPS.pm slimprotoFlags only sets the SSL flag
        # for direct streams). http streams always go direct.
        try:
            from lyrion.player.manager import PlayerManager
            pstate = PlayerManager().get_player(mac)
        except Exception:
            pstate = None
        parsed_tmp = urlparse(url)
        if parsed_tmp.scheme == "https" and not getattr(pstate, "can_https", False):
            logger.info("Player %s cannot do TLS (no CanHTTPS cap) — proxying %s",
                        mac, url[:60])
            return await self._send_proxy_stream(mac, url, codec)

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"unsupported URL scheme: {url[:40]}")
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            # Squeezelite's slimproto server_ip field is 4 bytes — IPv4 only.
            # Force AF_INET so getaddrinfo cannot return an IPv6 address
            # (inet_aton would reject it).
            infos = await asyncio.get_event_loop().getaddrinfo(
                host, port, family=_socket.AF_INET, type=_socket.SOCK_STREAM
            )
            ip = str(infos[0][4][0])
            server_ip = int.from_bytes(_socket.inet_aton(ip), "big")
        except Exception as exc:
            logger.warning("Direct stream setup failed for %s (%s) — using proxy",
                           url[:60], exc)
            return await self._send_proxy_stream(mac, url, codec)

        # Request string exactly like the Perl-LMS HTTP.pm requestString:
        #   GET <path> HTTP/1.0 + Accept + Cache-Control + User-Agent +
        #   Icy-MetaData: 1 + Connection: close + Host
        host_header = host if port in (80, 443) else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.0\r\n"
            f"Accept: */*\r\n"
            f"Cache-Control: no-cache\r\n"
            f"User-Agent: LyrionMusicServer/9.2.0\r\n"
            f"Icy-MetaData: 1\r\n"
            f"Connection: close\r\n"
            f"Host: {host_header}\r\n"
            f"\r\n"
        ).encode("ascii")

        # Direct stream: autostart 1+2=3 (player waits for 'cont' after
        # RESP); SSL flag 0x20 for https (HTTPS.pm slimprotoFlags).
        flags = 0x20 if parsed.scheme == "https" else 0
        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=3,
            server_port=port, server_ip=server_ip, flags=flags,
        )
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent DIRECT strm to %s (remote: %s -> %s:%d)",
                        mac, url[:60], ip, port)
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send direct strm to %s: %s", mac, exc)
            return False

        # Wait for the player's RESP (source headers) and send 'cont' with
        # the metaint so the player can strip Icecast metadata itself.
        asyncio.create_task(self._send_cont_after_resp(mac, url))
        return True

    async def _send_proxy_stream(self, mac: str, url: str, codec: str = "m") -> bool:
        """Fallback: server-side proxy stream (player fetches /stream.mp3
        from this server, which relays the remote URL)."""
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            logger.warning("No active connection for player %s", mac)
            return False

        request = (
            f"GET /stream.mp3?player={mac} HTTP/1.0\r\n"
            f"\r\n"
        ).encode("ascii")

        frame = self._build_stream_frame(
            request=request, codec=codec, autostart=1,
            server_port=self.web_port,
        )
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent proxy strm to %s (remote: %s)", mac, url[:60])
            # No 'cont' frame — matches real LMS behaviour.
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("Failed to send remote strm to %s: %s", mac, exc)
            return False

    async def _send_cont_after_resp(self, mac: str, url: str) -> None:
        """Wait for the player's RESP frame (source HTTP headers) for a
        direct stream, then send the 'cont' frame that starts the decoder
        and passes the Icecast metaint (Squeezebox2.pm sendContCommand).
        """
        mac_key = mac.upper().replace(":", "")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._resp_waiters[mac_key] = fut
        try:
            try:
                metaint = await asyncio.wait_for(fut, timeout=20)
            except asyncio.TimeoutError:
                logger.warning("No RESP from %s for direct stream %s — cont metaint=0",
                               mac, url[:50])
                metaint = 0
                # The source never answered (dead/unreachable station) —
                # don't leave the UI showing "playing" with no audio.
                try:
                    from lyrion.player.manager import PlayerManager
                    player = PlayerManager().get_player(mac)
                    if player is not None:
                        player.mode = "stop"
                except Exception:
                    pass
            await self._send_cont(mac, metaint)
        except Exception as exc:
            logger.warning("cont after RESP failed for %s: %s", mac, exc)
        finally:
            self._resp_waiters.pop(mac_key, None)

    async def _send_cont(self, mac: str, metaint: int) -> bool:
        """Send a 'cont' frame to a player.

        Exact Perl-LMS format (Squeezebox2.pm sendContCommand,
        pack('NCnC*', metaint, loop, count, guids)) — Perl 'C' is unsigned
        char = Python 'B', Perl 'n' is unsigned short BE = Python 'H'.
        Squeezelite's cont_packet is opcode + metaint(u32 BE) + loop(u8);
        count/guids are ignored. `>IBH` = N(4 BE) + B(1) + H(2 BE).
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = struct.pack(">IBH", metaint, 0, 0)  # metaint, loop, count
        frame = struct.pack(">H", 4 + len(payload)) + b"cont" + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent cont metaint=%d to %s", metaint, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_stop_to_player(self, mac: str) -> bool:
        """Send a 'strm' stop command ('q') to a player."""
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        # strm command 'q' = flush/stop. Fields after endianness:
        # threshold(1) spdif(1) transition_period(1) transition_type(1)
        # flags(1) output_threshold(1) slaves(1) = 7 bytes, then
        # replay_gain(4 BE) server_port(2 BE) server_ip(4 BE)
        payload = b"".join([
            b"strm", b"q", b"0", b"?", b"0", b"0", b"0", b"l",
            bytes(7),                 # threshold..slaves
            struct.pack(">I", 0),     # replay_gain
            struct.pack(">H", 0),     # server_port
            struct.pack(">I", 0),     # server_ip
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent strm 'q' (stop) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_pause_to_player(self, mac: str, pause_ms: int = 0) -> bool:
        """Send a 'strm' pause command ('p') to a player.

        LMS semantics (Squeezebox.pm pause -> stream('p') without
        interval): replay_gain = 0 means the output is held paused
        INDEFINITELY (Squeezelite OUTPUT_STOPPED). A value > 0 would
        auto-resume after that many milliseconds. Resume is a separate
        command ('u'), see send_unpause_to_player.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"".join([
            b"strm", b"p", b"0", b"?", b"0", b"0", b"0", b"l",
            bytes(7),
            struct.pack(">I", pause_ms),  # replay_gain = pause interval ms
            struct.pack(">H", 0),
            struct.pack(">I", 0),
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent strm 'p' (pause=%d) to %s", pause_ms, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_unpause_to_player(self, mac: str) -> bool:
        """Send a 'strm' unpause command ('u') — LMS resume.

        Squeezebox2.pm resume -> stream('u'); Squeezelite process_strm
        'u' with replay_gain 0 unpauses immediately.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"".join([
            b"strm", b"u", b"0", b"?", b"0", b"0", b"0", b"l",
            bytes(7),
            struct.pack(">I", 0),  # replay_gain = unpause jiffies (0 = now)
            struct.pack(">H", 0),
            struct.pack(">I", 0),
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent strm 'u' (unpause) to %s", mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_volume_to_player(self, mac: str, volume: int) -> bool:
        """Send an 'audg' volume frame to a player.

        audg_packet: opcode(4) old_gainL(4) old_gainR(4) adjust(1) preamp(1)
        gainL(4) gainR(4) — gains are 0..65536 (0-100% * 655.36), BE.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        volume = max(0, min(100, int(volume)))
        gain = int(volume * 655.36)
        payload = b"".join([
            b"audg",
            struct.pack(">I", 0),     # old_gainL
            struct.pack(">I", 0),     # old_gainR
            bytes([1]),               # adjust: apply gainL/gainR
            bytes([0]),               # preamp
            struct.pack(">I", gain),  # gainL
            struct.pack(">I", gain),  # gainR
        ])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent audg volume=%d to %s", volume, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    async def send_aude(self, mac: str, spdif: bool = False, dac: bool = True) -> bool:
        """Send an 'aude' frame (enable/disable audio outputs).

        aude_packet: opcode(4) spdif(1) dac(1) — 1 = output enabled.
        Squeezebox hardware uses this to route audio; software players
        (squeezelite/jive) ignore it.
        """
        mac = mac.upper().replace(":", "")
        writer = self._player_writers.get(mac)
        if writer is None or writer.is_closing():
            return False
        payload = b"aude" + bytes([1 if spdif else 0, 1 if dac else 0])
        frame = struct.pack(">H", len(payload)) + payload
        try:
            writer.write(frame)
            await writer.drain()
            logger.info("Sent aude spdif=%d dac=%d to %s", spdif, dac, mac)
            return True
        except (ConnectionError, OSError, RuntimeError):
            return False

    def _handle_resp_frame(self, mac_str: str, payload: bytes) -> None:
        """Handle a RESP frame: the player forwards the source's HTTP
        response headers after connecting for a direct stream (Squeezelite
        sendRESP). Extract icy-metaint and resolve the pending cont waiter
        (or send 'cont' directly if the server restarted in between).
        """
        try:
            text = payload.decode("latin1", errors="replace")
            m = re.search(r"(?im)^icy-metaint:\s*(\d+)\s*$", text)
            metaint = int(m.group(1)) if m else 0
            mac_key = mac_str.upper().replace(":", "")
            fut = self._resp_waiters.get(mac_key)
            if fut is not None and not fut.done():
                fut.set_result(metaint)
            else:
                # No waiter (e.g. server restarted between strm and RESP) —
                # send cont directly so the decoder still starts.
                asyncio.create_task(self._send_cont(mac_str, metaint))
            logger.info("RESP from %s: metaint=%d", mac_str, metaint)
        except Exception as exc:
            logger.warning("RESP parse failed for %s: %s", mac_str, exc)

    def _handle_stat_frame(self, mac_str: str, payload: bytes) -> None:
        """Parse a STAT frame from a player (best effort, for logging/state).

        STAT packet layout (from squeezelite slimproto.h / LMS _stat_handler):
            event(4) num_crlf(1) mas_initialized(1) mas_mode(1)
            stream_buffer_size(4) stream_buffer_fullness(4)
            bytes_received_H(4) bytes_received_L(4) signal_strength(2)
            jiffies(4) output_buffer_size(4) output_buffer_fullness(4)
            elapsed_seconds(4) voltage(2) elapsed_milliseconds(4)
            server_timestamp(4) error_code(2)
        """
        try:
            if len(payload) >= 4:
                event = payload[0:4].decode("ascii", errors="replace").strip("\x00")
            else:
                event = "?"
            # Squeezelite/SqueezePlayer pack the statstruct_t WITHOUT
            # C padding (53 bytes). Verified against live frames:
            # output_buffer_size(4) at 29:33 == 0x0035d540 (3.5MB), so
            # jiffies is at 25:29 and elapsed_seconds at 37:41.
            jiffies = int.from_bytes(payload[25:29], "big") if len(payload) >= 29 else 0
            elapsed = int.from_bytes(payload[37:41], "big") if len(payload) >= 41 else 0
            logger.debug("STAT from %s: event=%s jiffies=%d elapsed=%ds hex=%s",
                         mac_str, event, jiffies, elapsed, payload[:60].hex())

            # event "setd" carries the player-assigned name (squeezelite/IPAD style)
            if event == "setd":
                # Try to extract a printable name from the payload tail
                try:
                    tail = payload[4:]
                    # Name is NUL-terminated ASCII somewhere after the event field
                    for sep in (b"\x00", b"\xff", b"\x00\x00"):
                        idx = tail.find(sep)
                        if idx > 0:
                            raw_name = tail[:idx]
                            try:
                                candidate = raw_name.decode("utf-8").strip()
                            except UnicodeDecodeError:
                                # SqueezePlay sends the name in latin-1
                                # (e.g. "Küche" -> b'K\xfcche'); utf-8 with
                                # errors="replace" would store "K�che".
                                candidate = raw_name.decode("latin-1").strip()
                            if candidate and candidate.isprintable() and len(candidate) < 64:
                                logger.info("STAT setd from %s: name=%r (payload %d bytes)", mac_str, candidate, len(payload))
                                from lyrion.player.manager import PlayerManager
                                PlayerManager().rename_player(mac_str, candidate)
                                break
                except Exception as exc:
                    logger.debug("STAT setd parse failed for %s: %s", mac_str, exc)

            # Update player state (mode) if possible
            try:
                from lyrion.player.manager import PlayerManager
                pm = PlayerManager()
                player = pm.get_player(mac_str)
                if player is not None:
                    # Track playback position (STAT elapsed_seconds, sent
                    # with every STMt) — used for status 'time'.
                    if elapsed and elapsed < 3600 * 24:  # sanity: < 24h
                        player.elapsed = elapsed
                    if event == "STMd":
                        # DECODE_COMPLETE — decoder has no more data. This
                        # fires BOTH at natural track end AND when the user
                        # stopped the player (strm 'q' also runs the decoder
                        # to completion) AND when pausing a live stream
                        # (pause = stop for streams). Only auto-advance on
                        # natural end: if the server already set mode=stop
                        # or mode=pause, the STMd is the player
                        # acknowledging the stop.
                        if player.mode not in ("stop", "pause"):
                            asyncio.create_task(_advance_after_track(pm, mac_str))
                        # Keep "pause" if the player was paused (pause of a
                        # live stream = stop, STMd acknowledges it).
                        if player.mode != "pause":
                            player.mode = "stop"
                    elif event == "STMs":
                        # TRACK_STARTED — a new track started playing
                        player.mode = "play"
                    elif event == "STMf":
                        # FLUSH/STOP ack — the player stopped; mark stop
                        # only if the server didn't already (natural end
                        # vs. user stop).
                        if player.mode not in ("pause",):
                            player.mode = "stop"
                    elif event == "STMp":
                        # PAUSE ack
                        player.mode = "pause"
                    elif event == "STMr":
                        # RESUME ack
                        player.mode = "play"
                    elif event == "STMn":
                        # DECODE_ERROR — the player could not decode the
                        # stream; log and fall back to stop.
                        logger.warning("STAT STMn (decode error) from %s", mac_str)
                        player.mode = "stop"
                    elif event == "STMo":
                        # UNDERRUN — buffer underrun, harmless, log only
                        logger.debug("STAT STMo (underrun) from %s", mac_str)
                    elif event == "pause":
                        player.mode = "pause"
                    elif event == "stop":
                        player.mode = "stop"
                    elif event == "play":
                        player.mode = "play"
                    elif event == "load":
                        player.mode = "loading"
                    player.last_activity = __import__("time").time()
                    # Wake CLI subscribers so they push the fresh status.
                    try:
                        from lyrion.control.cli import CLIHandler
                        CLIHandler.notify_subscribers(player.mac)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Failed to parse STAT from %s: %s", mac_str, exc)

    # ------------------------------------------------------------------
    # End-of-track handling
    # ------------------------------------------------------------------


async def _advance_after_track(pm, mac_str: str) -> None:
    """Advance the playlist when the player reports track end (STAT STMd).

    LMS behaviour: after the last track the player stops; otherwise the
    next playlist item gets a new strm frame. Do NOT wrap around (LMS
    default has repeat off).
    """
    try:
        player = pm.get_player(mac_str)
        if player is None or not player.playlist:
            return
        if player.playlist_position < len(player.playlist) - 1:
            logger.info("Track finished on %s — advancing playlist", mac_str)
            await pm.playlist_next(mac_str)
        else:
            logger.info("Last track finished on %s — stopping", mac_str)
            await pm.stop_player(mac_str)
    except Exception as exc:
        logger.warning("advance after track failed for %s: %s", mac_str, exc)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def send_stat(self, stat: StatMessage) -> None:
        """Send a STAT message to the server."""
        await self._send_frame(CMD_STAT, stat.to_bytes())

    async def send_cli(self, mac: str, command: str) -> bool:
        """Send a CLI command to a connected player over its TCP channel."""
        mac_clean = mac.upper().replace(":", "").replace("-", "")
        writer = self._player_writers.get(mac_clean)
        if writer is None or writer.is_closing():
            logger.warning("send_cli: no writer for player %s", mac)
            return False
        try:
            # For binary SlimProto players, send as EVNT frame
            payload = command.encode("utf-8") + b"\n"
            frame = struct.pack(">H", len(payload) + 4)
            writer.write(frame + b"strm" + payload)
            await writer.drain()
            return True
        except Exception as exc:
            logger.warning("send_cli to %s failed: %s", mac, exc)
            return False

    async def send_body(self, chunk: bytes) -> None:
        """Send a BODY chunk (audio data)."""
        await self._send_frame(CMD_BODY, chunk)

    async def send_stmu(self, metadata: bytes) -> None:
        """Send a STMU (stream metadata update)."""
        await self._send_frame(CMD_STMU, metadata)

    async def send_anic(self, image_data: bytes) -> None:
        """Send ANIC (album art / now-playing image)."""
        await self._send_frame(CMD_ANIC, image_data)
