#!/usr/bin/env python3
"""Point every Friends Kitchen agent at a different provider or model.

    python skills/friends-kitchen-llm-configuration/scripts/select_llm.py anthropic
    python .../select_llm.py ollama --model qwen2.5:14b
    python .../select_llm.py llamacpp --set base_url=http://localhost:8080/v1 --set ctx=16384
    python .../select_llm.py --fields ollama

`PUT /api/llm/config` for the selection, `PUT /api/llm/settings` for a
provider's own knobs. The selection is a file all four services read when they
build a model client, so a change here reaches the ordering agent, both A2A
agents and the Foodpanda dispatcher on the next agent each of them builds —
without a restart, and whichever service this was sent to.

Settings are a **merge, not a replace**: only the fields sent are touched, and
a field sent as `null` goes back to its .env default.

Never a credential. Keys live in .env, out of reach of an endpoint that writes
a file; `keyEnv` on a provider says which variable to put one in.

Exit codes: 0 changed, 1 the change was refused, 2 no service is answering.
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
        prog="select_llm",
        description="Change the provider, model or provider settings every agent follows.",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to select. Omit with --set to configure the active one.",
    )
    parser.add_argument("--model", default=None, help="Model id. Omit for the provider's default.")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "One of the provider's own fields. Repeatable. "
            "Use KEY=null to put a field back to its .env default."
        ),
    )
    parser.add_argument(
        "--fields",
        nargs="?",
        const="",
        metavar="PROVIDER",
        help="List the fields a provider declares, and stop.",
    )
    parser.add_argument(
        "--service",
        choices=tuple(services.BY_KEY),
        default=None,
        help="Which service to send this to. Any of them will do — they share one selection.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def pick_service(wanted: str | None) -> services.Service:
    """The service to send this to. Any of the four will do."""
    if wanted:
        return services.get(wanted)
    for service in services.ALL:
        up, _ = http.reachable(f"{service.base_url}{PREFIX}/config", timeout=2.0)
        if up:
            return service
    return services.ORDERING


def parse_values(pairs: list[str]) -> dict:
    """`KEY=VALUE` strings as a settings payload.

    Values are sent as they were typed, except `null`, which is how a field is
    put back to its .env default, and the JSON literals — a port typed as
    `8080` should not arrive as the string `"8080"`.
    """
    values: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set wants KEY=VALUE, got {pair!r}.")
        key, _, raw = pair.partition("=")
        key, raw = key.strip(), raw.strip()
        if raw == "null":
            values[key] = None
            continue
        try:
            values[key] = json.loads(raw)
        except json.JSONDecodeError:
            values[key] = raw
    return values


def main() -> int:
    terminal.utf8()
    args = parse_args()
    service = pick_service(args.service)
    base = f"{service.base_url}{PREFIX}"

    try:
        if args.fields is not None:
            query = f"?provider={args.fields}" if args.fields else ""
            payload = http.get(f"{base}/settings{query}", timeout=args.timeout)
            return _render_fields(payload, args)

        if args.set:
            provider = args.provider or http.get(f"{base}/config", timeout=args.timeout)[
                "active"
            ]["provider"]
            result = http.request(
                "PUT",
                f"{base}/settings",
                {"provider": provider, "values": parse_values(args.set)},
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"{result.get('displayName')} settings now:")
                for key, value in (result.get("values") or {}).items():
                    print(f"  {key} = {value!r}")

        if args.provider is None:
            if not args.set:
                print(
                    "Name a provider to select, or use --set to configure one. "
                    "`show_llm.py --providers` lists them.",
                    file=sys.stderr,
                )
                return 2
            return 0

        selected = http.request(
            "PUT",
            f"{base}/config",
            {"provider": args.provider, "model": args.model},
            timeout=args.timeout,
        )

    except http.ServiceDown as exc:
        print(
            f"No Friends Kitchen service answered.\n  {exc}\n"
            f"  Start one, e.g.: {service.start_hint}",
            file=sys.stderr,
        )
        return 2
    except http.ServiceError as exc:
        # 400 is the ordinary refusal: a provider this deployment cannot use, or
        # a name that is not a provider at all. The sentence says which.
        print(str(exc), file=sys.stderr)
        return 1

    active = selected.get("active") or {}
    if args.json:
        print(json.dumps(active, indent=2))
    else:
        print(f"Every agent now runs on {active.get('provider')}/{active.get('model')}.")
        print(f"  ready : {'yes' if active.get('ready') else 'NO'}")
        if active.get("problem"):
            print(f"  problem: {active['problem']}")
        print(
            "\nTakes effect on the next agent each service builds — no restart, "
            "and the other three follow too."
        )

    return 0 if active.get("ready") else 1


def _render_fields(payload: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    fields = payload.get("fields") or []
    print(f"{payload.get('displayName')} ({payload.get('provider')})")
    if not fields:
        print("  Nothing to configure — this provider declares no fields.")
        return 0

    values = payload.get("values") or {}
    for field in fields:
        key = field.get("key")
        print(f"  --set {key}={values.get(key)!r}")
        print(f"      {field.get('label')} [{field.get('kind')}, default {field.get('default')!r}]")
        if field.get("help"):
            print(f"      {field['help']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
