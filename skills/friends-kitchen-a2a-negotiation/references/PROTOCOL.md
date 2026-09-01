# The A2A protocol, as this system implements it

`agent/a2a/protocol.py` is the definition. This describes it; it does not
change it.

Protocol version `0.3`, the http+json subset. Not the full A2A specification —
what is here is messages, task states, artifacts and a streaming channel, which
is the part two agents need to trade.

## Parts

A message is a list of parts, and there are two kinds:

```json
{"kind": "text", "text": "Two zinger burgers and a large drink, take away."}
{"kind": "data", "data": {"couponCode": "FK-8H2K-9QW1"}}
```

The split is load-bearing. **A coupon code goes in a data part, never in the
prose** — transcribed into a sentence it is a string a model can mistype, and a
mistyped coupon fails as "not found" three tool calls later. The merchant is
handed data parts as labelled JSON, exactly as written.

## Messages

```json
{"messageId": "a1b2c3d4e5f6", "taskId": "...", "role": "buyer",
 "parts": [ ], "ts": 1735689600.123}
```

`role` is `buyer` or `merchant`. **The sender mints `messageId`**, so one
utterance has the same id in both transcripts and the two sides can be
correlated afterwards rather than guessed at.

## Task states

| State | Means |
| --- | --- |
| `submitted` | Opened, not yet worked on |
| `working` | The merchant is thinking |
| `input_required` | The merchant has answered and is waiting. **Not a failure.** |
| `completed` | Paid. The merchant's only self-reached success. |
| `rejected` | The merchant closed it — including the max-turns guard |
| `failed` | Something broke |
| `cancelled` | Stopped from outside |

Terminal: `completed`, `failed`, `rejected`, `cancelled`.

A confirmed-but-unpaid order sits at `input_required` on purpose. The buyer may
still be deciding, and closing the task would strand it.

`A2A_MAX_TURNS` bounds the conversation. Past it the merchant closes the order
unfinished — two polite agents will otherwise keep being polite at each other
until somebody's token budget runs out.

## Artifacts

Structured results the merchant produces alongside its prose. Two names are
agreed between both sides; anything else is free-form.

| `name` | Produced by | Carries |
| --- | --- | --- |
| `quote` | `send_quote` | Lines, subtotal, tax, total — in PKR |
| `receipt` | `take_payment` | Order number, what was charged, what the coupon covered |

```json
{"artifactId": "...", "name": "quote",
 "parts": [{"kind": "data", "data": { }}], "ts": 1735689600.123}
```

A receipt existing is what flips the task to `completed`. The buyer reads both
of these **with code rather than with the model** — an amount a model retyped is
an amount that can be wrong.

Only artifacts produced *in this turn* come back from a send. An estimate quote
and the firm quote that superseded it are the same shape, and a buyer handed
both will sooner or later check its budget against the wrong one.

## Events on a run's stream

| `type` | Carries |
| --- | --- |
| `status` | One of the task states above |
| `message` | `speaker`, `text`, `data`, `messageId`, `ts` |
| `artifact` | `speaker`, `name`, `data`, `artifactId` |
| `tool` | `speaker`, `toolUseId`, `name` |
| `tool_result` | `speaker`, plus the tool's return value |
| `final` | `text`, `wallet`, `paid`, `orderNumber`, and `afterError` when the run died after paying |
| `error` | One sentence, written for a person |
| `end` | Always last |

Every event that has a side carries `speaker`, so one stream reads as one
conversation. Streamed text deltas are deliberately **not** emitted — both
agents' prose arrives as whole messages.

## Discovery

`GET /.well-known/agent-card.json`, unwrapped. The buyer reads it with
`discover_merchant` before it says anything.

The card's `skills` array — `order_food`, `quote_order`, `apply_coupon` — is
the A2A agent card's own idea of a skill: how one agent advertises itself to
another. It is not the Agent Skills format this directory implements, and
neither reads the other.

## Where it goes, and whether they asked

Two different facts, and they come apart:

* `userLocation` — **where** a paid take-away order should be delivered. Absent
  is not an error: the flow falls back to the customer's saved address.
* `whereItGoes` — **whether the customer asked** for it to be brought to them.
  This is the consent the delivery agent would otherwise stop and ask for at the
  far end of the handover.

Naming a drop is not the same as asking for delivery, which is why there are two
fields. `whereItGoes` is accepted as `where_it_goes` too: it crosses to another
agent, and a field named after a switch is the one most likely to be typed the
other way round. Absent reads as `false`, which is the behaviour the service has
always had — consent can only ever remove a question, never add one.
