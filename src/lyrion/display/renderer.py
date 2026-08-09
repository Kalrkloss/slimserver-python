"""Display renderer for player LCD/VFD screens."""
from __future__ import annotations

import logging
import math
import struct
from pathlib import Path
from typing import Generator, Literal, Optional

logger = logging.getLogger(__name__)

# Colour565 packed as 2 bytes: RRRR RGGG GGGB BBBB
_Black = 0x0000
_White = 0xFFFF
_Fill = 0x0000  # Default fill colour

# Built-in 5x7 bitmap font (ASCII 0x20–0x7E)
_FONT_5X7: dict[int, bytes] = {}
_BUILTIN_FONT_LOADED = False


def _load_builtin_font() -> None:
    global _BUILTIN_FONT_LOADED, _FONT_5X7
    if _BUILTIN_FONT_LOADED:
        return
    _BUILTIN_FONT_LOADED = True
    # Each glyph is 5 bytes (one per column), rows 0-6, row 7 unused
    _GLYPHS = {
        0x20: bytes([0x00, 0x00, 0x00, 0x00, 0x00]),  # space
        0x21: bytes([0x00, 0x00, 0x5F, 0x00, 0x00]),  # !
        0x22: bytes([0x00, 0x07, 0x03, 0x07, 0x00]),  # "
        0x23: bytes([0x14, 0x7F, 0x14, 0x7F, 0x14]),  # #
        0x24: bytes([0x24, 0x2E, 0x69, 0x2A, 0x10]),  # $
        0x25: bytes([0x46, 0x26, 0x10, 0x0C, 0x62]),  # %
        0x26: bytes([0x36, 0x49, 0x55, 0x22, 0x50]),  # &
        0x27: bytes([0x00, 0x00, 0x07, 0x00, 0x00]),  # '
        0x28: bytes([0x00, 0x1C, 0x22, 0x41, 0x00]),  # (
        0x29: bytes([0x00, 0x41, 0x22, 0x1C, 0x00]),  # )
        0x2A: bytes([0x14, 0x08, 0x3E, 0x08, 0x14]),  # *
        0x2B: bytes([0x08, 0x08, 0x3E, 0x08, 0x08]),  # +
        0x2C: bytes([0x00, 0x50, 0x30, 0x00, 0x00]),  # ,
        0x2D: bytes([0x08, 0x08, 0x08, 0x08, 0x08]),  # -
        0x2E: bytes([0x00, 0x30, 0x30, 0x00, 0x00]),  # .
        0x2F: bytes([0x20, 0x10, 0x08, 0x04, 0x02]),  # /
        0x30: bytes([0x3E, 0x51, 0x49, 0x45, 0x3E]),  # 0
        0x31: bytes([0x00, 0x00, 0x4F, 0x00, 0x00]),  # 1
        0x32: bytes([0x00, 0x27, 0x45, 0x45, 0x39]),  # 2
        0x33: bytes([0x00, 0x22, 0x49, 0x49, 0x36]),  # 3
        0x34: bytes([0x0C, 0x0A, 0x7F, 0x0A, 0x0C]),  # 4
        0x35: bytes([0x72, 0x51, 0x51, 0x51, 0x4E]),  # 5
        0x36: bytes([0x1E, 0x29, 0x49, 0x49, 0x06]),  # 6
        0x37: bytes([0x40, 0x4F, 0x08, 0x10, 0x60]),  # 7
        0x38: bytes([0x36, 0x49, 0x49, 0x49, 0x36]),  # 8
        0x39: bytes([0x32, 0x49, 0x49, 0x29, 0x1E]),  # 9
        0x3A: bytes([0x00, 0x00, 0x36, 0x36, 0x00]),  # :
        0x3B: bytes([0x00, 0x50, 0x30, 0x10, 0x00]),  # ;
        0x3C: bytes([0x08, 0x14, 0x22, 0x41, 0x00]),  # <
        0x3D: bytes([0x14, 0x14, 0x14, 0x14, 0x14]),  # =
        0x3E: bytes([0x00, 0x41, 0x22, 0x14, 0x08]),  # >
        0x3F: bytes([0x00, 0x47, 0x09, 0x09, 0x3F]),  # ?
        0x40: bytes([0x36, 0x49, 0x59, 0x29, 0x36]),  # @
        0x41: bytes([0x7E, 0x09, 0x09, 0x09, 0x7E]),  # A
        0x42: bytes([0x7F, 0x49, 0x49, 0x49, 0x36]),  # B
        0x43: bytes([0x3E, 0x41, 0x41, 0x41, 0x22]),  # C
        0x44: bytes([0x7F, 0x41, 0x41, 0x22, 0x1C]),  # D
        0x45: bytes([0x7F, 0x49, 0x49, 0x49, 0x41]),  # E
        0x46: bytes([0x7F, 0x09, 0x09, 0x09, 0x01]),  # F
        0x47: bytes([0x3E, 0x41, 0x49, 0x49, 0x7A]),  # G
        0x48: bytes([0x7F, 0x08, 0x08, 0x08, 0x7F]),  # H
        0x49: bytes([0x00, 0x41, 0x7F, 0x41, 0x00]),  # I
        0x4A: bytes([0x20, 0x40, 0x41, 0x3F, 0x01]),  # J
        0x4B: bytes([0x7F, 0x08, 0x14, 0x22, 0x41]),  # K
        0x4C: bytes([0x7F, 0x40, 0x40, 0x40, 0x40]),  # L
        0x4D: bytes([0x7F, 0x02, 0x0C, 0x02, 0x7F]),  # M
        0x4E: bytes([0x7F, 0x04, 0x08, 0x10, 0x7F]),  # N
        0x4F: bytes([0x3E, 0x41, 0x41, 0x41, 0x3E]),  # O
        0x50: bytes([0x7F, 0x09, 0x09, 0x09, 0x06]),  # P
        0x51: bytes([0x3E, 0x41, 0x51, 0x21, 0x5E]),  # Q
        0x52: bytes([0x7F, 0x09, 0x19, 0x29, 0x46]),  # R
        0x53: bytes([0x26, 0x49, 0x49, 0x49, 0x32]),  # S
        0x54: bytes([0x01, 0x01, 0x7F, 0x01, 0x01]),  # T
        0x55: bytes([0x3F, 0x40, 0x40, 0x40, 0x3F]),  # U
        0x56: bytes([0x1F, 0x20, 0x40, 0x20, 0x1F]),  # V
        0x57: bytes([0x3F, 0x40, 0x30, 0x40, 0x3F]),  # W
        0x58: bytes([0x63, 0x14, 0x08, 0x14, 0x63]),  # X
        0x59: bytes([0x07, 0x08, 0x70, 0x08, 0x07]),  # Y
        0x5A: bytes([0x61, 0x51, 0x49, 0x45, 0x43]),  # Z
        0x5B: bytes([0x00, 0x00, 0x7F, 0x41, 0x00]),  # [
        0x5C: bytes([0x02, 0x04, 0x08, 0x10, 0x20]),  # backslash
        0x5D: bytes([0x00, 0x41, 0x7F, 0x00, 0x00]),  # ]
        0x5E: bytes([0x04, 0x02, 0x01, 0x02, 0x04]),  # ^
        0x5F: bytes([0x40, 0x40, 0x40, 0x40, 0x40]),  # _
        0x60: bytes([0x00, 0x01, 0x02, 0x04, 0x00]),  # `
        0x61: bytes([0x20, 0x54, 0x54, 0x78, 0x40]),  # a
        0x62: bytes([0x7F, 0x28, 0x44, 0x44, 0x38]),  # b
        0x63: bytes([0x38, 0x44, 0x44, 0x44, 0x20]),  # c
        0x64: bytes([0x38, 0x44, 0x44, 0x28, 0x7F]),  # d
        0x65: bytes([0x38, 0x54, 0x54, 0x54, 0x18]),  # e
        0x66: bytes([0x08, 0x7E, 0x09, 0x01, 0x02]),  # f
        0x67: bytes([0x0C, 0x52, 0x52, 0x4C, 0x38]),  # g
        0x68: bytes([0x7F, 0x08, 0x04, 0x04, 0x78]),  # h
        0x69: bytes([0x00, 0x44, 0x7D, 0x40, 0x00]),  # i
        0x6A: bytes([0x20, 0x40, 0x44, 0x3D, 0x00]),  # j
        0x6B: bytes([0x7F, 0x10, 0x28, 0x44, 0x00]),  # k
        0x6C: bytes([0x00, 0x41, 0x7F, 0x40, 0x00]),  # l
        0x6D: bytes([0x7C, 0x04, 0x78, 0x04, 0x78]),  # m
        0x6E: bytes([0x7C, 0x08, 0x04, 0x04, 0x78]),  # n
        0x6F: bytes([0x38, 0x44, 0x44, 0x44, 0x38]),  # o
        0x70: bytes([0x7C, 0x14, 0x14, 0x14, 0x08]),  # p
        0x71: bytes([0x08, 0x14, 0x14, 0x14, 0x7C]),  # q
        0x72: bytes([0x7C, 0x08, 0x04, 0x04, 0x08]),  # r
        0x73: bytes([0x48, 0x54, 0x54, 0x54, 0x24]),  # s
        0x74: bytes([0x04, 0x3F, 0x44, 0x40, 0x20]),  # t
        0x75: bytes([0x3C, 0x40, 0x40, 0x20, 0x7C]),  # u
        0x76: bytes([0x1C, 0x20, 0x40, 0x20, 0x1C]),  # v
        0x77: bytes([0x3C, 0x40, 0x30, 0x40, 0x3C]),  # w
        0x78: bytes([0x44, 0x28, 0x10, 0x28, 0x44]),  # x
        0x79: bytes([0x0C, 0x50, 0x50, 0x50, 0x3C]),  # y
        0x7A: bytes([0x44, 0x64, 0x54, 0x4C, 0x44]),  # z
        0x7B: bytes([0x00, 0x08, 0x36, 0x41, 0x00]),  # {
        0x7C: bytes([0x00, 0x00, 0x77, 0x00, 0x00]),  # |
        0x7D: bytes([0x00, 0x41, 0x36, 0x08, 0x00]),  # }
        0x7E: bytes([0x08, 0x08, 0x2A, 0x1C, 0x08]),  # ~
    }
    _FONT_5X7.update(_GLYPHS)


_load_builtin_font()


class DisplayRenderer:
    """Frame-buffer renderer for player LCD/VFD screens.

    Supports a configurable display geometry (default 320x32 for classic
    VFD, but can be any size). Provides primitive drawing operations and
    text rendering via a built-in 5×7 bitmap font. Output is a packed
    16-bit RGB565 byte buffer via render().
    """

    def __init__(
        self,
        width: int = 320,
        height: int = 32,
        bpp: int = 16,
        brightness: int = 100,
    ) -> None:
        self.width = width
        self.height = height
        self.bpp = bpp
        self.brightness = max(0, min(100, brightness))
        self.mode: Literal[
            "nowplaying", "menu", "volume", "screensaver", "clock"
        ] = "nowplaying"

        self._framebuffer: list[int] = [_Black] * (width * height)
        self._dirty = True
        self._font_cache: dict[str, dict[int, bytes]] = {"standard": _FONT_5X7}
        self._graphics_dir = Path(__file__).parent.parent.parent.parent / "graphics"
        self._scroll_state: dict = {}

    def clear(self) -> bytes:
        """Clear the framebuffer and return the empty buffer."""
        self._framebuffer = [_Black] * (self.width * self.height)
        self._dirty = True
        return self.render()

    def set_pixel(self, x: int, y: int, color: int) -> None:
        """Set a single pixel to a packed RGB565 colour.

        Silently ignores out-of-bounds coordinates.
        """
        if 0 <= x < self.width and 0 <= y < self.height:
            self._framebuffer[y * self.width + x] = color
            self._dirty = True

    def get_pixel(self, x: int, y: int) -> int:
        """Return the packed RGB565 colour at (x, y), or Black if OOB."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._framebuffer[y * self.width + x]
        return _Black

    def draw_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: int,
        filled: bool = False,
    ) -> None:
        """Draw a rectangle.

        Args:
            x, y: Top-left corner.
            w, h: Dimensions.
            color: RGB565 colour.
            filled: If True fill the rectangle, else draw outline only.
        """
        if filled:
            for dy in range(h):
                for dx in range(w):
                    self.set_pixel(x + dx, y + dy, color)
        else:
            for dx in range(w):
                self.set_pixel(x + dx, y, color)
                self.set_pixel(x + dx, y + h - 1, color)
            for dy in range(h):
                self.set_pixel(x, y + dy, color)
                self.set_pixel(x + w - 1, y + dy, color)

    def draw_text(
        self,
        text: str,
        x: int,
        y: int,
        font: str = "standard",
        color: int = _White,
    ) -> int:
        """Draw null-terminated text at (x, y) using a bitmap font.

        Args:
            text: String to render (stops at first 0x00 byte).
            x, y: Origin (top-left of first glyph).
            font: Font name (only 'standard' is built-in).
            color: RGB565 colour for set pixels.

        Returns:
            X coordinate after the last glyph.
        """
        glyphs = self._font_cache.get(font, _FONT_5X7)
        cx = x
        for ch in text:
            code = ord(ch)
            glyph = glyphs.get(code)
            if glyph is None:
                # Try uppercase variant if lowercase missing
                glyph = glyphs.get(code - 0x20) if 0x61 <= code <= 0x7A else None
            if glyph is None:
                cx += 6  # Advance for unknown glyph
                continue
            for col_idx, col in enumerate(glyph):
                for row in range(8):  # 8 rows (7 used)
                    if (col >> row) & 1:
                        self.set_pixel(cx + col_idx, y + row, color)
            cx += 6  # 5 pixels + 1 spacing
        return cx

    def draw_bitmap(
        self,
        bitmap: bytes,
        x: int,
        y: int,
        w: int,
        h: int,
        color: int = _White,
    ) -> None:
        """Draw a 1-bpp bitmap as a glyph.

        Args:
            bitmap: Packed bytes, MSB first within each byte.
            x, y: Top-left position.
            w, h: Bitmap dimensions in pixels.
            color: RGB565 colour for set bits.
        """
        idx = 0
        for row in range(h):
            for col in range(w):
                byte_idx = idx // 8
                bit = 7 - (idx % 8)
                if byte_idx < len(bitmap) and (bitmap[byte_idx] >> bit) & 1:
                    self.set_pixel(x + col, y + row, color)
                idx += 1

    def scroll_text(
        self,
        text: str,
        y: int,
        speed: float = 1.0,
    ) -> Generator[bytes, None, None]:
        """Generator that renders a horizontally scrolling marquee.

        Args:
            text: String to scroll.
            y: Y coordinate of the text baseline.
            speed: Pixels per step.

        Yields:
            Rendered framebuffer bytes after each step.
        """
        key = f"_scroll_{y}"
        if key not in self._scroll_state:
            self._scroll_state[key] = {
                "x": self.width,
                "text": text,
                "pos": 0,
            }
        state = self._scroll_state[key]

        while True:
            self.clear()
            state["x"] -= speed
            if state["x"] < -(len(text) * 6):
                state["x"] = self.width
            self.draw_text(state["text"], int(state["x"]), y)
            self._dirty = True
            yield self.render()

    def set_brightness(self, level: int) -> None:
        """Set display brightness (0-100).

        On hardware this would adjust backlight PWM. Here we store it.
        """
        self.brightness = max(0, min(100, level))

    def render(self) -> bytes:
        """Pack the framebuffer into RGB565 bytes and return it.

        Returns:
            bytes of length width * height * 2 (16-bit little-endian).
        """
        buf = bytearray(self.width * self.height * 2)
        struct.pack_into(f"<{self.width * self.height}H", buf, 0, *self._framebuffer)
        self._dirty = False
        return bytes(buf)

    def load_font_from_file(
        self, name: str, path: Path, glyph_w: int = 8, glyph_h: int = 16
    ) -> None:
        """Load a custom font from a raw binary file.

        Each glyph is glyph_w * glyph_h bits (rows padded to bytes),
        stored consecutively.

        Args:
            name: Identifier for the font.
            path: Path to the binary font file.
            glyph_w, glyph_h: Dimensions per glyph.
        """
        try:
            data = path.read_bytes()
            glyphs: dict[int, bytes] = {}
            bytes_per_glyph = (glyph_w * glyph_h + 7) // 8
            for code in range(0x20, 0x7F):
                offset = (code - 0x20) * bytes_per_glyph
                glyphs[code] = data[offset : offset + bytes_per_glyph]
            self._font_cache[name] = glyphs
        except Exception as e:
            logger.warning("Failed to load font '%s' from %s: %s", name, path, e)

    def fade_to(self, target: int, steps: int = 10) -> Generator[bytes, None, None]:
        """Generator that fades the display to black.

        Yields intermediate frames for smooth animation.
        """
        start = self.brightness
        for step in range(steps):
            self.brightness = start - int((start / steps) * step)
            self._dirty = True
            yield self.render()
        self.brightness = 0
        self.clear()
        yield self.render()
