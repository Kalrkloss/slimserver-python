"""Tests for SlimProtoClient method wiring.

Regression: send_cli (and its sibling frame helpers) were accidentally
nested inside the module-level ``_advance_after_track`` helper instead of
being methods on ``SlimProtoClient`` — so ``PlayerManager.send_command``
found no ``send_cli`` and every player-bound command (power/IR/display)
was silently dropped.
"""

import inspect

from lyrion.networking import protocol as proto
from lyrion.networking.protocol import SlimProtoClient


def test_slimproto_client_exposes_player_command_methods():
    for name in ("send_cli", "send_stat", "send_body", "send_stmu", "send_anic"):
        assert callable(getattr(SlimProtoClient, name, None)), (
            f"{name} must be a SlimProtoClient method, not a nested helper"
        )


def test_send_cli_is_not_a_module_function():
    # send_cli must live on the class; it must NOT be a nested function of
    # _advance_after_track.
    assert not hasattr(proto, "send_cli")
    assert inspect.isfunction(getattr(proto, "_advance_after_track", None))
