"""Unit tests for CLI authentication (the ``login`` command)."""

import asyncio

from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.control.cli_commands import cmd_login


def _login(handler, password):
    ctx = CLIContext()
    result = asyncio.run(cmd_login(handler, ctx, [password]))
    return result, ctx.authenticated


def test_login_accepts_when_no_server_password_configured():
    handler = CLIHandler()  # _auth_password is None/empty → auth disabled
    result, authenticated = _login(handler, "whatever")
    assert result == ["login: 1"]
    assert authenticated is True


def test_login_rejects_wrong_password():
    handler = CLIHandler()
    handler.set_auth_password("secret")
    result, authenticated = _login(handler, "wrong")
    assert result == ["login: 0"]
    assert authenticated is False


def test_login_accepts_correct_password():
    handler = CLIHandler()
    handler.set_auth_password("secret")
    result, authenticated = _login(handler, "secret")
    assert result == ["login: 1"]
    assert authenticated is True
