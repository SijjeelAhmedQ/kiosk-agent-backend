# The four services

Each is its own FastAPI app on its own port, and that separation is the point:
the ordering agent reaches the courier over real HTTP even when both are running
on one laptop, because that is what makes it agent-to-agent rather than a
function call wearing a costume.

Every endpoint answers `{"success": true, "data": ...}`, or `{"detail": "…"}` on
a refusal — **except** the well-known agent cards, which are served bare so that
a stranger's agent gets the card the spec describes rather than this project's
envelope round one.

## `server.py` — the ordering agent, port 8100

The front door the errand console drives. `POST /api/agent/runs` starts an
errand, `GET /api/agent/runs/{id}/events` streams it.

**Runs are serialised, deliberately.** The wallet, the cart and the browser are
one-per-process singletons — the right model for "one agent, one errand" and the
wrong one for concurrency. Rather than pretend otherwise, a second run waits and
the UI is told it is queued. `busy` on the health payload is that lock.

Health reports `hasApiKey` (can this thing run at all — a local Ollama needs no
key and is ready), `credentialProblem`, the active `llm`, the `delivery`
provider, the branch count and the customer's saved address.

Full contract: the `friends-kitchen-ordering` skill's `references/HTTP-API.md`.

## `a2a_server.py` — the ordering desk, port 8101

Two agents. `POST /api/a2a/runs` sends the buyer out; the merchant lives behind
`/api/a2a/merchant/tasks`, which is what another agent talks to, and publishes
a card at `/.well-known/agent-card.json`.

Health reports the two sides **separately**, because they are configured
separately: "no API key" is useless advice when there are two agents and only
one is missing one.

With API hands, negotiations may overlap — each task carries its own basket.
With browser hands they serialise: Chromium is one per process.

Full contract: the `friends-kitchen-a2a-negotiation` skill.

## `delivery_server.py` — the in-house courier, port 8102

Friends Kitchen's own delivery agent, and the default `DELIVERY_PROVIDER`. The
provider that needs **no credentials**, which is what lets the whole delivery
flow be demonstrated without an account anywhere.

A job here runs itself from request to doorstep. No gates, no dispatcher agent.

## `foodpanda_server.py` — the demonstration dispatcher, port 8103

A real AI dispatcher that reads a request, decides whether the run is makeable,
assigns a rider, collects and delivers. The **ride** is simulated: legs are
compressed to seconds and awaited, never skipped. Not connected to Foodpanda,
and the agent card says so.

Two steps wait to be asked for — a rider, and the delivery itself. A job sitting
at `accepted` is waiting, not stalled.

Refuses a job with **503** when its dispatcher has no usable model credentials,
rather than taking it and sitting on it: a courier that accepts work it cannot
start leaves the restaurant believing an order is on its way.

Full contract: the `friends-kitchen-delivery-dispatch` skill.

## Mounted on all four

### `{prefix}/console` and `{prefix}/console/events`

One factory, four services — the endpoints are identical and the namespace is
the argument. `console` returns the scrollback this process still holds;
`console/events` follows it live, with `after=<seq>` to pick up where a dropped
stream left off.

A line:

```json
{"seq": 412, "at": 1735689600123, "service": "ordering", "agent": "ordering",
 "level": "info", "kind": "tool", "text": "…", "ref": "9f2a41c0e1",
 "tool": "authorize_payment", "ok": true, "source": "agent", "data": null}
```

`ref` is the run or job the line belongs to — filter by it to read one errand
out of a busy log. `source` is `agent` for a service's own events and `log` for
anything that went through Python logging. The live stream also sends a
keep-alive carrying only a timestamp; a real line always has `seq`.

Installed at import rather than in a startup hook, because the logging handler
has to be on the root logger before anything at module scope can log.

### `/api/llm/...`

The shared model selection, identical everywhere. A screen that could only be
reached while one particular service happened to be up would be the wrong place
to fix a configuration problem. See the `friends-kitchen-llm-configuration`
skill.

## Where the logs on disk are

`var/`, one pair per service: `agent-8100.out.log` / `.err.log`,
`a2a_server.*`, `delivery_server.*`, `foodpanda_server.*`, plus
`llama-server.*` when the local runtime is in use. These hold what the console
endpoint cannot — anything written before the handler was installed, and
anything after a process died.

## CORS

Each service allows `localhost:5174` (the agent control panel) and
`localhost:5173` (Friends Kitchen itself). The restaurant's own API allows only
its own origin, which is why the coupon list is **proxied** through 8100 and
8101 rather than fetched from the browser — widening its CORS so a second app
could read it would be a bigger change than forwarding one read.

## Telemetry

`FK_OTEL` turns tracing on in every service; off by default and it never raises.
`traceparent` goes out on httpx and is read back in by FastAPI, so one errand
across the ordering agent, a courier and the restaurant is one trace rather than
four. Each health payload reports whether an exporter is configured — never
what is in the headers, because `OTEL_EXPORTER_OTLP_HEADERS` usually carries a
token.
