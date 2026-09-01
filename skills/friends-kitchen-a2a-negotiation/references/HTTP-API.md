# The ordering desk on port 8101

The contract `a2a_server.py` already publishes. A description, not a definition.

Base URL: `http://localhost:8101`, or `A2A_PUBLIC_BASE` when it is set.

Everything answers `{"success": true, "data": ...}` — **except the agent card**,
which is served bare.

## Discovery

### `GET /.well-known/agent-card.json`

The merchant's card, unwrapped. Name, description, `url`, transport,
capabilities, and a `skills` array (`order_food`, `quote_order`,
`apply_coupon`) — A2A card skills, unrelated to Agent Skills.

## The merchant's endpoint — what another agent talks to

### `POST /api/a2a/merchant/tasks` → 201

```json
{
  "message": "Two zinger burgers and a large drink, take away.",
  "data": {"couponCode": "FK-8H2K-9QW1"},
  "buyerId": "…",
  "messageId": "…"
}
```

`message` is 1..4000 characters. `data` is optional and is where figures and
codes belong. `messageId` lets the sender mint the id.

There is **no coupon field and no budget field**. The merchant finds out about
money only if the buyer chooses to mention it.

### `POST /api/a2a/merchant/tasks/{task_id}/messages`

The same body minus `buyerId`. 404 for an unknown task, 409 when the
conversation is already terminal, 422 when there is neither text nor data.

Both return the merchant's latest turn alone — not the whole history, and only
the artifacts produced in that turn.

### `GET /api/a2a/merchant/tasks/{task_id}`

`{"taskId", "state", "artifacts", "history"}`, and `error` when there is one.

### `GET /api/a2a/merchant/tasks/{task_id}/events`

SSE, from the merchant's side. Not what the console reads — that follows the
run, which carries both sides. This is what to open when the question is
*what did the merchant think it was told?*

## The console's endpoint — sending the buyer out

### `POST /api/a2a/runs` → 201

```json
{
  "instruction": "Order a chicken burger and fries, take away",
  "couponCode": "FK-8H2K-9QW1",
  "cashLimit": 2500,
  "customerId": null,
  "whereItGoes": false,
  "userLocation": {"latitude": 33.5875, "longitude": 72.995, "label": "Office", "source": "manual"}
}
```

Deliberately the same shape as the errand flow's `StartRunIn` on 8100, minus
`mode`. `cashLimit` is **rupees**.

422 with neither a coupon nor a cash limit, and 422 for a `userLocation` that is
not a place on Earth — validated at the edge, before a run exists to carry it,
because a swapped latitude and longitude is a delivery to the wrong hemisphere.

Returns `{"runId": "…", "status": "queued"}`.

### `GET /api/a2a/runs/{run_id}`

```json
{"runId": "…", "status": "…", "instruction": "…", "merchantTaskId": "…",
 "finalText": "…", "error": null, "wallet": { }, "events": [ ]}
```

`wallet` figures are raw numbers and every one is rupees.

### `GET /api/a2a/runs/{run_id}/events`

SSE carrying **both sides** of the negotiation. Event types are in
`PROTOCOL.md`.

### `POST /api/a2a/runs/{run_id}/cancel`

`{"runId": "…", "status": "cancelling"}`. Cancelling does not un-buy anything.

## Health and coupons

### `GET /api/a2a/health`

Reports the two sides separately, because they are configured separately —
"no API key" is useless advice when there are two agents and only one is missing
one.

```json
{"a2a": "ok", "restaurantApi": true, "llm": { },
 "buyer":    {"provider": "…", "model": "…", "pinned": false, "ready": true, "problem": null},
 "merchant": {"provider": "…", "model": "…", "pinned": false, "hands": "api", "ready": true, "problem": null},
 "busy": false, "customer": { }, "telemetry": { }}
```

`pinned` is true only when an `A2A_BUYER_*` / `A2A_MERCHANT_*` variable is
actually deciding that side — that is, when nobody has chosen on the LLM screen.
A side running on something other than the central selection is never silent
about it.

`hands` is `api` or `browser`: whether the merchant calls the restaurant's API
or taps its touchscreen. The tool names are the same either way.

### `GET /api/a2a/coupons`

The same list as 8100 serves, read independently so the A2A console works
whether or not 8100 is running.

## Concurrency

With API hands each task carries its own basket, so negotiations may overlap.
With browser hands they are serialised: Chromium is one per process, and two
negotiations driving the same window would fight over it.

## Also mounted here

`/api/a2a/console`, `/api/a2a/console/events`, and `/api/llm/...` — the same
shared LLM selection all four services read.
