#!/usr/bin/env python3
"""Send the ordering agent on one errand, and follow it to its report.

    python skills/friends-kitchen-ordering/scripts/place_order.py \
        "Order two cheeseburgers and a large drink" --coupon FK-8H2K-9QW1 --limit 2500

    python .../place_order.py "Order a chicken burger" --limit 1500 --mode browser
    python .../place_order.py "Order a family bucket" --coupon FK-... --lat 33.5875 --lon 72.9950

Two calls, exactly as the control panel makes them: `POST /api/agent/runs`
starts the errand and returns an id, then `GET /api/agent/runs/{id}/events`
streams what the agent is doing. Nothing here decides anything about the order
— the agent does that, out of `agent/prompts.py` and `agent/tools/`, and this
script only carries the request in and the trace out.

Exit codes: 0 the run finished, 1 it failed or was cancelled, 2 the service is
not answering or refused the request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402


def rupees(amount: object) -> str:
    """A figure as the rest of this system writes it: `Rs 1,093`.

    The same rule as `agent/wallet.rupees`, not an import of it: this script is
    meant to run under whatever Python is to hand, and reaching into the
    application package to format a number would make it need the repository on
    `sys.path` to print a total.
    """
    if isinstance(amount, (int, float)):
        return f"Rs {amount:,.2f}".removesuffix(".00")
    return "Rs 0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="place_order",
        description="Run one Friends Kitchen ordering errand through the service on 8100.",
    )
    parser.add_argument("instruction", help="What to order, in plain language.")
    parser.add_argument("--coupon", default=None, help="Coupon code to spend.")
    parser.add_argument(
        "--limit",
        type=float,
        default=0.0,
        help="Cash in rupees the agent may spend beyond the coupon. Default 0.",
    )
    parser.add_argument(
        "--mode",
        choices=["api", "browser"],
        default="api",
        help="api: order through the REST API. browser: drive the real website.",
    )
    parser.add_argument("--customer", default=None, help="Customer id for the redemption record.")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Browser mode only: show the window instead of running headless.",
    )
    parser.add_argument("--lat", type=float, default=None, help="Delivery latitude.")
    parser.add_argument("--lon", type=float, default=None, help="Delivery longitude.")
    parser.add_argument("--label", default=None, help="Delivery address in words.")
    parser.add_argument("--quiet", action="store_true", help="Only print the final report.")
    parser.add_argument("--json", action="store_true", help="Print the finished run as JSON.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Seconds to follow the run before giving up on the stream.",
    )
    return parser.parse_args()


def build_body(args: argparse.Namespace) -> dict:
    """The `StartRunIn` payload, exactly as server.py declares it."""
    body: dict = {
        "instruction": args.instruction,
        "couponCode": args.coupon,
        "cashLimit": args.limit,
        "mode": args.mode,
        "customerId": args.customer,
        "headless": not args.headed,
    }

    if (args.lat is None) != (args.lon is None):
        raise SystemExit("Give both --lat and --lon, or neither.")

    if args.lat is not None:
        body["userLocation"] = {
            "latitude": args.lat,
            "longitude": args.lon,
            "label": args.label,
            "source": "manual",
        }

    return body


def render(event: dict) -> None:
    """One event from the run's stream, as a line.

    The event vocabulary is server.py's: status, location, browser, tool,
    tool_result, text, final, error, end.
    """
    kind = event.get("type")

    if kind == "status":
        queued = " (queued behind another run)" if event.get("queued") else ""
        print(f"[{event.get('status')}]{queued}")
    elif kind == "location":
        restaurant = (event.get("restaurant") or {}).get("name", "?")
        print(
            f"  delivery: {restaurant} -> customer, "
            f"{event.get('distanceKm')} km, via {event.get('deliveryService')}"
        )
    elif kind == "browser":
        print(f"  browser {event.get('state')}")
    elif kind == "tool":
        print(f"  -> {event.get('name')}")
    elif kind == "text":
        print(event.get("text", ""), end="", flush=True)
    elif kind == "error":
        print(f"\n  ! {event.get('message')}", file=sys.stderr)


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.ORDERING
    body = build_body(args)

    if not body["couponCode"] and body["cashLimit"] <= 0:
        print(
            "Give the agent a coupon, a cash limit, or both — it cannot buy "
            "anything with neither.",
            file=sys.stderr,
        )
        return 2

    runs = f"{service.base_url}{service.prefix}/runs"

    try:
        started = http.post(runs, body, timeout=30.0)
    except http.ServiceDown as exc:
        print(f"{exc}\nStart it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = started["runId"]
    if not args.quiet:
        print(f"run {run_id}: {args.instruction}")

    try:
        for event in http.stream(f"{runs}/{run_id}/events", timeout=args.timeout):
            if not args.quiet:
                render(event)
    except (http.ServiceDown, http.ServiceError) as exc:
        # The run itself may well have survived the stream dropping, so fall
        # through to the final read rather than calling this a failure.
        print(f"\n  ! the trace stopped: {exc}", file=sys.stderr)

    try:
        run = http.get(f"{runs}/{run_id}", timeout=30.0)
    except (http.ServiceDown, http.ServiceError) as exc:
        print(f"\nCould not read the finished run: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(run, indent=2))
    else:
        print()
        if run.get("finalText"):
            print(run["finalText"].strip())
        if run.get("error"):
            print(f"\nThe run {run.get('status')}: {run['error']}", file=sys.stderr)

        wallet = run.get("wallet") or {}
        if wallet:
            # `wallet.summary()` is raw numbers, deliberately — the console
            # renders them itself. Everything in this system is rupees, so
            # formatting them here is the same rule the tools follow, and a bare
            # `1093` on a terminal is exactly the figure somebody reads as
            # dollars.
            print(
                f"\n  coupon redeemed : {rupees(wallet.get('couponRedeemed'))}"
                f"\n  cash spent      : {rupees(wallet.get('cashSpent'))}"
                f" of {rupees(wallet.get('cashLimit'))}"
            )

    return 0 if run.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
