from __future__ import annotations

import argparse
import os
import sys

import admin_service
from store import Store


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admin.py", description="smsverbot admin CLI")
    parser.add_argument(
        "--db",
        default=os.environ.get("MCP_DB", "./mcp.db"),
        help="path to the SQLite database (default: MCP_DB or ./mcp.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create an API key")
    create.add_argument("--label", required=True)
    create.add_argument("--topup", type=float, default=0)

    subparsers.add_parser("list", help="list API keys")

    topup = subparsers.add_parser("topup", help="top up a key balance")
    topup.add_argument("--key-id", required=True)
    topup.add_argument("--amount", type=float, required=True)

    balance = subparsers.add_parser("balance", help="show a key balance")
    balance.add_argument("--key-id", required=True)

    orders = subparsers.add_parser("orders", help="list recent orders for a key")
    orders.add_argument("--key-id", required=True)
    orders.add_argument("--limit", type=int, default=10)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = Store(args.db)

    try:
        if args.command == "create":
            info = admin_service.create_key(store, args.label, args.topup)
            print(f"key_id: {info['key_id']}")
            print(info["api_key"])
            print("Warning: this API key is shown only once. Save it now.")
        elif args.command == "list":
            for key in admin_service.list_keys(store):
                print(f"{key['key_id']}  {key['label']}  {key['balance_rub']}")
        elif args.command == "topup":
            info = admin_service.topup(store, args.key_id, args.amount)
            print(f"balance_rub: {info['balance_rub']}")
        elif args.command == "balance":
            info = admin_service.key_balance(store, args.key_id)
            print(f"balance_rub: {info['balance_rub']}")
        elif args.command == "orders":
            rows = admin_service.orders(store, args.key_id, args.limit)
            if not rows:
                print("No orders")
            for row in rows:
                print(
                    f"{row['order_id']}  {row['service']}  {row['country']}"
                    f"  {row['phone']}  {row['price_rub']}  {row['status']}"
                )
        else:
            parser.error(f"unknown command: {args.command}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
