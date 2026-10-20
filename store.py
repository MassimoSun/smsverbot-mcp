from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    label TEXT,
    key_hash TEXT UNIQUE,
    balance_kop INTEGER DEFAULT 0,
    created_at TEXT,
    owner_tg INTEGER,
    revoked_at TEXT,
    daily_limit_kop INTEGER
);
CREATE TABLE IF NOT EXISTS topup_refs (
    ref TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    amount_kop INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT,
    provider TEXT,
    provider_order_id TEXT,
    service TEXT,
    country TEXT,
    phone TEXT,
    price_kop INTEGER,
    cost_kop INTEGER,
    status TEXT,
    code TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT,
    order_id INTEGER NULL,
    amount_kop INTEGER,
    kind TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_key ON orders(key_id);
CREATE INDEX IF NOT EXISTS idx_orders_provider ON orders(provider_order_id);
CREATE INDEX IF NOT EXISTS idx_ledger_key ON ledger(key_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_key(plain_key: str) -> str:
    return hashlib.sha256(plain_key.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path: str, rate_limit_per_15min: int = 20) -> None:
        self.db_path = str(db_path)
        self.rate_limit_per_15min = int(rate_limit_per_15min)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
        self._rate_lock = threading.Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._waits: dict[str, int] = defaultdict(int)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "owner_tg" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN owner_tg INTEGER")
        if "revoked_at" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN revoked_at TEXT")
        if "daily_limit_kop" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN daily_limit_kop INTEGER")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def create_key(self, label: str, owner_tg: int | None = None) -> tuple[str, str]:
        key_id = uuid.uuid4().hex
        plain_key = "svb_" + secrets.token_urlsafe(32)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, label, key_hash, balance_kop, created_at, owner_tg)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (key_id, label, hash_key(plain_key), now_iso(), owner_tg),
            )
        return key_id, plain_key

    def list_keys_by_owner(self, owner_tg: int) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, label, balance_kop, created_at, key_hash, revoked_at, daily_limit_kop"
                " FROM api_keys WHERE owner_tg = ? ORDER BY created_at DESC",
                (int(owner_tg),),
            ).fetchall()
        return [dict(row) for row in rows]

    def topup_once(self, key_id: str, amount_kop: int, ref: str) -> bool:
        amount = int(amount_kop)
        if amount <= 0:
            raise ValueError("amount_kop must be positive")
        if not ref:
            raise ValueError("ref is required")
        conn = self._connect()
        try:
            self._begin(conn)
            row = conn.execute(
                "SELECT balance_kop FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(key_id)
            try:
                conn.execute(
                    "INSERT INTO topup_refs (ref, key_id, amount_kop, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (ref, key_id, amount, now_iso()),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE api_keys SET balance_kop = balance_kop + ? WHERE id = ?",
                (amount, key_id),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, NULL, ?, 'topup', ?)",
                (key_id, amount, now_iso()),
            )
            conn.commit()
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def find_key(self, plain_key: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(plain_key),)
            ).fetchone()
        if not row:
            return None
        key = dict(row)
        if key.get("revoked_at"):
            return None
        return key

    def revoke_key(self, key_id: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now_iso(), key_id),
            )
        return cur.rowcount > 0

    def set_daily_limit(self, key_id: str, limit_kop: int | None) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET daily_limit_kop = ? WHERE id = ?", (limit_kop, key_id)
            )
        return cur.rowcount > 0

    def reissue_key(self, key_id: str) -> tuple[str, str] | None:
        """Отозвать ключ и выдать новый с тем же балансом (одна транзакция)."""
        new_id = uuid.uuid4().hex
        plain_key = "svb_" + secrets.token_urlsafe(32)
        conn = self._connect()
        try:
            self._begin(conn)
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            if not row or row["revoked_at"]:
                conn.rollback()
                return None
            balance = int(row["balance_kop"])
            owner = row["owner_tg"]
            conn.execute(
                "UPDATE api_keys SET revoked_at = ?, balance_kop = 0 WHERE id = ?",
                (now_iso(), key_id),
            )
            conn.execute(
                "INSERT INTO api_keys (id, label, key_hash, balance_kop, created_at, owner_tg,"
                " daily_limit_kop) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id, f"tg{owner}" if owner else row["label"], hash_key(plain_key),
                 balance, now_iso(), owner, row["daily_limit_kop"]),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, NULL, ?, 'reissue_out', ?)",
                (key_id, -balance, now_iso()),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, NULL, ?, 'reissue_in', ?)",
                (new_id, balance, now_iso()),
            )
            conn.commit()
            return new_id, plain_key
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def key_daily_limit(self, key_id: str) -> int | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT daily_limit_kop FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        if not row or row["daily_limit_kop"] is None:
            return None
        return int(row["daily_limit_kop"])

    def spend_today_kop(self, key_id: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._db() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(-amount_kop), 0) AS spent FROM ledger"
                " WHERE key_id = ? AND kind IN ('debit', 'refund')"
                " AND substr(created_at, 1, 10) = ?",
                (key_id, today),
            ).fetchone()
        return int(row["spent"]) if row else 0

    def try_begin_wait(self, key_id: str, per_key: int, global_max: int) -> bool:
        with self._rate_lock:
            if self._waits.get("__all__", 0) >= global_max:
                return False
            if self._waits.get(key_id, 0) >= per_key:
                return False
            self._waits[key_id] = self._waits.get(key_id, 0) + 1
            self._waits["__all__"] = self._waits.get("__all__", 0) + 1
            return True

    def end_wait(self, key_id: str) -> None:
        with self._rate_lock:
            if self._waits.get(key_id, 0) > 0:
                self._waits[key_id] -= 1
            if self._waits.get("__all__", 0) > 0:
                self._waits["__all__"] -= 1

    def get_balance(self, key_id: str) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT balance_kop FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        if not row:
            raise KeyError(key_id)
        return int(row["balance_kop"])

    def topup(self, key_id: str, amount_kop: int) -> int:
        if int(amount_kop) <= 0:
            raise ValueError("amount_kop must be positive")
        conn = self._connect()
        try:
            self._begin(conn)
            row = conn.execute(
                "SELECT balance_kop FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise KeyError(key_id)
            conn.execute(
                "UPDATE api_keys SET balance_kop = balance_kop + ? WHERE id = ?",
                (int(amount_kop), key_id),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, NULL, ?, 'topup', ?)",
                (key_id, int(amount_kop), now_iso()),
            )
            conn.commit()
            return int(row["balance_kop"]) + int(amount_kop)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def debit_atomic(self, key_id: str, order_id: int, amount_kop: int) -> bool:
        amount = int(amount_kop)
        if amount <= 0:
            return False
        conn = self._connect()
        try:
            self._begin(conn)
            row = conn.execute(
                "SELECT balance_kop FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            if not row or int(row["balance_kop"]) < amount:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE api_keys SET balance_kop = balance_kop - ? WHERE id = ?",
                (amount, key_id),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, ?, ?, 'debit', ?)",
                (key_id, order_id, -amount, now_iso()),
            )
            conn.commit()
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def refund_atomic(self, order_id: int) -> bool:
        conn = self._connect()
        try:
            self._begin(conn)
            order = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (int(order_id),)
            ).fetchone()
            if not order or order["status"] not in ("open", "waiting"):
                conn.rollback()
                return False
            price = int(order["price_kop"])
            conn.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now_iso(), int(order_id)),
            )
            conn.execute(
                "UPDATE api_keys SET balance_kop = balance_kop + ? WHERE id = ?",
                (price, order["key_id"]),
            )
            conn.execute(
                "INSERT INTO ledger (key_id, order_id, amount_kop, kind, created_at)"
                " VALUES (?, ?, ?, 'refund', ?)",
                (order["key_id"], int(order_id), price, now_iso()),
            )
            conn.commit()
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def create_order(
        self,
        key_id: str,
        provider: str,
        provider_order_id: str,
        service: str,
        country: str,
        phone: str,
        price_kop: int,
        cost_kop: int,
        status: str = "waiting",
    ) -> int:
        ts = now_iso()
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO orders (key_id, provider, provider_order_id, service, country,"
                " phone, price_kop, cost_kop, status, code, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    key_id,
                    provider,
                    provider_order_id,
                    service,
                    country,
                    phone,
                    int(price_kop),
                    int(cost_kop),
                    status,
                    ts,
                    ts,
                ),
            )
            return int(cur.lastrowid)

    def update_order_status(self, order_id: int, status: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), int(order_id)),
            )
            return cur.rowcount > 0

    def set_order_code(self, order_id: int, code: str) -> bool:
        with self._db() as conn:
            order = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (int(order_id),)
            ).fetchone()
            if not order or order["status"] == "cancelled":
                return False
            if order["status"] == "delivered":
                return True
            conn.execute(
                "UPDATE orders SET code = ?, status = 'delivered', updated_at = ?"
                " WHERE id = ?",
                (str(code), now_iso(), int(order_id)),
            )
            return True

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (int(order_id),)
            ).fetchone()
        return dict(row) if row else None

    def list_orders(self, key_id: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE key_id = ? ORDER BY id DESC LIMIT ?",
                (key_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_ledger(self, key_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger WHERE key_id = ? ORDER BY id DESC LIMIT ?",
                (key_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_code_count(self, key_id: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE key_id = ?"
                " AND status != 'cancelled' AND substr(created_at, 1, 10) = ?",
                (key_id, today),
            ).fetchone()
        return int(row["n"]) if row else 0

    def allow_request(self, key_id: str) -> bool:
        window = 15 * 60
        now = time.monotonic()
        with self._rate_lock:
            bucket = self._requests[key_id]
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            if len(bucket) >= self.rate_limit_per_15min:
                return False
            bucket.append(now)
            return True

    def reset_rate_limits(self) -> None:
        with self._rate_lock:
            self._requests.clear()


def ledger_sum(rows: Iterable[dict[str, Any]]) -> int:
    return sum(int(r["amount_kop"]) for r in rows)
