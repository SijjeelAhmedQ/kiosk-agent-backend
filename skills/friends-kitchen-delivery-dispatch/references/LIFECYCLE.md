# A delivery, state by state

`agent/foodpanda/jobs.py` is the definition — the transition table there is what
makes an invented `delivered` impossible. This describes it.

## The happy path

```
requested → accepted → courier_assigned → picked_up → in_transit → delivered
```

| Status | Reached by | Means |
| --- | --- | --- |
| `requested` | The job being created | Taken in. Nobody has decided anything. |
| `accepted` | `accept_job` | The dispatcher will make this run. **No rider yet.** |
| `courier_assigned` | `assign_rider` | Somebody is carrying it. Still at the restaurant. |
| `picked_up` | `collect_from_restaurant` | The rider has the food. |
| `in_transit` | `deliver_to_customer` | On the way. |
| `delivered` | `deliver_to_customer` returning | **With the customer.** |

Terminal: `delivered`, `rejected`, `failed`, `cancelled`.

`rejected` is the dispatcher refusing the run — most often a drop outside the
service radius. `failed` is something breaking, including a dispatcher that
stopped talking halfway through; the job is failed on its behalf, because a job
left at `accepted` for ever means an ordering agent politely polling a delivery
nobody is riding.

## What may follow what

Every other transition is refused by the store, not by the prompt:

| From | May become |
| --- | --- |
| `requested` | `accepted`, `rejected`, `failed`, `cancelled` |
| `accepted` | `courier_assigned`, `failed`, `cancelled` |
| `courier_assigned` | `picked_up`, `failed`, `cancelled` |
| `picked_up` | `in_transit`, `failed`, `cancelled` |
| `in_transit` | `delivered`, `failed`, `cancelled` |
| anything terminal | nothing |

The prompt in `agent/foodpanda/prompts.py` states the same rules, and the order
of the two matters: the table is what makes an invented status impossible, and
the prompt is what makes the agent produce a sensible refusal instead of an
opaque tool error it then argues with.

## The two gates

Two steps wait to be asked for, because they are the two a customer can see
happening: a rider being found, and the food leaving the restaurant.

| Gate | Job waits at | Opened by | Then |
| --- | --- | --- | --- |
| `rider` | `accepted` | `POST /api/foodpanda/jobs/{id}/find-rider` | `assign_rider` returns |
| `delivery` | `picked_up` | `POST /api/foodpanda/jobs/{id}/deliver` | the ride runs, then `delivered` |

The dispatcher calls `assign_rider` as soon as it accepts, and the call **holds**
until the request arrives. That wait is the normal case, not a fault, and a
caller that reads a pause as a stall reports a working delivery as a broken one.

`awaiting` on the job view says which gate is open — `"rider"`, `"delivery"` or
`null`. The delivery board lights one button off that field and nothing else.

Two ways the gates change shape:

* `MOCK_FOODPANDA_MANUAL_STEPS=false` removes both. A job runs from request to
  doorstep on its own, and `x-lifecycle.operatorSteps` on the agent card is
  empty so a caller can see that it will.
* `whereItGoes: true` on the job pre-answers the **second** gate only. The
  customer asked for delivery at the moment they ordered; asking again is not a
  second safeguard, it is a delivery that stalls because nobody expected the
  question. Consent can only ever remove a question here, never add one, and it
  never moves a job by itself.

## The dispatcher's own sequence

`agent/foodpanda/prompts.py`. Each tool refuses if the job is not at the point
where it makes sense, so the order is not advisory.

1. `read_delivery_request` — always first. The only source of what is being
   carried; nothing in the conversation is trusted over it, and the coordinates
   a rider is sent to are the ones that arrived over the wire rather than ones a
   model retyped.
2. `accept_job` or `reject_job`, with a reason either way.
3. `assign_rider` — **waits** at the first gate.
4. `collect_from_restaurant` — a few seconds, then the rider has it.
5. `deliver_to_customer` — **waits** at the second gate, then rides. The only
   thing that completes a delivery.

`report_problem` is for something that genuinely stops the run — nothing to
collect, an address that does not exist.

Each step is called once and left to wait. A tool that has not come back yet is
a delivery that has not happened yet, and calling it again only asks the same
question twice. A wait that times out says so, and that is a problem to report
rather than a step to start again.

## The one judgement that is the dispatcher's

Whether the run is makeable. The radius is `MOCK_FOODPANDA_RADIUS_KM`,
measured from the collecting restaurant, and it is written into the brief so the
agent refuses in words rather than by failing a tool.

What is *not* the dispatcher's to decide: whether the order was worth buying,
what it cost, or what is in it. That is settled before the job exists.

## What is simulated and what is not

The dispatcher is a real agent making real decisions on a real model. The ride
is simulated — the legs are compressed to seconds and **awaited, never
skipped**, so the states are reached in order and in real time. It is not
connected to Foodpanda; the agent card says so in `x-service.simulation`.
