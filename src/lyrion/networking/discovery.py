"""
Player and server discovery via UDP broadcast and SSDP/UPnP multicast.

Discovery protocol:
  - UDP broadcast on port 3483 (SLIMPROTO_PORT): players send beacon packets
    announcing themselves.  Format: "e:$mac:$name:$type:$ip:$port"
  - SSDP/UPnP multicast on 239.255.255.250:1900 for LMS server discovery.
  - The discovery service tracks discovered players and can send heartbeats.

Player beacon format (UDP from player → server, port 3483):
    "e:$MAC:$NAME:$MODEL:$IP:$PORT"
    e.g.  "e:00:04:20:12:34:56:Squeezebox Radio:faustini:192.168.1.10:3483"

SSDP NOTIFY / M-SEARCH (LMS server on 239.255.255.250:1900):
    NOTIFY * HTTP/1.1
    HOST: 239.255.255.250:1900
    NT: urn:schemas-squeezebox:device:Server:1
    NTS: ssdp:alive
    SERVER: Lyrion/9.2.0 UPnP/1.0
    USN: uuid:LYRION-server-...
    LOCATION: http://$server_ip:$port/

This module provides:
  - DiscoveryService: polls/records players, manages subscriptions
  - parse_player_beacon(): parse a UDP beacon string into a dataclass
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISCOVERY_PORT = 3483  # slimproto UDP port for player beacons
SSDP_PORT = 1900
SSDP_MULTICAST = "239.255.255.250"

# SSDP search target for LMS server
SSDP_ST = "urn:schemas-squeezebox:device:Server:1"
SSDP_MX = 3  # seconds to wait for responses


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class DiscoveredPlayer:
    """A Squeezebox player discovered on the network."""

    mac: str  # MAC address, colon-separated
    name: str
    model: str  # e.g. "Squeezebox Radio", "Squeezebox Touch"
    ip: str
    port: int
    uuid: str = ""
    seq: int = 0  # beacon sequence number

    @property
    def address(self) -> tuple[str, int]:
        return (self.ip, self.port)

    @property
    def mac_clean(self) -> str:
        """MAC without colons, uppercase."""
        return self.mac.replace(":", "").upper()


@dataclass
class DiscoveredServer:
    """An LMS server discovered via SSDP."""

    usn: str  # Unique Service Name (UUID)
    location: str  # HTTP URL of the server
    server: str  # Server header string
    ip: str = ""
    port: int = 9000

    @classmethod
    def from_ssdp_notify(cls, headers: dict[str, str]) -> DiscoveredServer | None:
        """Parse SSDP NOTIFY headers into a DiscoveredServer."""
        usn = headers.get("USN", "")
        location = headers.get("LOCATION", "")
        server = headers.get("SERVER", "")
        ip = ""
        port = 9000
        # Extract host:port from LOCATION URL
        m = re.match(r"http://([^:/]+):(\d+)", location)
        if m:
            ip = m.group(1)
            port = int(m.group(2))
        if not usn or not location:
            return None
        return cls(usn=usn, location=location, server=server, ip=ip, port=port)


# ---------------------------------------------------------------------------
# Beacon parsing
# ---------------------------------------------------------------------------
_PLAYER_BEACON_RE = re.compile(
    r"^e:([0-9a-fA-F:]{17}):([^:]+):([^:]+):(\d+\.\d+\.\d+\.\d+):(\d+)"
)


def parse_player_beacon(data: bytes | str) -> DiscoveredPlayer | None:
    """Parse a UDP player beacon into a DiscoveredPlayer.

    Beacon format: "e:$MAC:$NAME:$MODEL:$IP:$PORT"
    Returns None if the line doesn't match the expected format.
    """
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    text = text.strip()
    m = _PLAYER_BEACON_RE.match(text)
    if not m:
        return None
    mac, name, model, ip, port = m.groups()
    return DiscoveredPlayer(
        mac=mac.upper(),
        name=name,
        model=model,
        ip=ip,
        port=int(port),
    )


# ---------------------------------------------------------------------------
# DiscoveryService
# ---------------------------------------------------------------------------


class DiscoveryService:
    """Async player discovery service.

    Listens for player beacons (UDP broadcast on port 3483) and optionally
    performs SSDP discovery for LMS servers.

    Usage:
        service = DiscoveryService()
        service.on_player(lambda p: print(f"Player found: {p.name}"))
        await service.start()
        # ... run forever ...
        await service.stop()
    """

    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        discovery_port: int = DISCOVERY_PORT,
    ):
        self.bind_host = bind_host
        self.discovery_port = discovery_port
        self._players: dict[str, DiscoveredPlayer] = {}
        self._servers: dict[str, DiscoveredServer] = {}
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()

        # Callbacks
        self._player_callbacks: list[Callable[[DiscoveredPlayer], None | Coroutine[Any, Any, None]]] = []
        self._server_callbacks: list[Callable[[DiscoveredServer], None | Coroutine[Any, Any, None]]] = []
        self._player_removed_callbacks: list[Callable[[str], None | Coroutine[Any, Any, None]]] = []

        # UDP socket for beacon listening
        self._beacon_socket: socket.socket | None = None
        self._beacon_transport: asyncio.DatagramProtocol | None = None

        # SSDP socket
        self._ssdp_socket: socket.socket | None = None
        self._ssdp_transport: asyncio.DatagramProtocol | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the discovery service (both UDP beacon and SSDP listener)."""
        if self._running:
            return
        self._running = True

        # UDP beacon listener (player → server)
        self._tasks.append(asyncio.create_task(self._listen_beacons()))

        # SSDP multicast listener (server announcements)
        self._tasks.append(asyncio.create_task(self._listen_ssdp()))

        logger.info("DiscoveryService started on port %d", self.discovery_port)

    async def stop(self) -> None:
        """Stop the discovery service and close all sockets."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._beacon_socket:
            try:
                self._beacon_socket.close()
            except Exception:
                pass
            self._beacon_socket = None
        if self._ssdp_socket:
            try:
                self._ssdp_socket.close()
            except Exception:
                pass
            self._ssdp_socket = None
        logger.info("DiscoveryService stopped")

    # ------------------------------------------------------------------
    # Beacon listener
    # ------------------------------------------------------------------

    async def _listen_beacons(self) -> None:
        """Async task: listen for player UDP beacons on the discovery port."""

        loop = asyncio.get_event_loop()

        def make_socket() -> socket.socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # On Linux, join the broadcast group
            try:
                sock.bind(("", self.discovery_port))
            except OSError as exc:
                logger.warning("Could not bind beacon socket to %d: %s", self.discovery_port, exc)
                sock.bind(("0.0.0.0", self.discovery_port))
            return sock

        sock = make_socket()
        self._beacon_socket = sock

        async def reader() -> None:
            # Blocking mode: recvfrom runs in a worker thread and must block
            # until a datagram arrives (non-blocking would return EAGAIN).
            sock.setblocking(True)
            while self._running:
                try:
                    # NOTE: uvloop does not implement loop.sock_recvfrom —
                    # it raises NotImplementedError. Use a thread instead.
                    data, addr = await asyncio.to_thread(sock.recvfrom, 4096)
                    await self._handle_beacon(data, addr)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("Beacon recv error: %s", exc)
                    await asyncio.sleep(1)

        try:
            await reader()
        finally:
            sock.close()

    async def _handle_beacon(
        self,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        """Handle an incoming player beacon or discovery query."""
        # ── SlimProto discovery request (Squeezebox hardware, classic):
        # starts with 'd', then deviceid(1) + revision(1) + 8 bytes +
        # mac(6) — respond 'D' + 17-byte hostname (LMS Discovery.pm). ──
        if data[0:1] == b"d" or (len(data) == 8 and data[0] in (2, 3, 4)):
            await self._reply_discovery(addr)
            return
        # ── TLV discovery (Jive/SqueezeCtrl/SqueezePlay): 'e' + TLV
        # blocks (NAME/IPAD/JSON/VERS/UUID) → answer with 'E' + TLVs. ──
        if data[0:1] == b"e" and len(data) > 1:
            await self._reply_tlv(addr, data)
            return
        # ── Short discovery probes from remote apps ──
        if data in (b"D", b"d"):
            await self._reply_discovery(addr)
            return

        # ── Player beacons / other traffic: existing handling ──
        await self._handle_player_beacon(data, addr)

    async def _reply_tlv(
        self,
        addr: tuple[str, int],
        data: bytes,
    ) -> None:
        """Answer a Jive 'e' TLV discovery request with an 'E' response
        (Slim/Networking/Discovery.pm gotTLVRequest)."""
        sock = self._beacon_socket
        if sock is None:
            return
        import socket as _socket
        from lyrion import __version__

        # Local IP reachable by the client (the socket is bound to
        # 0.0.0.0, so getsockname is useless here).
        local_ip = "0.0.0.0"
        try:
            probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            probe.settimeout(1)
            probe.connect(("192.168.1.1", 1))
            local_ip = probe.getsockname()[0]
            probe.close()
        except Exception:
            pass
        if local_ip == "0.0.0.0":
            try:
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                probe.connect(("8.8.8.8", 80))
                local_ip = probe.getsockname()[0]
                probe.close()
            except Exception:
                pass
        try:
            from lyrion.config import get_config
            # Advertise the native Cometd streaming port (9080) so Jive
            # apps (Orange Squeeze, SqueezePlay) connect to the server
            # that can serve the Bayeux streaming protocol.
            http_port = int(get_config().get("cometd_stream_port") or 9080)
        except Exception:
            http_port = 9080

        hostname = _socket.gethostname()[:16]
        values = {
            b"NAME": hostname.encode("iso-8859-1", errors="replace"),
            b"IPAD": local_ip.encode(),
            b"JSON": str(http_port).encode(),
            b"VERS": __version__.encode("ascii", errors="replace"),
            b"UUID": b"lyrion-server-0001",
        }
        # parse TLV blocks: T(4) L(1) V(L)
        body = data[1:]
        response = bytearray(b"E")
        pos = 0
        while pos + 5 <= len(body):
            t = body[pos:pos + 4]
            l = body[pos + 4]
            v = body[pos + 5:pos + 5 + l] if l else b""
            if t in values:
                r = values[t]
                response += t + bytes([len(r)]) + r
            pos += 5 + l
        # Always include the core fields even if not requested (the
        # Jive client needs the server address to connect).
        if b"IPAD" not in response:
            response += b"IPAD" + bytes([len(local_ip)]) + local_ip.encode()
        if b"JSON" not in response:
            response += b"JSON" + bytes([len(str(http_port))]) + str(http_port).encode()
        try:
            await asyncio.to_thread(sock.sendto, bytes(response), addr)
            logger.info("TLV discovery response sent to %s (%d bytes)",
                        addr[0], len(response))
        except Exception as exc:
            logger.debug("TLV discovery response failed: %s", exc)

    async def _reply_discovery(
        self,
        addr: tuple[str, int],
        device_id: int | None = None,
    ) -> None:
        """Answer a SlimProto discovery request with the LMS-compatible
        'D' + hostname packet (18 bytes) — NOT JSON (apps expect the
        original protocol)."""
        sock = self._beacon_socket
        if sock is None:
            return
        import socket as _socket
        hostname = _socket.gethostname()[:16].encode(
            "iso-8859-1", errors="replace")
        hostname = hostname.ljust(17, b"\x00")
        response = b"D" + hostname
        try:
            await asyncio.to_thread(sock.sendto, response, addr)
            logger.info("Discovery response sent to %s (deviceid=%s)",
                        addr[0], device_id if device_id is not None else "?")
        except Exception as exc:
            logger.debug("Discovery response failed: %s", exc)

    async def _handle_player_beacon(
        self,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        """Handle a player UDP beacon (announce/heartbeat)."""
        player = parse_player_beacon(data)
        if player is None:
            logger.debug("Unrecognized beacon from %s: %r", addr, data)
            return

        async with self._lock:
            is_new = player.mac not in self._players
            self._players[player.mac] = player

        if is_new:
            logger.info(
                "Player discovered: %s (%s) at %s:%d",
                player.name,
                player.model,
                player.ip,
                player.port,
            )
            for cb in self._player_callbacks:
                try:
                    result = cb(player)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Player callback error: %s", exc)
        else:
            logger.debug("Player heartbeat: %s", player.name)

    # ------------------------------------------------------------------
    # SSDP listener
    # ------------------------------------------------------------------

    async def _listen_ssdp(self) -> None:
        """Async task: listen for SSDP NOTIFY messages from LMS servers."""

        loop = asyncio.get_event_loop()

        def make_ssdp_socket() -> socket.socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            try:
                # Join SSDP multicast group
                mreq = struct.pack(
                    "4s4s",
                    socket.inet_aton(SSDP_MULTICAST),
                    socket.inet_aton("0.0.0.0"),
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                logger.warning("Could not join SSDP multicast group: %s", exc)
            sock.bind(("", SSDP_PORT))
            return sock

        sock = make_ssdp_socket()
        self._ssdp_socket = sock

        async def reader() -> None:
            # Blocking mode: recvfrom runs in a worker thread and must block
            # until a datagram arrives.
            sock.setblocking(True)
            while self._running:
                try:
                    # NOTE: uvloop does not implement loop.sock_recvfrom.
                    data, addr = await asyncio.to_thread(sock.recvfrom, 4096)
                    await self._handle_ssdp(data, addr)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("SSDP recv error: %s", exc)
                    await asyncio.sleep(1)

        try:
            await reader()
        finally:
            sock.close()

    async def _local_server_addr(self) -> tuple[str, int]:
        """Best-effort (ip, http_port) of THIS server for discovery replies.

        The IP is the local address reachable from the requester's subnet
        probe; the port comes from the running config ('serverport').
        """
        import socket as _socket
        local_ip = "0.0.0.0"
        for probe_host in ("192.168.1.1", "8.8.8.8"):
            try:
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                probe.settimeout(1)
                probe.connect((probe_host, 1))
                ip = probe.getsockname()[0]
                probe.close()
                if ip and ip != "0.0.0.0":
                    local_ip = ip
                    break
            except Exception:
                continue
        try:
            from lyrion.config import get_config
            http_port = int(get_config().get("serverport", 9000) or 9000)
        except Exception:
            http_port = 9000
        return local_ip, http_port

    async def _handle_ssdp(
        self,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        """Parse and handle an SSDP message."""
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        headers: dict[str, str] = {}
        for line in lines:
            if ": " in line:
                key, value = line.split(": ", 1)
                headers[key.upper()] = value

        nt = headers.get("NT", "")
        nts = headers.get("NTS", "")
        man = headers.get("MAN", "")

        # Respond to M-SEARCH queries (SqueezeCtrl/Squeezer discovery)
        st = headers.get("ST", "")
        if "squeezebox" in st.lower() and "ssdp:discover" in man.lower():
            try:
                server_ip, http_port = await self._local_server_addr()
                sock = self._ssdp_socket
                if sock:
                    response = (
                        "HTTP/1.1 200 OK\r\n"
                        "CACHE-CONTROL: max-age=1800\r\n"
                        f"LOCATION: http://{server_ip}:{http_port}/\r\n"
                        "SERVER: Lyrion/9.2.0 UPnP/1.0\r\n"
                        "ST: urn:schemas-squeezebox:device:Server:1\r\n"
                        "USN: uuid:lyrion-server-0001::urn:schemas-squeezebox:device:Server:1\r\n"
                        "\r\n"
                    )
                    await asyncio.to_thread(sock.sendto, response.encode(), addr)
                    logger.debug("SSDP M-SEARCH response sent to %s", addr)
            except Exception as exc:
                logger.debug("SSDP M-SEARCH response error: %s", exc)
            return

        if "squeezebox" not in nt.lower() and "squeezebox" not in headers.get("SERVER", "").lower():
            return

        if nts == "ssdp:alive" or "NOTIFY" in text:
            server = DiscoveredServer.from_ssdp_notify(headers)
            if server:
                async with self._lock:
                    is_new = server.usn not in self._servers
                    self._servers[server.usn] = server
                if is_new:
                    logger.info("LMS server discovered via SSDP: %s", server.location)
                    for cb in self._server_callbacks:
                        try:
                            result = cb(server)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            logger.error("Server callback error: %s", exc)

    # ------------------------------------------------------------------
    # Active SSDP discovery (M-SEARCH)
    # ------------------------------------------------------------------

    async def discover_servers(self, timeout: float = 5.0) -> list[DiscoveredServer]:
        """Send an SSDP M-SEARCH and collect server responses."""
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setblocking(False)

        msearch = (
            f"M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_MULTICAST}:{SSDP_PORT}\r\n"
            f"MAN: \"ssdp:discover\"\r\n"
            f"MX: {SSDP_MX}\r\n"
            f"ST: {SSDP_ST}\r\n"
            f"USER-AGENT: Lyrion/9.2.0\r\n"
            f"\r\n"
        )

        results: list[DiscoveredServer] = []
        ready = asyncio.Event()

        async def recv_loop() -> None:
            sock.settimeout(timeout)  # socket.timeout is an OSError subclass
            try:
                while not ready.is_set():
                    try:
                        # uvloop has no loop.sock_recvfrom → thread it.
                        data, addr = await asyncio.to_thread(sock.recvfrom, 4096)
                        text = data.decode("utf-8", errors="replace")
                        if "HTTP/1.1 200" in text:
                            lines = text.split("\r\n")
                            headers: dict[str, str] = {}
                            for line in lines:
                                if ": " in line:
                                    key, value = line.split(": ", 1)
                                    headers[key.upper()] = value
                            server = DiscoveredServer.from_ssdp_notify(headers)
                            if server:
                                results.append(server)
                    except OSError:
                        break
            finally:
                ready.set()

        try:
            await asyncio.to_thread(
                sock.sendto,
                msearch.encode("ascii"),
                (SSDP_MULTICAST, SSDP_PORT),
            )
            recv_task = asyncio.create_task(recv_loop())
            await asyncio.wait_for(ready.wait(), timeout=timeout + 1)
            await recv_task
        except asyncio.TimeoutError:
            pass
        finally:
            sock.close()

        return results

    # ------------------------------------------------------------------
    # Heartbeat (server → player)
    # ------------------------------------------------------------------

    async def send_heartbeat(self, player: DiscoveredPlayer) -> None:
        """Send a heartbeat packet to a discovered player.

        The heartbeat is a UDP packet to the player's IP:port on the
        discovery port, confirming the server is still alive.
        """
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        try:
            msg = f"h:{player.mac}\n".encode("ascii")
            # uvloop has no loop.sock_sendto → thread it.
            await asyncio.to_thread(sock.sendto, msg, (player.ip, player.port))
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def players(self) -> dict[str, DiscoveredPlayer]:
        return self._players.copy()

    @property
    def servers(self) -> dict[str, DiscoveredServer]:
        return self._servers.copy()

    def get_player(self, mac: str) -> DiscoveredPlayer | None:
        return self._players.get(mac)

    def get_player_by_name(self, name: str) -> DiscoveredPlayer | None:
        for p in self._players.values():
            if p.name == name:
                return p
        return None

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def on_player(
        self,
        callback: Callable[[DiscoveredPlayer], None | Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback for newly discovered players."""
        self._player_callbacks.append(callback)

    def on_server(
        self,
        callback: Callable[[DiscoveredServer], None | Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback for newly discovered servers."""
        self._server_callbacks.append(callback)

    def on_player_removed(
        self,
        callback: Callable[[str], None | Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback for players that disappear."""
        self._player_removed_callbacks.append(callback)
