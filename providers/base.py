from __future__ import annotations

from typing import Protocol


class ProviderError(Exception):
    def __init__(self, code: str = "UNAVAILABLE", message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class Provider(Protocol):
    async def list_services(self, country: str = "indonesia") -> list[dict]: ...

    async def list_countries(self) -> list[dict]: ...

    async def price(self, service: str, country: str) -> int: ...

    async def buy(self, service: str, country: str) -> dict: ...

    async def code(self, provider_order_id: str) -> str | None: ...

    async def cancel(self, provider_order_id: str) -> bool: ...
