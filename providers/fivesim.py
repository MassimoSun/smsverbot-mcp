from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import httpx

from providers.base import ProviderError
from providers.countries import to_iso, to_provider_country

BASE_URL = "https://5sim.net/v1"


class FiveSimProvider:
    name = "fivesim"

    def __init__(
        self,
        token: str,
        usd_kop_rate: int = 9200,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.usd_kop_rate = int(usd_kop_rate)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if not self.token:
            raise ProviderError("UNAVAILABLE", "FIVESIM_TOKEN is not configured")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = self._get_client()
        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError("UPSTREAM_ERROR", str(exc)) from exc
        text = response.text
        lowered = text.lower()
        status = response.status_code
        if status == 400:
            if (
                "bad country" in lowered
                or "country is incorrect" in lowered
                or "incorrect country" in lowered
            ):
                raise ProviderError(
                    "UNAVAILABLE",
                    f"Country is not available on 5sim: {text[:200]}",
                )
            if "no free phones" in lowered or "no numbers" in lowered:
                raise ProviderError("NO_NUMBERS", "No free phones")
        if status == 404:
            raise ProviderError(
                "UNAVAILABLE", f"5sim resource not found: {text[:200]}"
            )
        if status in {401, 403}:
            raise ProviderError(
                "UNAUTHORIZED", f"5sim authorization failed: {text[:200]}"
            )
        if status == 429:
            raise ProviderError("RATE_LIMITED", "5sim rate limit exceeded")
        if status >= 400:
            raise ProviderError(
                "UPSTREAM_ERROR", f"5sim HTTP {status}: {text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("UPSTREAM_ERROR", "Invalid JSON from 5sim") from exc

    def _to_kop(self, value: Any) -> int:
        try:
            amount = Decimal(str(value)) * Decimal(self.usd_kop_rate)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProviderError("UPSTREAM_ERROR", "Invalid price from 5sim") from exc
        return int(amount.to_integral_value(rounding=ROUND_HALF_UP))

    async def list_countries(self) -> list[dict]:
        data = await self._get("/guest/countries")
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_ERROR", "Unexpected countries payload")
        return [
            {"code": to_iso(code), "name": info.get("text_en") or code}
            for code, info in sorted(data.items())
            if isinstance(info, dict)
        ]

    async def list_services(self, country: str = "russia") -> list[dict]:
        country = to_provider_country(country)
        data = await self._get(f"/guest/products/{country}/any")
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_ERROR", "Unexpected services payload")
        return [
            {"code": code, "name": info.get("text_en") or code}
            for code, info in sorted(data.items())
            if isinstance(info, dict)
        ]

    async def list_services_by_country(self, country: str) -> list[dict]:
        country = to_provider_country(country)
        data = await self._get(f"/guest/products/{country}/any")
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_ERROR", "Unexpected services payload")
        return [
            {"code": code, "name": info.get("text_en") or code}
            for code, info in sorted(data.items())
            if isinstance(info, dict)
        ]

    def _cheapest_offer(
        self, data: Any, service: str, country: str
    ) -> tuple[int, str]:
        in_stock: list[tuple[Any, str, str]] = []
        all_offers: list[tuple[Any, str, str]] = []
        if isinstance(data, dict):
            for country_data in data.values():
                if not isinstance(country_data, dict):
                    continue
                for product_data in country_data.values():
                    if not isinstance(product_data, dict):
                        continue
                    for operator, provider_data in product_data.items():
                        if not isinstance(provider_data, dict):
                            continue
                        try:
                            count = int(provider_data.get("count") or 0)
                        except (TypeError, ValueError):
                            count = 0
                        for field_name, field_value in provider_data.items():
                            if field_value is None:
                                continue
                            field = field_name.lower()
                            if field not in {"price", "cost"}:
                                continue
                            offer = (field_value, str(operator), field)
                            all_offers.append(offer)
                            if count > 0:
                                in_stock.append(offer)

        for offers, use_operator in ((in_stock, True), (all_offers, False)):
            preferred = [o for o in offers if o[2] == "price"] or [
                o for o in offers if o[2] == "cost"
            ]
            candidates = [
                (self._to_kop(value), operator)
                for value, operator, _ in preferred
            ]
            candidates = [(cost, operator) for cost, operator in candidates if cost > 0]
            if candidates:
                cost, operator = min(candidates, key=lambda item: item[0])
                return cost, operator if use_operator else "any"
        raise ProviderError("NO_NUMBERS", f"No prices for {service}/{country}")

    async def _price_with_operator(
        self, service: str, country: str
    ) -> tuple[int, str]:
        country = to_provider_country(country)
        data = await self._get(
            "/guest/prices", params={"country": country, "product": service}
        )
        return self._cheapest_offer(data, service, country)

    async def price(self, service: str, country: str) -> int:
        cost_kop, _operator = await self._price_with_operator(service, country)
        return cost_kop

    async def _buy_with_operator(
        self, service: str, country: str, operator: str
    ) -> dict:
        data = await self._get(
            f"/user/buy/activation/{country}/{operator}/{service}"
        )
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_ERROR", "Unexpected buy payload")
        if data.get("error"):
            raise ProviderError(
                "UNAVAILABLE", f"5sim buy error: {str(data['error'])[:200]}"
            )
        return data

    async def buy(self, service: str, country: str) -> dict:
        country = to_provider_country(country)
        try:
            _cost_kop, operator = await self._price_with_operator(service, country)
        except ProviderError:
            operator = "any"
        operator = operator or "any"
        try:
            data = await self._buy_with_operator(service, country, operator)
        except ProviderError as exc:
            if operator == "any" or exc.code in {"UNAUTHORIZED", "RATE_LIMITED"}:
                raise
            data = await self._buy_with_operator(service, country, "any")
        provider_order_id = data.get("id")
        phone = data.get("phone")
        if provider_order_id is None or not phone:
            raise ProviderError("UPSTREAM_ERROR", "Incomplete buy payload")
        cost_kop: int | None = None
        for field_name in ("price", "cost"):
            if data.get(field_name) is not None:
                try:
                    cost_kop = self._to_kop(data[field_name])
                except ProviderError:
                    cost_kop = None
                break
        if not cost_kop or cost_kop <= 0:
            cost_kop = await self.price(service, country)
        return {
            "provider_order_id": str(provider_order_id),
            "phone": str(phone),
            "cost_kop": int(cost_kop),
        }

    async def code(self, provider_order_id: str) -> str | None:
        data = await self._get(f"/user/check/{provider_order_id}")
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_ERROR", "Unexpected check payload")
        sms = data.get("sms") or []
        if not sms or not isinstance(sms, list):
            return None
        first = sms[0]
        if not isinstance(first, dict) or not first.get("code"):
            return None
        return str(first["code"])

    async def cancel(self, provider_order_id: str) -> bool:
        data = await self._get(f"/user/cancel/{provider_order_id}")
        if isinstance(data, dict) and data.get("status"):
            return str(data["status"]).upper() in {"CANCELLED", "CANCELED"}
        return True
