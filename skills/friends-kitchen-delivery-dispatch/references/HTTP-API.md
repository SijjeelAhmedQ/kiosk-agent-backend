# The courier services on 8102 and 8103

The contracts `delivery_server.py` and `foodpanda_server.py` already publish.
A description, not a definition.

Both answer `{"success": true, "data": ...}`, except the agent cards, which are
served bare.

## One job format, two services

The shapes below mirror `DeliveryRequest.to_message()` in
`agent/delivery/contract.py`. One wire format whichever courier is carrying it
is what makes `DELIVERY_PROVIDER` a config change rather than a code change.

Each service validates the request again on arrival even though the ordering
agent's contract already did. That repetition is deliberate: this is a network
boundary, and a service that trusts its caller to have checked breaks the first
time something else calls it.

### `POST /api/delivery/jobs` (8102) · `POST /api/foodpanda/jobs` (8103) → 201

```json
{
  "order": {
    "orderId": "…", "orderNumber": "FK-2412", "status": "paid", "paid": true,
    "itemCount": 3,
    "items": [{"name": "Zinger Burger", "quantity": 2, "note": null}]
  },
  "pickup":  {"latitude": 33.6, "longitude": 73.05, "address": "…", "name": "…", "phone": "…", "note": null},
  "dropoff": {"latitude": 33.5875, "longitude": 72.995, "address": "…", "name": "…", "phone": "…", "note": null},
  "branchId": "…",
  "distanceKm": 4.2,
  "notes": null,
  "whereItGoes": false
}
```

* `order.paid` must be true — **409 otherwise**. The restaurant will not release
  food nobody has bought.
* `order.items` must be non-empty — **422 otherwise**. Nothing to collect.
* `whereItGoes` (8103 only) is accepted as `where_it_goes` too, and absent reads
  as `false` — which is what a caller written before the field existed sends,
  and what the service has always done.

Returns the job. **Always `requested`** on 8103: what comes back is a job that
has been taken in, not one that has been delivered, and the dispatcher has only
just started reading it.

8103 refuses with **503** when its dispatcher has no usable model credentials,
rather than taking the job and sitting on it — a courier that accepts work it
cannot start leaves the restaurant believing an order is on its way.

### The job view

```json
{
  "jobId": "fpj_…", "orderId": "…", "orderNumber": "FK-2412",
  "status": "accepted", "delivered": false, "done": false,
  "awaiting": "rider",
  "message": "…", "decision": "…",
  "courier": {"name": "Ayesha Khan"},
  "etaMinutes": 18, "etaSeconds": 1080, "fee": "Rs 267",
  "trackingUrl": "/api/foodpanda/jobs/fpj_…",
  "pickup": { }, "dropoff": { }, "items": [ ],
  "itemCount": 3, "distanceKm": 4.2, "notes": null,
  "whereItGoes": false, "branchId": "…"
}
```

`delivered` is spelled out rather than left implicit in `status`, because
`requested` and `delivered` are one field apart and the mistake is expensive.

`awaiting`, `done`, `decision`, `itemCount`, `whereItGoes` and `branchId` are
8103's. The in-house courier's view is the same minus those — it has no gates,
so it has nothing to be awaiting.

`fee` is already formatted as `Rs 267`. Every amount in this system is rupees.

## Reading a job

* `GET /api/{delivery|foodpanda}/jobs/{job_id}` — one job. 404 if unknown.
* `GET /api/{delivery|foodpanda}/jobs` — `{"items": [...]}`, newest first.
* `GET /api/foodpanda/jobs/{job_id}/events` — SSE for one job: `status`,
  `message`, `tool`, `tool_result`, `error`, `end`. **8103 only.**

## The gates — 8103 only

* `POST /api/foodpanda/jobs/{job_id}/find-rider`
* `POST /api/foodpanda/jobs/{job_id}/deliver`

404 for a job nobody has, **409 for a step this job is not sitting at** — which
is what a page left open on a finished delivery asks for.

Each returns the job as it stands the instant the request lands, which is still
the status it was waiting in. The next status arrives on the stream a moment
later.

## Cancelling

`POST /api/{delivery|foodpanda}/jobs/{job_id}/cancel`. 409 once the order has
been delivered — that cannot be undone — and 409 on 8103 for a job that is
already terminal.

On 8103 the status moves first and the agent is stopped second. The other order
would leave a window where the dispatcher has been killed and the board still
says a rider is on the way.

## Health

### `GET /api/foodpanda/health`

```json
{"foodpanda": "ok", "service": "…", "llm": { },
 "dispatcher": {"provider": "…", "model": "…", "pinned": false, "ready": true, "problem": null},
 "radiusKm": 25, "legSeconds": {"pickup": 8, "transit": 12},
 "operatorSteps": true, "activeJobs": 0, "totalJobs": 3, "telemetry": { }}
```

`operatorSteps` is whether the board is a control or a window. `pinned` is true
only when a `MOCK_FOODPANDA_*` variable is deciding the brain — that is, when
nobody has chosen on the LLM screen.

### `GET /api/delivery/health`

The in-house courier. No credentials to report, because it needs none.

## Discovery

`GET /.well-known/agent-card.json` on both, unwrapped. Beyond the standard
fields they carry two extensions:

* **`x-lifecycle`** — the statuses, `terminalSuccess: "delivered"`, the note
  that dispatch returns `requested` and never `delivered`, and `operatorSteps`
  naming each gate with the URL that opens it. Not in the A2A spec, and the
  single most useful field on the card: a caller that reads acceptance as
  arrival tells a customer their food is there when it is not.
* **`x-service`** — the radius, the dispatcher's model, and a plain statement
  that the ride is simulated and this is not connected to Foodpanda.
