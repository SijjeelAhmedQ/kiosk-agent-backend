#!/usr/bin/env python3
"""What is up on the floor, and what to type to start what is not.

    python skills/friends-kitchen-operations/scripts/service_status.py
    python .../service_status.py --json
    python .../service_status.py --require ordering delivery

Asks each service's own health endpoint. It starts nothing: starting a process
is the operator's, and a script that did it silently would be a script that
started four of them on the wrong ports. What it gives back is the state and
the exact command.

Exit codes: 0 everything asked for is up and able to work, 1 something is
missing. With `--require`, only the named services count towards that.
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
        prog="service_status",
        description="Report which Friends Kitchen agent services are running.",
    )
    parser.add_argument(
        "--require",
        nargs="*",
        choices=tuple(services.BY_KEY),
        default=None,
        help="Only these services decide the exit code. Default: all four.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=4.0)
    return parser.parse_args()


def probe(service: services.Service, timeout: float) -> dict:
    """One service, asked about itself.

    Two different questions, and a service can answer yes to the first and no to
    the second: *is it up* is whether health answers at all, and *can it work*
    is whether it has a usable brain. They have different fixes.
    """
    row: dict = {
        "key": service.key,
        "name": service.name,
        "url": service.base_url,
        "up": False,
        "ready": None,
        "problem": None,
        "detail": {},
        "startHint": service.start_hint,
    }

    try:
        health = http.get(service.health_url, timeout=timeout)
    except (http.ServiceDown, http.ServiceError) as exc:
        row["problem"] = str(exc)
        return row

    row["up"] = True
    row["detail"] = health

    # Each service words the same question its own way. Reading all four here,
    # rather than making the caller know which key belongs to which port, is the
    # only reason this function is longer than one line.
    if service.key == "ordering":
        row["ready"] = bool(health.get("hasApiKey"))
        row["problem"] = health.get("credentialProblem")
        llm = health.get("llm") or {}
        row["running"] = f"{llm.get('provider')}/{llm.get('model')}"
    elif service.key == "a2a":
        buyer, merchant = health.get("buyer") or {}, health.get("merchant") or {}
        row["ready"] = bool(buyer.get("ready")) and bool(merchant.get("ready"))
        row["problem"] = buyer.get("problem") or merchant.get("problem")
        row["running"] = (
            f"buyer {buyer.get('provider')}/{buyer.get('model')}, "
            f"merchant {merchant.get('provider')}/{merchant.get('model')}"
        )
    elif service.key == "foodpanda":
        dispatcher = health.get("dispatcher") or {}
        row["ready"] = bool(dispatcher.get("ready"))
        row["problem"] = dispatcher.get("problem")
        row["running"] = f"{dispatcher.get('provider')}/{dispatcher.get('model')}"
    else:
        # The in-house courier has no model and no credential to get wrong, so
        # answering at all is the whole of its readiness.
        row["ready"] = True
        row["running"] = "no model — this courier needs none"

    return row


def main() -> int:
    terminal.utf8()
    args = parse_args()
    rows = [probe(service, args.timeout) for service in services.ALL]

    restaurant = services.restaurant_api()
    # The ordering service already asks the restaurant on our behalf, and its
    # answer is the one that matters — it is the address the agents actually
    # buy from. Fall back to asking directly when 8100 is down.
    ordering = next(row for row in rows if row["key"] == "ordering")
    if ordering["up"]:
        restaurant_up = bool((ordering["detail"] or {}).get("restaurantApi"))
    else:
        # The same URL `agent/friends_kitchen_api.health` builds: the base with
        # everything from `/api/` onwards trimmed off, plus `/health`. The
        # restaurant's health endpoint sits above its versioned API, not inside it.
        root = restaurant.rsplit("/api/", 1)[0]
        restaurant_up, _ = http.reachable(f"{root}/health", timeout=args.timeout)

    if args.json:
        print(
            json.dumps(
                {
                    "restaurantApi": {"url": restaurant, "up": restaurant_up},
                    "services": rows,
                },
                indent=2,
            )
        )
    else:
        print(f"{'restaurant API':<42} {'up' if restaurant_up else 'DOWN':<5} {restaurant}")
        for row in rows:
            state = "up" if row["up"] else "DOWN"
            print(f"{row['name']:<42} {state:<5} {row['url']}")
            if row["up"]:
                print(f"{'':<48} {row.get('running', '')}")
                if row["ready"] is False:
                    print(f"{'':<48} ! cannot build an agent: {row['problem']}")
            else:
                print(f"{'':<48} start it: {row['startHint']}")

    wanted = set(args.require) if args.require else set(services.BY_KEY)
    missing = [
        row for row in rows if row["key"] in wanted and (not row["up"] or row["ready"] is False)
    ]

    if not restaurant_up:
        if not args.json:
            print(
                "\n! The restaurant API is not answering. Every agent here buys "
                "from it, so nothing can complete an errand.\n"
                "  Start it in ../friends-kitchen-backend:\n"
                "    .venv\\Scripts\\python -m uvicorn app.main:app --port 8000"
            )
        return 1

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
