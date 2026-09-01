#!/usr/bin/env python3
"""Where the deliveries have got to.

    python skills/friends-kitchen-delivery-dispatch/scripts/track_delivery.py
    python .../track_delivery.py --job fpj_9f2a41c0e1
    python .../track_delivery.py --job fpj_9f2a41c0e1 --follow
    python .../track_delivery.py --service delivery

Reads the courier's own job routes. It moves nothing: opening a gate is
`advance_delivery.py`, and everything else about a delivery is the dispatcher's.

`--follow` streams one job's events until it is terminal, which only the
Foodpanda agent on 8103 offers — the in-house courier on 8102 has no per-job
stream, so `--follow` there polls instead.

Exit codes: 0 read it, 1 the job ended in something other than `delivered`,
2 the service is not answering or there is no such job.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

#: The status that actually means the food is with the customer. Every other
#: one — `accepted` and `courier_assigned` most of all — does not.
DELIVERED = "delivered"

TERMINAL = frozenset({DELIVERED, "rejected", "failed", "cancelled"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="track_delivery",
        description="List delivery jobs, or follow one to its outcome.",
    )
    parser.add_argument(
        "--service",
        choices=("foodpanda", "delivery"),
        default="foodpanda",
        help="Which courier to read. Default: the Foodpanda demonstration agent on 8103.",
    )
    parser.add_argument("--job", default=None, help="One job id. Default: list them all.")
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Watch one job until it reaches a terminal state.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args()


def describe(job: dict) -> str:
    """One job as a line, with the gate it is waiting at named."""
    status = job.get("status", "?")
    mark = "*" if status == DELIVERED else " "
    courier = (job.get("courier") or {}).get("name") or "no rider yet"
    waiting = job.get("awaiting")
    gate = {
        "rider": "  <- waiting to be asked for a rider",
        "delivery": "  <- waiting to be asked to deliver",
    }.get(waiting, "")
    return (
        f" {mark} {job.get('jobId'):<18} {status:<18} order {job.get('orderNumber')}"
        f"  {courier}{gate}"
    )


def follow_stream(base: str, prefix: str, job_id: str, timeout: float) -> None:
    """Print the job's own event stream until it stops."""
    for event in http.stream(f"{base}{prefix}/jobs/{job_id}/events", timeout=timeout):
        kind = event.get("type")
        if kind == "status":
            print(f"[{event.get('status')}] {event.get('message') or ''}".rstrip())
        elif kind == "message":
            print(f"  dispatcher: {(event.get('text') or '').strip()}")
        elif kind == "tool":
            print(f"  -> {event.get('name')}")
        elif kind == "error":
            print(f"  ! {event.get('message')}", file=sys.stderr)


def follow_polling(base: str, prefix: str, job_id: str, timeout: float) -> dict:
    """The in-house courier has no per-job stream, so ask it every second."""
    deadline = time.monotonic() + timeout
    last = None
    job: dict = {}
    while time.monotonic() < deadline:
        job = http.get(f"{base}{prefix}/jobs/{job_id}", timeout=15.0)
        status = job.get("status")
        if status != last:
            print(f"[{status}] {job.get('message') or ''}".rstrip())
            last = status
        if status in TERMINAL:
            return job
        time.sleep(1.0)
    print("Gave up waiting; the job is still running.", file=sys.stderr)
    return job


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.get(args.service)
    base, prefix = service.base_url, service.prefix

    try:
        if args.job is None:
            payload = http.get(f"{base}{prefix}/jobs", timeout=20.0)
            jobs = list(payload.get("items") or [])
            if args.json:
                print(json.dumps(jobs, indent=2))
                return 0
            if not jobs:
                print(f"{service.name} is carrying nothing and has carried nothing.")
                return 0
            for job in jobs:
                print(describe(job))
            print()
            print(f"{sum(1 for j in jobs if j.get('status') == DELIVERED)} delivered "
                  f"of {len(jobs)}.")
            return 0

        if args.follow:
            if service.key == "foodpanda":
                follow_stream(base, prefix, args.job, args.timeout)
                job = http.get(f"{base}{prefix}/jobs/{args.job}", timeout=20.0)
            else:
                job = follow_polling(base, prefix, args.job, args.timeout)
        else:
            job = http.get(f"{base}{prefix}/jobs/{args.job}", timeout=20.0)

    except http.ServiceDown as exc:
        print(f"{exc}\nStart it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(job, indent=2))
    else:
        print()
        print(f"job {job.get('jobId')} — order {job.get('orderNumber')}")
        print(f"  status    : {job.get('status')}")
        print(f"  delivered : {'yes' if job.get('delivered') else 'no'}")
        if job.get("awaiting"):
            print(f"  awaiting  : {job['awaiting']} — ask for it with advance_delivery.py")
        if job.get("courier"):
            print(f"  courier   : {(job.get('courier') or {}).get('name')}")
        if job.get("etaMinutes") is not None:
            print(f"  eta       : {job['etaMinutes']} min")
        if job.get("fee"):
            print(f"  fee       : {job['fee']}")
        if job.get("decision"):
            print(f"  decision  : {job['decision']}")
        if job.get("message"):
            print(f"  message   : {job['message']}")

    # A job that is still moving is not a failure, and neither is one waiting at
    # a gate — that pause is the normal case. Only a job that has *finished* as
    # something other than `delivered` is worth a non-zero exit.
    status = job.get("status")
    return 1 if status in TERMINAL and status != DELIVERED else 0


if __name__ == "__main__":
    raise SystemExit(main())
