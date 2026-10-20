from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="smsverbot-auth-")
os.environ["MCP_DB"] = str(Path(_TMP) / "auth.db")
os.environ["PROVIDER"] = "mock"

import server


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _RequestContext:
    def __init__(self, request: _Request) -> None:
        self.request = request


class _Ctx:
    def __init__(self, headers: dict[str, str]) -> None:
        self.request_context = _RequestContext(_Request(headers))


class _BrokenHeaders:
    @property
    def headers(self):
        raise AttributeError("no headers here")


class _BrokenRequestContext:
    def __init__(self) -> None:
        self.request = _BrokenHeaders()


class _BrokenCtx:
    def __init__(self) -> None:
        self.request_context = _BrokenRequestContext()


@pytest.fixture(autouse=True)
def no_default_key(monkeypatch):
    monkeypatch.setattr(server.settings, "default_api_key", "")


def test_header_only():
    ctx = _Ctx({"authorization": "Bearer svb_header"})
    assert server.resolve_api_key("", ctx) == "svb_header"


def test_header_with_argument():
    ctx = _Ctx({"authorization": "Bearer svb_header"})
    assert server.resolve_api_key("svb_argument", ctx) == "svb_argument"


def test_no_header():
    ctx = _Ctx({})
    assert server.resolve_api_key("", ctx) == ""


def test_no_context():
    assert server.resolve_api_key("") == ""


def test_empty_strings():
    ctx = _Ctx({"authorization": ""})
    assert server.resolve_api_key("", ctx) == ""


def test_default_key_beats_header(monkeypatch):
    monkeypatch.setattr(server.settings, "default_api_key", "svb_default")
    ctx = _Ctx({"authorization": "Bearer svb_header"})
    assert server.resolve_api_key("", ctx) == "svb_default"


def test_missing_attributes_returns_empty():
    assert server.resolve_api_key("", _BrokenCtx()) == ""
    assert server.resolve_api_key("", object()) == ""


@pytest.mark.asyncio
async def test_api_key_is_optional_in_tool_schema():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name in ("whoami", "get_price"):
        schema = tools[name].inputSchema
        assert "api_key" in schema["properties"]
        assert "api_key" not in schema.get("required", [])


@pytest.mark.asyncio
async def test_whoami_valid_key():
    key_id, plain_key = server.store.create_key("whoami-valid")
    server.store.topup(key_id, 12_345)

    result = await server.whoami(plain_key)
    assert result["ok"] is True
    assert result["key_id"] == key_id
    assert result["balance_rub"] == 123.45


@pytest.mark.asyncio
async def test_whoami_garbage_key():
    result = await server.whoami("svb_not_a_real_key")
    assert result["ok"] is False
    assert result["error"] == "UNAUTHORIZED"
