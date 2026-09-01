---
name: friends-kitchen-delivery-dispatch
description: Track and advance a food delivery after a paid order has been handed to a courier agent — read where a job has got to, ask for a rider, ask for the order to be brought out, and tell an accepted job apart from a delivered one. Use when asked about delivery, dispatch, couriers, riders, the delivery board, job status, handover, the Foodpanda demonstration dispatcher on port 8103, or the in-house courier on port 8102 (foodpanda_server.py, delivery_server.py, agent/foodpanda/, agent/delivery/).
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+ and whichever courier service DELIVERY_PROVIDER selects — the in-house courier on port 8102, or the Foodpanda demonstration agent on port 8103, which also needs usable model credentials for its dispatcher. Talks to localhost only.
metadata:
  author: friends-kitchen
  agents: "operator"
  version: "1.0"
  services: "delivery-8102, foodpanda-8103"
  terminal-success: "delivered"
allowed-tools: Bash(python:*) Read
---

# Delivery, after the order is paid for

A paid take-away order is handed to a courier agent by `authorize_payment` — the
ordering agent does not arrange delivery and has no step for it. What this skill
covers is everything after that handover: reading where a job is, and opening
the two gates a customer is meant to open.

It adds no dispatch logic. The decision to take a job, the rider, and the ride
are the courier agent's, in `agent/foodpanda/` and `delivery_server.py`.

## The one thing to get right

**A handover starts a delivery. It does not complete one.**

`requested` and `delivered` are one field apart, and a caller that reads
acceptance as arrival tells a customer their food is there when it is not.

```
requested → accepted → courier_assigned → picked_up → in_transit → delivered
                ▲                              ▲
          waits for a rider             waits for the customer
          to be asked for               to ask for it
```

`delivered` is the only status that means the food is with the customer. It is
reached only by the dispatcher's `deliver_to_customer` returning successfully —
never by accepting, never by assigning, never by collecting. `rejected`,
`failed` and `cancelled` are the other terminal states. Full table in
`references/LIFECYCLE.md`.

## Three providers, one wire format

`DELIVERY_PROVIDER` chooses one. They speak the same job format, which is what
lets a deployment swap one for another with a variable rather than a code
change — so the ordering agent never learns which one carried the order.

| `DELIVERY_PROVIDER` | Port | What it is |
| --- | --- | --- |
| `internal` | 8102 | Friends Kitchen's own courier. No credentials, no model. The default, and the fallback for an unknown name. |
| `mock_foodpanda` | 8103 | A real AI dispatcher that decides, refuses and reports. The ride is simulated; no real courier is called. |
| `foodpanda` | — | Foodpanda's actual courier API. Needs `FOODPANDA_API_KEY`, and has no mock path: point it at the real service and it dispatches for real. |

This skill's scripts cover the two local ones. Only 8103 has gates and an agent
— on 8102 a job runs itself from request to doorstep.

## Reading a job

```
python skills/friends-kitchen-delivery-dispatch/scripts/track_delivery.py
python .../track_delivery.py --job fpj_9f2a41c0e1 --follow
```

With no arguments it lists every job the courier is carrying or has carried,
newest first, with the gate each one is waiting at. `--follow` streams a single
job's events until it reaches a terminal state.

## Opening a gate

```
python .../advance_delivery.py fpj_9f2a41c0e1 --rider
python .../advance_delivery.py fpj_9f2a41c0e1 --deliver
```

These are **requests, not commands**. `--rider` opens the gate the dispatcher is
already waiting at, and the dispatcher is still the one that assigns anybody;
`--deliver` is the customer asking for the collected food to be brought out.

A job legitimately sits at `accepted` until somebody asks. **That pause is the
normal case, not a stall** — do not report a waiting job as a broken one. The
`awaiting` field on the job says which gate, if any, is open to be asked:
`"rider"`, `"delivery"`, or `null`.

Both refuse with 409 when the job is not sitting at that step, which is what a
console left open on a finished delivery would ask for.

`MOCK_FOODPANDA_MANUAL_STEPS=false` removes the gates entirely and a job runs
from request to doorstep on its own. An order created with `whereItGoes: true`
keeps the first gate and skips the second: the customer already said yes when
they ordered, and asking twice is not a second safeguard — it is a delivery that
stalls because nobody expected to be asked again.

## Refusals that are not failures

* **Unpaid order → 409.** The restaurant will not release food nobody has
  bought, so a rider sent for it is a rider sent for nothing.
* **No items → 422.** There is nothing to collect.
* **Outside the radius → the dispatcher rejects it**, in a sentence. That is
  the one real judgement it makes, and refusing now lets the restaurant arrange
  something else instead of leaving a customer waiting on food that never
  arrives.

The first two are checked at the edge rather than left to the agent, because
they are not judgement calls.

## When a handover fails

The order is still bought and paid for. Both halves have to be reported: what
was ordered and paid, and that it has no rider. Do not place a second order.
`arrange_delivery` on the ordering agent exists as a retry for exactly this
case — it is not the normal route, and retrying an unchanged refusal fails the
same way.

## Files

* `scripts/track_delivery.py` — list jobs, or follow one to its outcome.
* `scripts/advance_delivery.py` — ask for a rider, or ask for the delivery.
* `references/LIFECYCLE.md` — states, transitions, gates and what each means.
* `references/HTTP-API.md` — the exact contracts on 8102 and 8103.
* `assets/job-request.example.json` — a filled-in delivery job request.
