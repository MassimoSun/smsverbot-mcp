from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any

try:  # primary transport for the wallet bridge
    import aiohttp
except ModuleNotFoundError:  # pragma: no cover - deployment without aiohttp
    aiohttp = None  # type: ignore[assignment]

import httpx  # already a project dependency; fallback when aiohttp is missing

from config import Settings
from providers.base import Provider, ProviderError
from providers.sandbox import SANDBOX_CODE, SANDBOX_MASKED_PHONE
from store import Store, now_iso

LOG = logging.getLogger("smsverbot.service")


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


# --- wallet bridge errors ------------------------------------------------


class WalletUnavailable(ServiceError):
    """Bot wallet is unreachable: never sell on credit, never guess."""

    code = "UNAVAILABLE"


class WalletInsufficient(InsufficientFunds):
    """Wallet refused the debit: same code, distinct origin."""

    code = "INSUFFICIENT_FUNDS"


class WalletBanned(ServiceError):
    code = "BANNED"


def _kop_to_rub(amount_kop: int) -> float:
    return round(int(amount_kop) / 100.0, 2)


def _rub_to_kop(balance_rub: Any) -> int:
    try:
        return int(round(float(balance_rub) * 100))
    except (TypeError, ValueError):
        return 0


class WalletClient:
    """Client for the bot's local wallet bridge (127.0.0.1:8767).

    The bot database is the single source of truth for money. Every call is
    idempotent on ``ref`` on the bot side, so retries are safe.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 8.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = float(timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.token, "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if aiohttp is not None:
            status, data = await self._request_aiohttp(
                method, url, params=params, payload=payload
            )
        else:  # pragma: no cover - httpx fallback path
            status, data = await self._request_httpx(
                method, url, params=params, payload=payload
            )
        if status >= 500:
            raise WalletUnavailable(f"Wallet bridge returned HTTP {status}")

        if not isinstance(data, dict):
            raise WalletUnavailable("Wallet bridge returned a non-object payload")
        if not data.get("ok"):
            error = str(data.get("error") or "")
            if error == "INSUFFICIENT_FUNDS":
                raise WalletInsufficient("Not enough balance to buy this number")
            if error == "BANNED":
                raise WalletBanned("Account is banned")
            raise WalletUnavailable(
                str(data.get("message") or f"Wallet error: {error or 'UNKNOWN'}")
            )
        return data

    async def _request_aiohttp(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, url, params=params, json=payload, headers=self._headers()
                ) as resp:
                    status = resp.status
                    try:
                        data = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        data = None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise WalletUnavailable("Wallet is temporarily unavailable") from exc
        return status, data

    async def _request_httpx(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(
                    method, url, params=params, json=payload, headers=self._headers()
                )
                status = resp.status_code
                try:
                    data = resp.json()
                except ValueError:
                    data = None
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            raise WalletUnavailable("Wallet is temporarily unavailable") from exc
        return status, data

    async def balance(self, owner_tg: int) -> dict[str, Any]:
        data = await self._request(
            "GET", "/wallet/balance", params={"owner_tg": int(owner_tg)}
        )
        return {
            "balance_rub": float(data.get("balance_rub") or 0.0),
            "balance_kop": _rub_to_kop(data.get("balance_rub")),
            "banned": bool(data.get("banned")),
        }

    async def debit(self, owner_tg: int, amount_kop: int, ref: str) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/wallet/debit",
            payload={
                "owner_tg": int(owner_tg),
                "amount_rub": _kop_to_rub(amount_kop),
                "ref": str(ref),
            },
        )
        return {
            "debited": bool(data.get("debited")),
            "balance_kop": _rub_to_kop(data.get("balance_rub")),
            "balance_rub": float(data.get("balance_rub") or 0.0),
        }

    async def refund(self, owner_tg: int, amount_kop: int, ref: str) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/wallet/refund",
            payload={
                "owner_tg": int(owner_tg),
                "amount_rub": _kop_to_rub(amount_kop),
                "ref": str(ref),
            },
        )
        return {
            "refunded": bool(data.get("refunded")),
            "balance_kop": _rub_to_kop(data.get("balance_rub")),
            "balance_rub": float(data.get("balance_rub") or 0.0),
        }


class WalletBackedBalance(int):
    """Balance in kopecks that can also be awaited to refresh from the wallet.

    ``server.py`` reads ``sales.balance(key_id)`` synchronously and keeps
    working: this is a real int, taken from the local cache. New call sites
    should write ``await sales.balance(key_id)`` - the await goes to the bot
    wallet and falls back to the cache when the bridge is down.
    """

    def __new__(cls, value: int, refresh=None) -> "WalletBackedBalance":
        obj = super().__new__(cls, int(value))
        obj._refresh = refresh
        return obj

    def __await__(self):
        async def _resolve() -> int:
            if self._refresh is None:
                return int(self)
            try:
                return int(await self._refresh())
            except ServiceError:
                return int(self)

        return _resolve().__await__()


class SalesService:
    def __init__(
        self,
        store: Store,
        provider: Provider,
        settings: Settings,
        wallet: WalletClient | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings
        if wallet is None and getattr(settings, "wallet_token", ""):
            wallet = WalletClient(settings.wallet_url, settings.wallet_token)
        self.wallet = wallet

    # --- pricing ---------------------------------------------------------
    def _price_for(self, cost_kop: int) -> int:
        markup = Decimal(str(self.settings.markup))
        marked = int(
            (Decimal(int(cost_kop)) * markup).to_integral_value(rounding=ROUND_CEILING)
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

    # --- helpers ---------------------------------------------------------
    def _expires_at(self) -> str:
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=int(self.settings.code_wait_seconds))
        ).isoformat(timespec="seconds")

    def _owner_tg(self, key_id: str) -> int | None:
        key = self.store.get_key(key_id)
        if not key:
            raise InvalidKey("Invalid API key")
        owner = key.get("owner_tg")
        return int(owner) if owner is not None else None

    @staticmethod
    def _order_is_sandbox(order: dict[str, Any]) -> bool:
        return str(order.get("provider") or "") == "sandbox"

    @staticmethod
    def _sandbox_elapsed(order: dict[str, Any]) -> float:
        raw = order.get("created_at")
        if not raw:
            return 0.0
        try:
            created = datetime.fromisoformat(str(raw))
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds()

    async def _cancel_provider_order(self, provider_order_id: str) -> None:
        try:
            await self.provider.cancel(provider_order_id)
        except ProviderError:
            LOG.warning("provider cancel failed for %s", provider_order_id)

    # --- buy -------------------------------------------------------------
    async def buy(self, key_id: str, service: str, country: str) -> dict[str, Any]:
        if not self.store.allow_request(key_id):
            raise RateLimited("Rate limit exceeded, try again later")
        if self.store.daily_code_count(key_id) >= self.settings.daily_code_limit:
            raise DailyLimitExceeded("Daily code limit reached")

        if self.store.is_sandbox(key_id):
            return await self._buy_sandbox(key_id, service, country)

        quote = await self.quote(service, country)
        limit_kop = (
            self.store.key_daily_limit(key_id)
            or self.settings.daily_spend_limit_rub * 100
        )
        spent_kop = self.store.spend_today_kop(key_id)
        if spent_kop + int(quote["price_kop"]) > int(limit_kop):
            raise DailyLimitExceeded(
                f"Daily spend limit reached: spent {spent_kop / 100:.2f} of "
                f"{limit_kop / 100:.2f} RUB today"
            )
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
        quote_price_kop = int(quote["price_kop"])
        expires_at = self._expires_at()

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
            expires_at=expires_at,
        )

        if self.wallet is None or not self.wallet.enabled:
            # No wallet bridge configured (dev / tests): local prepaid cache.
            if not self.store.debit_atomic(key_id, order_id, price_kop):
                await self._cancel_provider_order(provider_order_id)
                self.store.update_order_status(order_id, "cancelled")
                raise InsufficientFunds("Not enough balance to buy this number")
            return {
                "order_id": order_id,
                "phone": phone,
                "price_kop": price_kop,
                "quote_price_kop": quote_price_kop,
                "charged_price_kop": price_kop,
                "expires_at": expires_at,
            }

        owner_tg = self._owner_tg(key_id)
        if owner_tg is None:
            await self._cancel_provider_order(provider_order_id)
            self.store.delete_order(order_id)
            raise ServiceError(
                "API key is not linked to a Telegram account; get a key with /api_key"
            )
        try:
            debited = await self.wallet.debit(
                owner_tg, price_kop, ref=f"order:{order_id}"
            )
        except (WalletInsufficient, InsufficientFunds) as exc:
            await self._cancel_provider_order(provider_order_id)
            self.store.update_order_status(order_id, "cancelled")
            raise InsufficientFunds(str(exc)) from exc
        except WalletBanned as exc:
            await self._cancel_provider_order(provider_order_id)
            self.store.update_order_status(order_id, "cancelled")
            raise WalletBanned(str(exc)) from exc
        except WalletUnavailable as exc:
            # Never sell on credit: the order must not exist at all.
            await self._cancel_provider_order(provider_order_id)
            self.store.delete_order(order_id)
            raise WalletUnavailable(
                "Wallet is temporarily unavailable, try again"
            ) from exc

        self.store.note_wallet_move(
            key_id,
            order_id,
            -price_kop,
            "debit",
            balance_kop=debited.get("balance_kop"),
        )
        return {
            "order_id": order_id,
            "phone": phone,
            "price_kop": price_kop,
            "quote_price_kop": quote_price_kop,
            "charged_price_kop": price_kop,
            "expires_at": expires_at,
        }

    async def _buy_sandbox(
        self, key_id: str, service: str, country: str
    ) -> dict[str, Any]:
        """Free test mode: no provider call, no money, fake number."""
        used = self.store.sandbox_ops_today(key_id)
        if used >= int(self.settings.sandbox_daily_limit):
            raise RateLimited(
                f"Sandbox daily limit reached: {used} of "
                f"{self.settings.sandbox_daily_limit} operations today"
            )
        order_id = self.store.create_order(
            key_id=key_id,
            provider="sandbox",
            provider_order_id=f"sandbox-local-{key_id[:8]}-{time.time_ns()}",
            service=service,
            country=country,
            phone=SANDBOX_MASKED_PHONE,
            price_kop=0,
            cost_kop=0,
            status="waiting",
            expires_at=self._expires_at(),
        )
        return {
            "sandbox": True,
            "order_id": order_id,
            "phone": SANDBOX_MASKED_PHONE,
            "price_kop": 0,
            "expires_at": self.store.get_order(order_id)["expires_at"],
        }

    # --- wait ------------------------------------------------------------
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
        if not self.store.try_begin_wait(
            key_id, self.settings.max_waits_per_key, self.settings.max_waits_global
        ):
            raise RateLimited(
                "Too many parallel wait_for_code calls: finish or cancel the current ones first"
            )
        try:
            return await self._poll_code(order, order_id, wait)
        finally:
            self.store.end_wait(key_id)

    async def _poll_code(
        self, order: dict[str, Any], order_id: int, wait: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, wait)
        if self._order_is_sandbox(order):
            return await self._poll_sandbox_code(order, order_id, deadline)
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

    async def _poll_sandbox_code(
        self, order: dict[str, Any], order_id: int, deadline: float
    ) -> dict[str, Any]:
        delay = float(self.settings.sandbox_code_delay)
        while True:
            if self._sandbox_elapsed(order) >= delay:
                self.store.set_order_code(order_id, SANDBOX_CODE)
                return {
                    "status": "delivered",
                    "code": SANDBOX_CODE,
                    "phone": order["phone"],
                    "seconds_left": max(0, int(deadline - time.monotonic())),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.5, remaining))
        return {
            "status": "pending",
            "code": None,
            "phone": order["phone"],
            "seconds_left": 0,
        }

    # --- cancel / refund -------------------------------------------------
    async def _refund_and_close(self, order: dict[str, Any]) -> tuple[bool, int]:
        """Cancel a waiting order and refund it exactly once."""
        price_kop = int(order["price_kop"] or 0)
        order_id = int(order["id"])
        if price_kop <= 0:
            return self.store.close_order(order_id, "cancelled", 0), 0
        if self.wallet is None or not self.wallet.enabled:
            refunded = price_kop if self.store.refund_atomic(order_id) else 0
            return bool(refunded), refunded

        owner_tg = self._owner_tg(order["key_id"])
        if owner_tg is None:
            LOG.error("order %s has no owner_tg, cannot refund", order_id)
            return self.store.close_order(order_id, "cancelled", price_kop), 0
        # The wallet refund is idempotent on ref, so a retry never pays twice.
        result = await self.wallet.refund(
            owner_tg, price_kop, ref=f"refund:{order_id}"
        )
        closed = self.store.close_order(
            order_id,
            "cancelled",
            price_kop,
            balance_kop=result.get("balance_kop"),
        )
        return closed, price_kop

    async def cancel(self, key_id: str, order_id: int) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        if not order or order["key_id"] != key_id:
            raise OrderNotFound("Order not found")
        if order["status"] == "cancelled":
            return {"status": "cancelled", "refunded_kop": 0}
        if order["status"] == "delivered":
            return {"status": "delivered", "refunded_kop": 0}

        if self._order_is_sandbox(order):
            self.store.close_order(order_id, "cancelled", 0)
            return {"status": "cancelled", "refunded_kop": 0}

        cancelled = await self.provider.cancel(order["provider_order_id"])
        if not cancelled:
            return {"status": order["status"], "refunded_kop": 0}
        closed, refunded = await self._refund_and_close(order)
        return {
            "status": "cancelled" if closed else order["status"],
            "refunded_kop": refunded if closed else 0,
        }

    # --- auto cancel -----------------------------------------------------
    async def auto_cancel_expired(self, now: str | None = None) -> int:
        """Cancel waiting orders whose expires_at has passed. Returns count."""
        moment = now or now_iso()
        cancelled_count = 0
        for order in self.store.list_expired_orders(moment):
            order_id = int(order["id"])
            try:
                if self._order_is_sandbox(order):
                    if self.store.close_order(order_id, "cancelled", 0):
                        cancelled_count += 1
                    continue
                # Best effort: the number may already be released provider-side.
                await self._cancel_provider_order(order["provider_order_id"])
                closed, _ = await self._refund_and_close(order)
                if closed:
                    cancelled_count += 1
                    LOG.info("auto-cancelled expired order %s", order_id)
            except WalletUnavailable as exc:
                # Leave the order waiting: the next tick retries the refund and
                # the wallet deduplicates it by ref.
                LOG.warning("auto-cancel of order %s postponed: %s", order_id, exc)
            except Exception:  # noqa: BLE001 - a bad order must not stop the sweep
                LOG.exception("auto-cancel of order %s failed", order_id)
        return cancelled_count

    async def run_auto_cancel_loop(self, interval: float | None = None) -> None:
        """Background sweeper. Never dies on a single failed tick."""
        period = float(interval or self.settings.auto_cancel_interval or 60)
        while True:
            try:
                count = await self.auto_cancel_expired()
                if count:
                    LOG.info("auto-cancel sweep: %d order(s) cancelled", count)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                LOG.exception("auto-cancel sweep failed")
            await asyncio.sleep(period)

    # --- balance / history ----------------------------------------------
    def balance(self, key_id: str) -> WalletBackedBalance:
        """Cached balance in kopecks; ``await`` it for the live wallet value."""
        if self.store.is_sandbox(key_id):
            return WalletBackedBalance(0)
        try:
            cached = int(self.store.get_balance(key_id))
        except KeyError:
            raise InvalidKey("Invalid API key") from None
        if self.wallet is None or not self.wallet.enabled:
            return WalletBackedBalance(cached)

        async def _refresh() -> int:
            owner_tg = self._owner_tg(key_id)
            if owner_tg is None:
                return cached
            snapshot = await self.wallet.balance(owner_tg)
            self.store.set_balance_cache(key_id, snapshot["balance_kop"])
            return int(snapshot["balance_kop"])

        return WalletBackedBalance(cached, _refresh)

    async def wallet_balance(self, key_id: str) -> dict[str, Any]:
        """Live balance from the bot wallet, with the cache as a fallback."""
        if self.store.is_sandbox(key_id):
            return {
                "balance_kop": 0,
                "balance_rub": 0.0,
                "sandbox": True,
                "banned": False,
                "source": "sandbox",
            }
        cached = int(self.store.get_balance(key_id))
        base = {
            "balance_kop": cached,
            "balance_rub": cached / 100,
            "sandbox": False,
            "banned": False,
            "source": "cache",
        }
        if self.wallet is None or not self.wallet.enabled:
            return base
        owner_tg = self._owner_tg(key_id)
        if owner_tg is None:
            return base
        try:
            snapshot = await self.wallet.balance(owner_tg)
        except WalletUnavailable:
            return base
        self.store.set_balance_cache(key_id, snapshot["balance_kop"])
        return {
            "balance_kop": snapshot["balance_kop"],
            "balance_rub": snapshot["balance_kop"] / 100,
            "sandbox": False,
            "banned": bool(snapshot.get("banned")),
            "source": "wallet",
        }

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
