"""CLI: Spielerlose Kommandos binden an den ersten Client (Perl clientRandom).

Live-Beleg (Perl-LMS :9090, 2026-09-12, read-only):

    mixer volume ?   -> 24:0a:c4:29:77:90 mixer volume 23
    status 0 1       -> 24:0a:c4:29:77:90 status 0 1 player_name%3ASchlafzimmer …
    players 0 1      -> players 0 1 count%3A4 …          (kein Client-Präfix)
    player count ?   -> player count 4                    (kein Client-Präfix)

Perl wählt also für spielergebundene Kommandos den ERSTEN verbundenen Client
(``Slim/Player/Client.pm:411-415`` ``clientRandom()`` → ``(clients())[0]``,
Aufruf in ``Slim/Control/Stdio.pm:82-86``) und stellt dessen id voran
(``Stdio.pm:131``). Wir hatten nur einen nie gesetzten ``_default_player`` und
antworteten deshalb „no player selected".
"""

from __future__ import annotations

from lyrion.control.request import RequestDispatcher
from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState

MAC1 = "1C:87:2C:47:FC:36"
MAC2 = "02:11:22:33:44:55"


def _pm_with(*macs: str) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {
        mac: PlayerState(mac=mac, name=f"P{mac[-2:]}", ip="127.0.0.1", port=0)
        for mac in macs
    }
    return pm


def test_default_player_is_the_first_connected_client():
    _pm_with(MAC1, MAC2)
    assert RequestDispatcher().get_default_player() == MAC1


def test_default_player_is_none_without_players():
    _pm_with()
    assert RequestDispatcher().get_default_player() is None


def test_explicit_default_player_wins():
    _pm_with(MAC1, MAC2)
    d = RequestDispatcher()
    d.set_default_player(MAC2)
    assert d.get_default_player() == MAC2
