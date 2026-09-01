"""The arithmetic of a Friends Kitchen order, so that every run gets it right.

Standard library only, and no network: a script that first needs a package
installed is not a deterministic script, and this one is asked for an answer
mid-errand. It never reads the restaurant either — it computes what it is
handed, which is what makes it checkable.

Prints JSON on stdout. The runtime parses that and hands the agent a structured
result instead of prose it has to read back.
"""

from __future__ import annotations

import argparse
import json
import sys

#: What a menu price may arrive wrapped in. Every figure in this system is a
#: whole-rupee amount already written as `Rs 1,093`, and a bare `1093` means the
#: same thing — never paisa. See references/MONEY.md.
_STRIP = " 	,  "


def parse_money(raw: str | float | int) -> float:
    """`Rs 1,093` or `1093` — both mean one thousand and ninety-three rupees."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    for prefix in ("Rs.", "Rs", "PKR", "₨"):
        if text.upper().startswith(prefix.upper()):
            text = text[len(prefix) :]
            break
    text = "".join(ch for ch in text if ch not in _STRIP)
    if not text:
        raise ValueError("an amount was expected and nothing was given")
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{raw!r} is not an amount") from None


def rupees(amount: float) -> str:
    """Money as Friends Kitchen writes it. The same rule as agent/wallet.py.

    Duplicated on purpose rather than imported: this script has to run under
    whatever Python is to hand, and reaching into the application package to
    format a number would make it need the repository importable to print a
    total. The rule is three lines and it is written down in references/.
    """
    text = f"Rs {amount:,.2f}"
    return text[:-3] if text.endswith(".00") else text


def _line(spec: str) -> dict:
    """`Big Mac:530:2` — name, unit price, quantity. Quantity defaults to 1."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"{spec!r} is not a line — write it as 'Name:price[:quantity]'")
    name = parts[0].strip() or "An item"
    price = parse_money(parts[1])
    quantity = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 1
    if quantity < 1:
        raise ValueError(f"{spec!r} has a quantity of {quantity} — it must be at least 1")
    return {
        "name": name,
        "unitPrice": rupees(price),
        "quantity": quantity,
        "lineTotal": rupees(price * quantity),
        "_total": price * quantity,
    }


def cart(args: argparse.Namespace) -> dict:
    """What a set of lines comes to."""
    lines = [_line(item) for item in args.item]
    subtotal = sum(line.pop("_total") for line in lines)
    return {
        "lines": lines,
        "itemCount": sum(line["quantity"] for line in lines),
        "subtotal": rupees(subtotal),
    }


def coverage(args: argparse.Namespace) -> dict:
    """How much of a bill a coupon covers, and whether the rest is affordable.

    The question this exists to answer is the one that gets a payment refused:
    a coupon worth less than the bill leaves a remainder, and the remainder has
    to fit inside the cash limit. Asking here costs nothing; finding out at
    `authorize_payment` costs a step and a retry.
    """
    total = parse_money(args.total)
    coupon = parse_money(args.coupon) if args.coupon is not None else 0.0
    covered = min(coupon, total)
    due = total - covered

    answer = {
        "total": rupees(total),
        "couponValue": rupees(coupon),
        "couponCovers": rupees(covered),
        "couponLeftOver": rupees(coupon - covered),
        "amountDue": rupees(due),
        "fullyCovered": due <= 0,
    }

    if args.cash_limit is not None:
        limit = parse_money(args.cash_limit)
        answer["cashLimit"] = rupees(limit)
        answer["withinCashLimit"] = due <= limit
        answer["shortfall"] = rupees(max(0.0, due - limit))
    return answer


def budget(args: argparse.Namespace) -> dict:
    """What is left of the cash limit."""
    limit = parse_money(args.limit)
    spent = parse_money(args.spent) if args.spent is not None else 0.0
    remaining = limit - spent
    return {
        "cashLimit": rupees(limit),
        "cashSpent": rupees(spent),
        "cashRemaining": rupees(remaining),
        "spentPercent": round((spent / limit) * 100) if limit > 0 else 0,
        "overLimit": remaining < 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="The arithmetic of a Friends Kitchen order.")
    commands = parser.add_subparsers(dest="command", required=True)

    one = commands.add_parser("cart", help="line totals and a subtotal")
    one.add_argument(
        "--item",
        action="append",
        default=[],
        required=True,
        metavar="NAME:PRICE[:QTY]",
        help="one cart line; repeat for each",
    )
    one.set_defaults(run=cart)

    two = commands.add_parser("coverage", help="what a coupon covers, and what is left to pay")
    two.add_argument("--total", required=True, help="the bill, e.g. 'Rs 1,240'")
    two.add_argument("--coupon", default=None, help="the coupon's value, e.g. 'Rs 500'")
    two.add_argument("--cash-limit", default=None, help="what you may spend in cash")
    two.set_defaults(run=coverage)

    three = commands.add_parser("budget", help="what is left of the cash limit")
    three.add_argument("--limit", required=True, help="the cash limit for this errand")
    three.add_argument("--spent", default=None, help="what has been charged so far")
    three.set_defaults(run=budget)

    args = parser.parse_args()
    try:
        answer = args.run(args)
    except ValueError as exc:
        print(f"order-math: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
