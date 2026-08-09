"""IR/RF button handling for Lyrion Music Server."""
from typing import Callable, Optional

# Button code → action mapping for different player types
BUTTON_MAPS: dict[str, dict[int, str]] = {
    "squeezebox": {
        0: "power",
        1: "pause",
        2: "play",
        3: "stop",
        4: "prev",
        5: "next",
        6: "volume_up",
        7: "volume_down",
        8: "rew",
        9: "fwd",
        10: "add",
        11: "sleep",
    },
    "squeezebox2": {
        # Extended button codes for SB2
        0: "power",
        1: "pause",
        2: "play",
        3: "stop",
        4: "prev",
        5: "next",
        6: "volume_up",
        7: "volume_down",
        8: "rew",
        9: "fwd",
        10: "add",
        11: "sleep",
        12: "shuffle",
        13: "repeat",
        14: "now_playing",
        15: "browse",
    },
    "squeezebox3": {
        0: "power",
        1: "pause",
        2: "play",
        3: "stop",
        4: "prev",
        5: "next",
        6: "volume_up",
        7: "volume_down",
        8: "rew",
        9: "fwd",
        10: "add",
        11: "sleep",
        12: "shuffle",
        13: "repeat",
        14: "now_playing",
        15: "browse",
        16: "size",
        17: "brightness",
    },
    "squeezeboxradio": {
        0: "power",
        1: "pause",
        2: "play",
        3: "stop",
        4: "prev",
        5: "next",
        6: "volume_up",
        7: "volume_down",
        8: "rew",
        9: "fwd",
        10: "home",
        11: "add",
        12: "search",
        13: "revert",
        14: "now_playing",
        15: "browse",
    },
    "squeezeboxtouch": {
        0: "power",
        1: "pause",
        2: "play",
        3: "stop",
        4: "prev",
        5: "next",
        6: "volume_up",
        7: "volume_down",
        8: "rew",
        9: "fwd",
        10: "favorites",
        11: "add",
        12: "now_playing",
        13: "browse",
        14: "search",
        15: "settings",
        16: "sleep",
    },
    "pext": {
        # PiDiSi / PICOTCU / ESP32 extended codes
        0x01: "power",
        0x02: "play",
        0x03: "pause",
        0x04: "stop",
        0x05: "prev",
        0x06: "next",
        0x07: "volume_up",
        0x08: "volume_down",
        0x09: "rew",
        0x0A: "fwd",
        0x0B: "mute",
        0x0C: "shuffle",
        0x0D: "repeat",
    },
}


class ButtonHandler:
    """Maps raw button/IR codes to named actions and dispatches callbacks.

    Args:
        player_mac: MAC address of the player this handler belongs to.
        player_type: Player type key from BUTTON_MAPS (default: "squeezebox").
    """

    def __init__(self, player_mac: str, player_type: str = "squeezebox"):
        self.player_mac = player_mac
        self.player_type = player_type
        self.button_map = BUTTON_MAPS.get(player_type, BUTTON_MAPS["squeezebox"])
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, action: str, callback: Callable) -> None:
        """Register a callback for a named action.

        Args:
            action: Action name (e.g. "play", "volume_up").
            callback: Callable that accepts (action: str, mac: str).
        """
        self._handlers[action] = callback

    def unregister_handler(self, action: str) -> None:
        """Remove the callback for an action."""
        self._handlers.pop(action, None)

    def handle_button(self, button_code: int) -> Optional[str]:
        """Process a raw button code and dispatch to the registered handler.

        Args:
            button_code: Integer button code from IR/RF or front panel.

        Returns:
            The action name that was dispatched, or None if no handler.
        """
        action = self.button_map.get(button_code, f"unknown_{button_code}")
        handler = self._handlers.get(action)
        if handler:
            return handler(action)
        return action

    def handle_ir_sequence(self, sequence: list[int]) -> list[Optional[str]]:
        """Process a sequence of IR codes (e.g. from a multi-button press).

        Args:
            sequence: List of integer button codes.

        Returns:
            List of action names that were dispatched (may contain None).
        """
        results = []
        for code in sequence:
            results.append(self.handle_button(code))
        return results
