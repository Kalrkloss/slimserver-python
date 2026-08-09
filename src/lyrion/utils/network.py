"""
Network utilities for Lyrion Music Server.

Ported from Slim::Utils::Network. Provides network address resolution,
interface discovery, HTTP client helpers, and port management.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("lyrion.network")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class NetworkInterface:
    """A network interface with its addresses."""
    name: str
    mac: str | None
    ipv4_address: str | None
    ipv6_address: str | None
    is_loopback: bool
    is_up: bool


# ---------------------------------------------------------------------------
# Address utilities
# ---------------------------------------------------------------------------

def is_loopback_addr(addr: str) -> bool:
    """Return True if address is a loopback address."""
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr in {"localhost", "::1", "127.0.0.1"}


def is_private_addr(addr: str) -> bool:
    """Return True if address is in private/routed range."""
    try:
        ip = ipaddress.ip_address(addr)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def normalize_address(addr: str, default_port: int = 0) -> tuple[str, int]:
    """Parse 'host:port' into (host, port), with defaults."""
    if addr.count(":") > 1 and not addr.startswith("["):
        # IPv6 without brackets — rare, try to split on last :
        last_colon = addr.rfind(":")
        host = addr[:last_colon]
        port_str = addr[last_colon + 1 :]
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    elif addr.startswith("["):
        # IPv6 with brackets: [::1]:8080
        bracket_end = addr.find("]")
        if bracket_end != -1:
            host = addr[1:bracket_end]
            rest = addr[bracket_end + 1 :]
            if rest.startswith(":"):
                try:
                    port = int(rest[1:])
                except ValueError:
                    port = default_port
            else:
                port = default_port
        else:
            host = addr[1:]
            port = default_port
    elif ":" in addr:
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        host = addr
        port = default_port

    return host, port


def format_address(host: str, port: int) -> str:
    """Format a host/port pair as a string."""
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def get_local_ip(target: str = "8.8.8.8") -> str:
    """
    Determine the local IP address that would be used to reach target.
    Uses a UDP connection trick (no actual data sent).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect((target, 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except OSError:
        return "127.0.0.1"


def get_all_addresses() -> list[str]:
    """Return all non-loopback IP addresses on the machine."""
    addresses: list[str] = []
    try:
        for iface in get_network_interfaces():
            if iface.ipv4_address and not iface.is_loopback:
                addresses.append(iface.ipv4_address)
            if iface.ipv6_address and not iface.is_loopback:
                addresses.append(iface.ipv6_address)
    except Exception:
        addresses.append(get_local_ip())
    if not addresses:
        addresses.append("127.0.0.1")
    return addresses


# ---------------------------------------------------------------------------
# Interface discovery
# ---------------------------------------------------------------------------

def get_network_interfaces() -> list[NetworkInterface]:
    """Discover all network interfaces on this machine."""
    interfaces: list[NetworkInterface] = []

    try:
        # Unix: use ip or ifconfig
        result = subprocess.run(
            ["ip", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            interfaces = _parse_ip_addr(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass

    if not interfaces:
        # Fall back to socket module
        try:
            hostname = socket.gethostname()
            addrs = socket.getaddrinfo(hostname, None)
            for addr_family, _, _, _, sockaddr in addrs:
                ip, _ = sockaddr[:2]
                if ip not in {iface.ipv4_address for iface in interfaces}:
                    interfaces.append(NetworkInterface(
                        name="default",
                        mac=None,
                        ipv4_address=ip if addr_family == socket.AF_INET else None,
                        ipv6_address=ip if addr_family == socket.AF_INET6 else None,
                        is_loopback=is_loopback_addr(ip),
                        is_up=True,
                    ))
        except Exception:
            pass

    return interfaces


def _parse_ip_addr(output: str) -> list[NetworkInterface]:
    """Parse `ip addr show` output into NetworkInterface objects."""
    interfaces: dict[str, NetworkInterface] = {}
    current_name: str | None = None

    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue

        # New interface: "1: lo: ..."
        if parts[0].endswith(":") and "." not in parts[0]:
            current_name = parts[1].rstrip(":")
            flags_str = " ".join(parts[2:])
            is_up = "UP" in flags_str.upper()
            is_loopback = "LOOPBACK" in flags_str.upper()
            interfaces[current_name] = NetworkInterface(
                name=current_name,
                mac=None,
                ipv4_address=None,
                ipv6_address=None,
                is_loopback=is_loopback,
                is_up=is_up,
            )

        # Address line: "    inet 192.168.1.1/24 ..."
        elif parts[0] == "inet":
            addr_cidr = parts[1]
            addr = addr_cidr.split("/")[0]
            if current_name and current_name in interfaces:
                if ":" not in addr:
                    interfaces[current_name].ipv4_address = addr
                else:
                    interfaces[current_name].ipv6_address = addr

        # MAC address line: "    link/ether aa:bb:cc:dd:ee:ff ..."
        elif parts[0] == "link/ether":
            mac = parts[1]
            if current_name and current_name in interfaces:
                interfaces[current_name].mac = mac

    return list(interfaces.values())


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None
_http_lock = threading.Lock()


async def get_http_client(
    timeout: float = 30.0,
    user_agent: str | None = None,
) -> httpx.AsyncClient:
    """Return a shared httpx AsyncClient."""
    global _http_client
    if _http_client is None:
        with _http_lock:
            if _http_client is None:
                _http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=True,
                    headers={
                        "User-Agent": user_agent
                        or "LyrionMusicServer/9.2.0",
                    },
                )
    return _http_client


async def http_get(
    url: str,
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Perform an async HTTP GET request."""
    client = await get_http_client(timeout=timeout)
    return await client.get(url, headers=headers or {})


async def http_post(
    url: str,
    *,
    data: dict[str, str] | None = None,
    json: Any = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Perform an async HTTP POST request."""
    client = await get_http_client(timeout=timeout)
    return await client.post(url, data=data, json=json)


async def http_close() -> None:
    """Close the shared HTTP client."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# ---------------------------------------------------------------------------
# Port utilities
# ---------------------------------------------------------------------------

def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Return True if a port is available to bind."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        return False


def find_free_port(start: int = 9000, end: int = 65535, host: str = "0.0.0.0") -> int:
    """Find a free port in the given range."""
    for port in range(start, end + 1):
        if is_port_available(port, host):
            return port
    raise RuntimeError(f"No free port in range {start}-{end}")


async def wait_for_port(
    host: str,
    port: int,
    timeout: float = 5.0,
) -> bool:
    """Wait for a TCP port to become available (server to start)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=0.5,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# DNS / resolution
# ---------------------------------------------------------------------------

async def resolve_hostname(hostname: str) -> list[str]:
    """Resolve a hostname to all IP addresses."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
        return list({info[4][0] for info in infos})
    except Exception:
        return []


def resolve_hostname_sync(hostname: str) -> list[str]:
    """Synchronous hostname resolution."""
    try:
        addr_info = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return list({info[4][0] for info in addr_info})
    except Exception:
        return []


# ---------------------------------------------------------------------------
# SqueezeProto discovery
# ---------------------------------------------------------------------------

DISCOVERY_PORT = 3483
DISCOVERY_BROADCAST_ADDR = "255.255.255.255"


async def send_discovery_broadcast(
    port: int = DISCOVERY_PORT,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """
    Send a SlimProto discovery broadcast and collect responses.

    Returns a list of discovered player info dicts.
    """
    responses: list[dict[str, Any]] = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)

        # Send discovery packet (SqueezeProto v1)
        sock.sendto(b"e\0\0\0\0", (DISCOVERY_BROADCAST_ADDR, port))

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                if len(data) >= 4:
                    responses.append({
                        "addr": addr[0],
                        "port": addr[1],
                        "data": data.hex(),
                    })
            except socket.timeout:
                break

        sock.close()
    except OSError as e:
        logger.debug("Discovery broadcast failed: %s", e)

    return responses


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

def guess_content_type(url: str) -> str:
    """Guess MIME type from URL file extension."""
    ext = Path(url).suffix.lower()
    content_types = {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/opus",
        ".m4a": "audio/mp4",
        ".aac": "audio/mp4",
        ".mp4": "audio/mp4",
        ".wav": "audio/wav",
        ".aiff": "audio/aiff",
        ".aif": "audio/aiff",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".html": "text/html",
        ".htm": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".xml": "application/xml",
    }
    return content_types.get(ext, "application/octet-stream")
