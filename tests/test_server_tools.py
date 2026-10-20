from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="smsverbot-server-tools-")
os.environ["MCP_DB"] = str(Path(_TMP) / "server-tools.db")
os.environ["PROVIDER"] = "mock"
os.environ["CODE_WAIT_SECONDS"] = "5"

import server
from config import Settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def api_key() -> str:
    key_id, plain_key = server.store.create_key("server-tools")
    server.store.topup(key_id, 100_000)
    return plain_key


async def test_list_services_default_country(api_key):
    result = await server.list_services(api_key)
    assert result["ok"] is True
    assert len(result["services"]) == 3


async def test_get_price(api_key):
    result = await server.get_price(api_key, "telegram", "ru")
    assert result["ok"] is True
    assert result["price_kop"] == 3000


async def test_empty_api_key_falls_back_to_env_default(api_key, monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", api_key)
    monkeypatch.setattr(
        server.settings, "default_api_key", Settings.from_env().default_api_key
    )

    result = await server.get_balance("")
    assert result["ok"] is True
    assert result["balance_kop"] == 100000


async def test_buy_number(api_key):
    result = await server.buy_number(api_key, "telegram", "ru")
    assert result["ok"] is True
    assert result["order_id"] > 0
    assert result["phone"].startswith("+7900")
    assert result["balance_kop"] == 97000


async def test_wait_for_code(api_key):
    bought = await server.buy_number(api_key, "telegram", "ru")
    assert bought["ok"] is True
    result = await server.wait_for_code(api_key, bought["order_id"])
    assert result["ok"] is True
    assert result["code"] == "482913"
    assert result["status"] == "delivered"


async def test_cancel_order_refunds_once(api_key):
    bought = await server.buy_number(api_key, "telegram", "ru")
    assert bought["ok"] is True

    first = await server.cancel_order(api_key, bought["order_id"])
    assert first["ok"] is True
    assert first["refunded_kop"] == 3000

    second = await server.cancel_order(api_key, bought["order_id"])
    assert second["ok"] is True
    assert second["refunded_kop"] == 0

    balance = await server.get_balance(api_key)
    assert balance["ok"] is True
    assert balance["balance_kop"] == 100000


async def test_cancel_after_delivery_does_not_refund(api_key):
    bought = await server.buy_number(api_key, "telegram", "ru")
    await server.wait_for_code(api_key, bought["order_id"])

    cancelled = await server.cancel_order(api_key, bought["order_id"])
    assert cancelled["ok"] is True
    assert cancelled["refunded_kop"] == 0

    balance = await server.get_balance(api_key)
    assert balance["ok"] is True
    assert balance["balance_kop"] == 97000


async def test_unexpected_error_is_logged(api_key, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server.sales, "quote", boom)
    caplog.set_level(logging.ERROR, logger="smsverbot")

    result = await server.get_price(api_key, "telegram", "ru")
    assert result["ok"] is False
    assert result["error"] == "INTERNAL"
    assert "unhandled tool error" in caplog.text
