---
name: order-math
description: Work out the arithmetic of a Friends Kitchen order — a cart subtotal from its lines, how much of a bill a coupon covers, whether the rest fits inside the cash limit, and what is left of that limit after paying. Use whenever an errand needs a figure that is not printed on a tool result — before deciding whether to add an item, before applying a coupon, or before authorizing a payment that might be refused.
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+ and nothing else — standard library only, no network, no services. Runs anywhere the agent runs.
metadata:
  author: friends-kitchen
  agents: "ordering"
  version: "1.0"
  unit: "Pakistani rupees, whole amounts written as Rs 1,093"
allowed-tools: Bash(python:*) Read
---

# The arithmetic of an order

Every figure in this errand is in rupees and is handed to you already written
that way — `Rs 1,093`. What the tools do not do is combine them. Whether a
coupon covers a bill, whether the remainder fits your cash limit, what a cart
of four lines comes to: those are yours to work out, and working them out in
your head is where a run goes wrong.

So do not. Run the script. Its output is the answer, and it is the same answer
every time.

## When to use this

- **Before adding an item** you are not sure you can afford — `cart`.
- **Before applying a coupon**, to know what it will and will not cover, and
  whether what is left fits your cash limit — `coverage`.
- **Before authorizing a payment** that might be refused — `coverage` tells you
  the shortfall in advance, and a refused payment is a wasted step.
- **After paying**, for what is left of the limit — `budget`.

## Steps

1. Run the script. It is the definition of the correct result:

   ```bash
   python scripts/order_math.py cart --item "Big Mac:530:2" --item "Fries:180:1"
   python scripts/order_math.py coverage --total "Rs 1,240" --coupon "Rs 500" --cash-limit "Rs 800"
   python scripts/order_math.py budget --limit "Rs 2,400" --spent "Rs 740"
   ```

2. Report what it returned. Do not recompute it, and do not adjust it.

Every amount may be written either way — `Rs 1,240` or `1240`. Every amount it
prints back is already in the `Rs 1,240` form the customer should see, so use
those strings exactly as given.

## What it answers

| Command | Answers |
| --- | --- |
| `cart` | Line totals, the subtotal, and how many items are in it |
| `coverage` | What the coupon covers, what is left to pay, whether the cash limit covers that, and the shortfall if it does not |
| `budget` | What is left of the cash limit, and how much of it is spent |

`coverage` is the one that prevents a refused payment: it reports
`withinCashLimit` and, when that is false, the exact `shortfall`. A payment the
wallet is going to refuse is a step you can skip by asking first.

## Notes

`references/MONEY.md` has the rounding and formatting rules the script follows,
and the one mistake this skill exists to stop — reading `Rs 1,093` as a minor
unit. Read it if a figure ever looks a hundred times too large or too small.

The script never talks to the restaurant. It has no idea what is really in your
cart; it computes what you hand it. The cash limit is still enforced by
`authorize_payment`, which is the only thing that can actually refuse a charge.
