"""Base audio format representation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass
class AudioFormat:
    """
    Represents a supported audio format.

    Attributes
    ----------
    extension : str
        File extension without the leading dot (e.g. "mp3", "flac").
    mime_type : str
        MIME type for HTTP streaming (e.g. "audio/mpeg").
    can_stream : bool
        Whether the format can be served directly to a player via HTTP.
    can_direct_stream : bool
        Whether the player can decode it natively without transcoding.
    needs_transcode : bool
        Whether this format must be transcoded for at least one player type.
    description : str
        Human-readable description of the format.
    """

    extension: str
    mime_type: str
    can_stream: bool = True
    can_direct_stream: bool = True
    needs_transcode: bool = False
    description: str = ""

    # Class-level registry populated by FormatRegistry
    _registry: ClassVar[dict[str, AudioFormat]] = {}

    def __post_init__(self) -> None:
        if not self.description:
            self.description = self.extension.upper()

    @property
    def extension_dot(self) -> str:
        """Return the extension with a leading dot."""
        return f".{self.extension}"

    @property
    def is_lossless(self) -> bool:
        """Return True for lossless formats (FLAC, WAV, AIFF, ALAC)."""
        return self.extension.lower() in {"flac", "wav", "aiff", "alac", "ape", "wmal"}

    @property
    def is_lossy(self) -> bool:
        """Return True for lossy formats (MP3, AAC, OGG, WMA, OPUS)."""
        return self.extension.lower() in {"mp3", "aac", "m4a", "ogg", "wma", "opus"}

    # ----- magic-number detection -----

    MAGIC_SIGNATURES: ClassVar[dict[bytes, str]] = {
        b"ID3": "mp3",
        b"\xff\xfb": "mp3",
        b"\xff\xfa": "mp3",
        b"\xff\xf3": "mp3",
        b"\xff\xf2": "mp3",
        b"fLaC": "flac",
        b"OggS": "ogg",
        b"RIFF": "wav",   # may be WAV or AVI; subclass detection needed
        b"frmA": "aiff",  # not standard but used by some AIFF handlers
        b"ID3 ": "mp3",   # MP3 with ID3v2.4
        b"@FLAC": "flac", # older FLAC variant
        b"OpusHead": "opus",
        # M4A/AAC usually don't have a reliable magic header in first bytes;
        # we rely on extension + container inspection.
    }

    @classmethod
    def detect_from_file(cls, path: Path) -> AudioFormat | None:
        """
        Attempt to detect the audio format from the first few bytes of a file.

        Falls back to extension-based lookup if magic detection is inconclusive.
        Returns None if the format is not registered.
        """
        try:
            with open(path, "rb") as fh:
                header = fh.read(16)
        except OSError:
            return None

        # Try magic detection
        for magic, fmt_ext in cls.MAGIC_SIGNATURES.items():
            if header.startswith(magic):
                return cls._registry.get(fmt_ext)

        # Fall back to extension
        ext = path.suffix.lstrip(".").lower()
        if ext == "m4a":
            ext = "aac"
        return cls._registry.get(ext)

    @classmethod
    def get(cls, extension: str) -> AudioFormat | None:
        """Return the registered format for a given extension."""
        return cls._registry.get(extension.lower().lstrip("."))
