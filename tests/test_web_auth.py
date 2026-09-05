"""Unit + integration tests for web HTTP authorization."""

import asyncio
import base64

from lyrion.web import app as wapp
from lyrion.web.app import _authorize


# ── pure helper ──────────────────────────────────────────────────────

def test_authorize_disabled_allows_all():
    assert _authorize({"headers": []}, authorize=False, username="", password="") is True


def test_authorize_requires_basic_header():
    assert _authorize({"headers": []}, authorize=True, username="u", password="p") is False


def test_authorize_rejects_wrong_password():
    cred = "Basic " + base64.b64encode(b"u:wrong").decode()
    scope = {"headers": [(b"authorization", cred.encode())]}
    assert _authorize(scope, True, "u", "p") is False


def test_authorize_accepts_valid_credentials():
    cred = "Basic " + base64.b64encode(b"u:p").decode()
    scope = {"headers": [(b"authorization", cred.encode())]}
    assert _authorize(scope, True, "u", "p") is True


# ── wiring into the ASGI app ──────────────────────────────────────────

def test_app_returns_401_when_unauthorized(tmp_path, monkeypatch):
    static = tmp_path / "html"
    static.mkdir()
    (static / "index.html").write_text("ok")

    asgi = wapp.create_app(static_dir=str(static))
    monkeypatch.setattr(wapp, "_auth_config", lambda: (True, "u", "p"))

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        sent.append(event)

    asyncio.run(asgi(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""},
        receive, send,
    ))

    assert sent[0]["status"] == 401
    assert any(k == b"www-authenticate" for k, _ in sent[0]["headers"])
