from __future__ import annotations

import pytest

from config import Settings
from providers.fivesim import FiveSimProvider
from service import SalesService
from store import Store

pytestmark = pytest.mark.asyncio


def make_provider(payload: dict) -> FiveSimProvider:
    provider = FiveSimProvider("test-token", usd_kop_rate=9200)

    async def fake_get(path: str, params: dict | None = None) -> dict:
        return payload

    provider._get = fake_get  # type: ignore[method-assign]
    return provider


async def test_price_ignores_cheaper_out_of_stock_offer():
    provider = make_provider(
        {
            "indonesia": {
                "telegram": {
                    "virtual2": {"cost": 0.2064, "count": 0},
                    "virtual53": {"cost": 0.3, "count": 5},
                }
            }
        }
    )

    assert await provider.price("telegram", "indonesia") == 2760


async def test_price_falls_back_when_everything_is_out_of_stock():
    provider = make_provider(
        {
            "indonesia": {
                "telegram": {
                    "virtual2": {"cost": 0.2064, "count": 0},
                    "virtual53": {"cost": 0.3, "count": None},
                    "virtual99": {"cost": 0.25},
                }
            }
        }
    )

    assert await provider.price("telegram", "indonesia") == 1899


class ChangingPriceProvider:
    name = "fake"

    async def price(self, service: str, country: str) -> int:
        return 1899

    async def buy(self, service: str, country: str) -> dict:
        return {
            "provider_order_id": "fake-1",
            "phone": "+79000000001",
            "cost_kop": 3000,
        }

    async def cancel(self, provider_order_id: str) -> bool:
        return True


async def test_buy_reports_quote_and_charged_price_when_price_increases(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        provider="fake",
        markup=2.5,
        rate_limit_per_15min=20,
        daily_code_limit=200,
        code_wait_seconds=5,
        usd_kop_rate=9200,
    )
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    service = SalesService(store, ChangingPriceProvider(), settings)
    key_id, _ = store.create_key("test")
    store.topup(key_id, 100_000)

    bought = await service.buy(key_id, "telegram", "indonesia")

    assert bought["quote_price_kop"] == 4800
    assert bought["charged_price_kop"] == 7500
    assert bought["price_kop"] == 7500
    assert store.get_order(bought["order_id"])["status"] == "waiting"
    assert store.get_balance(key_id) == 92_500
