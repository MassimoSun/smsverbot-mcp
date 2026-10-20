from __future__ import annotations

from typing import Any

from store import Store


def _rub_to_kop(value: float) -> int:
    return int(round(value * 100))


def _kop_to_rub(value: int) -> float:
    return value / 100


def create_key(store: Store, label: str, topup_rub: float = 0) -> dict[str, Any]:
    if topup_rub < 0:
        raise ValueError("Top-up amount must not be negative")
    key_id, plain_key = store.create_key(label)
    if topup_rub > 0:
        store.topup(key_id, _rub_to_kop(topup_rub))
    balance_kop = store.get_balance(key_id)
    return {
        "key_id": key_id,
        "api_key": plain_key,
        "balance_rub": _kop_to_rub(balance_kop),
    }


def topup(store: Store, key_id: str, rub: float) -> dict[str, Any]:
    if rub < 0:
        raise ValueError("Top-up amount must not be negative")
    amount_kop = _rub_to_kop(rub)
    try:
        balance_kop = (
            store.get_balance(key_id)
            if amount_kop == 0
            else store.topup(key_id, amount_kop)
        )
    except KeyError as exc:
        raise ValueError(f"Unknown key id: {key_id}") from exc
    return {"key_id": key_id, "balance_rub": _kop_to_rub(balance_kop)}


def list_keys(store: Store) -> list[dict[str, Any]]:
    with store._db() as conn:
        rows = conn.execute(
            "SELECT id, label, balance_kop, created_at FROM api_keys"
            " ORDER BY created_at DESC, id"
        ).fetchall()
    return [
        {
            "key_id": str(row["id"]),
            "label": row["label"],
            "balance_rub": _kop_to_rub(int(row["balance_kop"])),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def key_balance(store: Store, key_id: str) -> dict[str, Any]:
    try:
        balance_kop = store.get_balance(key_id)
    except KeyError as exc:
        raise ValueError(f"Unknown key id: {key_id}") from exc
    return {"key_id": key_id, "balance_rub": _kop_to_rub(balance_kop)}


def orders(store: Store, key_id: str, limit: int = 10) -> list[dict[str, Any]]:
    key_balance(store, key_id)
    rows = store.list_orders(key_id, limit)
    return [
        {
            "order_id": row["id"],
            "service": row["service"],
            "country": row["country"],
            "phone": row["phone"],
            "price_rub": _kop_to_rub(int(row["price_kop"])),
            "status": row["status"],
            "code": row["code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
