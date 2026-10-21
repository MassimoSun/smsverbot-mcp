from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pytest

from providers.base import ProviderError
from providers.fivesim import FiveSimProvider

pytestmark = pytest.mark.asyncio

PRICES = {
    "indonesia": {
        "google": {
            "virtual58": {"cost": 0.0821, "count": 8090},
            "virtual53": {"cost": 0.1923, "count": 2373},
            "virtual4": {"cost": 0.2, "count": 25000},
            "virtual34": {"cost": 0.1523, "count": 515355},
        }
    }
}


def expected_kop(value: str, rate: int) -> int:
    amount = Decimal(value) * Decimal(rate)
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


async def test_price_and_buy_use_cheapest_in_stock_operator():
    provider = FiveSimProvider("test-token", usd_kop_rate=9200)
    calls: list[str] = []

    async def fake_get(path: str, params: dict | None = None) -> dict:
        calls.append(path)
        if path == "/guest/prices":
            return PRICES
        if path == "/user/buy/activation/indonesia/virtual58/google":
            return {"id": 1, "phone": "+628000000001", "price": 0.0821}
        raise AssertionError(f"unexpected request: {path}")

    provider._get = fake_get  # type: ignore[method-assign]

    assert await provider.price("google", "indonesia") == expected_kop("0.0821", 9200)

    bought = await provider.buy("google", "indonesia")

    assert "/user/buy/activation/indonesia/virtual58/google" in calls
    assert all("/any/" not in path for path in calls)
    assert bought["cost_kop"] == expected_kop("0.0821", 9200)


async def test_buy_falls_back_to_any_when_operator_is_rejected():
    provider = FiveSimProvider("test-token", usd_kop_rate=9200)
    calls: list[str] = []

    async def fake_get(path: str, params: dict | None = None) -> dict:
        calls.append(path)
        if path == "/guest/prices":
            return PRICES
        if path == "/user/buy/activation/indonesia/virtual58/google":
            raise ProviderError("UNAVAILABLE", "operator ran dry")
        if path == "/user/buy/activation/indonesia/any/google":
            return {"id": 2, "phone": "+628000000002", "price": 0.1923}
        raise AssertionError(f"unexpected request: {path}")

    provider._get = fake_get  # type: ignore[method-assign]

    bought = await provider.buy("google", "indonesia")

    assert calls[-2:] == [
        "/user/buy/activation/indonesia/virtual58/google",
        "/user/buy/activation/indonesia/any/google",
    ]
    assert bought["provider_order_id"] == "2"
    assert bought["cost_kop"] == expected_kop("0.1923", 9200)
