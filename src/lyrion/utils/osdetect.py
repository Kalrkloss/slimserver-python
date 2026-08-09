"""
OS detection utilities for Lyrion Music Server.

Provides accurate OS family detection mirroring Slim::Utils::OSDetect.
"""

from __future__ import annotations

import os
import sys
import platform
import subprocess
from pathlib import Path
from typing import NamedTuple


# ---- OS family constants ----

class OSFamily(NamedTuple):
    WINDOWS = "MSWin32"
    MAC = "darwin"
    LINUX = "linux"
    FREEBSD = "freebsd"
    NETBSD = "netbsd"
    OPENBSD = "openbsd"
    SOLARIS = "sunos"
    BSD = "bsd"


WINDOWS = OSFamily.WINDOWS
MAC = OSFamily.MAC
LINUX = OSFamily.LINUX
FREEBSD = OSFamily.FREEBSD
NETBSD = OSFamily.NETBSD
OPENBSD = OSFamily.OPENBSD
SOLARIS = OSFamily.SOLARIS
BSD = OSFamily.BSD

ALL_BSD = {FREEBSD, NETBSD, OPENBSD}


# ---- Detection ----

def get_os() -> str:
    """Return the detected OS family string."""
    system = platform.system().lower()
    if system == "windows":
        return WINDOWS
    elif system == "darwin":
        return MAC
    elif system == "linux":
        return LINUX
    elif system == "freebsd":
        return FREEBSD
    elif system == "netbsd":
        return NETBSD
    elif system == "openbsd":
        return OPENBSD
    elif system == "sunos":
        return SOLARIS
    return system


def get_detail() -> str:
    """Return detailed OS info: family-version-architecture."""
    return platform.platform()


def get_arch() -> str:
    """Return the CPU architecture."""
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        return "x86_64"
    if arch in {"i386", "i686", "x86"}:
        return "i386"
    if arch.startswith("arm"):
        return "arm"
    if arch.startswith("aarch64"):
        return "aarch64"
    if arch.startswith("riscv"):
        return "riscv"
    return arch


def get_kernel_version() -> tuple[int, int, int]:
    """Return the kernel version as (major, minor, patch)."""
    return platform.version().split(".")[:3]  # type: ignore[return-value]


# ---- Booleans ----

CURRENT_OS = get_os()

IS_WINDOWS = CURRENT_OS == WINDOWS
IS_MAC = CURRENT_OS == MAC
IS_LINUX = CURRENT_OS == LINUX
IS_UNIX = not IS_WINDOWS
IS_BSD = CURRENT_OS in ALL_BSD
IS_SOLARIS = CURRENT_OS == SOLARIS


# ---- Directory helpers ----

def get_home_dir() -> Path:
    """Return the user's home directory."""
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        return Path(home)
    return Path.home()


def get_temp_dir() -> Path:
    """Return the system temp directory."""
    import tempfile
    return Path(tempfile.gettempdir())


def get_app_dir() -> Path:
    """Return the platform-specific application data directory."""
    if IS_WINDOWS:
        base = os.environ.get("PROGRAMDATA", "C:/ProgramData")
        return Path(base) / "Lyrion"
    elif IS_MAC:
        return get_home_dir() / "Library" / "Application Support" / "Lyrion"
    elif IS_LINUX:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "lyrion"
        return get_home_dir() / ".local" / "share" / "lyrion"
    else:
        return get_home_dir() / ".lyrion"


def get_log_dir() -> Path:
    """Return the log directory."""
    if IS_WINDOWS:
        base = os.environ.get("PROGRAMDATA", "C:/ProgramData")
        return Path(base) / "Lyrion" / "Logs"
    elif IS_MAC:
        return get_home_dir() / "Library" / "Logs" / "Lyrion"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "lyrion" / "logs"
        return get_home_dir() / ".lyrion" / "Logs"


def get_cache_dir() -> Path:
    """Return the cache directory."""
    if IS_MAC:
        return get_home_dir() / "Library" / "Caches" / "Lyrion"
    elif IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", str(get_home_dir())))
        return Path(base) / "Lyrion" / "Cache"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            return Path(xdg_cache) / "lyrion"
        return get_home_dir() / ".cache" / "lyrion"


def get_pid_dir() -> Path:
    """Return the directory for PID files."""
    if IS_UNIX and os.getuid() == 0:
        return Path("/run")
    return get_app_dir()


# ---- System info ----

def cpu_count() -> int:
    """Return the number of CPU cores."""
    return os.cpu_count() or 1


def memory_total() -> int | None:
    """Return total physical memory in bytes (Unix)."""
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            c_ulong = ctypes.c_ulong  # type: ignore[attr-defined]
            class MEMORYSTATUS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", c_ulong),
                    ("dwMemoryLoad", c_ulong),
                    ("dwTotalPhys", c_ulong),
                    ("dwAvailPhys", c_ulong),
                    ("dwTotalPageFile", c_ulong),
                    ("dwAvailPageFile", c_ulong),
                    ("dwTotalVirtual", c_ulong),
                    ("dwAvailVirtual", c_ulong),
                ]
            stat = MEMORYSTATUS()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUS)
            kernel32.GlobalMemoryStatus(ctypes.byref(stat))
            return stat.dwTotalPhys
        except Exception:
            return None
    elif IS_UNIX:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            pass
    return None


def hostname() -> str:
    """Return the system hostname."""
    return platform.node()


def has_systemd() -> bool:
    """Return True if systemd is running (Linux)."""
    if not IS_LINUX:
        return False
    try:
        return Path("/run/systemd/system").exists()
    except OSError:
        return False


def is_wsl() -> bool:
    """Return True if running under WSL (Windows Subsystem for Linux)."""
    if not IS_LINUX:
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def is_docker() -> bool:
    """Return True if running inside a Docker container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        with open("/proc/1/cgroup") as f:
            return "docker" in f.read().lower()
    except OSError:
        return False
    return False


def is_flatpak() -> bool:
    """Return True if running inside a Flatpak sandbox."""
    return Path("/.flatpak-info").exists()
