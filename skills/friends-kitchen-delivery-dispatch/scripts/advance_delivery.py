#!/usr/bin/env python3
"""Ask for the next step of a delivery that is waiting to be asked.

    python skills/friends-kitchen-delivery-dispatch/scripts/advance_delivery.py JOB --rider
    python .../advance_delivery.py JOB --deliver
    python .../advance_delivery.py JOB --cancel

These are the customer's half of a delivery, and they are **requests, not
commands**. `--rider` opens the gate the dispatcher is already waiting at — the
dispatcher is still the one that assigns anybody. `--deliver` is the customer
asking for food the rider is holding at the restaurant to be brought out.

Both return the job as it stands the instant the request lands, which is still
the status it was waiting in. The next status arrives on the job's stream a
moment later: follow it with `track_delivery.py --follow`.

Only the Foodpanda demonstration agent on 8103 has gates. On the in-house
courier a job runs itself, and there is nothing here to ask for.

Exit codes: 0 the gate opened, 1 the job was not sitting at that step (409),
2 the service is not answering or there is no such job.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

#: The gate each action opens, and the route that opens it.
ACTIONS = {
    "rider": ("find-rider", "a rider to be found"),
    "deliver": ("deliver", "the order to be brought out"),
    "cancel": ("cancel", "the delivery to be called off"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="advance_delivery",
        description="Ask a waiting delivery job for its next step.",
    )
    parser.add_argument("job", help="The job id, e.g. fpj_9f2a41c0e1.")

    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--rider",
        action="store_const",
        const="rider",
        dest="action",
        help="Ask for a rider. Opens the gate the dispatcher waits at after accepting.",
    )
    what.add_argument(
        "--deliver",
        action="store_const",
        const="deliver",
        dest="action",
        help="Ask for the collected order to be brought out. The step that ends in `delivered`.",
    )
    what.add_argument(
        "--cancel",
        action="store_const",
        const="cancel",
        dest="action",
        help="Call the delivery off. Refused once it has been delivered.",
    )

    parser.add_argument(
        "--service",
        choices=("foodpanda", "delivery"),
        default="foodpanda",
        help="Which courier. Only the Foodpanda agent on 8103 has --rider and --deliver.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.get(args.service)
    route, wanted = ACTIONS[args.action]

    if service.key == "delivery" and args.action != "cancel":
        print(
            "The in-house courier on 8102 has no gates — a job there runs itself "
            "from request to doorstep. Only --cancel applies. Use --service "
            "foodpanda for the agent that waits to be asked.",
            file=sys.stderr,
        )
        return 2

    url = f"{service.base_url}{service.prefix}/jobs/{args.job}/{route}"

    try:
        job = http.post(url, timeout=args.timeout)
    except http.ServiceDown as exc:
        print(f"{exc}\nStart it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        message = str(exc)
        if " -> 404" in message:
            print(f"No such delivery job: {args.job}", file=sys.stderr)
            return 2
        # 409 is the ordinary refusal here: the job is not sitting at that step.
        # A console left open on a finished delivery asks for exactly this.
        print(message, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(job, indent=2))
        return 0

    print(f"Asked for {wanted}.")
    print(f"  job     : {job.get('jobId')} (order {job.get('orderNumber')})")
    print(f"  status  : {job.get('status')}")
    if job.get("awaiting"):
        print(f"  awaiting: {job['awaiting']}")
    if job.get("courier"):
        print(f"  courier : {(job.get('courier') or {}).get('name')}")
    if job.get("message"):
        print(f"  message : {job['message']}")

    if args.action != "cancel":
        print(
            "\nThat status is the one it was waiting in — the step happens next "
            "and lands on the stream.\n"
            f"  Follow it: track_delivery.py --job {job.get('jobId')} --follow"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
