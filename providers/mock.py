from __future__ import annotations

import time

from providers.base import ProviderError

SERVICES = [
    {"code": "telegram", "name": "Telegram"},
    {"code": "whatsapp", "name": "WhatsApp"},
    {"code": "google", "name": "Google"},
]

COUNTRIES = [
    {"code": "ru", "name": "Russia"},
    {"code": "kz", "name": "Kazakhstan"},
    {"code": "id", "name": "Indonesia"},
    {"code": "de", "name": "Germany"},
]

PRICES_KOP = {
    ("telegram", "ru"): 1200,
    ("telegram", "id"): 950,
    ("whatsapp", "ru"): 1800,
    ("google", "ru"): 600,
}
DEFAULT_PRICE_KOP = 1000
MOCK_CODE = "482913"


class MockProvider:
    name = "mock"

    def __init__(self) -> None:
        self._counter = 0
        self._orders: dict[str, dict] = {}

    async def list_services(self) -> list[dict]:
        return [dict(item) for item in SERVICES]

    async def list_countries(self) -> list[dict]:
        return [dict(item) for item in COUNTRIES]

    async def price(self, service: str, country: str) -> int:
        self._validate(service, country)
        return PRICES_KOP.get((service, country), DEFAULT_PRICE_KOP)

    async def buy(self, service: str, country: str) -> dict:
        self._validate(service, country)
        self._counter += 1
        provider_order_id = f"mock-{self._counter}"
        cost_kop = PRICES_KOP.get((service, country), DEFAULT_PRICE_KOP)
        phone = f"+7900{self._counter:06d}"
        self._orders[provider_order_id] = {
            "phone": phone,
            "cost_kop": cost_kop,
            "service": service,
            "country": country,
            "created": time.monotonic(),
            "cancelled": False,
        }
        return {
            "provider_order_id": provider_order_id,
            "phone": phone,
            "cost_kop": cost_kop,
        }

    async def code(self, provider_order_id: str) -> str | None:
        order = self._orders.get(provider_order_id)
        if order is None:
            raise ProviderError("UNAVAILABLE", "Unknown mock order")
        if order["cancelled"]:
            return None
        if time.monotonic() - order["created"] >= 1.0:
            return MOCK_CODE
        return None

    async def cancel(self, provider_order_id: str) -> bool:
        order = self._orders.get(provider_order_id)
        if order is None or order["cancelled"]:
            return False
        order["cancelled"] = True
        return True

    @staticmethod
    def _validate(service: str, country: str) -> None:
        if service not in {s["code"] for s in SERVICES}:
            raise ProviderError("UNAVAILABLE", f"Unknown service: {service}")
        if country not in {c["code"] for c in COUNTRIES}:
            raise ProviderError("UNAVAILABLE", f"Unknown country: {country}")
