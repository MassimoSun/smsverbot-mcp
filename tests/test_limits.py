from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config import Settings
from providers.mock import MockProvider
from service import DailyLimitExceeded, RateLimited, SalesService
from store import Store

pytestmark = pytest.mark.asyncio


def make_env(tmp_path, **overrides):
    settings = Settings(db_path=str(tmp_path / "limits.db"), provider="mock")
    for name, value in overrides.items():
        setattr(settings, name, value)
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    service = SalesService(store, MockProvider(), settings)
    key_id, _ = store.create_key("limits")
    store.topup(key_id, 100_000)
    return SimpleNamespace(settings=settings, store=store, service=service, key_id=key_id)


async def test_default_daily_spend_limit_blocks_expensive_buy(tmp_path):
    env = make_env(tmp_path, daily_spend_limit_rub=1, daily_code_limit=999)
    with pytest.raises(DailyLimitExceeded) as exc:
        await env.service.buy(env.key_id, "telegram", "id")
    assert "Daily spend limit" in str(exc.value)


async def test_spend_accumulates_and_per_key_limit_applies(tmp_path):
    env = make_env(tmp_path, daily_spend_limit_rub=10_000, daily_code_limit=999)
    first = await env.service.buy(env.key_id, "telegram", "id")
    spent = env.store.spend_today_kop(env.key_id)
    assert spent == first["price_kop"] > 0

    env.store.set_daily_limit(env.key_id, spent + 1)
    assert env.store.key_daily_limit(env.key_id) == spent + 1
    with pytest.raises(DailyLimitExceeded):
        await env.service.buy(env.key_id, "telegram", "id")


async def test_refund_reduces_daily_spend(tmp_path):
    env = make_env(tmp_path, daily_spend_limit_rub=10_000, daily_code_limit=999)
    order = await env.service.buy(env.key_id, "telegram", "id")
    before = env.store.spend_today_kop(env.key_id)
    await env.service.cancel(env.key_id, order["order_id"])
    after = env.store.spend_today_kop(env.key_id)
    assert before > 0 and after < before


async def test_wait_concurrency_is_capped_and_released(tmp_path):
    env = make_env(tmp_path, max_waits_per_key=1, max_waits_global=10, code_wait_seconds=3)
    order = await env.service.buy(env.key_id, "telegram", "id")

    hold = asyncio.create_task(env.service.wait_for_code(env.key_id, order["order_id"], timeout=2))
    await asyncio.sleep(0.2)
    with pytest.raises(RateLimited):
        await env.service.wait_for_code(env.key_id, order["order_id"], timeout=1)

    await hold
    result = await env.service.wait_for_code(env.key_id, order["order_id"], timeout=0.1)
    assert result["status"] in {"pending", "delivered"}


async def test_global_wait_cap(tmp_path):
    env = make_env(tmp_path, max_waits_per_key=5, max_waits_global=1, code_wait_seconds=2)
    o1 = await env.service.buy(env.key_id, "telegram", "id")
    o2 = await env.service.buy(env.key_id, "telegram", "id")

    task = asyncio.create_task(env.service.wait_for_code(env.key_id, o1["order_id"], timeout=1.5))
    await asyncio.sleep(0.2)
    with pytest.raises(RateLimited):
        await env.service.wait_for_code(env.key_id, o2["order_id"], timeout=1)
    await task


async def test_revoked_key_cannot_authenticate(tmp_path):
    env = make_env(tmp_path)
    key_id, plain = env.store.create_key("revokable")
    assert env.store.find_key(plain) is not None
    assert env.store.revoke_key(key_id) is True
    assert env.store.find_key(plain) is None
    assert env.store.revoke_key(key_id) is False
