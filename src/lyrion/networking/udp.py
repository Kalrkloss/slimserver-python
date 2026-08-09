"""
UDP networking utilities.

Provides:
  - UDPClient: send UDP packets (unicast, broadcast)
  - UDPReceiver: receive UDP packets (unicast, multicast) using asyncio

Replaces the Perl Slim::Networking::UDP module with an async Python API.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Coroutine
from typing import Any, Callable

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UDPClient — sender
# ---------------------------------------------------------------------------


class UDPClient:
    """Async UDP sender (unicast, broadcast).

    Usage:
        sender = UDPClient()
        await sender.send(b"hello", ("192.168.1.10", 3483))
        await sender.broadcast(b"beacon", 3483)
        await sender.close()
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._closed = False

    async def _ensure_socket(self) -> socket.socket:
        """Lazily create and configure the socket."""
        if self._sock is None:
            loop = asyncio.get_event_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setblocking(False)
            self._sock = sock
        return self._sock

    async def send(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send a unicast UDP packet to the given address."""
        sock = await self._ensure_socket()
        # uvloop has no loop.sock_sendto → thread it.
        await asyncio.to_thread(sock.sendto, data, addr)

    async def broadcast(self, data: bytes, port: int, host: str = "<broadcast>") -> None:
        """Send a UDP broadcast to the given port.

        Args:
            data: payload bytes
            port: destination port
            host: broadcast address (default "<broadcast>", i.e. 255.255.255.255)
        """
        await self.send(data, (host, port))

    async def multicast(
        self,
        data: bytes,
        group: str,
        port: int,
        ttl: int = 4,
    ) -> None:
        """Send a UDP multicast packet.

        Args:
            data: payload bytes
            group: multicast group address (e.g. "239.255.255.250")
            port: destination port
            ttl: time-to-live for the multicast packet
        """
        sock = await self._ensure_socket()
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        # uvloop has no loop.sock_sendto → thread it.
        await asyncio.to_thread(sock.sendto, data, (group, port))

    async def close(self) -> None:
        """Close the socket."""
        if self._sock and not self._closed:
            self._closed = True
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# UDPReceiver — listener
# ---------------------------------------------------------------------------


class UDPReceiver:
    """Async UDP receiver (unicast, multicast, broadcast).

    Provides a callback-based interface for receiving UDP datagrams.

    Usage:
        receiver = UDPReceiver()
        receiver.on_datagram(lambda data, addr: print(f"Got: {data!r} from {addr}"))
        await receiver.listen(port=3483)
    """

    def __init__(
        self,
        port: int = 0,
        host: str = "0.0.0.0",
        multicast_group: str | None = None,
    ):
        self.port = port
        self.host = host
        self.multicast_group = multicast_group
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._callbacks: list[Callable[[bytes, tuple[str, int]], None | Coroutine[Any, Any, None]]] = []

    def on_datagram(
        self,
        callback: Callable[[bytes, tuple[str, int]], None | Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback for received datagrams.

        The callback receives (data: bytes, addr: tuple[str, int]).
        """
        self._callbacks.append(callback)

    def _make_socket(self) -> socket.socket:
        """Create and configure the listening socket."""
        if self.multicast_group:
            # Multicast socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", self.port))
            except OSError:
                sock.bind(("", 0))
            # Join multicast group
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(self.multicast_group),
                socket.inet_aton("0.0.0.0"),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        else:
            # Unicast / broadcast socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
        return sock

    async def _recv_loop(self, sock: socket.socket) -> None:
        """Main receive loop."""
        # Blocking mode: recvfrom runs in a worker thread and must block
        # until a datagram arrives (non-blocking would return EAGAIN).
        sock.setblocking(True)
        while self._running:
            try:
                # uvloop has no loop.sock_recvfrom → thread it.
                data, addr = await asyncio.to_thread(sock.recvfrom, 65536)
                for cb in self._callbacks:
                    try:
                        result = cb(data, addr)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.error("UDP callback error: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("UDP recv error: %s", exc)
                await asyncio.sleep(0.5)

    async def listen(self, port: int | None = None) -> None:
        """Start listening for UDP datagrams.

        Args:
            port: port to bind to (overrides constructor value if provided)
        """
        if port is not None:
            self.port = port
        if self._running:
            return
        self._running = True
        self._sock = self._make_socket()
        actual_port = self._sock.getsockname()[1]
        logger.info(
            "UDPReceiver listening on %s:%d%s",
            self.host,
            actual_port,
            f" (multicast {self.multicast_group})" if self.multicast_group else "",
        )
        self._task = asyncio.create_task(self._recv_loop(self._sock))

    async def stop(self) -> None:
        """Stop listening and close the socket."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        logger.info("UDPReceiver stopped")
