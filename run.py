"""Send the agent to Friends Kitchen.

    python run.py "Order two burgers and a large drink" --coupon FK-8H2K-9QW1 --limit 2500
    python run.py "Order a cheeseburger meal" --coupon FK-8H2K-9QW1 --mode browser

The instruction is the errand. The coupon and the limit are the money.
"""

from __future__ import annotations

import argparse
import sys

# The model writes em dashes and the menu is priced in rupees, and a Windows
# console defaults to cp1252 — without this the report comes out as mojibake.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from agent import friends_kitchen_api, telemetry  # noqa: E402  (after the stream fix, deliberately)
from agent.config import settings
from agent.wallet import wallet

# Enough colour to tell the agent's own words from the machinery around it.
DIM, BOLD, AMBER, GREEN, RED, RESET = (
    "\033[2m", "\033[1m", "\033[33m", "\033[32m", "\033[31m", "\033[0m",
)


def make_printer():
    """Strands streams the run through a callback; this renders it.

    Tool calls are announced once each — the callback fires per input token as
    the model composes the arguments, so without the `seen` guard a single
    `add_to_cart` prints thirty times.
    """
    seen: set[str] = set()
    state = {"in_text": False}

    def printer(**kwargs) -> None:
        tool_use = kwargs.get("current_tool_use") or {}
        tool_id = tool_use.get("toolUseId")
        if tool_id and tool_id not in seen and tool_use.get("name"):
            seen.add(tool_id)
            if state["in_text"]:
                print()
                state["in_text"] = False
            print(f"{AMBER}  → {tool_use['name']}{RESET}")

        text = kwargs.get("data")
        if text:
            state["in_text"] = True
            print(text, end="", flush=True)

    return printer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="friends-kitchen-agent-backend",
        description="An AI agent that orders food at Friends Kitchen on your behalf.",
    )
    parser.add_argument("instruction", help="What to order, in plain language.")
    parser.add_argument(
        "--coupon", default=None,
        help="The coupon code to spend. Omit to pay cash only.",
    )
    parser.add_argument(
        "--limit", type=float, default=0.0,
        help="Cash the agent may spend beyond the coupon. Default 0 — coupon only.",
    )
    parser.add_argument(
        "--mode", choices=["api", "browser"], default="api",
        help="api: order through the REST API. browser: drive the real website in Chromium.",
    )
    parser.add_argument(
        "--customer", default=None,
        help="Customer id to record against the coupon redemption.",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Tracing, if the operator asked for it. No app to instrument here — a CLI
    # run still produces the agent and httpx spans, which is the whole errand.
    telemetry.setup("ordering-agent-cli")

    wallet.coupon_code = args.coupon
    wallet.spend_limit = args.limit
    wallet.customer_id = args.customer

    if not args.coupon and args.limit <= 0:
        print(
            f"{RED}The agent has neither a coupon nor a cash limit, so it cannot buy "
            f"anything.{RESET}\nPass --coupon, --limit, or both.",
            file=sys.stderr,
        )
        return 2

    if not friends_kitchen_api.health():
        print(
            f"{RED}Friends Kitchen is not answering at {settings.api_base}.{RESET}\n"
            "Start the backend first:\n"
            "  cd ../friends-kitchen-backend && .venv\\Scripts\\python -m uvicorn app.main:app --port 8000",
            file=sys.stderr,
        )
        return 1

    # Imported here so a missing API key is reported after the cheaper checks.
    from agent.friends_kitchen_agent import MissingApiKey, build_agent

    try:
        agent = build_agent(
            wallet,
            mode=args.mode,
            callback_handler=None if args.quiet else make_printer(),
        )
    except MissingApiKey as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 2

    print(f"{BOLD}Friends Kitchen — ordering agent{RESET}")
    print(f"{DIM}  errand : {args.instruction}{RESET}")
    print(f"{DIM}  wallet : {args.coupon or 'no coupon'} + {args.limit:.0f} cash{RESET}")
    print(f"{DIM}  mode   : {args.mode} ({settings.model_id}){RESET}\n")

    try:
        result = agent(args.instruction)
    except KeyboardInterrupt:
        print(f"\n{RED}Stopped. Any order already paid for still stands.{RESET}")
        return 130
    except Exception as exc:
        print(f"\n{RED}The agent stopped: {exc}{RESET}", file=sys.stderr)
        return 1
    finally:
        if args.mode == "browser":
            from agent.browser.friends_kitchen_driver import browser

            if browser.started:
                input(f"\n{DIM}Press Enter to close the browser…{RESET}")
                browser.close()

    if args.quiet:
        print(result)

    spend = wallet.display()
    print(f"\n{GREEN}{BOLD}Done.{RESET}")
    print(
        f"{DIM}  coupon redeemed : {spend['couponRedeemed']}\n"
        f"  cash spent      : {spend['cashSpent']} of {spend['cashLimit']}{RESET}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
