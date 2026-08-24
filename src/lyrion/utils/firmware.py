"""
Firmware versioning utilities for Pyrion Music Server.

Handles Squeezebox player firmware version comparisons and compatibility.
Firmware versions follow the LMS major.minor.patch.build format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------

@dataclass
class FirmwareVersion:
    """
    Parsed firmware version number.

    Versions follow the format: major.minor.patch.build
    Examples: "127", "137", "160", "161.1", "7.7.2", "9.0.0.12345"
    """
    major: int = 0
    minor: int = 0
    patch: int = 0
    build: int = 0
    raw: str = ""

    @classmethod
    def parse(cls, version_str: str) -> FirmwareVersion:
        """Parse a firmware version string."""
        result = cls()
        result.raw = str(version_str).strip()

        # Remove leading 'v' if present
        version_str = version_str.lstrip("vV")

        # Try to split by dots
        parts = version_str.split(".")

        try:
            result.major = int(parts[0]) if len(parts) > 0 else 0
            result.minor = int(parts[1]) if len(parts) > 1 else 0
            result.patch = int(parts[2]) if len(parts) > 2 else 0
            result.build = int(parts[3]) if len(parts) > 3 else 0
        except (ValueError, IndexError):
            pass

        return result

    def __str__(self) -> str:
        if self.build:
            return f"{self.major}.{self.minor}.{self.patch}.{self.build}"
        if self.patch:
            return f"{self.major}.{self.minor}.{self.patch}"
        return f"{self.major}.{self.minor}"

    def __repr__(self) -> str:
        return f"FirmwareVersion({self.major}.{self.minor}.{self.patch}.{self.build})"

    def __lt__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() < other._tuple()

    def __le__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() <= other._tuple()

    def __gt__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() > other._tuple()

    def __ge__(self, other: FirmwareVersion | str) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        return self._tuple() >= other._tuple()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            other = FirmwareVersion.parse(other)
        if not isinstance(other, FirmwareVersion):
            return False
        return self._tuple() == other._tuple()

    def __hash__(self) -> int:
        return hash(self._tuple())

    def _tuple(self) -> tuple[int, int, int, int]:
        return (self.major, self.minor, self.patch, self.build)


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

# LMS / Lyrion version mapping to minimum firmware requirements
MIN_FIRMWARE_REQUIREMENTS: dict[str, str] = {
    # (lms_version_major, lms_version_minor): minimum_player_firmware
    (7, 0): "127",
    (7, 5): "137",
    (7, 6): "150",
    (7, 7): "155",
    (7, 8): "160",
    (7, 9): "161",
    (8, 0): "161",
    (9, 0): "161",
    (9, 2): "161",
}


def get_min_firmware_for_lms_version(lms_major: int, lms_minor: int) -> FirmwareVersion:
    """Return minimum player firmware version for a given LMS version."""
    key = (lms_major, lms_minor)
    min_version_str = MIN_FIRMWARE_REQUIREMENTS.get(key, "127")
    return FirmwareVersion.parse(min_version_str)


def is_player_compatible(
    player_firmware: str | FirmwareVersion,
    lms_version: str | FirmwareVersion,
) -> bool:
    """
    Return True if a player firmware is compatible with the LMS version.
    """
    if isinstance(player_firmware, str):
        player_firmware = FirmwareVersion.parse(player_firmware)
    if isinstance(lms_version, str):
        lms_version = FirmwareVersion.parse(lms_version)

    min_fw = get_min_firmware_for_lms_version(lms_version.major, lms_version.minor)
    return player_firmware >= min_fw


def needs_upgrade(
    player_firmware: str | FirmwareVersion,
    lms_version: str | FirmwareVersion,
) -> tuple[bool, str]:
    """
    Check if a player needs a firmware upgrade.

    Returns (needs_upgrade: bool, message: str)
    """
    if isinstance(player_firmware, str):
        player_firmware = FirmwareVersion.parse(player_firmware)
    if isinstance(lms_version, str):
        lms_version = FirmwareVersion.parse(lms_version)

    min_fw = get_min_firmware_for_lms_version(lms_version.major, lms_version.minor)

    if player_firmware >= min_fw:
        return False, "Compatible"

    return True, (
        f"Player firmware {player_firmware} is older than recommended "
        f"minimum {min_fw} for LMS {lms_version}. "
        "Upgrade recommended."
    )


# ---------------------------------------------------------------------------
# Firmware URL helpers
# ---------------------------------------------------------------------------

DEFAULT_FIRMWARE_BASE = "https://update.squeezebox.com/network/FirmwareUpdate"


def get_firmware_url(
    player_model: str,
    current_version: str | FirmwareVersion,
) -> str:
    """
    Construct a firmware update check URL.

    The LMS firmware update system uses HTTP GET with query parameters
    to check for available updates.
    """
    if isinstance(current_version, FirmwareVersion):
        current_version = str(current_version)

    # Query parameters matching the LMS SlimProto update mechanism
    params = [
        ("playerid", player_model),
        ("revision", current_version),
    ]
    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{DEFAULT_FIRMWARE_BASE}?{query}"


def parse_firmware_response(data: bytes | str) -> dict[str, Any] | None:
    """
    Parse the binary firmware update response from the LMS server.

    Returns None if no update available, or a dict with update info.
    """
    if isinstance(data, bytes):
        data = data.decode("latin-1")

    # The LMS binary response format: "UPD" followed by length-prefixed fields
    if not data.startswith("UPD"):
        return None

    # Simplified parse — real implementation would handle binary protocol
    return None


# ---------------------------------------------------------------------------
# Binary protocol helpers
# ---------------------------------------------------------------------------

def encode_firmware_request(
    player_id: str,
    revision: str,
) -> bytes:
    """
    Encode a firmware version request in the SlimProto binary format.
    """
    request = f"FRM:{player_id}:{revision}\r\n"
    return request.encode("latin-1")


def decode_firmware_response(data: bytes) -> dict[str, Any] | None:
    """
    Decode a firmware version response from the SlimProto protocol.

    Format: 4-byte type + 4-byte length + payload
    """
    if len(data) < 8:
        return None

    msg_type = data[:4]
    msg_len = int.from_bytes(data[4:8], "big")

    if len(data) < 8 + msg_len:
        return None

    payload = data[8 : 8 + msg_len]

    if msg_type == b"upd?":
        # Update query response
        parts = payload.decode("latin-1").split(":")
        if len(parts) >= 2:
            return {
                "has_update": parts[0] == "1",
                "version": parts[1] if len(parts) > 1 else None,
            }

    return None
