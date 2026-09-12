"""players_loop / serverstatus:playerfeedback-Felder wie im Perl-LMS.

Perl-Quellen:
* ``Slim/Control/Queries.pm:2624-2663`` — Feldliste beider Loops.
* ``Slim/Player/SqueezePlay.pm:79-85`` — HELO-Caps ``ModelName`` →
  ``_modelName`` (Feld ``modelname``), ``Firmware`` → ``firmware``.
* ``Slim/Networking/Slimproto.pm:962`` — uuid = ``unpack H32`` (16 Byte);
  ``Slim/Player/Client.pm:167-168`` — Null-uuid wird zu ``undef``.
* ``Slim/Networking/Slimproto.pm:1027-1120`` + ``Display/*::vfdmodel`` —
  ``displaytype``; für ``squeezeplay``/``controller``/``receiver`` ist das
  ``Slim::Display::NoDisplay`` → ``'none'`` (``NoDisplay.pm:59``).

Ground Truth: Live-Probe ``players 0 10`` gegen den Perl-LMS 9.1.1
(192.168.1.90:9000, read-only) am 2026-09-12 — u. a. jive
``{"model": "squeezeplay", "modelname": "SB Player", "uuid":
"7b62791c2b9745c7bb392644e774ceb2", "displaytype": "none"}`` und
squeezelite ``{"modelname": "SqueezeLite", "uuid": null}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lyrion.player.manager import (
    PlayerManager,
    display_type_for,
    model_name_for,
)
from lyrion.player.state import PlayerState
from lyrion.web.api import JSONRPCAPI

JIVE_MAC = "CA:C8:C7:26:6D:38"


def _pm_with(*players: PlayerState) -> PlayerManager:
    pm = PlayerManager()
    pm.players = {p.mac: p for p in players}
    return pm


def _jive(**kw: Any) -> PlayerState:
    base: dict[str, Any] = dict(
        mac=JIVE_MAC,
        name="Küche",
        ip="192.168.1.225",
        port=48494,
        model="squeezeplay",
        firmware="9.0.0-r1583",
        connected=True,
        power=True,
        uuid="7b62791c2b9745c7bb392644e774ceb2",
        model_name="SB Player",
    )
    base.update(kw)
    return PlayerState(**base)


# ── Feldwert-Tabellen (Perl-Quellen + Live-Probe) ──────────────────────────


def test_modelname_class_fallbacks_are_perls_class_defaults():
    # Overrides: SqueezePlay.pm:58, Boom.pm:209, Receiver.pm:43,
    # HTTP.pm:70, Disconnected.pm:59; Basis (Client.pm:936) liefert nichts.
    assert model_name_for("squeezeplay") == "SqueezePlay"
    assert model_name_for("boom") == "Squeezebox Boom"
    assert model_name_for("receiver") == "Squeezebox Receiver"
    assert model_name_for("http") == "Web Client"
    assert model_name_for("disconnected") == "Dummy Client"
    assert model_name_for("squeezebox2") == ""      # kein Override
    assert model_name_for("transporter") == ""


def test_displaytype_matches_perls_display_classes():
    assert display_type_for("squeezeplay") == "none"
    assert display_type_for("controller") == "none"
    assert display_type_for("receiver") == "none"
    assert display_type_for("squeezebox2") == "graphic-320x32"
    assert display_type_for("boom") == "graphic-160x32"
    assert display_type_for("transporter") == "graphic-320x32"
    # Live: Squeezebox Radio (deviceid 'baby') meldet 'none'
    assert display_type_for("baby") == "none"
    # Perl lässt das Feld nur beim http-Modell weg (Queries.pm:2647-2649)
    assert display_type_for("http") is None


# ── players-Loop ───────────────────────────────────────────────────────────


def test_players_loop_reports_caps_modelname_uuid_and_displaytype():
    _pm_with(_jive())
    entry = asyncio.run(
        JSONRPCAPI()._slim_request(JIVE_MAC, ["players", "0", "10"])
    )["players_loop"][0]
    assert entry["modelname"] == "SB Player"        # caps ModelName
    assert entry["uuid"] == "7b62791c2b9745c7bb392644e774ceb2"
    assert entry["uuid"] != JIVE_MAC                # NOT the MAC address
    assert entry["displaytype"] == "none"
    assert entry["firmware"] == "9.0.0-r1583"       # caps Firmware
    assert entry["ip"] == "192.168.1.225:48494"     # Perl ipport = ip:port
    # Feldliste exakt wie Queries.pm:2624-2663 (seq_no: Perl emittiert es,
    # sobald der Client eine Sequenznummer hat — live bei allen 4 Playern).
    assert set(entry) == {
        "playerindex", "playerid", "uuid", "ip", "name", "seq_no", "model",
        "modelname", "power", "isplaying", "isplayer", "canpoweroff",
        "connected", "firmware", "displaytype",
    }


def test_uuid_is_null_without_helo_uuid_never_the_mac():
    # Live: squeezelite/esp32 → "uuid": null
    _pm_with(_jive(mac="00:00:00:00:00:00", uuid=""))
    entry = asyncio.run(
        JSONRPCAPI()._slim_request("00:00:00:00:00:00", ["players", "0", "10"])
    )["players_loop"][0]
    assert entry["uuid"] is None


def test_modelname_falls_back_to_the_class_table():
    _pm_with(_jive(model_name="", model="boom"))
    entry = asyncio.run(
        JSONRPCAPI()._slim_request(JIVE_MAC, ["players", "0", "10"])
    )["players_loop"][0]
    assert entry["modelname"] == "Squeezebox Boom"
    assert entry["displaytype"] == "graphic-160x32"


def test_displaytype_omitted_for_http_model():
    _pm_with(_jive(model="http", model_name="Web Client"))
    entry = asyncio.run(
        JSONRPCAPI()._slim_request(JIVE_MAC, ["players", "0", "10"])
    )["players_loop"][0]
    assert "displaytype" not in entry


# ── serverstatus-Loop (gleiche Werte, keine erfundenen) ────────────────────


def test_serverstatus_players_loop_uses_real_values():
    _pm_with(_jive())
    res = asyncio.run(
        JSONRPCAPI()._slim_request(JIVE_MAC, ["serverstatus", "0", "5"])
    )
    entry = res["players_loop"][0]
    assert entry["uuid"] == "7b62791c2b9745c7bb392644e774ceb2"
    assert entry["displaytype"] == "none"       # war: "None"
    assert entry["modelname"] == "SB Player"    # war: das Modell
    assert entry["firmware"] == "9.0.0-r1583"
    assert entry["playerindex"] == "0"          # live: String
