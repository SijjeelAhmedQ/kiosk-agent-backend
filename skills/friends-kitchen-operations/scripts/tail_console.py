#!/usr/bin/env python3
"""One service's own log — the scrollback, or the live stream.

    python skills/friends-kitchen-operations/scripts/tail_console.py --service ordering
    python .../tail_console.py --service foodpanda --follow
    python .../tail_console.py --service a2a --limit 50 --level warn

Every service publishes the same two endpoints under its own prefix —
`{prefix}/console` for what it still holds and `{prefix}/console/events` to
follow it live. They are identical in all four because the only thing that
differs is the namespace, and the operations dashboard reads exactly this.

Read this when a run failed and the trace stopped before saying why. There are
also plain files under `var/` for whatever was written before the logging
handler was installed, or after a process died.

Exit codes: 0 read it, 2 the service is not answering.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

#: Quietest first, so `--level warn` means "warn and worse".
LEVELS = ("debug", "info", "warn", "error")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tail_console",
        description="Read one Friends Kitchen service's console log.",
    )
    parser.add_argument(
        "--service",
        choices=tuple(services.BY_KEY),
        default="ordering",
        help="Which service's log. Default: the ordering agent on 8100.",
    )
    parser.add_argument("--limit", type=int, default=100, help="How many lines of scrollback.")
    parser.add_argument(
        "--level",
        choices=LEVELS,
        default=None,
        help="Only this level and worse.",
    )
    parser.add_argument("--ref", default=None, help="Only lines belonging to one run or job id.")
    parser.add_argument("--follow", action="store_true", help="Keep reading as lines arrive.")
    parser.add_argument("--json", action="store_true", help="One JSON object per line.")
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args()


def keep(line: dict, args: argparse.Namespace) -> bool:
    # The stream sends a keep-alive every so often, carrying nothing but a
    # timestamp. Only a real line has a sequence number.
    if "seq" not in line:
        return False
    if args.ref and line.get("ref") != args.ref:
        return False
    if args.level:
        level = line.get("level", "info")
        if level not in LEVELS or LEVELS.index(level) < LEVELS.index(args.level):
            return False
    return True


def render(line: dict, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(line))
        return

    stamp = time.strftime("%H:%M:%S", time.localtime((line.get("at") or 0) / 1000))
    level = str(line.get("level", "info")).upper()
    agent = line.get("agent") or ""
    ref = f" [{line['ref']}]" if line.get("ref") else ""
    tool = f" {line['tool']}" if line.get("tool") else ""
    print(f"{stamp} {level:<5} {agent:<12}{ref}{tool} {line.get('text', '')}")


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = services.get(args.service)
    console = f"{service.base_url}{service.prefix}/console"

    try:
        backlog = http.get(f"{console}?limit={max(1, args.limit)}", timeout=20.0)
    except http.ServiceDown as exc:
        print(f"{exc}\nStart it with: {service.start_hint}", file=sys.stderr)
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.json:
        print(f"--- {backlog.get('service', service.key)} console, "
              f"{len(backlog.get('items') or [])} lines held ---")

    seq = backlog.get("seq", 0)
    for line in backlog.get("items") or []:
        if keep(line, args):
            render(line, args)

    if not args.follow:
        return 0

    try:
        # `after` picks up exactly where the backlog stopped, so nothing is
        # printed twice and nothing emitted in between is lost.
        for line in http.stream(f"{console}/events?after={seq}", timeout=args.timeout):
            if isinstance(line, dict) and keep(line, args):
                render(line, args)
    except KeyboardInterrupt:
        return 0
    except (http.ServiceDown, http.ServiceError) as exc:
        print(f"The stream stopped: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
