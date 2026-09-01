# The ordering service on port 8100

The contract `server.py` already publishes. Written down here so a script can be
checked against it without reading the server; **it is a description, not a
definition** — `server.py` is the definition, and nothing in this skill may
change it.

Base URL: `http://localhost:8100`, or `FK_AGENT_BASE` when it is set.

Every endpoint answers `{"success": true, "data": ...}`, or an HTTP error with
`{"detail": "<a sentence written for a person>"}`.

## `GET /api/agent/health`

Everything the console needs to say what is not ready.

```json
{
  "agent": "ok",
  "restaurantApi": true,
  "hasApiKey": true,
  "credentialProblem": null,
  "provider": "anthropic",
  "model": "claude-opus-5",
  "llm": { "provider": "...", "model": "...", "source": "...", "ready": true },
  "friendsKitchenWeb": "http://localhost:5173",
  "busy": false,
  "delivery": { "provider": "internal", "displayName": "...", "configured": true },
  "branches": 3,
  "customer": { "label": "...", "latitude": 33.5875, "longitude": 72.995 },
  "telemetry": { }
}
```

`hasApiKey` is the provider-agnostic question — "can this thing run?" A local
Ollama needs no key and is ready. `credentialProblem` is the sentence to show
when it cannot.

## `GET /api/agent/branches`

`{"items": [...]}` — the counters a courier can collect from.

## `GET /api/agent/coupons`

`{"items": [...]}`, each with `couponCode`, `couponType`, `status`,
`remainingBalance`, `originalAmount`, `productName`, `expiryDate`.

Spent, expired and cancelled coupons are included, labelled. Proxied through
this service because the Friends Kitchen API allows only its own origin.

## `POST /api/agent/runs` → 201

```json
{
  "instruction": "Order two cheeseburgers and a large drink",
  "couponCode": "FK-8H2K-9QW1",
  "cashLimit": 2500,
  "mode": "api",
  "customerId": null,
  "headless": true,
  "userLocation": {
    "latitude": 33.5875,
    "longitude": 72.995,
    "accuracyMeters": null,
    "label": "Office",
    "source": "manual"
  }
}
```

* `instruction` — 1..2000 characters, required.
* `couponCode` — up to 40 characters, or null.
* `cashLimit` — 0..1,000,000. **Rupees.**
* `mode` — `api` or `browser`.
* `userLocation` — omit for a counter order. Its presence turns the errand into
  a delivery; there is no separate switch.

422 when there is neither a coupon nor a cash limit: the agent cannot buy
anything with neither.

Returns `{"runId": "...", "status": "queued"}`.

**Runs are serialised.** The wallet, the cart and the browser are one per
process. A second run waits for the first, and is told it is queued.

## `GET /api/agent/runs/{id}/events`

Server-Sent Events. The backlog is replayed on connect, so subscribing a beat
after the POST — or after a page refresh — misses nothing. `{"type": "end"}` is
always the last event. Event types are listed in `ORDERING-FLOW.md`.

## `GET /api/agent/runs/{id}`

```json
{
  "runId": "...", "status": "done", "instruction": "...", "mode": "api",
  "finalText": "...", "error": null,
  "wallet": {
    "couponCode": "...", "couponRedeemed": 0, "cashLimit": 2500,
    "cashSpent": 1093, "cashRemaining": 1407
  },
  "events": [ ]
}
```

`status` is one of `queued`, `running`, `done`, `failed`, `cancelled`.

The wallet figures here are **raw numbers, and every one is rupees** — the
console formats them itself. Anything that prints them must write `Rs 1,093`.

## `POST /api/agent/runs/{id}/cancel`

Returns `{"runId": "...", "status": "cancelling"}`. A cancelled run does not
un-buy anything: an order already paid for still stands.

## Also mounted here

* `/api/agent/console`, `/api/agent/console/events` — this process's log.
* `/api/llm/...` — the shared LLM selection. Identical on all four services;
  see the `friends-kitchen-llm-configuration` skill.
