"""Perls Shuffle-Reihenfolge: ``shufflelist`` + ``reshuffle`` (Playlist.pm).

Perl haelt pro Player ZWEI Listen (``Slim/Player/Client.pm:246-247``):

* ``playlist`` — die Eintraege in der Reihenfolge, in der sie hinzugefuegt
  wurden (der User baut sie so auf),
* ``shufflelist`` — eine Permutation der Playlist-Indizes. QUEUE-Position *i*
  spielt ``playlist[shufflelist[i]]`` (``Playlist::track``, ``Playlist.pm:78-84``).

Alles, was ein Client sieht, ist in QUEUE-Positionen ausgedrueckt:
``playlist index ?``/``playlist jump``/``playlist delete`` und der
``playlist_cur_index`` des Status sind ``playingSongIndex`` (``Source.pm:229-233``),
die Playlist-Ansicht ist ``Playlist::songs`` (``:104-118``:
``(@{playList($client)}[ @{shuffleList($client)} ])[$start .. $end]``).

``reshuffle`` (``Playlist.pm:788-1000``) baut die Liste neu — immer aus
``(0 .. $#playlist)`` (``:826``), dann der Modus-Zweig: Fisher-Yates fuer
``shuffle == 1`` (``:855``) bzw. ``balancedShuffle``, wenn
``useBalancedShuffle`` gesetzt ist (``:833-854``), Album-Gruppierung fuer
``shuffle == 2`` (``:880-958``). Der laufende Titel wird danach an den ANFANG
der Liste getauscht (``:857-877``) und die Queue-Eintraege werden umnummeriert
(``:963-980``). Mit ``shuffle == 0`` bleibt es bei der Identitaet — die
Playlist-Reihenfolge kehrt zurueck.

Live-Perl 9.1.1 (read-only, 2026-09-22): ``pref useBalancedShuffle ?`` →
``pref useBalancedShuffle 1`` (Default ``$os->canDBHighMem() ? 1 : 0``,
``Utils/Prefs.pm:224``); ``pref reshuffleOnRepeat ?`` → ``0`` (``:184``).
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.player import manager as manager_mod
from lyrion.player.manager import (
    PlayerManager,
    balanced_shuffle,
    fischer_yates_shuffle,
    nextsong,
    playlist_item,
    playlist_queue_position,
    queue_order,
    queue_playlist_index,
    reshuffle,
    shufflelist,
)
from lyrion.player.state import PlayerState

MAC = "02:11:22:33:44:55"


class _StubRng:
    """Deterministischer ``rand``/``randrange``-Ersatz (Perls ``rand``)."""

    def __init__(self, randrange_value: int = 0, random_value: float = 0.0):
        self._rr = randrange_value
        self._r = random_value

    def randrange(self, stop: int) -> int:
        return min(self._rr, stop - 1)

    def random(self) -> float:
        return self._r


def _player(playlist=(10, 11, 12, 13), position=0, shuffle=0, repeat=0,
            **kw) -> PlayerState:
    p = PlayerState(mac=MAC, name="Test", ip="127.0.0.1", port=0)
    p.playlist = list(playlist)
    p.playlist_total = len(p.playlist)
    p.playlist_position = position
    p.shuffle = shuffle
    p.repeat = repeat
    for field, value in kw.items():
        setattr(p, field, value)
    return p


@pytest.fixture(autouse=True)
def _fischer_yates_not_balanced(monkeypatch):
    """Perl nimmt bei ``useBalancedShuffle`` die balancierte Variante — fuer
    die Reihenfolge-Tests hier ist Fisher-Yates der Zweig, der geprueft wird."""
    monkeypatch.setattr(manager_mod, "_use_balanced_shuffle", lambda: False)


# ---------------------------------------------------------------------------
# fischer_yates_shuffle — Playlist.pm:728-740
# ---------------------------------------------------------------------------

def test_fischer_yates_swaps_with_a_partner_at_or_below_i():
    """``my $a = int(rand($i + 1))`` mit ``a == 0`` in jeder Runde ergibt die
    bekannte "move to front"-Folge ``1,2,…,n-1,0`` — und belegt, dass die
    Schleife bei ``i = n-1`` beginnt und bis ``i = 1`` laeuft (:735)."""
    items = [0, 1, 2, 3]
    fischer_yates_shuffle(items, _StubRng(randrange_value=0))
    assert items == [1, 2, 3, 0]

    items = list(range(6))
    fischer_yates_shuffle(items, _StubRng(randrange_value=0))
    assert items == [1, 2, 3, 4, 5, 0]


def test_fischer_yates_partner_equals_i_is_a_no_op():
    """``a == i`` tauscht nichts (:738)."""
    items = [0, 1, 2, 3]
    fischer_yates_shuffle(items, _StubRng(randrange_value=99))
    assert items == [0, 1, 2, 3]


def test_fischer_yates_leaves_a_single_entry_alone():
    items = [7]
    fischer_yates_shuffle(items, _StubRng())
    assert items == [7]                                   # :731-733


def test_fischer_yates_is_a_permutation():
    items = list(range(50))
    fischer_yates_shuffle(items)
    assert sorted(items) == list(range(50))


# ---------------------------------------------------------------------------
# balanced_shuffle — Playlist.pm:748-786
# ---------------------------------------------------------------------------

def test_balanced_shuffle_spreads_equal_groups_evenly():
    """Zwei Gruppen zu je drei Eintraegen werden verschraenkt, nicht blockweise
    (:766-772: ``offset = rand(1/$itemsCount)*$count``, ``spacer =
    $count/$itemsCount``, Jitter ``±itemsCount/10``). Mit ``rand() == 0.5``
    landen beide Gruppen auf denselben Gewichten 1.0/3.0/5.0 und werden pro
    Gewicht in Einfuege-Reihenfolge (A zuerst) sortiert (:777-779)."""
    pairs = [(0, "A"), (1, "A"), (2, "A"), (3, "B"), (4, "B"), (5, "B")]
    rng = _StubRng(randrange_value=0, random_value=0.5)
    out = balanced_shuffle(pairs, True, rng)
    assert sorted(out) == [0, 1, 2, 3, 4, 5]
    # je Gruppe wurde intern gemischt (fischer_yates mit a=0): A -> 1,2,0
    assert out == [1, 4, 2, 5, 0, 3]


def test_balanced_shuffle_unknown_artist_is_its_own_group():
    """``$artist || ''`` (:847) — alles ohne Artist ist EINE Gruppe."""
    pairs = [(0, ""), (1, "A"), (2, "")]
    out = balanced_shuffle(pairs, True, _StubRng(random_value=0.0))
    assert sorted(out) == [0, 1, 2]


# ---------------------------------------------------------------------------
# reshuffle — Playlist.pm:788-1000
# ---------------------------------------------------------------------------

def test_shuffle_off_keeps_the_playlist_order():
    """Ohne Shuffle ist die Liste die Identitaet (:826, beide Zweige
    uebersprungen) — ``playlist shuffle 0`` stellt die Reihenfolge her."""
    p = _player(playlist=(10, 11, 12, 13), position=2, shuffle=0)
    reshuffle(p)
    assert p.shufflelist == [0, 1, 2, 3]
    assert p.playlist_position == 2            # Remap :963-980 (hier Identitaet)


def test_reshuffle_is_a_permutation_and_puts_the_playing_track_first():
    """``:857-877``: der laufende Titel wird an Position 0 der Liste getauscht;
    ``playlist_position`` folgt ihm (:963-980)."""
    p = _player(playlist=tuple(range(20)), position=7, shuffle=1)
    reshuffle(p)
    assert sorted(p.shufflelist) == list(range(20))
    assert p.shufflelist[0] == 7
    assert p.playlist_position == 0
    assert playlist_item(p, 0) == 7


def test_reshuffle_without_preserve_remaps_the_position_but_keeps_the_track():
    """``reshuffle($client, 1)`` (Load/Repeat-Wrap) mischt auch den laufenden
    Titel mit (:792) — der Queue-Eintrag spielt aber weiterhin DENSELBEN Track
    (:963-980)."""
    p = _player(playlist=tuple(range(20)), position=7, shuffle=1)
    reshuffle(p, dont_preserve_current=True)
    assert sorted(p.shufflelist) == list(range(20))
    assert playlist_item(p, p.playlist_position) == 7


def test_reshuffle_of_an_empty_playlist_empties_the_list():
    p = _player(playlist=(), position=0, shuffle=1)
    reshuffle(p)
    assert p.shufflelist == []                            # :795-802


def test_reshuffle_survives_a_position_past_the_end():
    p = _player(playlist=(10, 11), position=9, shuffle=1)
    reshuffle(p)
    assert sorted(p.shufflelist) == [0, 1]
    assert 0 <= p.playlist_position < 2                   # :982-986


# ---------------------------------------------------------------------------
# Album-Shuffle — Playlist.pm:880-958
# ---------------------------------------------------------------------------

def test_album_shuffle_keeps_albums_together(monkeypatch):
    """``shuffle == 2`` mischt ALBEN und laesst die Titel im Album in ihrer
    Reihenfolge (:956-961); das Album des laufenden Titels kommt nach vorne
    (:939-949)."""
    monkeypatch.setattr(
        manager_mod, "_entry_album_ids",
        lambda entries: {50: 100, 51: 100, 52: 200, 53: 200, 54: 300, 55: 300})
    p = _player(playlist=(50, 51, 52, 53, 54, 55), position=2, shuffle=2)
    reshuffle(p)
    assert sorted(p.shufflelist) == list(range(6))
    # Album 200 (Titel 2,3) steht vorne, und die beiden Titel bleiben benachbart
    first, second = p.shufflelist[0], p.shufflelist[1]
    assert {first, second} == {2, 3}
    for album in ((0, 1), (4, 5)):
        assert p.shufflelist.index(album[0]) + 1 == p.shufflelist.index(album[1])


def test_album_shuffle_skips_remote_entries(monkeypatch):
    """``next if Slim::Music::Info::isRemoteURL($track)`` (:889) — ein Stream
    landet in keiner Album-Gruppe und damit NICHT in der neuen Liste."""
    monkeypatch.setattr(
        manager_mod, "_entry_album_ids", lambda entries: {0: 1, 1: 1})
    p = _player(playlist=(0, "http://radio.invalid/x.mp3", 1), position=0,
                shuffle=2)
    reshuffle(p)
    assert p.shufflelist == [0, 1]


# ---------------------------------------------------------------------------
# shufflelist() — Pflege/Invariante (Perl: nach JEDER Playlist-Aenderung)
# ---------------------------------------------------------------------------

def test_shufflelist_is_rebuilt_when_the_mode_changes():
    """Perl ruft ``reshuffle`` im Kommando (``Commands.pm:1195``); Pfade, die
    das nicht koennen, werden hier nachgezogen — die Liste passt danach zum
    Modus."""
    p = _player(playlist=tuple(range(12)), position=3, shuffle=0)
    assert shufflelist(p) == list(range(12))
    p.shuffle = 1                                   # wie ``playlist shuffle 1``
    lst = shufflelist(p)
    assert sorted(lst) == list(range(12))
    assert lst[0] == 3                              # laufender Titel vorne
    p.shuffle = 0
    assert shufflelist(p) == list(range(12))        # Reihenfolge kommt zurueck


def test_shufflelist_is_rebuilt_when_the_playlist_changed_behind_its_back():
    """Ein ``playlist add`` aus einem anderen Modul haengt an ``playlist`` an,
    ohne die Liste zu pflegen — die naechste Aufloesung zieht sie nach."""
    p = _player(playlist=tuple(range(4)), position=1, shuffle=1)
    assert sorted(shufflelist(p)) == [0, 1, 2, 3]
    p.playlist.append(99)                            # Fremdpfad
    lst = shufflelist(p)
    assert sorted(lst) == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Aufloesung: Queue-Position -> Playlist-Eintrag (Playlist.pm:78-84)
# ---------------------------------------------------------------------------

def test_queue_position_maps_through_the_shufflelist():
    p = _player(playlist=(10, 11, 12, 13), position=0, shuffle=1)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert queue_playlist_index(p, 0) == 2
    assert queue_playlist_index(p, 3) == 1
    assert playlist_item(p, 2) == 13
    assert queue_order(p) == [12, 10, 13, 11]
    assert queue_playlist_index(p, 4) is None
    assert queue_playlist_index(p, -1) is None


def test_queue_position_of_a_track_uses_bug_14662_mapping():
    """``Commands.pm:1784-1790``: beim Spielen eines bestimmten Titels wird
    dessen Playlist-Index in seine Queue-Position umgerechnet."""
    p = _player(playlist=(10, 11, 12, 13), position=0, shuffle=1)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert playlist_queue_position(p, 12) == 0
    assert playlist_queue_position(p, 11) == 3
    assert playlist_queue_position(p, 77) is None


def test_queue_order_with_shuffle_off_is_the_playlist():
    p = _player(playlist=(10, 11, 12), position=0, shuffle=0)
    assert queue_order(p) == [10, 11, 12]


# ---------------------------------------------------------------------------
# nextsong — StreamingController.pm:847-899
# ---------------------------------------------------------------------------

def test_nextsong_advances_along_the_shuffle_list():
    p = _player(playlist=(10, 11, 12, 13), position=0, shuffle=1)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert nextsong(p) == 1                          # :881-882
    assert playlist_item(p, nextsong(p)) == 10       # = playlist[0]


def test_nextsong_repeat_song_stays_on_the_queue_position():
    p = _player(playlist=(10, 11, 12, 13), position=2, shuffle=1, repeat=1)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert nextsong(p) == 2                          # :871-873
    assert playlist_item(p, 2) == 13


def test_nextsong_wrap_does_not_reshuffle_by_default():
    """``reshuffleOnRepeat`` ist per Default 0 (``Utils/Prefs.pm:184``, live
    bestaetigt) — die Liste bleibt stehen."""
    p = _player(playlist=(10, 11, 12, 13), position=3, shuffle=1, repeat=2)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert nextsong(p) == 0                          # :883-893
    assert p.shufflelist == [2, 0, 3, 1]


def test_nextsong_wrap_reshuffles_with_the_pref(monkeypatch):
    """``:885-891``: shuffle + repeat-all + ``reshuffleOnRepeat`` →
    ``reshuffle($client, 1)`` (ohne den laufenden Titel zu schonen)."""
    monkeypatch.setattr(manager_mod, "_reshuffle_on_repeat", lambda: True)
    p = _player(playlist=(10, 11, 12, 13), position=3, shuffle=1, repeat=2)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert nextsong(p) == 0
    assert sorted(p.shufflelist) == [0, 1, 2, 3]
    assert p.shufflelist != [2, 0, 3, 1], "es wurde neu gemischt"


def test_nextsong_repeat_off_still_ends_the_playlist():
    p = _player(playlist=(10, 11, 12, 13), position=3, shuffle=1, repeat=0)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert nextsong(p) is None                       # :897


# ---------------------------------------------------------------------------
# Pflege in den Mutations-Pfaden des Ports
# ---------------------------------------------------------------------------

def test_playlist_add_reshuffles_and_keeps_the_current_track_first():
    """``Commands.pm:1766`` ``reshuffle($client, undef)`` nach ``add``."""
    pm = object.__new__(PlayerManager)
    pm.players = {}
    p = _player(playlist=tuple(range(10)), position=4, shuffle=1)
    p.shufflelist = list(range(10))
    p.shufflelist_mode = 1
    pm.players[p.mac] = p
    assert pm.playlist_add(p.mac, 99) is True
    assert p.playlist[-1] == 99
    assert sorted(p.shufflelist) == list(range(11))
    assert p.shufflelist[0] == 4                     # laufender Titel vorne
    assert 10 in p.shufflelist                       # der neue Eintrag ist erreichbar


def test_playlist_remove_drops_the_queue_position_and_renumbers():
    """``Playlist::removeTrack`` :380-400."""
    pm = object.__new__(PlayerManager)
    pm.players = {}
    p = _player(playlist=(10, 11, 12, 13), position=0, shuffle=1)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    pm.players[p.mac] = p
    assert pm.playlist_remove(p.mac, 1) is True      # Queue-Position 1 = Track 10
    assert p.playlist == [11, 12, 13]
    assert p.shufflelist == [1, 2, 0]


def test_playlist_remove_without_shuffle_restores_the_identity():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    p = _player(playlist=(10, 11, 12, 13), position=0, shuffle=0)
    pm.players[p.mac] = p
    assert pm.playlist_remove(p.mac, 1) is True
    assert p.playlist == [10, 12, 13]
    assert p.shufflelist == [0, 1, 2]


def test_playlist_clear_empties_the_list():
    pm = object.__new__(PlayerManager)
    pm.players = {}
    p = _player(playlist=(10, 11), position=1, shuffle=1)
    p.shufflelist = [1, 0]
    p.shufflelist_mode = 1
    pm.players[p.mac] = p
    assert pm.playlist_clear(p.mac) is True
    assert p.shufflelist == []


# ---------------------------------------------------------------------------
# Wiedergabe: ``playlist play``/``jump`` folgen der Shuffle-Liste
# ---------------------------------------------------------------------------

class _FakeHandler:
    def __init__(self) -> None:
        self.strm: list = []
        self.remote: list = []

    async def send_strm_to_player(self, mac, track_id, start_seconds=0.0):
        self.strm.append((mac, track_id))
        return True

    async def send_remote_stream(self, mac, url, codec, **kw):
        self.remote.append((mac, url))
        return True


def _pm(playlist, position=0, shuffle=1):
    pm = object.__new__(PlayerManager)
    pm.players = {}
    handler = _FakeHandler()
    pm._protocol_handler = handler
    p = _player(playlist=playlist, position=position, shuffle=shuffle,
                mode="play")
    p.power = True
    pm.players[p.mac] = p
    return pm, p, handler


def test_playlist_play_resolves_the_queue_position_through_the_shufflelist():
    pm, p, handler = _pm((10, 11, 12, 13), position=0)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert asyncio.run(pm.playlist_play(p.mac, 1)) is True
    assert handler.strm[-1] == (p.mac, 10)           # playlist[shufflelist[1]]
    assert p.playlist_position == 1                  # die Queue-Position


def test_playlist_jump_follows_the_shuffle_order():
    pm, p, handler = _pm((10, 11, 12, 13), position=0)
    p.shufflelist = [2, 0, 3, 1]
    p.shufflelist_mode = 1
    assert asyncio.run(pm.playlist_jump(p.mac, "+1")) is True
    assert handler.strm[-1] == (p.mac, 10)
    assert p.playlist_position == 1


def test_playlist_play_stream_entry_uses_the_shuffle_order():
    streams = ["http://a.invalid/1.mp3", "http://b.invalid/2.mp3"]
    pm, p, handler = _pm(tuple(streams), position=0)
    p.shufflelist = [1, 0]
    p.shufflelist_mode = 1
    assert asyncio.run(pm.playlist_play(p.mac, 0)) is True
    assert handler.remote[-1] == (p.mac, streams[1])
