#!/usr/bin/env python3
"""Send the buyer agent out to negotiate one order, and follow the transcript.

    python skills/friends-kitchen-a2a-negotiation/scripts/negotiate_order.py \
        "Order a chicken burger and fries, take away" \
        --coupon FK-8H2K-9QW1 --limit 2500

The same two calls the A2A console makes: `POST /api/a2a/runs` starts the
errand, `GET /api/a2a/runs/{id}/events` streams both sides of the conversation.
Nothing here negotiates. The buyer's judgement is in `agent/a2a/prompts.py` and
its hands are in `agent/a2a/buyer_tools.py`; this script carries the request in
and the transcript out.

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
    """`Rs 1,093` — the one form an amount takes anywhere in this system."""
    if isinstance(amount, (int, float)):
        return f"Rs {amount:,.2f}".removesuffix(".00")
    return "Rs 0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="negotiate_order",
        description="Run one agent-to-agent order through the ordering desk on 8101.",
    )
    parser.add_argument("instruction", help="What to order, in plain language.")
    parser.add_argument("--coupon", default=None, help="Coupon code the buyer carries.")
    parser.add_argument(
        "--limit",
        type=float,
        default=0.0,
        help="Cash in rupees the buyer may spend beyond the coupon. Default 0.",
    )
    parser.add_argument("--customer", default=None, help="Customer id for the redemption record.")
    parser.add_argument("--lat", type=float, default=None, help="Delivery latitude.")
    parser.add_argument("--lon", type=float, default=None, help="Delivery longitude.")
    parser.add_argument("--label", default=None, help="Delivery address in words.")
    parser.add_argument(
        "--where-it-goes",
        action="store_true",
        help=(
            "The customer has already asked for this to be delivered to them. "
            "Not the same as naming a drop: this is the consent the delivery "
            "agent would otherwise stop and ask for."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final report.")
    parser.add_argument("--json", action="store_true", help="Print the finished run as JSON.")
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser.parse_args()


def build_body(args: argparse.Namespace) -> dict:
    """The `StartRunIn` payload, exactly as agent/a2a/protocol.py declares it."""
    body: dict = {
        "instruction": args.instruction,
        "couponCode": args.coupon,
        "cashLimit": args.limit,
        "customerId": args.customer,
        "whereItGoes": args.where_it_goes,
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

    The vocabulary is `agent/a2a/protocol.py`: status, message, artifact, tool,
    tool_result, final, error, end. Every one that has a side carries a
    `speaker`, because a transcript with only half a conversation in it is worse
    than none.
    """
    kind = event.get("type")
    speaker = event.get("speaker", "")

    if kind == "status":
        print(f"[{event.get('status')}]")
    elif kind == "message":
        text = (event.get("text") or "").strip()
        print(f"\n{speaker}: {text}")
        data = event.get("data")
        if data:
            print(f"    + data: {json.dumps(data)}")
    elif kind == "artifact":
        print(f"  [{speaker} artifact: {event.get('name')}] {json.dumps(event.get('data'))}")
    elif kind == "tool":
        print(f"  ({speaker}) -> {event.get('name')}")
    elif kind == "error":
        print(f"\n  ! {event.get('message')}", file=sys.stderr)


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.A2A
    body = build_body(args)

    if not body["couponCode"] and body["cashLimit"] <= 0:
        print(
            "Give the buyer a coupon, a cash limit, or both — it cannot "
            "negotiate with neither.",
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
        # The negotiation may well have survived the stream dropping, so read
        # the run rather than calling this a failure.
        print(f"\n  ! the transcript stopped: {exc}", file=sys.stderr)

    try:
        run = http.get(f"{runs}/{run_id}", timeout=30.0)
    except (http.ServiceDown, http.ServiceError) as exc:
        print(f"\nCould not read the finished run: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(run, indent=2))
        return 0 if run.get("status") == "done" else 1

    print()
    if run.get("finalText"):
        print(run["finalText"].strip())
    if run.get("merchantTaskId"):
        print(f"\n  merchant task : {run['merchantTaskId']}")
    if run.get("error"):
        print(f"\nThe run {run.get('status')}: {run['error']}", file=sys.stderr)

    wallet = run.get("wallet") or {}
    if wallet:
        print(
            f"  coupon redeemed : {rupees(wallet.get('couponRedeemed'))}\n"
            f"  cash spent      : {rupees(wallet.get('cashSpent'))}"
            f" of {rupees(wallet.get('cashLimit'))}"
        )

    return 0 if run.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
