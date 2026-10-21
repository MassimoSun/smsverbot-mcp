"""Wallet bridge + sandbox + auto-cancel core behaviour.

Money lives in the bot wallet; these tests use a fake wallet client that
counts calls, so a double refund or a hidden debit fails loudly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web

from config import Settings
from providers.mock import MockProvider
from providers.sandbox import SANDBOX_CODE, SandboxProvider
from service import (
    InsufficientFunds,
    SalesService,
    WalletBanned,
    WalletClient,
    WalletInsufficient,
    WalletUnavailable,
)
from store import Store, now_iso


class FakeWallet:
    """Stand-in for WalletClient: records every move, can fail on demand."""

    enabled = True

    def __init__(
        self,
        balance_kop: int = 100_000,
        debit_error: Exception | None = None,
        refund_error: Exception | None = None,
        balance_error: Exception | None = None,
    ) -> None:
        self.balance_kop = int(balance_kop)
        self.debit_error = debit_error
        self.refund_error = refund_error
        self.balance_error = balance_error
        self.debit_calls: list[tuple[int, int, str]] = []
        self.refund_calls: list[tuple[int, int, str]] = []
        self.balance_calls = 0

    async def balance(self, owner_tg: int) -> dict:
        self.balance_calls += 1
        if self.balance_error:
            raise self.balance_error
        return {
            "balance_rub": self.balance_kop / 100,
            "balance_kop": self.balance_kop,
            "banned": False,
        }

    async def debit(self, owner_tg: int, amount_kop: int, ref: str) -> dict:
        self.debit_calls.append((int(owner_tg), int(amount_kop), str(ref)))
        if self.debit_error:
            raise self.debit_error
        self.balance_kop -= int(amount_kop)
        return {
            "debited": True,
            "balance_kop": self.balance_kop,
            "balance_rub": self.balance_kop / 100,
        }

    async def refund(self, owner_tg: int, amount_kop: int, ref: str) -> dict:
        self.refund_calls.append((int(owner_tg), int(amount_kop), str(ref)))
        if self.refund_error:
            raise self.refund_error
        self.balance_kop += int(amount_kop)
        return {
            "refunded": True,
            "balance_kop": self.balance_kop,
            "balance_rub": self.balance_kop / 100,
        }


def make_env(tmp_path, wallet=None, **overrides):
    settings = Settings(
        db_path=str(tmp_path / "wallet.db"),
        provider="mock",
        markup=2.5,
        rate_limit_per_15min=50,
        daily_code_limit=200,
        daily_spend_limit_rub=10_000,
        code_wait_seconds=30,
        sandbox_daily_limit=50,
        sandbox_code_delay=4,
        wallet_token="test-token",
    )
    for name, value in overrides.items():
        setattr(settings, name, value)
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    provider = MockProvider()
    wallet = wallet if wallet is not None else FakeWallet()
    service = SalesService(store, provider, settings, wallet=wallet)
    key_id, plain_key = store.create_key("wallet", owner_tg=777)
    return SimpleNamespace(
        settings=settings,
        store=store,
        provider=provider,
        wallet=wallet,
        service=service,
        key_id=key_id,
        plain_key=plain_key,
    )


def expire_order(store: Store, order_id: int) -> None:
    with store._db() as conn:
        conn.execute(
            "UPDATE orders SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", int(order_id)),
        )


# --- sandbox -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_buy_is_free_and_delivers_fixed_code(tmp_path):
    env = make_env(tmp_path, sandbox_code_delay=0.05)
    key_id, _ = env.store.create_key("sandbox", owner_tg=777, sandbox=True)
    assert env.store.is_sandbox(key_id) is True

    bought = await env.service.buy(key_id, "telegram", "id")

    assert bought["sandbox"] is True
    assert bought["price_kop"] == 0
    assert bought["order_id"] > 0
    assert env.wallet.debit_calls == []  # no money moved
    assert env.provider._orders == {}  # provider never called
    assert env.store.get_balance(key_id) == 0
    with env.store._db() as conn:
        row = conn.execute(
            "SELECT sandbox FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
    assert int(row["sandbox"]) == 1

    result = await env.service.wait_for_code(key_id, bought["order_id"], timeout=2)
    assert result["status"] == "delivered"
    assert result["code"] == SANDBOX_CODE == "123456"
    assert env.store.get_order(bought["order_id"])["status"] == "delivered"


@pytest.mark.asyncio
async def test_sandbox_cancel_refunds_zero(tmp_path):
    env = make_env(tmp_path)
    key_id, _ = env.store.create_key("sandbox", owner_tg=777, sandbox=True)
    bought = await env.service.buy(key_id, "discord", "br")
    cancelled = await env.service.cancel(key_id, bought["order_id"])
    assert cancelled == {"status": "cancelled", "refunded_kop": 0}
    assert env.wallet.refund_calls == []
    assert env.store.get_order(bought["order_id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sandbox_daily_limit(tmp_path):
    env = make_env(tmp_path, sandbox_daily_limit=1, daily_code_limit=999)
    key_id, _ = env.store.create_key("sandbox", owner_tg=777, sandbox=True)
    await env.service.buy(key_id, "google", "kz")
    from service import RateLimited

    with pytest.raises(RateLimited):
        await env.service.buy(key_id, "google", "kz")


@pytest.mark.asyncio
async def test_sandbox_daily_limit_counts_cancelled_ops(tmp_path):
    """A buy/cancel loop must not reset the sandbox counter."""
    env = make_env(tmp_path, sandbox_daily_limit=2, daily_code_limit=999)
    key_id, _ = env.store.create_key("sandbox", owner_tg=777, sandbox=True)
    for _ in range(2):
        bought = await env.service.buy(key_id, "google", "kz")
        await env.service.cancel(key_id, bought["order_id"])

    from service import RateLimited

    with pytest.raises(RateLimited):
        await env.service.buy(key_id, "google", "kz")
    assert env.store.sandbox_ops_today(key_id) == 2


@pytest.mark.asyncio
async def test_sandbox_provider_fixture():
    provider = SandboxProvider(code_delay=0.05)
    services = {s["code"] for s in await provider.list_services()}
    countries = {c["code"] for c in await provider.list_countries()}
    assert {"telegram", "discord", "google"} <= services
    assert {"id", "br", "kz"} <= countries
    assert await provider.price("telegram", "id") == 0

    bought = await provider.buy("telegram", "id")
    assert bought["provider_order_id"].startswith("sandbox-")
    assert bought["phone"] == "+10000000000"
    assert bought["cost_kop"] == 0
    assert await provider.code(bought["provider_order_id"]) is None

    await asyncio.sleep(0.06)
    assert await provider.code(bought["provider_order_id"]) == "123456"
    assert await provider.cancel(bought["provider_order_id"]) is True


# --- wallet debit on buy -------------------------------------------------


@pytest.mark.asyncio
async def test_real_buy_debits_wallet_once_with_order_ref(tmp_path):
    env = make_env(tmp_path)
    env.store.topup(env.key_id, 100_000)

    bought = await env.service.buy(env.key_id, "telegram", "ru")

    assert len(env.wallet.debit_calls) == 1
    owner_tg, amount_kop, ref = env.wallet.debit_calls[0]
    assert owner_tg == 777
    assert amount_kop == bought["price_kop"] == 3000
    assert ref == f"order:{bought['order_id']}"
    assert env.store.get_order(bought["order_id"])["status"] == "waiting"


@pytest.mark.asyncio
async def test_wallet_unavailable_creates_no_order(tmp_path):
    env = make_env(tmp_path, wallet=FakeWallet(debit_error=WalletUnavailable("down")))
    before = env.store.list_orders(env.key_id, 10)

    with pytest.raises(WalletUnavailable):
        await env.service.buy(env.key_id, "telegram", "ru")

    assert env.store.list_orders(env.key_id, 10) == before  # nothing left behind
    # the provider order was released, not leaked
    assert all(o["cancelled"] for o in env.provider._orders.values())


@pytest.mark.asyncio
async def test_wallet_insufficient_cancels_order(tmp_path):
    env = make_env(tmp_path, wallet=FakeWallet(debit_error=InsufficientFunds("no money")))
    with pytest.raises(InsufficientFunds):
        await env.service.buy(env.key_id, "telegram", "ru")
    orders = env.store.list_orders(env.key_id, 10)
    assert len(orders) == 1 and orders[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_wallet_banned_propagates(tmp_path):
    env = make_env(tmp_path, wallet=FakeWallet(debit_error=WalletBanned("banned")))
    with pytest.raises(WalletBanned) as exc:
        await env.service.buy(env.key_id, "telegram", "ru")
    assert exc.value.code == "BANNED"


# --- refunds / auto-cancel ----------------------------------------------


@pytest.mark.asyncio
async def test_cancel_refunds_through_wallet_once(tmp_path):
    env = make_env(tmp_path)
    env.store.topup(env.key_id, 100_000)
    bought = await env.service.buy(env.key_id, "telegram", "ru")

    first = await env.service.cancel(env.key_id, bought["order_id"])
    second = await env.service.cancel(env.key_id, bought["order_id"])

    assert first == {"status": "cancelled", "refunded_kop": 3000}
    assert second == {"status": "cancelled", "refunded_kop": 0}
    assert len(env.wallet.refund_calls) == 1
    assert env.wallet.refund_calls[0] == (777, 3000, f"refund:{bought['order_id']}")


@pytest.mark.asyncio
async def test_auto_cancel_expired_refunds_exactly_once(tmp_path):
    env = make_env(tmp_path)
    env.store.topup(env.key_id, 100_000)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    expire_order(env.store, bought["order_id"])

    cancelled = await env.service.auto_cancel_expired(now_iso())

    assert cancelled == 1
    assert len(env.wallet.refund_calls) == 1
    assert env.wallet.refund_calls[0][2] == f"refund:{bought['order_id']}"
    order = env.store.get_order(bought["order_id"])
    assert order["status"] == "cancelled"
    assert env.provider._orders[order["provider_order_id"]]["cancelled"] is True
    refunds = [r for r in env.store.list_ledger(env.key_id, 20) if r["kind"] == "refund"]
    assert len(refunds) == 1

    # a second sweep finds nothing: the order is no longer 'waiting'
    assert await env.service.auto_cancel_expired(now_iso()) == 0
    assert len(env.wallet.refund_calls) == 1


@pytest.mark.asyncio
async def test_auto_cancel_skips_live_and_delivered_orders(tmp_path):
    env = make_env(tmp_path)
    env.store.topup(env.key_id, 100_000)
    live = await env.service.buy(env.key_id, "telegram", "ru")
    delivered = await env.service.buy(env.key_id, "google", "ru")
    await env.service.wait_for_code(env.key_id, delivered["order_id"], timeout=5)
    expire_order(env.store, live["order_id"])
    expire_order(env.store, delivered["order_id"])

    assert await env.service.auto_cancel_expired(now_iso()) == 1
    assert env.store.get_order(live["order_id"])["status"] == "cancelled"
    assert env.store.get_order(delivered["order_id"])["status"] == "delivered"
    assert len(env.wallet.refund_calls) == 1


@pytest.mark.asyncio
async def test_auto_cancel_keeps_order_waiting_when_wallet_down(tmp_path):
    wallet = FakeWallet()
    env = make_env(tmp_path, wallet=wallet)
    env.store.topup(env.key_id, 100_000)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    expire_order(env.store, bought["order_id"])
    wallet.refund_error = WalletUnavailable("down")

    assert await env.service.auto_cancel_expired(now_iso()) == 0
    assert env.store.get_order(bought["order_id"])["status"] == "waiting"

    wallet.refund_error = None
    assert await env.service.auto_cancel_expired(now_iso()) == 1
    assert env.store.get_order(bought["order_id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_auto_cancel_loop_survives_a_failing_tick(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    calls = {"n": 0}

    async def boom(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick failed")
        return 0

    monkeypatch.setattr(env.service, "auto_cancel_expired", boom)
    task = asyncio.create_task(env.service.run_auto_cancel_loop(interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2  # the loop kept running after the failure


# --- balance -------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_uses_wallet_and_falls_back_to_cache(tmp_path):
    wallet = FakeWallet(balance_kop=50_000)
    env = make_env(tmp_path, wallet=wallet)
    env.store.topup(env.key_id, 100_000)

    snapshot = await env.service.wallet_balance(env.key_id)
    assert snapshot["source"] == "wallet"
    assert snapshot["balance_rub"] == 500.0
    assert env.store.get_balance(env.key_id) == 50_000  # cache refreshed

    wallet.balance_error = WalletUnavailable("down")
    cached = await env.service.wallet_balance(env.key_id)
    assert cached["source"] == "cache"
    assert cached["balance_kop"] == 50_000
    assert int(await env.service.balance(env.key_id)) == 50_000


@pytest.mark.asyncio
async def test_sandbox_balance_is_zero(tmp_path):
    env = make_env(tmp_path)
    key_id, _ = env.store.create_key("sandbox", owner_tg=777, sandbox=True)
    assert int(env.service.balance(key_id)) == 0
    snapshot = await env.service.wallet_balance(key_id)
    assert snapshot["sandbox"] is True and snapshot["balance_rub"] == 0.0
    assert env.wallet.balance_calls == 0


# --- migrations ----------------------------------------------------------


def test_migration_adds_sandbox_and_expires_at(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY,
            label TEXT,
            key_hash TEXT UNIQUE,
            balance_kop INTEGER DEFAULT 0,
            created_at TEXT,
            owner_tg INTEGER,
            revoked_at TEXT,
            daily_limit_kop INTEGER
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT,
            provider TEXT,
            provider_order_id TEXT,
            service TEXT,
            country TEXT,
            phone TEXT,
            price_kop INTEGER,
            cost_kop INTEGER,
            status TEXT,
            code TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO api_keys (id, label, key_hash, balance_kop, created_at)
        VALUES ('old-key', 'legacy', 'hash-old', 5000, '2026-01-01T00:00:00+00:00');
        INSERT INTO orders (key_id, provider, provider_order_id, service, country, phone,
                            price_kop, cost_kop, status, created_at, updated_at)
        VALUES ('old-key', 'mock', 'mock-1', 'telegram', 'ru', '+7900000001',
                3000, 1200, 'waiting', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = Store(db_path)

    with store._db() as conn:
        key_cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
        order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    assert "sandbox" in key_cols
    assert "expires_at" in order_cols

    # legacy rows survive and get the default sandbox flag
    assert store.get_balance("old-key") == 5000
    assert store.is_sandbox("old-key") is False
    assert store.set_sandbox("old-key", True) is True
    assert store.is_sandbox("old-key") is True

    # a legacy order without expires_at is not treated as expired
    assert store.list_expired_orders(now_iso()) == []
    # new orders carry expires_at and become visible to the sweeper
    new_id = store.create_order(
        key_id="old-key",
        provider="mock",
        provider_order_id="mock-2",
        service="telegram",
        country="ru",
        phone="+7900000002",
        price_kop=3000,
        cost_kop=1200,
        status="waiting",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    expired = store.list_expired_orders(now_iso())
    assert [o["id"] for o in expired] == [new_id]
    assert store.find_key("svb_nope") is None


# --- config ---------------------------------------------------------------


def test_settings_wallet_defaults(monkeypatch):
    for name in (
        "WALLET_URL",
        "WALLET_TOKEN",
        "AUTO_CANCEL_INTERVAL",
        "SANDBOX_DAILY_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.wallet_url == "http://127.0.0.1:8767"
    assert settings.wallet_token == ""
    assert settings.auto_cancel_interval == 60
    assert settings.sandbox_daily_limit == 50


def test_settings_wallet_env_overrides(monkeypatch):
    monkeypatch.setenv("WALLET_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("WALLET_TOKEN", "tok")
    monkeypatch.setenv("AUTO_CANCEL_INTERVAL", "5")
    monkeypatch.setenv("SANDBOX_DAILY_LIMIT", "3")
    settings = Settings.from_env()
    assert settings.wallet_url == "http://127.0.0.1:9999/"
    assert settings.wallet_token == "tok"
    assert settings.auto_cancel_interval == 5
    assert settings.sandbox_daily_limit == 3

    # a garbage interval falls back to the default instead of crashing
    monkeypatch.setenv("AUTO_CANCEL_INTERVAL", "not-a-number")
    assert Settings.from_env().auto_cancel_interval == 60


def test_wallet_client_disabled_without_token(tmp_path):
    client = WalletClient("http://127.0.0.1:8767", "", timeout=1)
    assert client.enabled is False
    settings = Settings(db_path=str(tmp_path / "nw.db"), wallet_token="")
    service = SalesService(Store(settings.db_path), MockProvider(), settings)
    assert service.wallet is None  # no bridge configured -> local cache mode

    settings_with_token = Settings(
        db_path=str(tmp_path / "nw2.db"), wallet_token="tok"
    )
    wired = SalesService(Store(settings_with_token.db_path), MockProvider(), settings_with_token)
    assert isinstance(wired.wallet, WalletClient)
    assert wired.wallet.base_url == settings_with_token.wallet_url


class _StubWalletBridge:
    """Minimal stand-in for the bot's bridge: same routes, same JSON shape."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.mode = "ok"
        self.balance_rub = 250.0

    async def _check(self, request: web.Request) -> None:
        assert request.headers.get("X-Admin-Token") == "secret-token"

    async def balance(self, request: web.Request) -> web.Response:
        await self._check(request)
        self.requests.append(
            {"route": "balance", "owner_tg": int(request.query["owner_tg"])}
        )
        if self.mode == "down":
            return web.Response(status=503, text="down")
        return web.json_response(
            {"ok": True, "owner_tg": 1, "balance_rub": self.balance_rub, "banned": False}
        )

    async def debit(self, request: web.Request) -> web.Response:
        await self._check(request)
        body = await request.json()
        self.requests.append({"route": "debit", **body})
        if self.mode == "down":
            return web.Response(status=503, text="down")
        if self.mode == "insufficient":
            return web.json_response({"ok": False, "error": "INSUFFICIENT_FUNDS"})
        if self.mode == "banned":
            return web.json_response({"ok": False, "error": "BANNED"})
        self.balance_rub = round(self.balance_rub - float(body["amount_rub"]), 2)
        return web.json_response(
            {"ok": True, "balance_rub": self.balance_rub, "debited": True}
        )

    async def refund(self, request: web.Request) -> web.Response:
        await self._check(request)
        body = await request.json()
        self.requests.append({"route": "refund", **body})
        if self.mode == "down":
            return web.Response(status=503, text="down")
        self.balance_rub = round(self.balance_rub + float(body["amount_rub"]), 2)
        return web.json_response(
            {"ok": True, "balance_rub": self.balance_rub, "refunded": True}
        )


@pytest_asyncio.fixture
async def bridge():
    stub = _StubWalletBridge()
    app = web.Application()
    app.router.add_get("/wallet/balance", stub.balance)
    app.router.add_post("/wallet/debit", stub.debit)
    app.router.add_post("/wallet/refund", stub.refund)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield stub, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_wallet_client_wire_protocol(bridge):
    stub, url = bridge
    client = WalletClient(url, "secret-token", timeout=3)

    snapshot = await client.balance(777)
    assert snapshot["balance_kop"] == 25_000
    assert stub.requests[-1] == {"route": "balance", "owner_tg": 777}

    debited = await client.debit(777, 1200, ref="order:42")
    assert debited["debited"] is True
    assert debited["balance_kop"] == 23_800
    assert stub.requests[-1] == {
        "route": "debit",
        "owner_tg": 777,
        "amount_rub": 12.0,
        "ref": "order:42",
    }

    refunded = await client.refund(777, 1200, ref="refund:42")
    assert refunded["refunded"] is True
    assert refunded["balance_kop"] == 25_000
    assert stub.requests[-1]["ref"] == "refund:42"
    assert stub.requests[-1]["amount_rub"] == 12.0


@pytest.mark.asyncio
async def test_wallet_client_maps_error_codes(bridge):
    stub, url = bridge
    client = WalletClient(url, "secret-token", timeout=3)

    stub.mode = "insufficient"
    with pytest.raises(WalletInsufficient) as exc:
        await client.debit(777, 3000, ref="order:1")
    assert exc.value.code == "INSUFFICIENT_FUNDS"

    stub.mode = "banned"
    with pytest.raises(WalletBanned) as exc:
        await client.debit(777, 3000, ref="order:2")
    assert exc.value.code == "BANNED"

    stub.mode = "down"
    with pytest.raises(WalletUnavailable):
        await client.debit(777, 3000, ref="order:3")
    with pytest.raises(WalletUnavailable):
        await client.refund(777, 3000, ref="refund:3")
    with pytest.raises(WalletUnavailable):
        await client.balance(777)


@pytest.mark.asyncio
async def test_wallet_client_rejects_wrong_token(bridge):
    _stub, url = bridge
    client = WalletClient(url, "wrong-token", timeout=3)
    # The stub asserts on the token; a failed request surfaces as UNAVAILABLE.
    with pytest.raises(WalletUnavailable):
        await client.balance(777)


@pytest.mark.asyncio
async def test_wallet_client_httpx_fallback(bridge, monkeypatch):
    """Deployments without aiohttp fall back to httpx on the same wire format."""
    import service as service_module

    stub, url = bridge
    monkeypatch.setattr(service_module, "aiohttp", None)
    client = WalletClient(url, "secret-token", timeout=3)

    snapshot = await client.balance(777)
    assert snapshot["balance_kop"] == 25_000
    debited = await client.debit(777, 500, ref="order:9")
    assert debited["debited"] is True
    assert stub.requests[-1] == {
        "route": "debit",
        "owner_tg": 777,
        "amount_rub": 5.0,
        "ref": "order:9",
    }

    stub.mode = "banned"
    with pytest.raises(WalletBanned):
        await client.debit(777, 500, ref="order:10")
    stub.mode = "down"
    with pytest.raises(WalletUnavailable):
        await client.refund(777, 500, ref="refund:9")


@pytest.mark.asyncio
async def test_wallet_client_unreachable_port():
    client = WalletClient("http://127.0.0.1:1", "secret-token", timeout=1)
    with pytest.raises(WalletUnavailable):
        await client.balance(777)


@pytest.mark.asyncio
async def test_buy_over_real_bridge_debits_once(tmp_path, bridge):
    stub, url = bridge
    env = make_env(tmp_path, wallet=WalletClient(url, "secret-token", timeout=3))

    bought = await env.service.buy(env.key_id, "telegram", "ru")

    debits = [r for r in stub.requests if r["route"] == "debit"]
    assert len(debits) == 1
    assert debits[0]["owner_tg"] == 777
    assert debits[0]["amount_rub"] == 30.0
    assert debits[0]["ref"] == f"order:{bought['order_id']}"
    assert bought["price_kop"] == 3000

    cancelled = await env.service.cancel(env.key_id, bought["order_id"])
    assert cancelled["refunded_kop"] == 3000
    refunds = [r for r in stub.requests if r["route"] == "refund"]
    assert len(refunds) == 1
    assert refunds[0]["ref"] == f"refund:{bought['order_id']}"

    # second cancel: no second refund over the wire
    await env.service.cancel(env.key_id, bought["order_id"])
    assert len([r for r in stub.requests if r["route"] == "refund"]) == 1
    assert json.loads(json.dumps(stub.requests)) == stub.requests

