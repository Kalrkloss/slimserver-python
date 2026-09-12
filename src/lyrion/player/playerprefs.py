"""Player-Prefs anwenden (Perl ``playerpref`` + ``setChange``-Callbacks).

Perl ``prefCommand`` (``Slim/Control/Commands.pm:2631-2677``) schreibt den Wert
in den Pref-Store des Clients::

    preferences($namespace)->client($client)->set($prefName, $newValue);

Das allein bliebe folgenlos — die Wirkung entsteht über die registrierten
``setChange``-Callbacks, z. B. ``Slim/Player/Player.pm:79``::

    $prefs->setChange( sub { my $client = $_[2]; $client->bass($_[1]); }, 'bass');

Unsere Frames lesen dieselben Werte später wieder aus:

* ``digitalVolumeControl`` / ``preampVolumeControl`` → audg-Frame
  (``Squeezebox2.pm:283-303``: ``$dvc``-Byte, ``$preamp = 255 - 2*preamp``)
* ``bass`` / ``treble`` / ``pitch`` → Mixer-Werte (``Client.pm:891-908``
  ``_mixerPrefs``, geklemmt auf die Bereiche aus ``Player.pm:366-367`` und
  ``Client.pm:682-689``)
* ``transitionType`` / ``transitionDuration`` → strm-Frame

Was nur abgelegt wird (Hardware/Display, für jive/squeezelite ohne Wirkung):
``syncVolume``, ``analogOutMode``, ``stereoxl``, ``Brightness``, ``*_curr``
(Font-Wahl), ``replayGainMode``/``localReplayGain``/``remoteReplayGain``
(ReplayGain ist noch nicht implementiert).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Perl: Player.pm:366-367 (min/max bass & treble), Client.pm:682-689
# (pitch ist für Player ohne echte Pitch-Steuerung auf 100 festgenagelt).
MIXER_PREF_RANGES: dict[str, tuple[int, int]] = {
    "bass": (0, 100),
    "treble": (0, 100),
    "pitch": (100, 100),
}
# Prefs, die wir nur speichern (kein Effekt in unserem Server).
STORE_ONLY_PREFS = frozenset({
    "syncVolume", "analogOutMode", "stereoxl", "Brightness",
    "replayGainMode", "localReplayGain", "remoteReplayGain",
    "transitionType", "transitionDuration",
})


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def apply_player_pref(player, key: str, value: Any) -> str:
    """Wert ablegen und die Wirkung auf den PlayerState anwenden.

    Returns a short label of what happened (for logging/tests):
    ``"applied"`` | ``"stored"`` | ``"clamped"`` | ``"unchanged"``.
    """
    if player is None:
        return "unchanged"
    prefs = getattr(player, "playerprefs", None)
    if prefs is None:
        prefs = {}
        player.playerprefs = prefs
    prefs[key] = value

    if key == "digitalVolumeControl":
        # Squeezebox2.pm:283-295 — das dvc-Byte des audg-Frames.
        new = _as_int(value, 1) != 0
        changed = bool(getattr(player, "digital_volume_control", True)) != new
        player.digital_volume_control = new
        return "applied" if changed else "unchanged"

    if key == "preampVolumeControl":
        # Player.pm:39 (Default 0) → preamp = 255 - int(2*preamp).
        new = _as_int(value, 0)
        changed = int(getattr(player, "preamp_volume_control", 0) or 0) != new
        player.preamp_volume_control = new
        return "applied" if changed else "unchanged"

    if key in MIXER_PREF_RANGES:
        lo, hi = MIXER_PREF_RANGES[key]
        raw = _as_int(value, 0)
        new = max(lo, min(hi, raw))
        old = int(getattr(player, key, 0) or 0)
        if new == old and raw == new:
            return "unchanged"
        setattr(player, key, new)
        return "clamped" if raw != new else "applied"

    # Font-Wahl im Stil von Jive.pm:1865 ('<font>_curr').
    if key.endswith("_curr"):
        return "stored"

    # Alles Übrige (STORE_ONLY_PREFS und unbekannte Namen) wird wie in Perl
    # gespeichert; unser Server hat dafür (noch) keine Wirkung. Kein Fehler.
    return "stored"
