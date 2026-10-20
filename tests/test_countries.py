from __future__ import annotations

import pytest

from config import Settings
from providers.base import ProviderError
from providers.countries import to_iso, to_provider_country
from providers.mock import MockProvider
from service import SalesService, ServiceError
from store import Store


def test_to_provider_country_maps_iso_codes():
    assert to_provider_country("RU") == "russia"
    assert to_provider_country("gb") == "england"
    assert to_provider_country("id") == "indonesia"
    assert to_provider_country("  De  ") == "germany"


def test_to_provider_country_keeps_provider_names_and_unknown_values():
    assert to_provider_country("indonesia") == "indonesia"
    assert to_provider_country("unknownland") == "unknownland"


def test_to_iso_reverse_lookup():
    assert to_iso("england") == "gb"
    assert to_iso("Indonesia") == "id"
    assert to_iso("unknownland") == "unknownland"


@pytest.mark.asyncio
async def test_mock_accepts_iso_and_provider_country_names():
    provider = MockProvider()
    assert await provider.price("telegram", "RU") == await provider.price(
        "telegram", "russia"
    )
    assert await provider.price("telegram", "id") == await provider.price(
        "telegram", "indonesia"
    )


class UnavailableProvider:
    name = "unavailable"

    async def price(self, service: str, country: str) -> int:
        raise ProviderError("UNAVAILABLE", f"bad country: {country}")


@pytest.mark.asyncio
async def test_quote_maps_provider_unavailable_to_service_error(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"), provider="mock")
    store = Store(settings.db_path, settings.rate_limit_per_15min)
    service = SalesService(store, UnavailableProvider(), settings)

    with pytest.raises(ServiceError) as exc:
        await service.quote("telegram", "ru")

    assert exc.value.code == "UNAVAILABLE"
