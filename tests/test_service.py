from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import Settings
from providers.mock import MockProvider
from service import (
    DailyLimitExceeded,
    InsufficientFunds,
    OrderNotFound,
    RateLimited,
    SalesService,
    ServiceError,
)
from store import Store, ledger_sum

pytestmark = pytest.mark.asyncio


def make_env(tmp_path, **overrides):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        provider="mock",
        markup=2.5,
        rate_limit_per_15min=20,
        daily_code_limit=200,
        code_wait_seconds=5,
        usd_kop_rate=9200,
    )
    for name, value in overrides.items():
        setattr(settings, name, value)
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    provider = MockProvider()
    service = SalesService(store, provider, settings)
    key_id, plain_key = store.create_key("test")
    store.topup(key_id, 100_000)
    return SimpleNamespace(
        settings=settings,
        store=store,
        provider=provider,
        service=service,
        key_id=key_id,
        plain_key=plain_key,
    )


async def test_create_key_and_topup(tmp_path):
    env = make_env(tmp_path)
    assert env.store.get_balance(env.key_id) == 100_000
    found = env.store.find_key(env.plain_key)
    assert found is not None and found["id"] == env.key_id
    assert env.store.find_key("svb_wrong") is None


async def test_quote_markup(tmp_path):
    env = make_env(tmp_path)
    telegram = await env.service.quote("telegram", "ru")
    assert telegram["cost_kop"] == 1200
    assert telegram["price_kop"] == 3000
    google = await env.service.quote("google", "ru")
    assert google["cost_kop"] == 600
    assert google["price_kop"] == 1600


async def test_buy_debits_balance(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    assert bought["price_kop"] == 3000
    assert bought["phone"].startswith("+7900")
    assert bought["order_id"] > 0
    assert env.store.get_balance(env.key_id) == 97_000
    order = env.store.get_order(bought["order_id"])
    assert order is not None and order["status"] == "waiting"


async def test_insufficient_funds_cancels_provider_order(tmp_path):
    env = make_env(tmp_path)
    poor_id, _ = env.store.create_key("poor")
    with pytest.raises(InsufficientFunds):
        await env.service.buy(poor_id, "telegram", "ru")
    orders = env.store.list_orders(poor_id, 10)
    assert len(orders) == 1
    assert orders[0]["status"] == "cancelled"
    assert env.provider._orders[orders[0]["provider_order_id"]]["cancelled"] is True
    assert env.store.get_balance(poor_id) == 0


async def test_wait_for_code_delivers(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "id")
    result = await env.service.wait_for_code(env.key_id, bought["order_id"], timeout=5)
    assert result["status"] == "delivered"
    assert result["code"] == "482913"
    assert result["phone"] == bought["phone"]
    assert env.store.get_order(bought["order_id"])["status"] == "delivered"


async def test_wait_timeout_returns_pending_without_refund(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    result = await env.service.wait_for_code(env.key_id, bought["order_id"], timeout=0)
    assert result["status"] == "pending"
    assert result["code"] is None
    assert env.store.get_order(bought["order_id"])["status"] == "waiting"
    assert env.store.get_balance(env.key_id) == 97_000


async def test_cancel_refunds_once(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    first = await env.service.cancel(env.key_id, bought["order_id"])
    assert first == {"status": "cancelled", "refunded_kop": 3000}
    assert env.store.get_balance(env.key_id) == 100_000
    order = env.store.get_order(bought["order_id"])
    assert order["status"] == "cancelled"


async def test_second_cancel_refunds_zero(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    await env.service.cancel(env.key_id, bought["order_id"])
    second = await env.service.cancel(env.key_id, bought["order_id"])
    assert second == {"status": "cancelled", "refunded_kop": 0}
    assert env.store.get_balance(env.key_id) == 100_000
    refunds = [r for r in env.store.list_ledger(env.key_id, 20) if r["kind"] == "refund"]
    assert len(refunds) == 1


async def test_history_and_ledger_rows(tmp_path):
    env = make_env(tmp_path)
    await env.service.buy(env.key_id, "google", "ru")
    orders = env.service.history(env.key_id, 10)
    assert len(orders) == 1 and orders[0]["service"] == "google"
    ledger = env.store.list_ledger(env.key_id, 20)
    kinds = [r["kind"] for r in ledger]
    assert kinds == ["debit", "topup"]
    assert ledger_sum(ledger) == env.store.get_balance(env.key_id)


async def test_daily_limit_blocks(tmp_path):
    env = make_env(tmp_path, daily_code_limit=1)
    await env.service.buy(env.key_id, "telegram", "ru")
    with pytest.raises(DailyLimitExceeded):
        await env.service.buy(env.key_id, "telegram", "ru")
    assert env.store.daily_code_count(env.key_id) == 1


async def test_rate_limiter_blocks_21st_request(tmp_path):
    env = make_env(tmp_path, rate_limit_per_15min=20, daily_code_limit=500)
    for _ in range(20):
        await env.service.buy(env.key_id, "google", "ru")
    with pytest.raises(RateLimited):
        await env.service.buy(env.key_id, "google", "ru")
    assert env.store.allow_request(env.key_id) is False


async def test_unknown_service_raises_service_error(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ServiceError) as exc:
        await env.service.quote("unknown", "ru")
    assert exc.value.code == "UNAVAILABLE"


async def test_wait_for_foreign_order_not_found(tmp_path):
    env = make_env(tmp_path)
    bought = await env.service.buy(env.key_id, "telegram", "ru")
    other_id, _ = env.store.create_key("other")
    with pytest.raises(OrderNotFound):
        await env.service.wait_for_code(other_id, bought["order_id"])
