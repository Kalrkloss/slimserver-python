"""Format registry for Lyrion Music Server."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from lyrion.formats.base import AudioFormat


class FormatRegistry:
    """
    Central registry of all supported audio formats.

    Mirrors Slim::Formats — maps file extensions and MIME types to
    AudioFormat objects with streaming / transcoding capabilities.

    Usage::

        registry = FormatRegistry()
        fmt = registry.get_by_extension("flac")
        if fmt.needs_transcode(player):
            transcoded = registry.transcode(fmt, "pcm", source_path)
    """

    __slots__ = ("_formats", "_transcode_rules")

    def __init__(self) -> None:
        self._formats: dict[str, AudioFormat] = {}
        self._transcode_rules: dict[str, dict[str, str]] = {}  # fmt -> player -> cmd template
        self._register_builtin_formats()
        self._register_builtin_transcode_rules()

    # -------------------------------------------------------------------------
    # Registration
    # -------------------------------------------------------------------------

    def register(self, fmt: AudioFormat) -> None:
        """Register an AudioFormat by its primary extension."""
        AudioFormat._registry[fmt.extension.lower()] = fmt
        self._formats[fmt.extension.lower()] = fmt

    def register_transcode_rule(
        self,
        source_ext: str,
        target_ext: str,
        command_template: str,
        *,
        player_hint: str = "default",
    ) -> None:
        """
        Register a transcoding rule.

        Parameters
        ----------
        source_ext
            Source format extension (e.g. "flac").
        target_ext
            Target format extension (e.g. "pcm").
        command_template
            ffmpeg command line template with placeholders:
            ``{input}`` and ``{output}`` are replaced with temp file paths.
        player_hint
            Optional player model hint (e.g. "squeezebox", "cast").
        """
        key = f"{source_ext}:{target_ext}"
        if key not in self._transcode_rules:
            self._transcode_rules[key] = {}
        self._transcode_rules[key][player_hint] = command_template

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def get_by_extension(self, ext: str) -> AudioFormat | None:
        """Return the registered format for a file extension."""
        return self._formats.get(ext.lower().lstrip("."))

    def get_by_mime(self, mime: str) -> AudioFormat | None:
        """Return the registered format for a MIME type."""
        for fmt in self._formats.values():
            if fmt.mime_type == mime:
                return fmt
        return None

    def all_formats(self) -> list[AudioFormat]:
        """Return all registered formats."""
        return list(self._formats.values())

    def get_transcode_command(
        self,
        source_ext: str,
        target_ext: str,
        player_hint: str = "default",
    ) -> str | None:
        """
        Return the transcoding command template for the given conversion.

        Returns ``None`` if no rule is registered.
        """
        key = f"{source_ext.lower()}:{target_ext.lower()}"
        rules = self._transcode_rules.get(key, {})
        return rules.get(player_hint) or rules.get("default")

    # -------------------------------------------------------------------------
    # Actual transcoding via ffmpeg
    # -------------------------------------------------------------------------

    def transcode(
        self,
        source_path: Path,
        target_ext: str,
        player_hint: str = "default",
        *,
        output_dir: Path | None = None,
    ) -> Path | None:
        """
        Transcode a source file to the target format using ffmpeg.

        Parameters
        ----------
        source_path
            Path to the source audio file.
        target_ext
            Target file extension (e.g. "pcm", "mp3").
        player_hint
            Player model hint used to select the right transcoding rule.
        output_dir
            Directory for the output file. Defaults to system temp directory.

        Returns
        -------
        Path to the transcoded file, or ``None`` if ffmpeg is unavailable or
        no rule matches.
        """
        import tempfile

        cmd_tpl = self.get_transcode_command(source_path.suffix.lstrip("."), target_ext, player_hint)
        if not cmd_tpl:
            return None

        out_dir = output_dir or Path(tempfile.gettempdir())
        out_path = out_dir / f"lyrion_transcode_{source_path.stem}.{target_ext}"

        cmd = cmd_tpl.format(input=str(source_path), output=str(out_path))
        try:
            subprocess.run(
                cmd,
                shell=True,
                check=True,
                capture_output=True,
            )
            return out_path
        except (subprocess.CalledProcessError, OSError):
            return None

    # -------------------------------------------------------------------------
    # Built-in format definitions
    # -------------------------------------------------------------------------

    def _register_builtin_formats(self) -> None:
        """Register the built-in audio formats."""
        formats: list[AudioFormat] = [
            # Lossless
            AudioFormat(
                extension="flac",
                mime_type="audio/flac",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="FLAC (Free Lossless Audio Codec)",
            ),
            AudioFormat(
                extension="wav",
                mime_type="audio/wav",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="WAV (Uncompressed PCM)",
            ),
            AudioFormat(
                extension="aiff",
                mime_type="audio/aiff",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="AIFF (Apple Interchange File Format)",
            ),
            AudioFormat(
                extension="alac",
                mime_type="audio/mp4",
                can_stream=True,
                can_direct_stream=False,
                needs_transcode=True,
                description="ALAC (Apple Lossless)",
            ),
            # Lossy
            AudioFormat(
                extension="mp3",
                mime_type="audio/mpeg",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="MP3 (MPEG-1/2 Layer III)",
            ),
            AudioFormat(
                extension="aac",
                mime_type="audio/aac",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="AAC (Advanced Audio Coding)",
            ),
            AudioFormat(
                extension="m4a",
                mime_type="audio/mp4",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="M4A (AAC in MP4 container)",
            ),
            AudioFormat(
                extension="ogg",
                mime_type="audio/ogg",
                can_stream=True,
                can_direct_stream=False,
                needs_transcode=True,
                description="OGG Vorbis",
            ),
            AudioFormat(
                extension="opus",
                mime_type="audio/opus",
                can_stream=True,
                can_direct_stream=False,
                needs_transcode=True,
                description="Opus Audio",
            ),
            AudioFormat(
                extension="wma",
                mime_type="audio/x-ms-wma",
                can_stream=True,
                can_direct_stream=False,
                needs_transcode=True,
                description="Windows Media Audio",
            ),
            # PCM / streamable
            AudioFormat(
                extension="pcm",
                mime_type="audio/L16",
                can_stream=True,
                can_direct_stream=True,
                needs_transcode=False,
                description="Raw PCM (16-bit LE)",
            ),
        ]

        for fmt in formats:
            self.register(fmt)

    def _register_builtin_transcode_rules(self) -> None:
        """Register built-in ffmpeg transcoding rules."""

        # FLAC → PCM (for Squeezebox native playback)
        self.register_transcode_rule(
            "flac",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="squeezebox",
        )
        self.register_transcode_rule(
            "flac",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="default",
        )

        # ALAC → PCM
        self.register_transcode_rule(
            "alac",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="squeezebox",
        )

        # OGG → PCM
        self.register_transcode_rule(
            "ogg",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="squeezebox",
        )

        # Opus → PCM
        self.register_transcode_rule(
            "opus",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 48000 -ac 2 {output}",
            player_hint="squeezebox",
        )

        # WMA → PCM
        self.register_transcode_rule(
            "wma",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="squeezebox",
        )

        # AIFF → PCM
        self.register_transcode_rule(
            "aiff",
            "pcm",
            "ffmpeg -i {input} -f s16le -acodec pcm_s16le -ar 44100 -ac 2 {output}",
            player_hint="squeezebox",
        )

        # MP3 passthrough (no transcode needed, just copy)
        self.register_transcode_rule(
            "mp3",
            "mp3",
            "ffmpeg -i {input} -acodec copy {output}",
            player_hint="default",
        )

        # FLAC → MP3 (for Chromecast / cast players)
        self.register_transcode_rule(
            "flac",
            "mp3",
            "ffmpeg -i {input} -acodec libmp3lame -q:a 2 {output}",
            player_hint="cast",
        )

        # AAC → MP3
        self.register_transcode_rule(
            "aac",
            "mp3",
            "ffmpeg -i {input} -acodec libmp3lame -q:a 2 {output}",
            player_hint="cast",
        )


# --------------------------------------------------------------------------+
# Module-level singleton registry                                          |
# --------------------------------------------------------------------------+

_registry: FormatRegistry | None = None


def get_registry() -> FormatRegistry:
    """Return the module-level FormatRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = FormatRegistry()
    return _registry
