from __future__ import annotations

import argparse
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.fastmcp import FastMCP

from config import Settings
from providers.base import ProviderError
from providers.fivesim import FiveSimProvider
from providers.mock import MockProvider
from service import InvalidKey, SalesService, ServiceError
from store import Store

settings = Settings.from_env()

store = Store(settings.db_path, settings.rate_limit_per_15min)

if settings.provider == "fivesim":
    provider = FiveSimProvider(settings.fivesim_token, settings.usd_kop_rate)
else:
    provider = MockProvider()

sales = SalesService(store, provider, settings)
mcp = FastMCP("smsverbot", host=settings.host, port=settings.port)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


def _handle(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return _error(exc.code, str(exc))
    if isinstance(exc, ServiceError):
        return _error(exc.code, str(exc))
    if isinstance(exc, KeyError):
        return _error("UNAUTHORIZED", "Invalid API key")
    logging.getLogger("smsverbot").exception("unhandled tool error")
    return _error("INTERNAL", "Unexpected server error")


def _key_id(api_key: str) -> str:
    row = store.find_key(api_key)
    if not row:
        raise InvalidKey("Invalid API key")
    return str(row["id"])


def _order_data(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": order["id"],
        "service": order["service"],
        "country": order["country"],
        "phone": order["phone"],
        "price_kop": order["price_kop"],
        "status": order["status"],
        "code": order["code"],
        "created_at": order["created_at"],
        "updated_at": order["updated_at"],
    }


@mcp.tool()
async def list_services(api_key: str) -> dict[str, Any]:
    """List services available for SMS activation."""
    try:
        _key_id(api_key)
        services = await provider.list_services()
        return {"ok": True, "services": services}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def list_countries(api_key: str) -> dict[str, Any]:
    """List countries available for SMS activation."""
    try:
        _key_id(api_key)
        countries = await provider.list_countries()
        return {"ok": True, "countries": countries}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def get_price(api_key: str, service: str, country: str) -> dict[str, Any]:
    """Get the retail price for a service and country without buying."""
    try:
        _key_id(api_key)
        quote = await sales.quote(service, country)
        return {"ok": True, **quote}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def buy_number(api_key: str, service: str, country: str) -> dict[str, Any]:
    """Buy a phone number and debit the prepaid balance."""
    try:
        key_id = _key_id(api_key)
        bought = await sales.buy(key_id, service, country)
        balance = sales.balance(key_id)
        return {"ok": True, **bought, "balance_kop": balance}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def wait_for_code(api_key: str, order_id: int) -> dict[str, Any]:
    """Wait for an SMS code for an existing order."""
    try:
        key_id = _key_id(api_key)
        result = await sales.wait_for_code(key_id, order_id)
        return {"ok": True, **result}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def cancel_order(api_key: str, order_id: int) -> dict[str, Any]:
    """Cancel an order and refund the balance exactly once."""
    try:
        key_id = _key_id(api_key)
        result = await sales.cancel(key_id, order_id)
        balance = sales.balance(key_id)
        return {"ok": True, **result, "balance_kop": balance}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def get_balance(api_key: str) -> dict[str, Any]:
    """Return the current prepaid balance."""
    try:
        key_id = _key_id(api_key)
        balance_kop = sales.balance(key_id)
        return {
            "ok": True,
            "balance_kop": balance_kop,
            "balance_rub": balance_kop / 100,
            "currency": "RUB",
        }
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def order_history(api_key: str, limit: int = 10) -> dict[str, Any]:
    """Return recent orders for the API key."""
    try:
        key_id = _key_id(api_key)
        orders = [_order_data(o) for o in sales.history(key_id, limit)]
        return {"ok": True, "orders": orders}
    except Exception as exc:
        return _handle(exc)


def _admin_create_key(label: str) -> None:
    key_id, plain_key = store.create_key(label)
    print(json.dumps({"key_id": key_id, "api_key": plain_key}, ensure_ascii=False))


def _admin_topup(api_key: str, amount_rub: str) -> None:
    row = store.find_key(api_key)
    if not row:
        raise SystemExit("Invalid API key")
    try:
        amount_kop = int((Decimal(amount_rub) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise SystemExit("Invalid amount")
    if amount_kop <= 0:
        raise SystemExit("Amount must be positive")
    balance = store.topup(str(row["id"]), amount_kop)
    print(json.dumps({"ok": True, "balance_kop": balance}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(prog="server.py", description="smsverbot MCP server")
    parser.add_argument("--stdio", action="store_true", help="run with stdio transport")
    parser.add_argument("--http", action="store_true", help="run with streamable HTTP")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--create-key", metavar="LABEL", help="create an API key and exit")
    parser.add_argument(
        "--topup",
        nargs=2,
        metavar=("API_KEY", "RUB"),
        help="top up a key balance in rubles and exit",
    )
    args = parser.parse_args()

    if args.create_key:
        _admin_create_key(args.create_key)
        return
    if args.topup:
        _admin_topup(args.topup[0], args.topup[1])
        return

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
