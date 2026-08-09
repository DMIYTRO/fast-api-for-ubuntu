#!/usr/bin/env python3
"""Script for fetching orderinfo from Sborka API (action=orderinfo)."""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://sborka.ua/api.php"
API_KEY_FILE = Path(__file__).with_name("sborka_api_key.txt")


def load_api_key() -> str:
    try:
        api_key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Cannot load API key file: {API_KEY_FILE}") from error

    if not api_key:
        raise RuntimeError(f"API key file is empty: {API_KEY_FILE}")
    return api_key


def fetch_order_info(
    orders: list[str | int],
    timeout: int = 20,
) -> tuple[int, str]:
    """Send orderinfo request for a list of order IDs in JSON array format."""
    order_ids = [str(o).strip() for o in orders if str(o).strip()]
    if not order_ids:
        raise ValueError("Order list cannot be empty")

    api_key = load_api_key()

    query_params = {
        "action": "orderinfo",
        "api_key": api_key,
    }
    
    # Sborka API expects orders as JSON array string, e.g. '[25661092]' or '[25661092,25676955]'
    post_payload = {
        "api_key": api_key,
        "orders": json.dumps([int(o) if str(o).isdigit() else str(o) for o in order_ids]),
    }

    query = urllib.parse.urlencode(query_params)
    payload = urllib.parse.urlencode(post_payload).encode("utf-8")

    request = urllib.request.Request(
        f"{API_URL}?{query}",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout, context=ctx) as response:
        return response.status, response.read().decode("utf-8", errors="replace").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch orderinfo from sborka.ua")
    parser.add_argument("orders", nargs="+", help="List of order IDs")
    args = parser.parse_args()

    try:
        status, body = fetch_order_info(args.orders)
        print(f"HTTP {status}")
        try:
            print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(body or "(empty body)")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
