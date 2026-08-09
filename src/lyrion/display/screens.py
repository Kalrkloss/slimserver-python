"""Display screens for Lyrion Music Server."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Generator, Literal, Optional

from .renderer import DisplayRenderer, _Black, _White

logger = logging.getLogger(__name__)


class Screen(ABC):
    """Abstract base class for a player display screen."""

    @abstractmethod
    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        """Render this screen into the renderer.

        Args:
            renderer: The DisplayRenderer to draw into.
            context: Dictionary with screen-specific data.
        """
        ...

    def update(self, context: dict) -> bool:
        """Return True if this screen needs a full re-render.

        Subclasses should compare relevant context fields with cached
        values and return True only when something visible changed.
        The default implementation always returns True.
        """
        return True

    def on_button(self, key: str, context: dict) -> Optional[str]:
        """Handle a front-panel button press.

        Args:
            key: Button identifier (e.g. "left", "right", "up", "down").
            context: Mutable context dict (can be modified in place).

        Returns:
            Screen name to transition to, or None to stay.
        """
        return None


class NowPlayingScreen(Screen):
    """Now Playing display showing artist, title, album, and progress."""

    def __init__(self) -> None:
        self._last_artist = ""
        self._last_title = ""
        self._last_album = ""
        self._last_position = -1
        self._last_duration = -1
        self._last_artwork = None

    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        artist = context.get("artist", "Unknown Artist")
        title = context.get("title", "Unknown Title")
        album = context.get("album", "")
        duration = context.get("duration", 0)
        position = context.get("position", 0)
        artwork = context.get("artwork")
        volume = context.get("volume", 0)
        mode = context.get("mode", "stop")

        # Track mode indicator (top-left)
        mode_symbols = {"play": "\x03", "pause": "\x04", "stop": "\x02", "loading": "\x01"}
        symbol = mode_symbols.get(mode, "")
        renderer.draw_text(symbol, 0, 0, color=_White)

        # Artist name (line 0)
        renderer.draw_text(artist[:40], 8, 0, color=_White)

        # Track title (line 1)
        renderer.draw_text(title[:40], 0, 10, color=_White)

        # Album (line 2)
        if album:
            renderer.draw_text(album[:40], 0, 20, color=0xBEEF)

        # Progress bar (line 3)
        if duration > 0:
            bar_width = renderer.width - 40
            bar_x = 20
            bar_y = renderer.height - 4
            progress = min(1.0, position / duration) if duration > 0 else 0
            filled = int(bar_width * progress)
            renderer.draw_rect(bar_x, bar_y, bar_width, 3, color=0x4208, filled=False)
            renderer.draw_rect(bar_x, bar_y, filled, 3, color=0x4208, filled=True)

            # Time
            time_str = f"{int(position)//60}:{int(position)%60:02d}"
            renderer.draw_text(time_str, 0, bar_y - 1, color=_White)

        # Volume indicator (right side)
        vol_str = f"\x02{volume}"
        renderer.draw_text(vol_str, renderer.width - 30, 0, color=_White)

        # Update cache
        self._last_artist = artist
        self._last_title = title
        self._last_album = album
        self._last_position = position
        self._last_duration = duration

    def update(self, context: dict) -> bool:
        changed = (
            context.get("artist") != self._last_artist
            or context.get("title") != self._last_title
            or context.get("album") != self._last_album
            or context.get("position") != self._last_position
            or context.get("duration") != self._last_duration
            or context.get("artwork") != self._last_artwork
        )
        return changed


class MenuScreen(Screen):
    """Browse/control menu with scrollable items."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._selected = 0
        self._scroll_pos = 0
        self._page_size = 4

    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        items = context.get("items", [])
        selected = context.get("selected", 0)
        title = context.get("title", "Menu")
        scroll_pos = context.get("scroll_pos", 0)

        self._items = items
        self._selected = selected

        # Title bar
        renderer.draw_text(title[:30], 0, 0, color=_White)
        renderer.draw_rect(0, 9, renderer.width, 1, color=0x4208)

        # Visible window
        visible = items[scroll_pos : scroll_pos + self._page_size]
        for i, item in enumerate(visible):
            y = 12 + i * 8
            if scroll_pos + i == selected:
                # Highlight selected
                renderer.draw_rect(0, y - 1, renderer.width, 8, color=0x4208, filled=True)
                renderer.draw_text(item[:38], 4, y, color=_Black)
            else:
                renderer.draw_text(item[:38], 4, y, color=_White)

        # Scroll indicator
        if len(items) > self._page_size:
            pct = scroll_pos / max(1, len(items) - self._page_size)
            bar_h = renderer.height - 12
            bar_y = 12 + int(pct * bar_h)
            renderer.draw_rect(renderer.width - 2, 12, 2, bar_h, color=0x7BEF)
            renderer.set_pixel(renderer.width - 2, bar_y, _White)

    def on_button(self, key: str, context: dict) -> Optional[str]:
        items = context.get("items", self._items)
        if key == "up":
            context["selected"] = max(0, self._selected - 1)
        elif key == "down":
            context["selected"] = min(len(items) - 1, self._selected + 1)
        elif key == "left":
            return "nowplaying"
        elif key == "right":
            return "nowplaying"
        return None


class VolumeScreen(Screen):
    """Volume level display with large bar and numeric indicator."""

    def __init__(self) -> None:
        self._last_volume = -1

    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        volume = context.get("volume", 0)

        # Large volume percentage text centered
        label = f"{volume}%"
        label_x = (renderer.width - len(label) * 8) // 2
        renderer.draw_text(label, max(0, label_x), 0, color=_White)

        # Horizontal bar
        bar_w = renderer.width - 20
        bar_x = 10
        bar_y = renderer.height // 2 - 4
        bar_h = 8
        renderer.draw_rect(bar_x, bar_y, bar_w, bar_h, color=0x7BEF, filled=False)
        filled = int(bar_w * volume / 100)
        renderer.draw_rect(bar_x, bar_y, filled, bar_h, color=0x4208, filled=True)

        # Volume icon hints
        renderer.draw_text("\x0D", 2, bar_y, color=_White)  # minus
        renderer.draw_text("\x0F", renderer.width - 10, bar_y, color=_White)  # plus

        self._last_volume = volume

    def update(self, context: dict) -> bool:
        return context.get("volume") != self._last_volume

    def on_button(self, key: str, context: dict) -> Optional[str]:
        if key in ("up", "right"):
            context["volume"] = min(100, context.get("volume", 50) + 5)
        elif key in ("down", "left"):
            context["volume"] = max(0, context.get("volume", 50) - 5)
        elif key in ("play", "pause"):
            return "nowplaying"
        return None


class ScreensaverScreen(Screen):
    """Animated screensaver — clock, spectrum, or starfield."""

    def __init__(self) -> None:
        self._mode = "clock"
        self._stars: list[dict] = []
        self._start_time = time.time()
        self._last_second = -1

    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        mode = context.get("screensaver_mode", self._mode)
        now = time.localtime()

        if mode == "clock" or mode == "analog":
            # Digital clock
            timestr = time.strftime("%H:%M", now)
            date_str = time.strftime("%a %b %d", now)
            # Large clock
            x = (renderer.width - len(timestr) * 12) // 2
            renderer.draw_text(timestr, max(0, x), 0, color=_White)
            x2 = (renderer.width - len(date_str) * 6) // 2
            renderer.draw_text(date_str, max(0, x2), 14, color=0xBEEF)
        elif mode == "visualizer":
            # Fake spectrum bars
            import math
            import random
            random.seed(int(time.time()) % 100)
            bars = 20
            bw = renderer.width // bars
            for i in range(bars):
                h = int((math.sin(time.time() * 3 + i) * 0.5 + 0.5) * (renderer.height - 4))
                x = i * bw + 1
                y = renderer.height - h - 2
                renderer.draw_rect(x, y, bw - 2, h, color=0x4208, filled=True)
        else:
            # Scrolling marquee
            text = context.get("marquee_text", "Lyrion Music Server")
            elapsed = int(time.time() - self._start_time) % 60
            offset = elapsed * 4
            renderer.draw_text(text, renderer.width - offset, renderer.height // 2 - 4, color=_White)

    def update(self, context: dict) -> bool:
        now = time.localtime()
        changed = now.tm_sec != self._last_second
        self._last_second = now.tm_sec
        return changed


class PowerScreen(Screen):
    """Power on/off screen."""

    def render(self, renderer: DisplayRenderer, context: dict) -> None:
        power = context.get("power", False)
        if power:
            renderer.draw_text("POWER ON", 0, renderer.height // 2 - 4, color=_White)
        else:
            renderer.draw_text("STANDBY", 0, renderer.height // 2 - 4, color=0x7BEF)

    def update(self, context: dict) -> bool:
        return True


# Screen registry for navigation
SCREEN_REGISTRY: dict[str, type[Screen]] = {
    "nowplaying": NowPlayingScreen,
    "menu": MenuScreen,
    "volume": VolumeScreen,
    "screensaver": ScreensaverScreen,
    "power": PowerScreen,
}
