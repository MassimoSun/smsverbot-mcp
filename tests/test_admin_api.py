import json
import os
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

from admin_api import build_server
from store import Store

TOKEN = "test-token-123"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("admin_api.TOKEN", TOKEN)
    db_path = str(tmp_path / "admin.db")
    store = Store(db_path, 20)
    server = build_server("127.0.0.1", 0, store)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"base": f"http://127.0.0.1:{port}", "store": store, "db": db_path}
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_wrong_token_is_rejected(api):
    status, body = call(api["base"], "GET", "/health", token="nope")
    assert status == 401 and body["error"] == "UNAUTHORIZED"


def test_create_key_and_topup_idempotent(api):
    status, created = call(api["base"], "POST", "/keys", {"label": "tg42", "owner_tg": 42})
    assert status == 201 and created["api_key"].startswith("svb_")
    assert created["balance_rub"] == 0.0
    key_id = created["key_id"]

    status, first = call(
        api["base"], "POST", "/topup",
        {"key_id": key_id, "amount_rub": 250, "ref": "charge-1"},
    )
    assert status == 200 and first["balance_rub"] == 250.0 and first["credited"] is True

    status, again = call(
        api["base"], "POST", "/topup",
        {"key_id": key_id, "amount_rub": 250, "ref": "charge-1"},
    )
    assert again["credited"] is False and again["balance_rub"] == 250.0

    status, second = call(
        api["base"], "POST", "/topup",
        {"key_id": key_id, "amount_rub": 100, "ref": "charge-2"},
    )
    assert second["credited"] is True and second["balance_rub"] == 350.0
    assert api["store"].get_balance(key_id) == 35000


def test_list_by_owner_masks_key(api):
    created = call(api["base"], "POST", "/keys", {"label": "tg7", "owner_tg": 7})[1]
    status, body = call(api["base"], "GET", "/keys?owner_tg=7")
    assert status == 200 and len(body["keys"]) == 1
    row = body["keys"][0]
    assert row["key_id"] == created["key_id"]
    assert len(row["masked_key"]) == 8
    assert "api_key" not in row


def test_unknown_key_404(api):
    status, body = call(api["base"], "GET", "/keys/deadbeef")
    assert status == 404 and body["error"] == "NOT_FOUND"
    status, body = call(
        api["base"], "POST", "/topup",
        {"key_id": "deadbeef", "amount_rub": 10, "ref": "x"},
    )
    assert status == 404


def test_validation_errors(api):
    status, body = call(api["base"], "POST", "/keys", {"label": ""})
    assert status == 400 and body["error"] == "VALIDATION"
    status, _ = call(api["base"], "POST", "/topup", {"key_id": "k", "amount_rub": -5, "ref": "r"})
    assert status == 400


def test_migration_adds_owner_tg(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE api_keys (id TEXT PRIMARY KEY, label TEXT, key_hash TEXT UNIQUE,"
        " balance_kop INTEGER DEFAULT 0, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    store = Store(db_path, 20)
    key_id, _ = store.create_key("legacy", owner_tg=99)
    assert store.list_keys_by_owner(99)[0]["id"] == key_id
    assert store.get_balance(key_id) == 0
