---
name: errand-report
description: Write the closing summary an errand ends with — what was ordered, the order number, what the coupon covered, what was charged, where the food went, and anything that did not go to plan. Use as the last step of every errand, once payment has been authorized or once it is clear the errand is ending without a paid order.
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+ and nothing else — standard library only, no network, no services. Runs anywhere the agent runs.
metadata:
  author: friends-kitchen
  agents: "ordering"
  version: "1.0"
  shape: "a few plain sentences, no headers and no tables"
allowed-tools: Bash(python:*) Read
---

# The report an errand ends with

The last thing you say is the thing the customer actually reads, and two runs
of the same errand should not read like two different people wrote them. So
the wording is fixed and lives in `assets/report.template.md`; you supply the
figures and the script fills it in.

The result is a few plain sentences. No headers, no tables, no recap of every
tool call — the control panel already shows the steps beside your report.

## Steps

1. Gather what the run actually produced: the items, the order number, the
   coupon and cash figures from the payment result, where it was delivered if
   it was a delivery, and anything that did not go to plan.

2. Run the script, handing it those values as JSON on stdin:

   ```bash
   python scripts/render_report.py
   ```

   ```json
   {
     "ordered": ["Big Mac × 2", "Fries × 1"],
     "orderNumber": "FK-10482",
     "couponCovered": "Rs 500",
     "cashSpent": "Rs 740",
     "cashRemaining": "Rs 1,660",
     "cashLimit": "Rs 2,400",
     "deliveredTo": "Flat 3B, Gulberg — handed to Foodpanda",
     "exceptions": ["No Coke Zero on the menu — ordered regular Coke instead."]
   }
   ```

   Every field is optional except `ordered`. Leave a field out and the sentence
   that needed it is left out too — that is the point of rendering rather than
   writing: a missing figure disappears instead of becoming "Rs 0" or "unknown".

3. Report `report` from the result, unedited. It is your final answer.

## What each field means

| Field | Put in it |
| --- | --- |
| `ordered` | The lines as bought, one per entry: `"Big Mac × 2"` |
| `orderNumber` | The restaurant's number for the order, from `place_order` or `get_order` |
| `couponCovered` | What the coupon took off, from the payment result |
| `cashSpent` | What was actually charged |
| `cashRemaining`, `cashLimit` | What is left, and of what. Both or neither |
| `deliveredTo` | Where it is going and who has it — only for a delivery, and only ever "handed to", never "delivered", unless the status really is `delivered` |
| `exceptions` | Substitutions, refusals, a failed handover. One sentence each |
| `failed` | `true` when the errand is ending without a paid order. Say why in `exceptions` |

## Notes

Read `assets/report.template.md` if you want to see the fixed wording before
you fill it. Do not rewrite it — changing the template in your own answer is
the thing this skill exists to prevent.

Report faithfully. If payment was refused, if an item was substituted, if the
handover failed, it goes in `exceptions` and the customer reads it. An errand
that half worked is not reported as an errand that worked.
