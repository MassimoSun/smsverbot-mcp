from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import inspect
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from config import Settings
from providers.base import ProviderError
from providers.fivesim import FiveSimProvider
from providers.mock import MockProvider
from service import InvalidKey, OrderNotFound, RateLimited, SalesService, ServiceError
from store import Store

try:  # ошибки единого кошелька появляются вместе с wallet-мостом
    from service import WalletBanned, WalletInsufficient, WalletUnavailable
except ImportError:  # соседняя зона ещё не дописана - держим те же коды локально

    class WalletUnavailable(ServiceError):
        code = "UNAVAILABLE"

    class WalletInsufficient(ServiceError):
        code = "INSUFFICIENT_FUNDS"

    class WalletBanned(ServiceError):
        code = "BANNED"


settings = Settings.from_env()

store = Store(settings.db_path, settings.rate_limit_per_15min)

if settings.provider == "fivesim":
    provider = FiveSimProvider(settings.fivesim_token, settings.usd_kop_rate)
else:
    provider = MockProvider()

sales = SalesService(store, provider, settings)

LOG = logging.getLogger("smsverbot")

# --- песочница ---------------------------------------------------------
SANDBOX_CODE = "123456"
SANDBOX_WAIT_SECONDS = 6.0  # потолок ожидания кода в песочнице
SANDBOX_CODE_DELAY = 3.0  # выдуманный заказ «висит» столько до выдачи кода
SANDBOX_DAILY_LIMIT = 50
SANDBOX_PROVIDER = "sandbox"

# --- фоновая автоотмена ------------------------------------------------
_auto_cancel_state: dict[str, Any] = {"task": None, "stop": None}


def auto_cancel_task() -> asyncio.Task | None:
    """Живая задача автоотмены (None, если не запущена или уже завершилась)."""
    task = _auto_cancel_state.get("task")
    return task if task is not None and not task.done() else None


def _auto_cancel_interval() -> float:
    for name in (
        "auto_cancel_interval_seconds",
        "auto_cancel_interval",
        "auto_cancel_every_seconds",
    ):
        value = getattr(settings, name, None)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 60.0


def _auto_cancel_coro(interval: float, stop_event: asyncio.Event) -> tuple[Any, bool] | None:
    """Собрать корутину цикла автоотмены под сигнатуру сервиса.

    Возвращает (корутина, слушает_ли_стоп). Если сервис умеет только один
    проход - оборачиваем его в свой цикл со стоп-событием.
    """
    fn = getattr(sales, "run_auto_cancel_loop", None)
    if callable(fn):
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            kwargs: dict[str, Any] = {}
            cooperative = False
            compatible = True
            for name, param in signature.parameters.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                low = name.lower()
                if "stop" in low or low in {"shutdown", "halt", "cancel_event"}:
                    kwargs[name] = stop_event
                    cooperative = True
                elif any(
                    key in low for key in ("interval", "period", "every", "delay", "sleep")
                ):
                    kwargs[name] = interval
                elif param.default is inspect.Parameter.empty:
                    compatible = False
            if compatible:
                try:
                    coro = fn(**kwargs)
                except TypeError:
                    coro = None
                if inspect.iscoroutine(coro):
                    return coro, cooperative
        for args in ((interval, stop_event), (interval,), ()):
            try:
                coro = fn(*args)
            except TypeError:
                continue
            if inspect.iscoroutine(coro):
                return coro, len(args) > 1
    expired = getattr(sales, "auto_cancel_expired", None)
    if callable(expired):
        return _fallback_auto_cancel_loop(expired, interval, stop_event), True
    return None


async def _fallback_auto_cancel_loop(
    expired, interval: float, stop_event: asyncio.Event
) -> None:
    """Свой цикл, если сервис умеет только один проход автоотмены."""
    while not stop_event.is_set():
        try:
            result = expired()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновая задача не должна умирать
            LOG.exception("auto-cancel pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def start_auto_cancel() -> asyncio.Task | None:
    """Поднять фоновую автоотмену (идемпотентно)."""
    existing = auto_cancel_task()
    if existing is not None:
        return existing
    stop_event = asyncio.Event()
    built = _auto_cancel_coro(_auto_cancel_interval(), stop_event)
    if built is None:
        LOG.info("auto-cancel loop not started: service exposes no auto-cancel hook")
        return None
    coro, cooperative = built
    task = asyncio.create_task(coro, name="smsverbot-auto-cancel")
    _auto_cancel_state.update(task=task, stop=stop_event, cooperative=cooperative)
    LOG.info("auto-cancel loop started (interval=%ss)", _auto_cancel_interval())
    return task


async def stop_auto_cancel(grace: float = 2.0) -> None:
    """Погасить фоновую автоотмену: сначала мягко, потом жёстко."""
    task = _auto_cancel_state.get("task")
    stop_event = _auto_cancel_state.get("stop")
    cooperative = bool(_auto_cancel_state.get("cooperative"))
    _auto_cancel_state.update(task=None, stop=None, cooperative=False)
    if stop_event is not None:
        with contextlib.suppress(Exception):
            stop_event.set()
    if task is None:
        return
    if cooperative and not task.done() and grace > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait({task}, timeout=grace)
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    LOG.info("auto-cancel loop stopped")


@asynccontextmanager
async def lifespan(_app: Any = None):
    """Хук FastMCP: автоотмена живёт ровно столько, сколько живёт сервер."""
    start_auto_cancel()
    try:
        yield None
    finally:
        await stop_auto_cancel()


mcp = FastMCP(
    "smsverbot",
    host=settings.host,
    port=settings.port,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
        enable_dns_rebinding_protection=True,
    ),
)


def _accept_positional(fn):
    signature = inspect.signature(fn, eval_str=True)
    names = list(signature.parameters)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if args:
            kwargs.update(zip(names, args))
        return await fn(**kwargs)

    wrapper.__signature__ = signature
    return wrapper


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


# Коды контракта для ошибок единого кошелька.
_WALLET_ERRORS: tuple[tuple[type[BaseException], str], ...] = (
    (WalletUnavailable, "UNAVAILABLE"),
    (WalletBanned, "BANNED"),
    (WalletInsufficient, "INSUFFICIENT_FUNDS"),
)
_WALLET_CODES_BY_NAME = {
    "WalletUnavailable": "UNAVAILABLE",
    "WalletBanned": "BANNED",
    "WalletInsufficient": "INSUFFICIENT_FUNDS",
}


def _handle(exc: Exception) -> dict[str, Any]:
    for cls, code in _WALLET_ERRORS:
        if isinstance(exc, cls):
            return _error(code, str(exc) or code)
    if isinstance(exc, ProviderError):
        return _error(exc.code, str(exc))
    if isinstance(exc, ServiceError):
        return _error(exc.code, str(exc))
    named = _WALLET_CODES_BY_NAME.get(type(exc).__name__)
    if named:
        return _error(named, str(exc) or named)
    if isinstance(exc, KeyError):
        return _error("UNAUTHORIZED", "Invalid API key")
    LOG.exception("unhandled tool error")
    return _error("INTERNAL", "Unexpected server error")


def resolve_api_key(api_key: str, ctx: Context | None = None) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    if settings.default_api_key:
        return settings.default_api_key
    try:
        headers = ctx.request_context.request.headers
        raw = headers.get("authorization") or headers.get("Authorization") or ""
    except (AttributeError, ValueError, TypeError):
        return ""
    value = str(raw).strip()
    if value.lower().startswith("bearer "):
        return value[len("bearer ") :].strip()
    return ""


def _key_id(api_key: str) -> str:
    if api_key == "":
        api_key = settings.default_api_key
    row = store.find_key(api_key)
    if not row:
        raise InvalidKey("Invalid API key")
    return str(row["id"])


# --- признак песочницы -------------------------------------------------
def _ensure_sandbox_column() -> bool:
    """Колонка sandbox есть (или создана). Терпимо к миграциям соседней зоны."""
    try:
        with store._db() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
            if "sandbox" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN sandbox INTEGER DEFAULT 0")
            return True
    except Exception:  # noqa: BLE001 - признак песочницы не должен ломать вызов
        LOG.debug("sandbox column unavailable", exc_info=True)
        return False


def _is_sandbox(key_id: str) -> bool:
    getter = getattr(store, "is_sandbox", None)
    if callable(getter):
        try:
            return bool(getter(key_id))
        except Exception:  # noqa: BLE001
            LOG.debug("store.is_sandbox failed", exc_info=True)
    row_getter = getattr(store, "get_key", None)
    if callable(row_getter):
        try:
            row = row_getter(key_id)
            if isinstance(row, dict) and "sandbox" in row:
                return bool(row["sandbox"])
        except Exception:  # noqa: BLE001
            LOG.debug("store.get_key failed", exc_info=True)
    try:
        with store._db() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
            if "sandbox" not in cols:
                return False
            row = conn.execute(
                "SELECT sandbox FROM api_keys WHERE id = ?", (str(key_id),)
            ).fetchone()
        return bool(row["sandbox"]) if row else False
    except Exception:  # noqa: BLE001
        return False


def _set_sandbox(key_id: str, enabled: bool) -> bool:
    setter = getattr(store, "set_sandbox", None)
    if callable(setter):
        try:
            setter(key_id, bool(enabled))
            return True
        except Exception:  # noqa: BLE001
            LOG.debug("store.set_sandbox failed", exc_info=True)
    if not _ensure_sandbox_column():
        return False
    with store._db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET sandbox = ? WHERE id = ?",
            (1 if enabled else 0, str(key_id)),
        )
    return cur.rowcount > 0


# --- песочница: выдуманные заказы без провайдера и без денег -----------
def _sandbox_phone() -> str:
    suffix = int(uuid.uuid4().hex[:6], 16) % 10_000
    return f"+100{suffix:04d}0000"


def _sandbox_expires_at() -> str:
    seconds = int(getattr(settings, "code_wait_seconds", 120) or 120)
    moment = datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 30))
    return moment.isoformat(timespec="seconds")


def _sandbox_buy(key_id: str, service: str, country: str) -> dict[str, Any]:
    limit = int(
        getattr(settings, "sandbox_daily_limit", SANDBOX_DAILY_LIMIT) or SANDBOX_DAILY_LIMIT
    )
    if store.daily_code_count(key_id) >= limit:
        raise RateLimited(f"Sandbox daily limit reached: {limit} operations")
    phone = _sandbox_phone()
    expires_at = _sandbox_expires_at()
    try:
        order_id = store.create_order(
            key_id=key_id,
            provider=SANDBOX_PROVIDER,
            provider_order_id=f"sandbox-{uuid.uuid4().hex[:12]}",
            service=service,
            country=country,
            phone=phone,
            price_kop=0,
            cost_kop=0,
            status="waiting",
            expires_at=expires_at,
        )
    except TypeError:  # store без колонки expires_at
        order_id = store.create_order(
            key_id=key_id,
            provider=SANDBOX_PROVIDER,
            provider_order_id=f"sandbox-{uuid.uuid4().hex[:12]}",
            service=service,
            country=country,
            phone=phone,
            price_kop=0,
            cost_kop=0,
            status="waiting",
        )
    return {
        "order_id": order_id,
        "phone": phone,
        "price_kop": 0,
        "expires_at": expires_at,
        "status": "waiting",
        "sandbox": True,
    }


async def _sandbox_wait(
    key_id: str, order_id: int, timeout: float | None = None
) -> dict[str, Any]:
    order = store.get_order(order_id)
    if not order or order["key_id"] != key_id:
        raise OrderNotFound("Order not found")
    if order["status"] == "cancelled":
        return {
            "status": "cancelled",
            "code": None,
            "phone": order["phone"],
            "seconds_left": 0,
            "sandbox": True,
        }
    if order["status"] == "delivered":
        return {
            "status": "delivered",
            "code": order["code"] or SANDBOX_CODE,
            "phone": order["phone"],
            "seconds_left": 0,
            "sandbox": True,
        }
    delay = SANDBOX_CODE_DELAY
    if timeout is not None:
        delay = min(delay, max(0.0, float(timeout)))
    delay = min(delay, SANDBOX_WAIT_SECONDS)
    await asyncio.sleep(delay)
    store.set_order_code(order_id, SANDBOX_CODE)
    return {
        "status": "delivered",
        "code": SANDBOX_CODE,
        "phone": order["phone"],
        "seconds_left": 0,
        "sandbox": True,
    }


def _service_owns_sandbox(*hooks: str) -> bool:
    """Сервис уже умеет песочницу сам - тогда внешний край только выставляет флаг."""
    return all(callable(getattr(sales, hook, None)) for hook in hooks)


async def _sandbox_purchase(key_id: str, service: str, country: str) -> dict[str, Any]:
    """Покупка в песочнице: сначала штатный путь сервиса, иначе локальный."""
    if _service_owns_sandbox("_buy_sandbox"):
        return await sales.buy(key_id, service, country)
    return _sandbox_buy(key_id, service, country)


async def _sandbox_await_code(key_id: str, order_id: int) -> dict[str, Any]:
    """Код в песочнице: не дольше SANDBOX_WAIT_SECONDS, фиксированный 123456."""
    if _service_owns_sandbox("_poll_sandbox_code"):
        result = await sales.wait_for_code(key_id, order_id, timeout=SANDBOX_WAIT_SECONDS)
        if result.get("status") != "pending":
            return result
        # Провайдер песочницы почему-то не успел: контракт важнее его таймера.
        store.set_order_code(order_id, SANDBOX_CODE)
        order = store.get_order(order_id) or {}
        return {
            "status": "delivered",
            "code": SANDBOX_CODE,
            "phone": order.get("phone"),
            "seconds_left": 0,
        }
    return await _sandbox_wait(key_id, order_id, timeout=SANDBOX_WAIT_SECONDS)


async def _sandbox_cancel(key_id: str, order_id: int) -> dict[str, Any]:
    if _service_owns_sandbox("_order_is_sandbox"):
        return await sales.cancel(key_id, order_id)
    order = store.get_order(order_id)
    if not order or order["key_id"] != key_id:
        raise OrderNotFound("Order not found")
    if order["status"] not in {"cancelled", "delivered"}:
        closer = getattr(store, "close_order", None)
        if callable(closer):
            closer(order_id, "cancelled", 0)
        else:
            store.update_order_status(order_id, "cancelled")
    current = store.get_order(order_id)
    return {
        "status": current["status"] if current else "cancelled",
        "refunded_kop": 0,
    }


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
async def list_services(
    api_key: str = "", country: str = "indonesia", ctx: Context = None
) -> dict[str, Any]:
    """List services available for SMS activation in a country."""
    try:
        _key_id(resolve_api_key(api_key, ctx))
        services = await provider.list_services(country)
        return {"ok": True, "services": services}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def list_countries(api_key: str = "", ctx: Context = None) -> dict[str, Any]:
    """List countries available for SMS activation."""
    try:
        _key_id(resolve_api_key(api_key, ctx))
        countries = await provider.list_countries()
        return {"ok": True, "countries": countries}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
@_accept_positional
async def get_price(
    api_key: str = "", *, service: str, country: str, ctx: Context = None
) -> dict[str, Any]:
    """Get the retail price for a service and country without buying."""
    try:
        _key_id(resolve_api_key(api_key, ctx))
        quote = await sales.quote(service, country)
        return {"ok": True, **quote}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
@_accept_positional
async def buy_number(
    api_key: str = "", *, service: str, country: str, ctx: Context = None
) -> dict[str, Any]:
    """Buy a phone number and debit the prepaid balance."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        if _is_sandbox(key_id):
            bought = await _sandbox_purchase(key_id, service, country)
            return {"ok": True, **bought, "balance_kop": 0, "sandbox": True}
        bought = await sales.buy(key_id, service, country)
        balance = sales.balance(key_id)
        return {"ok": True, **bought, "balance_kop": balance, "sandbox": False}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
@_accept_positional
async def wait_for_code(
    api_key: str = "", *, order_id: int, ctx: Context = None
) -> dict[str, Any]:
    """Wait for an SMS code for an existing order."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        if _is_sandbox(key_id):
            result = await _sandbox_await_code(key_id, order_id)
            return {"ok": True, **result, "sandbox": True}
        result = await sales.wait_for_code(key_id, order_id)
        return {"ok": True, **result, "sandbox": False}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
@_accept_positional
async def cancel_order(
    api_key: str = "", *, order_id: int, ctx: Context = None
) -> dict[str, Any]:
    """Cancel an order and refund the balance exactly once."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        if _is_sandbox(key_id):
            result = await _sandbox_cancel(key_id, order_id)
            return {
                "ok": True,
                **result,
                "refunded_kop": 0,
                "balance_kop": 0,
                "sandbox": True,
            }
        result = await sales.cancel(key_id, order_id)
        balance = sales.balance(key_id)
        return {"ok": True, **result, "balance_kop": balance, "sandbox": False}
    except Exception as exc:
        return _handle(exc)


async def _balance_snapshot(key_id: str) -> dict[str, Any]:
    """Снимок баланса: живой кошелёк бота, кэш — как запасной вариант."""
    live = getattr(sales, "wallet_balance", None)
    if callable(live):
        try:
            snapshot = await live(key_id)
            if isinstance(snapshot, dict):
                return snapshot
        except ServiceError:
            LOG.debug("wallet balance unavailable, falling back to cache", exc_info=True)
    sandbox = _is_sandbox(key_id)
    balance_kop = 0 if sandbox else sales.balance(key_id)
    return {
        "balance_kop": int(balance_kop),
        "balance_rub": int(balance_kop) / 100,
        "sandbox": sandbox,
        "banned": False,
    }


@mcp.tool()
async def get_balance(api_key: str = "", ctx: Context = None) -> dict[str, Any]:
    """Return the current prepaid balance."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        snapshot = await _balance_snapshot(key_id)
        balance_kop = int(snapshot.get("balance_kop") or 0)
        return {
            "ok": True,
            "balance_kop": balance_kop,
            "balance_rub": balance_kop / 100,
            "currency": "RUB",
            "sandbox": bool(snapshot.get("sandbox", _is_sandbox(key_id))),
            "banned": bool(snapshot.get("banned")),
        }
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def order_history(
    api_key: str = "", limit: int = 10, ctx: Context = None
) -> dict[str, Any]:
    """Return recent orders for the API key."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        orders = [_order_data(o) for o in sales.history(key_id, limit)]
        return {"ok": True, "orders": orders, "sandbox": _is_sandbox(key_id)}
    except Exception as exc:
        return _handle(exc)


@mcp.tool()
async def whoami(api_key: str = "", ctx: Context = None) -> dict[str, Any]:
    """Verify an API key and return its id and balance in rubles."""
    try:
        key_id = _key_id(resolve_api_key(api_key, ctx))
        snapshot = await _balance_snapshot(key_id)
        balance_kop = int(snapshot.get("balance_kop") or 0)
        return {
            "ok": True,
            "key_id": key_id,
            "balance_rub": balance_kop / 100,
            "balance_kop": balance_kop,
            "currency": "RUB",
            "sandbox": bool(snapshot.get("sandbox", _is_sandbox(key_id))),
            "banned": bool(snapshot.get("banned")),
        }
    except Exception as exc:
        return _handle(exc)


def _admin_create_key(label: str, sandbox: bool = False) -> None:
    if sandbox:
        try:
            key_id, plain_key = store.create_key(label, sandbox=True)
        except TypeError:
            key_id, plain_key = store.create_key(label)
        _set_sandbox(key_id, True)
    else:
        key_id, plain_key = store.create_key(label)
    print(
        json.dumps(
            {"key_id": key_id, "api_key": plain_key, "sandbox": bool(sandbox)},
            ensure_ascii=False,
        )
    )


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
        "--sandbox",
        action="store_true",
        help="with --create-key: mark the new key as a sandbox key",
    )
    parser.add_argument(
        "--topup",
        nargs=2,
        metavar=("API_KEY", "RUB"),
        help="top up a key balance in rubles and exit",
    )
    args = parser.parse_args()

    if args.create_key:
        _admin_create_key(args.create_key, args.sandbox)
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
