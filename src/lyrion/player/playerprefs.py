"""Player-Prefs: ablegen (persistiert), anwenden (Perl ``setChange``-Callbacks).

Ablage wie Perl — **je Client, nicht je Server**
=================================================

Perl hält Client-Prefs im Namespace-Store unter einem eigenen Slot je Client
und speichert sie zusammen mit den Server-Prefs in der Namespace-Datei::

    our $clientPreferenceTag = '_client';                     # Prefs/Client.pm:30
    $parent->{'prefs'}->{"$clientPreferenceTag:$clientid"} ||= { '_version' => 0 };
                                                              # Prefs/Client.pm:44
    $class->{'prefs'}->{ $pref } = $new;                      # Prefs/Base.pm:121
    $root->save;                                              # Prefs/Base.pm:124
    $class->{'prefs'}->{ '_ts_' . $pref } = time();           # Prefs/Base.pm:122

Geschrieben wird also **eager** bei jedem ``set`` (``Base.pm:82-124``), das
Schreiben der Datei selbst ist gebündelt: ``save`` legt einen Timer auf +10 s
(``Prefs/Namespace.pm:298-308``), ``savenow`` schreibt ``server.prefs``
(``:316-330``).  Gelesen wird die Datei beim Start/apc (``_load``, ``:259-277``)
und bei jedem Client-``init`` über ``initPrefs`` (``Active``: ``Player/Client.pm:334-349``
``$clientPrefs->init($defaultPrefs)``).

``init`` setzt nur, was fehlt oder ``undef`` ist (``Prefs/Base.pm:196-230``) —
gespeicherte Werte überleben deshalb ein Neuladen.

Lebensdauer (Perl):
* ``client forget`` löscht die Prefs **nicht**: ``forgetClient``
  (``Player/Client.pm:539-568``) räumt Timer/Display/Finish auf, der Pref-Slot
  bleibt im Store stehen; beim nächsten ``initPrefs`` sind die Werte wieder da.
* Nur ``$client->resetPrefs()`` (``Player/Client.pm:375-383``: ``remove(keys
  %{$clientPrefs->all})`` + ``initPrefs``) wirft sie weg; Perl ruft das
  ausschließlich aus dem ``resetprefs``-Handler der Basic-Seite
  (``Web/Settings/Player/Basic.pm:69-71``) und aus ``Squeezebox2::resetPrefs``
  (``Player/Squeezebox2.pm:1039-1049``).

Unser Speicher: der lebende Prefs-Store (``lyrion.config.PreferenceStore``,
``prefs.db`` — dieselbe DB wie die Server-Prefs, deshalb sind die Server-Werte
unberührt) mit dem Schlüssel ``_client:<MAC>:<Prefname>``.  Es gibt **keine**
zweite Pref-Logik: Lesen/Schreiben geht über ``store.get`` / ``store.set``;
nur ``remove`` fehlt dort (Perl ``Prefs/Base.pm:241-257``), das ist hier
implementiert.

Wirkung der Werte (Perl ``setChange``-Callbacks)
================================================

Perl ``prefCommand`` (``Slim/Control/Commands.pm:2631-2677``) schreibt den Wert
nur in den Pref-Store des Clients::

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

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Perl ``our $clientPreferenceTag = '_client';`` (``Prefs/Client.pm:30``).
CLIENT_PREF_TAG = "_client"

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

#: Werte ohne Event-Loop (selten: Skripte/Worker-Threads) werden hier gepuffert
#: (Schlüssel → ``(MAC, Prefname, Text)``) und beim nächsten asynchronen
#: Schreiben mitgeschrieben.
_PENDING: dict[str, tuple[str, str, str]] = {}
#: Referenz auf laufende Schreib-Tasks (sonst räumt der GC sie weg).
_TASKS: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Schlüssel + Store
# ---------------------------------------------------------------------------

def _mac_id(mac: Any) -> str:
    """Perls ``$client->id`` — die MAC, wie der Client sie gemeldet hat."""
    return str(mac or "").strip().lower()


def player_pref_key(mac: Any, name: str) -> str:
    """Speicher-Schlüssel eines Client-Prefs: Perls ``_client:<MAC>`` + Name.

    Perl legt je Client einen eigenen Hash ab (``Prefs/Client.pm:44``
    ``$parent->{'prefs'}->{"_client:$clientid"}``); unsere ``prefhash``-Tabelle
    ist flach, deshalb trägt jeder Schlüssel den Client: MAC + Prefname.
    """
    return f"{CLIENT_PREF_TAG}:{_mac_id(mac)}:{name}"


def _prefs_store():
    """Der lebende Prefs-Store (``~/.lyrion/Lyrion/Prefs/prefs.db``)."""
    from lyrion.config import get_prefs

    return get_prefs()


def _encode(value: Any) -> str:
    """Wert → ``prefhash``-Text.

    Der Store hält Strings (``config.PreferenceStore.set`` → ``str(value)``);
    Listen/Hashes (Perl-Refs wie ``menuItem``, ``disabledirsets``, ``presets``)
    würden als Python-Repr zerfallen, deshalb JSON.  Skalare bleiben Skalare —
    die Wege, die diese Prefs setzen (Web-Formular, CLI, Jive), liefern Strings.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(list(value) if not isinstance(value, dict) else value)
    return str(value)


def _decode(text: Any) -> Any:
    """``prefhash``-Text → Wert (Umkehrung von :func:`_encode`)."""
    if isinstance(text, str) and text[:1] in ("[", "{"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def stored_player_prefs(mac: Any) -> dict[str, Any]:
    """Alle gespeicherten Prefs dieses Clients (Perl ``$client->all``).

    Liest aus dem Store (der beim Start alle ``prefhash``-Zeilen in den Cache
    lädt) — kein DB-Roundtrip.
    """
    if not mac:
        return {}
    store = _prefs_store()
    prefix = f"{CLIENT_PREF_TAG}:{_mac_id(mac)}:"
    out: dict[str, Any] = {}
    try:
        names = store.keys()
    except Exception:                                  # noqa: BLE001
        return {}
    for name in names:
        if name.startswith(prefix):
            out[name[len(prefix):]] = _decode(store.get(name))
    return out


def load_player_prefs(player) -> dict[str, Any]:
    """Gespeicherte Client-Prefs in ``player.playerprefs`` nachtragen.

    Perl ``Client.pm:334-349`` ``initPrefs`` → ``$clientPrefs->init($defaultPrefs)``;
    ``init`` schreibt nur, was fehlt oder ``undef`` ist (``Prefs/Base.pm:196-230``
    ``if (!exists $class->{'prefs'}->{$pref} || !defined ...)``), bereits
    gesetzte Werte (Reconnect im laufenden Betrieb) bleiben also stehen.
    """
    if player is None:
        return {}
    prefs = getattr(player, "playerprefs", None)
    if prefs is None:
        prefs = {}
        player.playerprefs = prefs
    for name, value in stored_player_prefs(getattr(player, "mac", None)).items():
        if prefs.get(name) is None:                     # Base.pm:199
            prefs[name] = value
            # Perls Frames lesen die Pref direkt (`Squeezebox2.pm:283-295`
            # `$prefs->client($client)->get('digitalVolumeControl')`); unser
            # Port hält die Wirkung in PlayerState-Feldern, also muss der
            # setChange-Effekt (`Player.pm:79`) nach dem Laden neu laufen.
            apply_player_pref(player, name, value, persist=False)
    return prefs


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

async def _persist(mac: Any, name: str, value: Any) -> None:
    """Eine Zeile ``_client:<MAC>:<Pref>`` in ``prefhash`` schreiben."""
    store = _prefs_store()
    key = player_pref_key(mac, name)
    text = _encode(value)
    try:
        await store.set(key, text)                      # Base.pm:121-124
    except Exception as exc:                            # noqa: BLE001
        logger.warning("playerpref %s konnte nicht gespeichert werden: %s", key, exc)
        return
    _PENDING.pop(key, None)


def _persist_soon(mac: Any, name: str, value: Any) -> None:
    """Schreiben aus synchronem Code (Perls ``set`` → ``$root->save``).

    Läuft eine Loop, wird ein Task angehängt (der Aufrufer kann nicht ``await``:
    ``web/api.py`` ist ausserhalb dieser Änderung).  Ohne Loop (Skript,
    Worker-Thread) wird gepuffert — :func:`set_player_pref` schreibt den Puffer
    mit; ein DB-Schreibzugriff aus einem loop-losen Kontext heraus würde sonst
    an einer zweiten SQLite-Verbindung hängen.
    """
    key = player_pref_key(mac, name)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        _PENDING[key] = (_mac_id(mac), name, _encode(value))
        return
    try:
        task = loop.create_task(_persist(mac, name, value))
    except RuntimeError:
        _PENDING[key] = (_mac_id(mac), name, _encode(value))
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def set_player_pref(player, name: str, value: Any, *,
                          apply: bool = True) -> str:
    """Client-Pref setzen UND speichern (Perl ``$prefs->client($c)->set``).

    Nicht-parallele Ablage: Der Wert steht sofort im ``playerprefs``-Dict und
    in ``prefhash``; erst danach kehrt der Aufrufer zurück (Perl schreibt
    ebenfalls bei jedem ``set``, nur die Datei selbst wird gebündelt —
    ``Prefs/Namespace.pm:298-308``).
    """
    if player is None:
        return "unchanged"
    label = apply_player_pref(player, name, value, persist=False) if apply \
        else "stored"
    for key, (pending_mac, pending_name, text) in list(_PENDING.items()):
        _PENDING.pop(key, None)
        await _persist(pending_mac, pending_name, _decode(text))
    await _persist(getattr(player, "mac", None), name, value)
    return label


async def remove_player_prefs(mac: Any, names: list[str] | None = None) -> int:
    """Client-Prefs löschen (Perl ``Prefs/Base.pm:241-257`` ``remove``).

    ``names=None`` räumt alle Prefs dieses Clients ab — das tut Perl in
    ``resetPrefs`` (``Player/Client.pm:375-383``); **nicht** in ``forgetClient``
    (``:539-568``), ein vergessener Player behält seine Prefs.
    """
    if not mac:
        return 0
    store = _prefs_store()
    if names is None:
        prefix = f"{CLIENT_PREF_TAG}:{_mac_id(mac)}:"
        keys = [k for k in store.keys() if k.startswith(prefix)]
    else:
        keys = [player_pref_key(mac, n) for n in names]
    if not keys:
        return 0
    db = getattr(store, "_db", None)
    if db is None:                                      # Store nicht offen
        for key in keys:
            store._cache.pop(key, None)
        return 0
    for key in keys:
        await db.execute("DELETE FROM prefhash WHERE name = ?", (key,))
        store._cache.pop(key, None)                     # Base.pm:252-253
    await db.commit()
    return len(keys)


# ---------------------------------------------------------------------------
# Anwenden (Perl setChange-Callbacks)
# ---------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def apply_player_pref(player, key: str, value: Any, *,
                      persist: bool = True) -> str:
    """Wert ablegen und die Wirkung auf den PlayerState anwenden.

    ``persist=True`` (Default) schreibt den Wert zusätzlich in den Pref-Store —
    genau wie Perls ``set`` (``Prefs/Base.pm:121-124``).  :func:`set_player_pref`
    ruft die Wirkungs-Hälfte mit ``persist=False`` und schreibt selbst
    (dort ``await``-fähig).

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
    if persist:
        _persist_soon(getattr(player, "mac", None), key, value)

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

    if key == "mp3StreamingMethod":
        # Perl Slim/Player/Protocols/HTTP.pm:433-439 (``canDirectStream``):
        # "Allow user pref to select the method for streaming" — ``$method == 1``
        # means proxied streaming, so the player must fetch /stream.mp3 from
        # THIS server instead of being pointed at the remote URL. The value is
        # read back by SlimProtoClient.send_remote_stream before it decides
        # between a direct and a proxy strm frame.
        new = _as_int(value, 0)
        old = int(getattr(player, "mp3_streaming_method", 0) or 0)
        player.mp3_streaming_method = new
        return "applied" if new != old else "unchanged"

    # Font-Wahl im Stil von Jive.pm:1865 ('<font>_curr').
    if key.endswith("_curr"):
        return "stored"

    # Alles Übrige (STORE_ONLY_PREFS und unbekannte Namen) wird wie in Perl
    # gespeichert; unser Server hat dafür (noch) keine Wirkung. Kein Fehler.
    return "stored"
