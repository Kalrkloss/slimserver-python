"""ReplayGain anwenden — Perl ``Slim/Player/ReplayGain.pm`` (PROT-10).

``fetchGainMode`` (``ReplayGain.pm:22-79``)::

    my $rgmode = $prefs->client($client)->get('replayGainMode');
    my $handler = $song->currentTrackHandler();
    if ( $handler->can('trackGain') ) { return $handler->trackGain(...) }
    return undef if !$rgmode;                     # 0 = aus
    return 0 unless blessed($track) && $track->can('replay_gain');
    if ( isVolatileURL($url) ) { return preventClipping($track->replay_gain(), $track->replay_peak()) }
    if ( $track->remote ) {                       # Radio
        return preventClipping($track->replay_gain() // $prefs->client($client)->get('remoteReplayGain'),
                               $track->replay_peak());
    }
    if ($rgmode == 1) {                           # Track-Gain
        return preventClipping($track->replay_gain() // $prefs->client($client)->get('localReplayGain'),
                               $track->replay_peak());
    }
    if ($rgmode == 2) {                           # Album-Gain
        return preventClipping($album->replay_gain(), $album->replay_peak());
    }
    if ($rgmode == 3) {                           # smart
        if (defined $album->replay_gain() && ($class->trackAlbumMatch($client,-1) || $class->trackAlbumMatch($client,1))) {
            return preventClipping($album->replay_gain(), $album->replay_peak());
        }
        return preventClipping($track->replay_gain() // $prefs->client($client)->get('localReplayGain'),
                               $track->replay_peak());
    }

``preventClipping`` (``:267-278``)::

    if ( defined $peak && defined $gain && $peak > 0 ) {
        my $noclip = -20 * ( log($peak) / log(10) );
        if ( $noclip < $gain ) { return $noclip }
    }
    return $gain;

Defaults eines Players (``Squeezebox2.pm:47-49``): ``replayGainMode 0``
(aus), ``remoteReplayGain -5``, ``localReplayGain 0``.

Modus 0 ist der Default: ohne Player-Pref passiert also nichts (Perl liefert
``undef`` → der strm-Frame trägt 0).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Squeezebox2.pm:47-49
DEFAULT_REPLAY_GAIN_MODE = 0
DEFAULT_REMOTE_REPLAY_GAIN = -5.0
DEFAULT_LOCAL_REPLAY_GAIN = 0.0


def prevent_clipping(gain: Optional[float],
                     peak: Optional[float]) -> Optional[float]:
    """Perl ``preventClipping`` (ReplayGain.pm:267-278).

    Begrenzt den Gain so, dass die Spitze nicht über 0 dBFS läuft:
    ``-20 * log10(peak)``.
    """
    if peak is not None and gain is not None and peak > 0:
        try:
            noclip = -20 * (math.log(peak) / math.log(10))
        except (ValueError, ZeroDivisionError):
            return gain
        if noclip < gain:
            return noclip
    return gain


def _pref(player_prefs: dict, key: str, default: Any) -> Any:
    if not player_prefs:
        return default
    val = player_prefs.get(key)
    if val is None or val == "":
        return default
    return val


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def fetch_gain_mode(
    player_prefs: dict | None,
    *,
    track_gain: Optional[float] = None,
    track_peak: Optional[float] = None,
    album_gain: Optional[float] = None,
    album_peak: Optional[float] = None,
    remote: bool = False,
    neighbours_same_album: bool = False,
    volatile: bool = False,
) -> Optional[float]:
    """Anzuwendender ReplayGain in dB — ``None`` heißt „aus" (Perl ``undef``).

    ``neighbours_same_album`` ist Perls ``trackAlbumMatch($client, -1) ||
    trackAlbumMatch($client, 1)`` (ReplayGain.pm:129-170): der Aufrufer prüft,
    ob der vorige ODER nächste Playlist-Titel zum selben Album gehört.
    ``volatile`` entspricht ``isVolatileURL($url)``.
    """
    prefs = player_prefs or {}
    mode_raw = _pref(prefs, "replayGainMode", DEFAULT_REPLAY_GAIN_MODE)
    try:
        mode = int(float(str(mode_raw)))
    except (TypeError, ValueError):
        mode = DEFAULT_REPLAY_GAIN_MODE

    if not mode:
        return None                     # Modus 0 = ReplayGain ignorieren

    if volatile:
        return prevent_clipping(track_gain, track_peak)

    if remote:
        gain = track_gain
        if gain is None:
            gain = _num(_pref(prefs, "remoteReplayGain", DEFAULT_REMOTE_REPLAY_GAIN))
        return prevent_clipping(gain, track_peak)

    local_default = _num(_pref(prefs, "localReplayGain", DEFAULT_LOCAL_REPLAY_GAIN))

    if mode == 1:
        gain = track_gain if track_gain is not None else local_default
        return prevent_clipping(gain, track_peak)

    if mode == 2:
        return prevent_clipping(album_gain, album_peak)

    # Modus 3 (smart): Album-Gain nur, wenn das Album einen hat UND der
    # Nachbartitel zum selben Album gehört (ReplayGain.pm:69-77).
    if album_gain is not None and neighbours_same_album:
        return prevent_clipping(album_gain, album_peak)
    gain = track_gain if track_gain is not None else local_default
    return prevent_clipping(gain, track_peak)
