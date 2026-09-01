#!/usr/bin/env python3
"""What the agents are running on, and whether it works.

    python skills/friends-kitchen-llm-configuration/scripts/show_llm.py
    python .../show_llm.py --providers
    python .../show_llm.py --models ollama
    python .../show_llm.py --health
    python .../show_llm.py --test

Reads the `/api/llm` endpoints, which are mounted identically on all four
services — so `--service` only decides which process to ask, never what the
answer is. Nothing here is written; `select_llm.py` is the one that changes
anything.

`--health` asks the provider about itself. `--test` runs a real generation
through exactly the client an agent would get, which is the only check that
proves the whole path.

Exit codes: 0 the answer was yes (or nothing was asked), 1 the provider or
model is not usable, 2 no service is answering.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services, terminal  # noqa: E402

PREFIX = "/api/llm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="show_llm",
        description="Read the shared LLM selection every Friends Kitchen agent follows.",
    )
    parser.add_argument(
        "--service",
        choices=tuple(services.BY_KEY),
        default=None,
        help="Which service to ask. Default: the first one that answers.",
    )
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--providers", action="store_true", help="Everything selectable.")
    what.add_argument(
        "--models",
        nargs="?",
        const="",
        metavar="PROVIDER",
        help="What one provider can run. Default: the active provider.",
    )
    what.add_argument("--settings", nargs="?", const="", metavar="PROVIDER",
                      help="One provider's own configurable fields and their values.")
    what.add_argument("--health", action="store_true", help="Could it serve a run right now?")
    what.add_argument("--test", action="store_true", help="Actually run a generation.")

    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def pick_service(wanted: str | None) -> services.Service:
    """The service to ask.

    Any of the four will do — they read one selection — so with none named this
    takes the first that answers rather than making the caller find out which
    happens to be up.
    """
    if wanted:
        return services.get(wanted)
    for service in services.ALL:
        up, _ = http.reachable(f"{service.base_url}{PREFIX}/config", timeout=2.0)
        if up:
            return service
    return services.ORDERING


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = pick_service(args.service)
    base = f"{service.base_url}{PREFIX}"

    try:
        if args.providers:
            payload = http.get(f"{base}/providers", timeout=args.timeout)
            return _render_providers(payload, args)

        if args.models is not None:
            query = f"?provider={args.models}" if args.models else ""
            payload = http.get(f"{base}/models{query}", timeout=args.timeout)
            return _render_models(payload, args)

        if args.settings is not None:
            query = f"?provider={args.settings}" if args.settings else ""
            payload = http.get(f"{base}/settings{query}", timeout=args.timeout)
            return _render_settings(payload, args)

        if args.health:
            payload = http.get(f"{base}/health", timeout=args.timeout)
            return _render_health(payload, args)

        if args.test:
            payload = http.post(f"{base}/test", {}, timeout=args.timeout)
            return _render_test(payload, args)

        payload = http.get(f"{base}/config", timeout=args.timeout)
        return _render_config(payload, args, service)

    except http.ServiceDown as exc:
        print(
            f"No Friends Kitchen service answered.\n  {exc}\n"
            f"  Start one, e.g.: {service.start_hint}",
            file=sys.stderr,
        )
        return 2
    except http.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _render_config(payload: dict, args: argparse.Namespace, service: services.Service) -> int:
    active = payload.get("active") or {}
    if args.json:
        print(json.dumps(active, indent=2))
        return 0 if active.get("ready") else 1

    print(f"asked {service.name} at {service.base_url}")
    print(f"  provider : {active.get('provider')} ({active.get('displayName')}, "
          f"{active.get('kind')})")
    print(f"  model    : {active.get('model')}")
    print(f"  chosen by: {active.get('source')}")
    print(f"  ready    : {'yes' if active.get('ready') else 'NO'}")
    if active.get("problem"):
        print(f"  problem  : {active['problem']}")
    print("\nAll four services read this one selection.")
    return 0 if active.get("ready") else 1


def _render_providers(payload: dict, args: argparse.Namespace) -> int:
    items = payload.get("items") or []
    if args.json:
        print(json.dumps(items, indent=2))
        return 0

    active = (payload.get("active") or {}).get("provider")
    for item in items:
        mark = "*" if item.get("name") == active else " "
        state = "ready" if item.get("configured") else "not configured"
        print(f" {mark} {str(item.get('name')):<14} {item.get('displayName'):<26} {state}")
        if not item.get("configured") and item.get("problem"):
            print(f"        {item['problem']}")
    print("\n* = the one every agent is running on.")
    return 0


def _render_models(payload: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
        return 1 if payload.get("problem") else 0

    print(f"{payload.get('displayName')} ({payload.get('provider')})")
    if payload.get("problem"):
        # 200 with the problem in it, deliberately: "the local runtime is not
        # running" is the commonest state this is asked in.
        print(f"  ! {payload['problem']}")
        return 1
    for item in payload.get("items") or []:
        name = item.get("id") or item.get("name") if isinstance(item, dict) else item
        print(f"  {name}")
    return 0


def _render_settings(payload: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    fields = payload.get("fields") or []
    print(f"{payload.get('displayName')} ({payload.get('provider')})")
    if not fields:
        print("  Nothing to configure here — a cloud vendor's address is not "
              "this deployment's business.")
        return 0
    values = payload.get("values") or {}
    for field in fields:
        key = field.get("key")
        advanced = "  (advanced)" if field.get("advanced") else ""
        print(f"  {key} = {values.get(key)!r}{advanced}")
        print(f"      {field.get('label')} [{field.get('kind')}, default {field.get('default')!r}]")
        if field.get("help"):
            print(f"      {field['help']}")
    return 0


def _render_health(payload: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("ok") else 1

    print(f"{payload.get('displayName')} / {payload.get('model')} — "
          f"{'ok' if payload.get('ok') else 'NOT USABLE'}")
    for check in payload.get("checks") or []:
        mark = "ok  " if check.get("ok") else "FAIL"
        print(f"  {mark} {check.get('label')}")
        if check.get("detail"):
            print(f"       {check['detail']}")
    if payload.get("problem"):
        print(f"\n  {payload['problem']}")
    return 0 if payload.get("ok") else 1


def _render_test(payload: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("ok") else 1

    print(f"{payload.get('provider')} / {payload.get('model')}")
    for check in payload.get("checks") or []:
        mark = "ok  " if check.get("ok") else "FAIL"
        print(f"  {mark} {check.get('label')}")
        if check.get("detail"):
            print(f"       {check['detail']}")

    if payload.get("ok"):
        print(f"\nThe model answered: {payload.get('reply')!r}")
        return 0

    print(f"\nThe model did not answer.\n  {payload.get('problem')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
