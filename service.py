from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

from config import Settings
from providers.base import Provider, ProviderError
from store import Store


class ServiceError(Exception):
    code = "SERVICE_ERROR"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class InsufficientFunds(ServiceError):
    code = "INSUFFICIENT_FUNDS"


class RateLimited(ServiceError):
    code = "RATE_LIMITED"


class DailyLimitExceeded(ServiceError):
    code = "DAILY_LIMIT"


class OrderNotFound(ServiceError):
    code = "ORDER_NOT_FOUND"


class InvalidKey(ServiceError):
    code = "UNAUTHORIZED"


class ProviderUnavailable(ServiceError):
    code = "UNAVAILABLE"


class SalesService:
    def __init__(self, store: Store, provider: Provider, settings: Settings) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings

    def _price_for(self, cost_kop: int) -> int:
        markup = Decimal(str(self.settings.markup))
        marked = int(
            (Decimal(int(cost_kop)) * markup).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        price = max(marked, int(cost_kop) + 1000)
        return ((price + 99) // 100) * 100

    async def quote(self, service: str, country: str) -> dict[str, Any]:
        try:
            cost_kop = int(await self.provider.price(service, country))
        except ProviderError as exc:
            if exc.code == "UNAVAILABLE":
                raise ProviderUnavailable(str(exc)) from exc
            raise
        return {
            "service": service,
            "country": country,
            "cost_kop": cost_kop,
            "price_kop": self._price_for(cost_kop),
            "currency": "RUB",
        }

    async def buy(self, key_id: str, service: str, country: str) -> dict[str, Any]:
        if not self.store.allow_request(key_id):
            raise RateLimited("Rate limit exceeded, try again later")
        if self.store.daily_code_count(key_id) >= self.settings.daily_code_limit:
            raise DailyLimitExceeded("Daily code limit reached")

        quote = await self.quote(service, country)
        try:
            bought = await self.provider.buy(service, country)
        except ProviderError as exc:
            if exc.code == "UNAVAILABLE":
                raise ProviderUnavailable(str(exc)) from exc
            raise
        provider_order_id = str(bought["provider_order_id"])
        phone = str(bought["phone"])
        cost_kop = int(bought.get("cost_kop") or quote["cost_kop"])
        price_kop = self._price_for(cost_kop)

        order_id = self.store.create_order(
            key_id=key_id,
            provider=getattr(self.provider, "name", self.settings.provider),
            provider_order_id=provider_order_id,
            service=service,
            country=country,
            phone=phone,
            price_kop=price_kop,
            cost_kop=cost_kop,
            status="waiting",
        )
        if not self.store.debit_atomic(key_id, order_id, price_kop):
            try:
                await self.provider.cancel(provider_order_id)
            except ProviderError:
                pass
            self.store.update_order_status(order_id, "cancelled")
            raise InsufficientFunds("Not enough balance to buy this number")

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.code_wait_seconds
        )
        return {
            "order_id": order_id,
            "phone": phone,
            "price_kop": price_kop,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    async def wait_for_code(
        self,
        key_id: str,
        order_id: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        if not order or order["key_id"] != key_id:
            raise OrderNotFound("Order not found")
        if order["status"] == "delivered":
            return {
                "status": "delivered",
                "code": order["code"],
                "phone": order["phone"],
                "seconds_left": 0,
            }
        if order["status"] == "cancelled":
            return {
                "status": "cancelled",
                "code": None,
                "phone": order["phone"],
                "seconds_left": 0,
            }

        wait = (
            float(self.settings.code_wait_seconds)
            if timeout is None
            else min(float(timeout), float(self.settings.code_wait_seconds))
        )
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            code = await self.provider.code(order["provider_order_id"])
            if code:
                self.store.set_order_code(order_id, code)
                return {
                    "status": "delivered",
                    "code": code,
                    "phone": order["phone"],
                    "seconds_left": max(0, int(deadline - time.monotonic())),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(2.0, remaining))

        return {
            "status": "pending",
            "code": None,
            "phone": order["phone"],
            "seconds_left": 0,
        }

    async def cancel(self, key_id: str, order_id: int) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        if not order or order["key_id"] != key_id:
            raise OrderNotFound("Order not found")
        if order["status"] == "cancelled":
            return {"status": "cancelled", "refunded_kop": 0}
        if order["status"] == "delivered":
            return {"status": "delivered", "refunded_kop": 0}

        cancelled = await self.provider.cancel(order["provider_order_id"])
        if not cancelled:
            return {"status": order["status"], "refunded_kop": 0}
        refunded = int(order["price_kop"]) if self.store.refund_atomic(order_id) else 0
        return {"status": "cancelled", "refunded_kop": refunded}

    def balance(self, key_id: str) -> int:
        return self.store.get_balance(key_id)

    def history(self, key_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.store.list_orders(key_id, limit)

    async def catalog(self) -> dict[str, Any]:
        services = await self.provider.list_services()
        countries = await self.provider.list_countries()
        enriched: list[dict[str, Any]] = []
        for service in services:
            prices: dict[str, int] = {}
            for country in countries:
                try:
                    cost = await self.provider.price(service["code"], country["code"])
                except ProviderError:
                    continue
                prices[country["code"]] = self._price_for(cost)
            item = dict(service)
            item["prices"] = prices
            enriched.append(item)
        return {
            "currency": "RUB",
            "services": enriched,
            "countries": [dict(c) for c in countries],
        }
