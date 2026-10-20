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
                        "SELECT id, label, balance_kop FROM api_keys WHERE id = ?", (key_id,)
                    ).fetchone()
                if not row:
                    return self._fail(404, "NOT_FOUND", "Unknown key_id")
                return self._json(
                    200,
                    {
                        "ok": True,
                        "key_id": row["id"],
                        "label": row["label"],
                        "balance_rub": _rub(row["balance_kop"]),
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
                if not label or len(label) > 64:
                    return self._fail(400, "VALIDATION", "label is required (<=64 chars)")
                if owner is not None and not isinstance(owner, int):
                    return self._fail(400, "VALIDATION", "owner_tg must be an integer")
                key_id, api_key = self.store.create_key(label, owner)
                return self._json(
                    201,
                    {
                        "ok": True,
                        "key_id": key_id,
                        "api_key": api_key,
                        "label": label,
                        "balance_rub": _rub(self.store.get_balance(key_id)),
                    },
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
