"""Internal admin API for the MCP server.

Localhost-only helper used by the Telegram bot: create API keys and top up
their prepaid balance. Protected by a shared token in the X-Admin-Token header.

Run: python admin_api.py   (ADMIN_API_TOKEN must be set)
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from store import Store

try:  # у store есть now_iso; если сосед перепишет модуль - свой запас
    from store import now_iso as _now_iso
except ImportError:  # pragma: no cover
    from datetime import datetime, timezone

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

LOG = logging.getLogger("smsverbot.admin_api")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


HOST = os.environ.get("ADMIN_API_HOST", "127.0.0.1")
PORT = _env_int("ADMIN_API_PORT", 8766)
TOKEN = os.environ.get("ADMIN_API_TOKEN", "").strip()
DB_PATH = os.environ.get("MCP_DB", "./mcp.db")


def _rub(kop: int) -> float:
    return round(int(kop) / 100.0, 2)


# --- песочница: колонка api_keys.sandbox -------------------------------
def _ensure_sandbox_column(store: Store) -> bool:
    """Гарантировать колонку sandbox (идемпотентно, терпимо к миграциям store)."""
    try:
        with store._db() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
            if "sandbox" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN sandbox INTEGER DEFAULT 0")
            return True
    except Exception:  # noqa: BLE001 - API boundary
        LOG.debug("sandbox column unavailable", exc_info=True)
        return False


def _sandbox_of(store: Store, row: dict) -> bool:
    """Флаг песочницы для строки ключа (row должна содержать id)."""
    if "sandbox" in row and row["sandbox"] is not None:
        return bool(row["sandbox"])
    key_id = str(row.get("id") or row.get("key_id") or "")
    if not key_id:
        return False
    getter = getattr(store, "is_sandbox", None)
    if callable(getter):
        try:
            return bool(getter(key_id))
        except Exception:  # noqa: BLE001
            LOG.debug("store.is_sandbox failed", exc_info=True)
    try:
        with store._db() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)")}
            if "sandbox" not in cols:
                return False
            found = conn.execute(
                "SELECT sandbox FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return bool(found["sandbox"]) if found else False
    except Exception:  # noqa: BLE001
        return False


def _set_sandbox(store: Store, key_id: str, enabled: bool) -> bool:
    """Включить/выключить песочницу. False — ключа нет или колонки нет."""
    setter = getattr(store, "set_sandbox", None)
    if callable(setter):
        try:
            result = setter(key_id, bool(enabled))
            if result is None or result:
                return True
        except Exception:  # noqa: BLE001
            LOG.debug("store.set_sandbox failed", exc_info=True)
    if not _ensure_sandbox_column(store):
        return False
    with store._db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET sandbox = ? WHERE id = ?",
            (1 if enabled else 0, str(key_id)),
        )
    return cur.rowcount > 0


def _create_key(
    store: Store, label: str, owner: int | None, sandbox: bool
) -> tuple[str, str]:
    """Создать ключ, при необходимости сразу помеченный как песочница."""
    if sandbox:
        try:
            key_id, api_key = store.create_key(label, owner, sandbox=True)
        except TypeError:
            key_id, api_key = store.create_key(label, owner)
        _set_sandbox(store, key_id, True)
        return key_id, api_key
    return store.create_key(label, owner)


def _revoke_by_owner(store: Store, owner_tg: int) -> int:
    """Погасить все активные ключи владельца. Возвращает число погашенных."""
    helper = getattr(store, "revoke_keys_by_owner", None)
    if callable(helper):
        try:
            for args in ((int(owner_tg),), (int(owner_tg), "ban")):
                try:
                    return int(helper(*args) or 0)
                except TypeError:
                    continue
        except Exception:  # noqa: BLE001
            LOG.debug("store.revoke_keys_by_owner failed", exc_info=True)
    with store._db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE owner_tg = ? AND revoked_at IS NULL",
            (_now_iso(), int(owner_tg)),
        )
    return int(cur.rowcount or 0)


class Handler(BaseHTTPRequestHandler):
    store: Store | None = None
    server_version = "smsverbot-admin/0.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        LOG.info("%s - %s", self.address_string(), fmt % args)

    # --- helpers ---------------------------------------------------------
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, code: str, message: str = "") -> None:
        self._json(status, {"ok": False, "error": code, "message": message or code})

    def _auth(self) -> bool:
        got = self.headers.get("X-Admin-Token", "")
        if not TOKEN or not got or not hmac.compare_digest(got, TOKEN):
            self._fail(401, "UNAUTHORIZED", "Bad admin token")
            return False
        return True

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._fail(400, "VALIDATION", "Bad Content-Length")
            return None
        if length <= 0 or length > 65536:
            self._fail(400, "VALIDATION", "Empty or oversized body")
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._fail(400, "VALIDATION", "Body must be JSON")
            return None
        if not isinstance(data, dict):
            self._fail(400, "VALIDATION", "Body must be a JSON object")
            return None
        return data

    # --- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if not self._auth():
                return
            if path == "/health":
                with self.store._db() as conn:
                    keys = conn.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()["n"]
                return self._json(200, {"ok": True, "keys": int(keys)})
            if path == "/keys":
                qs = parse_qs(parsed.query)
                raw_owner = (qs.get("owner_tg") or [""])[0]
                if not raw_owner.isdigit():
                    return self._fail(400, "VALIDATION", "owner_tg must be an integer")
                rows = self.store.list_keys_by_owner(int(raw_owner))
                keys = [
                    {
                        "key_id": row["id"],
                        "label": row["label"],
                        "balance_rub": _rub(row["balance_kop"]),
                        "created_at": row["created_at"],
                        "masked_key": str(row["key_hash"])[:8],
                        "revoked": bool(row.get("revoked_at")),
                        "sandbox": _sandbox_of(self.store, row),
                        "daily_limit_rub": _rub(row["daily_limit_kop"])
                        if row.get("daily_limit_kop") is not None
                        else None,
                        "spent_today_rub": _rub(self.store.spend_today_kop(row["id"])),
                    }
                    for row in rows
                ]
                return self._json(200, {"ok": True, "keys": keys})
            if path.startswith("/keys/"):
                key_id = path[len("/keys/") :]
                with self.store._db() as conn:
                    row = conn.execute(
                        "SELECT * FROM api_keys WHERE id = ?", (key_id,)
                    ).fetchone()
                if not row:
                    return self._fail(404, "NOT_FOUND", "Unknown key_id")
                data = dict(row)
                return self._json(
                    200,
                    {
                        "ok": True,
                        "key_id": data["id"],
                        "label": data["label"],
                        "balance_rub": _rub(data["balance_kop"]),
                        "sandbox": _sandbox_of(self.store, data),
                        "revoked": bool(data.get("revoked_at")),
                    },
                )
            return self._fail(404, "NOT_FOUND", "Unknown route")
        except Exception:  # noqa: BLE001 - API boundary
            LOG.exception("GET %s failed", path)
            return self._fail(500, "INTERNAL", "Unexpected server error")
        finally:
            LOG.info(
                "GET %s -> done in %dms", path, int((time.monotonic() - started) * 1000)
            )

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if not self._auth():
                return
            if path == "/keys":
                data = self._body()
                if data is None:
                    return
                label = str(data.get("label") or "").strip()
                owner = data.get("owner_tg")
                sandbox = data.get("sandbox", False)
                if not label or len(label) > 64:
                    return self._fail(400, "VALIDATION", "label is required (<=64 chars)")
                if owner is not None and not isinstance(owner, int):
                    return self._fail(400, "VALIDATION", "owner_tg must be an integer")
                if not isinstance(sandbox, bool):
                    return self._fail(400, "VALIDATION", "sandbox must be a boolean")
                key_id, api_key = _create_key(self.store, label, owner, sandbox)
                return self._json(
                    201,
                    {
                        "ok": True,
                        "key_id": key_id,
                        "api_key": api_key,
                        "label": label,
                        "balance_rub": _rub(self.store.get_balance(key_id)),
                        "sandbox": bool(sandbox),
                    },
                )
            if path == "/keys/revoke_by_owner":
                data = self._body()
                if data is None:
                    return
                owner = data.get("owner_tg")
                reason = str(data.get("reason") or "ban").strip()[:64]
                if not isinstance(owner, int) or isinstance(owner, bool):
                    return self._fail(400, "VALIDATION", "owner_tg must be an integer")
                revoked = _revoke_by_owner(self.store, owner)
                LOG.info("revoked %d key(s) of owner %s (reason=%s)", revoked, owner, reason)
                return self._json(
                    200,
                    {"ok": True, "owner_tg": owner, "revoked": revoked, "reason": reason},
                )
            if path == "/topup":
                data = self._body()
                if data is None:
                    return
                key_id = str(data.get("key_id") or "").strip()
                ref = str(data.get("ref") or "").strip()
                try:
                    amount_rub = float(data.get("amount_rub"))
                except (TypeError, ValueError):
                    return self._fail(400, "VALIDATION", "amount_rub must be a number")
                if not key_id or not ref:
                    return self._fail(400, "VALIDATION", "key_id and ref are required")
                if amount_rub <= 0 or amount_rub > 100000:
                    return self._fail(400, "VALIDATION", "amount_rub out of range")
                amount_kop = int(round(amount_rub * 100))
                try:
                    credited = self.store.topup_once(key_id, amount_kop, ref)
                except KeyError:
                    return self._fail(404, "NOT_FOUND", "Unknown key_id")
                return self._json(
                    200,
                    {
                        "ok": True,
                        "key_id": key_id,
                        "balance_rub": _rub(self.store.get_balance(key_id)),
                        "credited": bool(credited),
                    },
                )
            if path.startswith("/keys/") and path.endswith("/reissue"):
                key_id = path[len("/keys/") : -len("/reissue")]
                result = self.store.reissue_key(key_id)
                if not result:
                    return self._fail(404, "NOT_FOUND", "Unknown key or already revoked")
                new_key_id, api_key = result
                return self._json(
                    200,
                    {
                        "ok": True,
                        "old_key_id": key_id,
                        "key_id": new_key_id,
                        "api_key": api_key,
                        "balance_rub": _rub(self.store.get_balance(new_key_id)),
                    },
                )
            if path.startswith("/keys/") and path.endswith("/revoke"):
                key_id = path[len("/keys/") : -len("/revoke")]
                if not key_id:
                    return self._fail(400, "VALIDATION", "key_id is required")
                if not self.store.revoke_key(key_id):
                    return self._fail(404, "NOT_FOUND", "Unknown key or already revoked")
                return self._json(200, {"ok": True, "key_id": key_id, "revoked": True})
            if path.startswith("/keys/") and path.endswith("/sandbox"):
                key_id = path[len("/keys/") : -len("/sandbox")]
                if not key_id:
                    return self._fail(400, "VALIDATION", "key_id is required")
                data = self._body()
                if data is None:
                    return
                # Принимаем оба имени: бот шлёт "sandbox", ранние клиенты — "enabled".
                # Раньше API требовал только "enabled", и кнопка «Тестовый режим»
                # падала с VALIDATION на каждом нажатии (баг 22.10.2026).
                if "enabled" in data:
                    enabled = data["enabled"]
                elif "sandbox" in data:
                    enabled = data["sandbox"]
                else:
                    return self._fail(400, "VALIDATION", "enabled is required")
                if not isinstance(enabled, bool):
                    return self._fail(400, "VALIDATION", "enabled must be a boolean")
                with self.store._db() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM api_keys WHERE id = ?", (key_id,)
                    ).fetchone()
                if not exists:
                    return self._fail(404, "NOT_FOUND", "Unknown key_id")
                if not _set_sandbox(self.store, key_id, enabled):
                    return self._fail(500, "INTERNAL", "Could not update sandbox flag")
                return self._json(
                    200, {"ok": True, "key_id": key_id, "sandbox": bool(enabled)}
                )
            if path.startswith("/keys/") and path.endswith("/limit"):
                key_id = path[len("/keys/") : -len("/limit")]
                data = self._body()
                if data is None:
                    return
                raw = data.get("amount_rub")
                if raw is None:
                    limit_kop = None
                else:
                    try:
                        amount = float(raw)
                    except (TypeError, ValueError):
                        return self._fail(400, "VALIDATION", "amount_rub must be a number or null")
                    if amount < 0 or amount > 100000:
                        return self._fail(400, "VALIDATION", "amount_rub out of range")
                    limit_kop = int(round(amount * 100))
                if not self.store.set_daily_limit(key_id, limit_kop):
                    return self._fail(404, "NOT_FOUND", "Unknown key_id")
                return self._json(
                    200,
                    {
                        "ok": True,
                        "key_id": key_id,
                        "daily_limit_rub": None if limit_kop is None else _rub(limit_kop),
                    },
                )
            return self._fail(404, "NOT_FOUND", "Unknown route")
        except Exception:  # noqa: BLE001 - API boundary
            LOG.exception("POST %s failed", path)
            return self._fail(500, "INTERNAL", "Unexpected server error")
        finally:
            LOG.info(
                "POST %s -> done in %dms", path, int((time.monotonic() - started) * 1000)
            )


def build_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    _ensure_sandbox_column(store)
    handler = type("BoundHandler", (Handler,), {"store": store})
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if not TOKEN:
        LOG.error("ADMIN_API_TOKEN is not set - refusing to start")
        return 2
    store = Store(DB_PATH)
    server = build_server(HOST, PORT, store)
    LOG.info("admin API on http://%s:%d (db=%s)", HOST, PORT, DB_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
