#!/usr/bin/env python3
"""Is everything an errand needs actually running?

    python skills/friends-kitchen-ordering/scripts/check_ready.py
    python skills/friends-kitchen-ordering/scripts/check_ready.py --json

Reads `GET /api/agent/health` and says which of the four things an errand
depends on is not ready. It changes nothing — the endpoint is the same one the
control panel polls, and this is that answer rendered for a terminal.

Exit codes: 0 ready, 1 something is missing, 2 the service is not answering.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_ready",
        description="Report whether the ordering agent can run an errand right now.",
    )
    parser.add_argument("--json", action="store_true", help="Print the health payload as JSON.")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Seconds to wait for the service."
    )
    return parser.parse_args()


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.ORDERING

    try:
        health = http.get(service.health_url, timeout=args.timeout)
    except http.ServiceDown as exc:
        print(f"{service.name} is not running.\n  {exc}", file=sys.stderr)
        print(f"  Start it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(f"{service.name} answered with a refusal: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(health, indent=2))

    problems: list[str] = []

    if not health.get("restaurantApi"):
        problems.append(
            "The Friends Kitchen REST API is not answering at "
            f"{services.restaurant_api()} — there is nothing to buy from. Start the "
            "restaurant backend first."
        )

    if not health.get("hasApiKey"):
        problems.append(
            health.get("credentialProblem")
            or "The selected model has no usable credentials, so no agent can be built."
        )

    delivery = health.get("delivery") or {}
    if delivery and not delivery.get("configured", True):
        problems.append(
            f"The delivery provider {delivery.get('name', '?')} is not configured. "
            "A counter order is unaffected; a delivery errand will fail at handover."
        )

    if args.json:
        return 1 if problems else 0

    llm = health.get("llm") or {}
    print(f"{service.name} at {service.base_url}")
    print(f"  restaurant API : {'up' if health.get('restaurantApi') else 'DOWN'}")
    print(f"  model          : {health.get('provider')}/{health.get('model')}")
    if llm.get("source"):
        print(f"  selected by    : {llm['source']}")
    print(f"  credentials    : {'ready' if health.get('hasApiKey') else 'MISSING'}")
    print(f"  delivery       : {delivery.get('displayName') or delivery.get('name') or 'none'}")
    print(f"  branches       : {health.get('branches')}")
    print(f"  busy           : {'yes — a run is in progress' if health.get('busy') else 'no'}")

    customer = health.get("customer") or {}
    if customer.get("label"):
        print(f"  saved address  : {customer['label']}")

    if problems:
        print()
        for problem in problems:
            print(f"  ! {problem}")
        return 1

    print("\nReady.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
