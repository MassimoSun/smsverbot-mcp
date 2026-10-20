from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from store import Store

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "list_services",
    "list_countries",
    "get_price",
    "buy_number",
    "wait_for_code",
    "cancel_order",
    "get_balance",
    "order_history",
}

pytestmark = pytest.mark.asyncio


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("No JSON payload in tool result")


async def test_stdio_client_lists_tools_and_reads_balance(tmp_path):
    db_path = tmp_path / "mcp-client.db"
    store = Store(str(db_path), 20)
    key_id, plain_key = store.create_key("mcp-client")
    store.topup(key_id, 5_000)

    env = {
        **os.environ,
        "MCP_DB": str(db_path),
        "PROVIDER": "mock",
        "PYTHONUNBUFFERED": "1",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py"), "--stdio"],
        env=env,
        cwd=str(ROOT),
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert names == EXPECTED_TOOLS
                result = await session.call_tool(
                    "get_balance", {"api_key": plain_key}
                )
                data = _payload(result)
                assert data["ok"] is True
                assert data["balance_rub"] == 50.0
                assert data["balance_kop"] == 5_000
    except (ImportError, OSError) as exc:
        pytest.skip(f"MCP stdio transport unavailable: {exc}")
