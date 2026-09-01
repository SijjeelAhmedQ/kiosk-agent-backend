#!/usr/bin/env python3
"""What the agent could actually be sent out with.

    python skills/friends-kitchen-ordering/scripts/list_coupons.py
    python skills/friends-kitchen-ordering/scripts/list_coupons.py --spendable
    python skills/friends-kitchen-ordering/scripts/list_coupons.py --json

Reads `GET /api/agent/coupons`, which is the ordering service's proxy of the
restaurant's coupon list — the same read the console's picker does, and proxied
for the same reason: the Friends Kitchen API only allows its own origin.

Spent, expired and cancelled coupons are listed too, with their status. That is
the point of listing them: a coupon that is "not working" is nearly always one
of those rather than a typo, and hiding them only raises the question.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

SPENDABLE = ("unused", "partially_redeemed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="list_coupons",
        description="List the restaurant's coupons, spendable ones first.",
    )
    parser.add_argument(
        "--spendable",
        action="store_true",
        help="Only coupons that still have value: unused or partially redeemed.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.ORDERING
    url = f"{service.base_url}{service.prefix}/coupons"

    try:
        payload = http.get(url, timeout=args.timeout)
    except http.ServiceDown as exc:
        print(f"{exc}\nStart it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    items = list(payload.get("items") or [])
    if args.spendable:
        items = [c for c in items if c.get("status") in SPENDABLE]

    if args.json:
        print(json.dumps(items, indent=2))
        return 0 if items else 1

    if not items:
        print("No coupons." if not args.spendable else "No spendable coupons.")
        return 1

    # Spendable first, then the rest — the order the picker greys them out in.
    items.sort(key=lambda c: (c.get("status") not in SPENDABLE, str(c.get("couponCode"))))

    width = max(len(str(c.get("couponCode") or "")) for c in items)
    for coupon in items:
        code = str(coupon.get("couponCode") or "?").ljust(width)
        status = str(coupon.get("status") or "?")
        balance = coupon.get("remainingBalance")
        # `Rs 1,093` — the one form an amount takes anywhere in this system.
        value = (
            f"Rs {balance:,.2f}".removesuffix(".00")
            if isinstance(balance, (int, float))
            else "—"
        )
        note = coupon.get("productName") or coupon.get("couponType") or ""
        expiry = coupon.get("expiryDate") or ""
        mark = " " if status in SPENDABLE else "x"
        print(f" {mark} {code}  {status:<20} {value:>12}  {note} {expiry}".rstrip())

    print()
    print(f"{sum(1 for c in items if c.get('status') in SPENDABLE)} spendable of {len(items)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
