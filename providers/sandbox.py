"""Sandbox provider: fake numbers, fixed code, no money.

Used by the free test mode. A sandbox API key never reaches a real provider
and never touches the wallet: numbers are fake and the code is always the
same, so integrators can debug their flow for free.
"""

from __future__ import annotations

import time
import uuid

from providers.base import ProviderError
from providers.countries import to_iso

SANDBOX_CODE = "123456"
SANDBOX_PHONE = "+10000000000"
SANDBOX_MASKED_PHONE = "+100****0000"
CODE_DELAY_SECONDS = 4.0

SERVICES = [
    {"code": "telegram", "name": "Telegram"},
    {"code": "discord", "name": "Discord"},
    {"code": "google", "name": "Google"},
]

COUNTRIES = [
    {"code": "id", "name": "Indonesia"},
    {"code": "br", "name": "Brazil"},
    {"code": "kz", "name": "Kazakhstan"},
]

_SERVICE_CODES = {item["code"] for item in SERVICES}
_COUNTRY_CODES = {item["code"] for item in COUNTRIES}


class SandboxProvider:
    """Provider with zero cost, fake numbers and a fixed delayed code."""

    name = "sandbox"

    def __init__(self, code_delay: float = CODE_DELAY_SECONDS) -> None:
        self.code_delay = float(code_delay)
        self._orders: dict[str, dict] = {}

    async def list_services(self, country: str = "indonesia") -> list[dict]:
        return [dict(item) for item in SERVICES]

    async def list_countries(self) -> list[dict]:
        return [dict(item) for item in COUNTRIES]

    async def price(self, service: str, country: str) -> int:
        self._validate(service, to_iso(country))
        return 0

    async def buy(self, service: str, country: str) -> dict:
        self._validate(service, to_iso(country))
        provider_order_id = f"sandbox-{uuid.uuid4().hex}"
        self._orders[provider_order_id] = {
            "created": time.monotonic(),
            "cancelled": False,
        }
        return {
            "provider_order_id": provider_order_id,
            "phone": SANDBOX_PHONE,
            "cost_kop": 0,
        }

    async def code(self, provider_order_id: str) -> str | None:
        order = self._orders.get(provider_order_id)
        if order is None:
            # Unknown id (e.g. the order was created by the service without a
            # provider call): start the clock now instead of failing.
            order = {"created": time.monotonic(), "cancelled": False}
            self._orders[provider_order_id] = order
        if order["cancelled"]:
            return None
        if time.monotonic() - order["created"] >= self.code_delay:
            return SANDBOX_CODE
        return None

    async def cancel(self, provider_order_id: str) -> bool:
        order = self._orders.get(provider_order_id)
        if order is None:
            return True
        if order["cancelled"]:
            return False
        order["cancelled"] = True
        return True

    @staticmethod
    def _validate(service: str, country: str) -> None:
        if service not in _SERVICE_CODES:
            raise ProviderError("UNAVAILABLE", f"Unknown service: {service}")
        if country not in _COUNTRY_CODES:
            raise ProviderError("UNAVAILABLE", f"Unknown country: {country}")
