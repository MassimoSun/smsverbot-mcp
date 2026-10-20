from __future__ import annotations

import pytest

import admin_service
from store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "admin.db"), 20)


def test_create_key_with_topup(store):
    info = admin_service.create_key(store, "client1", 500)

    assert info["key_id"]
    assert info["api_key"].startswith("svb_")
    assert info["balance_rub"] == 500.0
    assert store.get_balance(info["key_id"]) == 50000


def test_topup_adds_rubles(store):
    info = admin_service.create_key(store, "client1", 500)
    result = admin_service.topup(store, info["key_id"], 100)

    assert result["key_id"] == info["key_id"]
    assert result["balance_rub"] == 600.0
    assert store.get_balance(info["key_id"]) == 60000


def test_list_keys_returns_created_key(store):
    admin_service.create_key(store, "client1", 500)

    rows = admin_service.list_keys(store)

    assert len(rows) == 1
    assert rows[0]["label"] == "client1"
    assert rows[0]["balance_rub"] == 500.0
    assert rows[0]["created_at"]


def test_key_balance_unknown_id_raises(store):
    with pytest.raises(ValueError):
        admin_service.key_balance(store, "missing")


def test_negative_amounts_raise(store):
    with pytest.raises(ValueError):
        admin_service.create_key(store, "negative", -1)

    info = admin_service.create_key(store, "client1")
    with pytest.raises(ValueError):
        admin_service.topup(store, info["key_id"], -100)


def test_orders_empty_for_new_key(store):
    info = admin_service.create_key(store, "client1")

    assert admin_service.orders(store, info["key_id"]) == []
