"""Внешний край: песочница в инструментах, коды ошибок кошелька, admin-API.

Проверяем ровно то, что видит клиент: ответы MCP-инструментов и HTTP админ-API.
Ничего не мокаем в бизнес-логике — работаем с реальным Store на временной БД.
"""

from __future__ import annotations

import json
import os
import asyncio
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="smsverbot-edge-")
os.environ.setdefault("MCP_DB", str(Path(_TMP) / "edge.db"))
os.environ.setdefault("PROVIDER", "mock")

import admin_api  # noqa: E402
import server  # noqa: E402
from config import Settings  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from service import SalesService  # noqa: E402
from store import Store  # noqa: E402

TOKEN = "edge-token-456"


# ----------------------------------------------------------------------
# admin-API
# ----------------------------------------------------------------------
@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_api, "TOKEN", TOKEN)
    store = Store(str(tmp_path / "admin.db"), 20)
    httpd = admin_api.build_server("127.0.0.1", 0, store)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"base": f"http://127.0.0.1:{port}", "store": store}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(base, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_create_sandbox_key_and_flag_in_list(api):
    status, created = call(
        api["base"], "POST", "/keys",
        {"label": "tg-sandbox", "owner_tg": 501, "sandbox": True},
    )
    assert status == 201, created
    assert created["sandbox"] is True
    assert created["api_key"].startswith("svb_")

    status, body = call(api["base"], "GET", "/keys?owner_tg=501")
    assert status == 200
    row = body["keys"][0]
    assert row["key_id"] == created["key_id"]
    assert row["sandbox"] is True
    assert "api_key" not in row
    # флаг реально в БД, а не только в ответе
    assert api["store"].is_sandbox(created["key_id"]) is True

    # по умолчанию ключ не песочница
    plain = call(api["base"], "POST", "/keys", {"label": "tg-real", "owner_tg": 501})[1]
    assert plain["sandbox"] is False
    rows = call(api["base"], "GET", "/keys?owner_tg=501")[1]["keys"]
    flags = {r["key_id"]: r["sandbox"] for r in rows}
    assert flags[plain["key_id"]] is False


def test_revoke_by_owner_kills_all_keys_and_is_idempotent(api):
    first = call(
        api["base"], "POST", "/keys",
        {"label": "tg-77-a", "owner_tg": 77, "sandbox": True},
    )[1]
    second = call(api["base"], "POST", "/keys", {"label": "tg-77-b", "owner_tg": 77})[1]
    other = call(api["base"], "POST", "/keys", {"label": "tg-78", "owner_tg": 78})[1]

    assert api["store"].find_key(first["api_key"]) is not None

    status, body = call(
        api["base"], "POST", "/keys/revoke_by_owner",
        {"owner_tg": 77, "reason": "ban"},
    )
    assert status == 200 and body["ok"] is True
    assert body["revoked"] == 2

    # оба ключа владельца больше не аутентифицируются, чужой ключ жив
    assert api["store"].find_key(first["api_key"]) is None
    assert api["store"].find_key(second["api_key"]) is None
    assert api["store"].find_key(other["api_key"]) is not None

    status, again = call(
        api["base"], "POST", "/keys/revoke_by_owner", {"owner_tg": 77, "reason": "ban"},
    )
    assert status == 200 and again["revoked"] == 0

    status, missing = call(api["base"], "POST", "/keys/revoke_by_owner", {"owner_tg": 999})
    assert status == 200 and missing["revoked"] == 0


def test_sandbox_toggle_endpoint(api):
    created = call(api["base"], "POST", "/keys", {"label": "tg-90", "owner_tg": 90})[1]
    key_id = created["key_id"]
    assert created["sandbox"] is False

    status, body = call(
        api["base"], "POST", f"/keys/{key_id}/sandbox", {"enabled": True}
    )
    assert status == 200 and body["sandbox"] is True
    assert call(api["base"], "GET", f"/keys/{key_id}")[1]["sandbox"] is True
    assert call(api["base"], "GET", "/keys?owner_tg=90")[1]["keys"][0]["sandbox"] is True

    status, body = call(
        api["base"], "POST", f"/keys/{key_id}/sandbox", {"enabled": False}
    )
    assert status == 200 and body["sandbox"] is False
    assert call(api["base"], "GET", f"/keys/{key_id}")[1]["sandbox"] is False


def test_sandbox_endpoint_validation_and_404(api):
    status, body = call(api["base"], "POST", "/keys/deadbeef/sandbox", {"enabled": True})
    assert status == 404 and body["error"] == "NOT_FOUND"

    created = call(api["base"], "POST", "/keys", {"label": "tg-91"})[1]
    status, body = call(api["base"], "POST", f"/keys/{created['key_id']}/sandbox", {})
    assert status == 400 and body["error"] == "VALIDATION"

    status, body = call(
        api["base"], "POST", f"/keys/{created['key_id']}/sandbox", {"enabled": "yes"}
    )
    assert status == 400 and body["error"] == "VALIDATION"

    status, body = call(
        api["base"], "POST", "/keys", {"label": "tg-92", "sandbox": "yes"}
    )
    assert status == 400 and body["error"] == "VALIDATION"


def test_admin_api_requires_token(api):
    status, body = call(api["base"], "GET", "/health", token=None)
    assert status == 401 and body["error"] == "UNAUTHORIZED"

    status, body = call(
        api["base"], "POST", "/keys/revoke_by_owner",
        {"owner_tg": 77, "reason": "ban"}, token="wrong",
    )
    assert status == 401 and body["error"] == "UNAUTHORIZED"

    status, body = call(
        api["base"], "POST", "/keys", {"label": "x", "sandbox": True}, token=""
    )
    assert status == 401 and body["error"] == "UNAUTHORIZED"


# ----------------------------------------------------------------------
# MCP-инструменты
# ----------------------------------------------------------------------
@pytest.fixture()
def edge(tmp_path, monkeypatch):
    """Свежий Store + сервис на нём, подменяющие глобальные объекты server."""
    settings = Settings(db_path=str(tmp_path / "edge.db"), provider="mock", code_wait_seconds=30)
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    assert server._ensure_sandbox_column.__module__ == "server"
    service = SalesService(store, MockProvider(), settings)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "sales", service)
    monkeypatch.setattr(server, "settings", settings)
    with store._db() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "sandbox" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN sandbox INTEGER DEFAULT 0")

    def make_key(sandbox: bool, topup_kop: int = 100_000) -> str:
        key_id, plain = store.create_key("edge")
        if topup_kop:
            store.topup(key_id, topup_kop)
        if sandbox:
            with store._db() as conn:
                conn.execute("UPDATE api_keys SET sandbox = 1 WHERE id = ?", (key_id,))
        return plain

    return {"store": store, "service": service, "settings": settings, "make_key": make_key}


def test_handle_maps_wallet_errors_to_contract_codes():
    assert server._handle(server.WalletUnavailable("bridge down")) == {
        "ok": False,
        "error": "UNAVAILABLE",
        "message": "bridge down",
    }
    assert server._handle(server.WalletBanned("banned"))["error"] == "BANNED"
    assert (
        server._handle(server.WalletInsufficient("no money"))["error"]
        == "INSUFFICIENT_FUNDS"
    )


def test_handle_maps_foreign_wallet_error_by_name():
    class WalletBanned(Exception):  # не наш класс, но то же имя
        pass

    result = server._handle(WalletBanned("user banned"))
    assert result["ok"] is False and result["error"] == "BANNED"


@pytest.mark.asyncio
async def test_sandbox_buy_skips_provider_and_charges_nothing(edge, monkeypatch):
    key = edge["make_key"](True)

    async def boom(*args, **kwargs):
        raise AssertionError("provider must not be called in sandbox")

    monkeypatch.setattr(server.sales.provider, "buy", boom)

    bought = await server.buy_number(key, "telegram", "ru")
    assert bought["ok"] is True, bought
    assert bought["sandbox"] is True
    assert bought["order_id"] > 0
    assert bought["phone"].startswith("+100")
    assert bought["price_kop"] == 0
    assert bought["balance_kop"] == 0
    assert bought["expires_at"]
    assert edge["store"].get_balance(edge["store"].find_key(key)["id"]) == 100_000


@pytest.mark.asyncio
async def test_sandbox_wait_for_code_returns_fixed_code_within_6s(edge):
    key = edge["make_key"](True)
    bought = await server.buy_number(key, "telegram", "ru")
    assert bought["ok"] is True

    started = time.monotonic()
    waited = await server.wait_for_code(key, bought["order_id"])
    elapsed = time.monotonic() - started

    assert waited["ok"] is True, waited
    assert waited["code"] == "123456"
    assert waited["status"] == "delivered"
    assert waited["sandbox"] is True
    assert elapsed <= 6.0

    # повторный вызов мгновенно отдаёт тот же код
    again = await server.wait_for_code(key, bought["order_id"])
    assert again["code"] == "123456"


@pytest.mark.asyncio
async def test_sandbox_wait_never_hangs_past_six_seconds(edge, monkeypatch):
    """Даже если песочница сервиса не успела, клиент получает 123456 ≤ 6 c."""
    key = edge["make_key"](True)
    bought = await server.buy_number(key, "telegram", "ru")
    assert bought["ok"] is True

    async def stuck(*args, **kwargs):
        return {"status": "pending", "code": None, "phone": bought["phone"], "seconds_left": 0}

    monkeypatch.setattr(server.sales, "wait_for_code", stuck)

    started = time.monotonic()
    waited = await server.wait_for_code(key, bought["order_id"])
    assert time.monotonic() - started <= 6.0
    assert waited["ok"] is True and waited["code"] == "123456"
    assert edge["store"].get_order(bought["order_id"])["code"] == "123456"


@pytest.mark.asyncio
async def test_sandbox_flags_in_whoami_and_balance(edge):
    key = edge["make_key"](True)
    who = await server.whoami(key)
    assert who["ok"] is True and who["sandbox"] is True
    assert who["balance_rub"] == 0.0

    balance = await server.get_balance(key)
    assert balance["ok"] is True and balance["sandbox"] is True
    assert balance["balance_kop"] == 0 and balance["balance_rub"] == 0.0


@pytest.mark.asyncio
async def test_real_key_reports_sandbox_false(edge):
    key = edge["make_key"](False)
    who = await server.whoami(key)
    assert who["ok"] is True and who["sandbox"] is False

    bought = await server.buy_number(key, "telegram", "ru")
    assert bought["ok"] is True and bought["sandbox"] is False
    assert bought["balance_kop"] == 97_000

    balance = await server.get_balance(key)
    assert balance["sandbox"] is False and balance["balance_kop"] == 97_000


@pytest.mark.asyncio
async def test_sandbox_cancel_refunds_nothing(edge):
    key = edge["make_key"](True)
    bought = await server.buy_number(key, "telegram", "ru")
    cancelled = await server.cancel_order(key, bought["order_id"])
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["refunded_kop"] == 0
    assert cancelled["sandbox"] is True


@pytest.mark.asyncio
async def test_buy_maps_wallet_errors_to_contract_codes(edge):
    """Ошибки кошелька доходят до клиента как UNAVAILABLE / BANNED / INSUFFICIENT_FUNDS."""
    key = edge["make_key"](False)
    service = edge["service"]
    store = edge["store"]

    class StubWallet:
        enabled = True

        def __init__(self, error):
            self.error = error

        async def debit(self, owner_tg, amount_kop, ref):
            raise self.error

        async def balance(self, owner_tg):
            raise self.error

    key_id = str(store.find_key(key)["id"])
    with store._db() as conn:
        conn.execute("UPDATE api_keys SET owner_tg = 4242 WHERE id = ?", (key_id,))

    original = service.wallet
    try:
        service.wallet = StubWallet(server.WalletUnavailable("bridge down"))
        result = await server.buy_number(key, "telegram", "ru")
        assert result["ok"] is False and result["error"] == "UNAVAILABLE"

        service.wallet = StubWallet(server.WalletBanned("banned"))
        result = await server.buy_number(key, "telegram", "ru")
        assert result["ok"] is False and result["error"] == "BANNED"

        service.wallet = StubWallet(server.WalletInsufficient("no money"))
        result = await server.buy_number(key, "telegram", "ru")
        assert result["ok"] is False and result["error"] == "INSUFFICIENT_FUNDS"
    finally:
        service.wallet = original

    # неудачные попытки не оставили висящих заказов и не списали деньги
    assert store.get_balance(key_id) == 100_000


@pytest.mark.asyncio
async def test_lifespan_auto_cancels_expired_order(edge):
    """Фоновая задача из lifespan реально гасит просроченный заказ с возвратом."""
    store = edge["store"]
    key_id, plain = store.create_key("autocancel")
    store.topup(key_id, 100_000)
    bought = await server.buy_number(plain, "telegram", "ru")
    assert bought["ok"] is True

    with store._db() as conn:
        conn.execute(
            "UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (bought["order_id"],),
        )

    async with server.lifespan(server.mcp):
        for _ in range(40):
            await asyncio.sleep(0.1)
            if store.get_order(bought["order_id"])["status"] == "cancelled":
                break

    assert store.get_order(bought["order_id"])["status"] == "cancelled"
    assert store.get_balance(key_id) == 100_000
    kinds = [row["kind"] for row in store.list_ledger(key_id, 10)]
    assert kinds.count("refund") == 1
    assert server.auto_cancel_task() is None


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_auto_cancel(monkeypatch):
    started = {"count": 0, "stopped": False}

    class StubSales:
        async def run_auto_cancel_loop(self, interval: float, stop_event):
            started["count"] += 1
            await stop_event.wait()
            started["stopped"] = True

    monkeypatch.setattr(server, "sales", StubSales())
    await server.stop_auto_cancel()  # чистое состояние перед проверкой
    assert server.auto_cancel_task() is None

    async with server.lifespan(server.mcp):
        task = server.auto_cancel_task()
        assert task is not None and not task.done()
        await asyncio.sleep(0)  # дать задаче реально стартовать
        assert started["count"] == 1

    assert server.auto_cancel_task() is None
    assert started["stopped"] is True


@pytest.mark.asyncio
async def test_start_auto_cancel_is_idempotent(monkeypatch):
    class StubSales:
        async def run_auto_cancel_loop(self, interval: float, stop_event):
            await stop_event.wait()

    monkeypatch.setattr(server, "sales", StubSales())
    await server.stop_auto_cancel()

    first = server.start_auto_cancel()
    second = server.start_auto_cancel()
    try:
        assert first is not None and first is second
    finally:
        await server.stop_auto_cancel()
    assert server.auto_cancel_task() is None
