#!/usr/bin/env python3
"""Fetch an agent card from a well-known URL.

    python skills/friends-kitchen-a2a-negotiation/scripts/merchant_card.py
    python .../merchant_card.py --service foodpanda
    python .../merchant_card.py --url http://localhost:8102 --json

`GET /.well-known/agent-card.json` — the one endpoint in this system that is not
wrapped in `{success, data}`, because the well-known URL is what a stranger's
agent reads and it should be the card the spec describes rather than this
project's envelope round one.

This is the same read the buyer's `discover_merchant` tool performs before it
says anything. Three of the four services publish a card: the A2A merchant on
8101, the in-house courier on 8102, and the Foodpanda demonstration agent on
8103.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

WITH_CARDS = tuple(service.key for service in services.ALL if service.has_agent_card)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merchant_card",
        description="Read an A2A agent card from its well-known URL.",
    )
    parser.add_argument(
        "--service",
        choices=WITH_CARDS,
        default="a2a",
        help="Which agent's card to read. Default: the A2A merchant.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="A base URL to read instead, for an agent outside this system.",
    )
    parser.add_argument("--json", action="store_true", help="Print the card verbatim.")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args()


def main() -> int:
    terminal.utf8()
    args = parse_args()

    if args.url:
        base = args.url.rstrip("/")
        card_url = f"{base}/.well-known/agent-card.json"
        start_hint = None
    else:
        service = services.get(args.service)
        card_url = service.card_url or ""
        start_hint = service.start_hint

    try:
        card = http.get(card_url, timeout=args.timeout)
    except http.ServiceDown as exc:
        print(str(exc), file=sys.stderr)
        if start_hint:
            print(f"Start it with: {start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(card, indent=2))
        return 0

    print(f"{card.get('name')}  (A2A {card.get('protocolVersion')})")
    print(f"  {card.get('description', '').strip()}")
    print()
    print(f"  url        : {card.get('url')}")
    print(f"  transport  : {card.get('preferredTransport')}")
    print(f"  provider   : {(card.get('provider') or {}).get('organization')}")

    capabilities = card.get("capabilities") or {}
    if capabilities:
        offered = ", ".join(k for k, v in capabilities.items() if v) or "none"
        print(f"  capable of : {offered}")

    print()
    print("  skills advertised on this card (A2A card skills, not Agent Skills):")
    for skill in card.get("skills") or []:
        print(f"    {skill.get('id')} — {skill.get('name')}")
        description = (skill.get("description") or "").strip()
        if description:
            print(f"        {description}")

    # The extensions are where the two courier cards say the thing a caller most
    # needs and the spec has no field for.
    lifecycle = card.get("x-lifecycle")
    if lifecycle:
        print()
        print("  x-lifecycle:")
        print(f"    statuses        : {', '.join(lifecycle.get('statuses') or [])}")
        print(f"    terminal success: {lifecycle.get('terminalSuccess')}")
        if lifecycle.get("note"):
            print(f"    note            : {lifecycle['note']}")
        for step in lifecycle.get("operatorSteps") or []:
            print(f"    waits at {step.get('waitsAt')} for {step.get('step')} — {step.get('askAt')}")

    currency = card.get("x-currency")
    if currency:
        print()
        print(f"  x-currency  : {currency.get('code')} shown as {currency.get('display')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
