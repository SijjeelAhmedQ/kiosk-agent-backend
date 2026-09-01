---
name: friends-kitchen-a2a-negotiation
description: Run and read an agent-to-agent order — a buyer agent carrying a wallet negotiates in plain language with the restaurant's own merchant agent, which quotes, redeems the coupon and takes payment. Use when asked about A2A, agent-to-agent ordering, the buyer or merchant agent, the negotiation transcript, quote and receipt artifacts, agent cards and the /.well-known/agent-card.json discovery endpoint, or the ordering desk service on port 8101 (a2a_server.py, agent/a2a/).
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+, the Friends Kitchen REST API on port 8000, and the A2A ordering desk on port 8101. Two agents run per negotiation, so both sides need usable model credentials. Talks to localhost only.
metadata:
  author: friends-kitchen
  agents: "operator"
  version: "1.0"
  service: a2a-ordering-desk
  port: "8101"
  protocol: "A2A 0.3, http+json subset"
allowed-tools: Bash(python:*) Read
---

# Agent-to-agent ordering

Two agents, two model clients, one real HTTP hop between them. The buyer is
sent out with a coupon and a cash limit; the merchant is the restaurant's own
agent and holds the menu, the prices and the till. They talk in plain language
and neither can see the other's tools.

This skill drives what `agent/a2a/` already does. It adds no protocol, no
prompt and no tool of its own.

> **"Skills" means two different things here.** The `skills` array on an A2A
> agent card — `order_food`, `quote_order`, `apply_coupon` — is part of the A2A
> agent card format and is how one agent advertises itself to another. It is
> unrelated to Agent Skills, which is what this directory is. Both are correct;
> they are different registries and neither reads the other.

## The shape of a negotiation

```
console ──POST /api/a2a/runs──▶ buyer agent (8101)
                                    │  talk_to_merchant
                                    ▼
                               POST /api/a2a/merchant/tasks   ← a real HTTP hop
                                    │
                               merchant agent (8101)
                                    │  browse_menu, send_quote, take_payment
                                    ▼
                               Friends Kitchen REST API (8000)
```

The buyer runs as **one continuous turn**. It is conducting an errand, not
answering messages, and decides for itself how many exchanges that takes — every
`talk_to_merchant` is one tool call inside one long Strands turn.

The merchant runs **one turn per message**, on an agent kept alive on the task.
That is what lets "can you drop the drink?" mean anything: a merchant that
forgot the basket between messages would be no use in a negotiation.

## Sending the buyer out

```
python skills/friends-kitchen-a2a-negotiation/scripts/negotiate_order.py \
  "Order a chicken burger and fries, take away" \
  --coupon FK-8H2K-9QW1 --limit 2500
```

Add `--lat`/`--lon` to name a drop, and `--where-it-goes` to say the customer
has already asked for it to be delivered — those are two different facts and
they come apart. See `references/PROTOCOL.md`.

The script prints the transcript as it arrives — both sides, plus each agent's
tool calls labelled by speaker — then the buyer's report and the wallet.

**A negotiation runs two agents on one API key by default.** The most common
failure is a rate limit rather than a bug; the run's error says so and names
`A2A_BUYER_PROVIDER` / `A2A_MERCHANT_PROVIDER` as the fix.

## Reading the merchant's card

```
python skills/friends-kitchen-a2a-negotiation/scripts/merchant_card.py
```

`GET /.well-known/agent-card.json`, unwrapped — the one endpoint in this system
that does not use the `{success, data}` envelope, because a stranger's agent
should get the card the spec describes rather than this project's wrapper round
one. It is what the buyer reads before it says anything, via `discover_merchant`.

Pass `--service delivery` or `--service foodpanda` for the two courier agents'
cards.

## What each side can do

**Buyer** (`agent/a2a/buyer_tools.py`) — `discover_merchant`,
`talk_to_merchant`, `offer_coupon`, `check_wallet`, `authorize_payment`,
`verify_order`.

**Merchant** (`agent/a2a/merchant_tools.py`) — `list_categories`,
`browse_menu`, `add_to_basket`, `remove_from_basket`, `view_basket`,
`send_quote`, `check_coupon`, `confirm_order`, `redeem_coupon`, `take_payment`,
`look_up_order`.

The merchant can also be given browser hands (`A2A_MERCHANT_HANDS=browser`,
`agent/a2a/merchant_browser_tools.py`). **The tool names are identical either
way**, so the brief and the negotiation do not change — only whether the
merchant is calling the API or tapping a touchscreen.

Note what the merchant is *not* told: no coupon, no budget. It learns about the
money only if the buyer brings it up. That asymmetry is the point of the
exercise, and it is enforced by `MerchantTaskIn` having no field for either.

## The two facts that matter when reading a run

* **`completed` is the merchant's only self-reached success**, and it means
  paid. `input_required` — the state a confirmed-but-unpaid order sits in — is
  not a failure; the buyer may still be deciding, and closing the task would
  strand it.
* **A run can die after the money has moved.** A provider's token budget running
  out between paying and reporting is the ordinary way that happens, so the
  facts go out on the `final` event either way, marked `afterError`. A report
  beginning `[no report from the agent]` was assembled from the session, not
  written by the model — read it especially carefully.

## Files

* `scripts/negotiate_order.py` — run one negotiation and follow the transcript.
* `scripts/merchant_card.py` — fetch an agent card from the well-known URL.
* `references/PROTOCOL.md` — states, artifacts, events, and the wire format.
* `references/HTTP-API.md` — the exact contract on port 8101.
* `assets/merchant-task.example.json` — a filled-in opening message.
