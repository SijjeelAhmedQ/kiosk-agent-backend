# Friends Kitchen — Ordering Agent

A standalone AI agent that goes to Friends Kitchen and places an order for you,
carrying a coupon and a spending limit.

There are two ways to drive it — a control panel, or the command line.

**The UI** (recommended). Start the server here, then run the panel:

```bash
.venv\Scripts\python -m uvicorn server:app --port 8100
```

…and `npm run dev` in **`friends-kitchen-agent-frontend`** (port 5174). Write the errand, pick a
coupon, watch the agent work. See [that app's README](../friends-kitchen-agent-frontend/README.md).

**The other three processes.** Each console here talks to its own agent on its own
port, and a console whose agent is not running says so rather than pretending.
Start the ones you want:

```bash
.venv\Scripts\python -m uvicorn a2a_server:app       --port 8101 --reload  # /a2a.html
.venv\Scripts\python -m uvicorn delivery_server:app  --port 8102 --reload  # the in-house courier
.venv\Scripts\python -m uvicorn foodpanda_server:app --port 8103 --reload  # /foodpanda.html
```

The delivery pair is an either/or, and `DELIVERY_PROVIDER` picks which: `internal`
sends paid orders to **8102**, `mock_foodpanda` to **8103**. They are different
services and neither can answer the other's jobs — so if a paid order gets no
rider, the first thing to check is that the port your provider names is the port
you started.

**The CLI**, for scripting and quick checks:

```bash
python run.py "Order two cheeseburgers" --coupon AGENT-8247EA9611 --limit 3000
```

It is a separate application from Friends Kitchen. It has its own process, its own
virtualenv, and its own Anthropic API key, and it talks to the restaurant the
same way a customer would — over the public API, or by driving the actual
website in a real browser.

---

## The idea

> "Think of it like giving money to a child and saying: go to the shop and buy
> this for me. The token is the money, and the AI agent is the person carrying
> out the task."

That maps onto three things:

| The metaphor | Here |
|---|---|
| The errand | A plain-language instruction: *"order two burgers"* |
| The money | A **coupon code** (the restaurant's existing token) plus a **cash ceiling** |
| The person | A Claude agent with tools for browsing, adding, couponing and paying |

The spending limit is not a suggestion in the prompt. It lives in
[`agent/wallet.py`](agent/wallet.py) and the payment tool checks it before it
charges anything, so an over-budget order comes back to the agent as a refusal
it has to deal with rather than an overspend nobody noticed.

---

## Setup

### 1. The restaurant must be running

```bash
# Backend — from friends-kitchen-backend/
.venv\Scripts\python -m uvicorn app.main:app --port 8000

# Frontend — from friends-kitchen-frontend/, only needed for --mode browser
npm run dev
```

### 2. Install the agent

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # only for --mode browser
```

### 3. Give it a key

```bash
copy .env.example .env
```

Then put a key in `.env` — the agent cannot use your Claude Code session, it
needs its own. `AGENT_PROVIDER` picks which one: `huggingface` is the free
default here (a token from
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), no
card, and it needs the *Make calls to Inference Providers* permission), and
`anthropic` is the best tool-caller if you have credits at
[console.anthropic.com](https://console.anthropic.com/settings/keys). `groq`,
`gemini` and `ollama` also work; see `.env.example` for all six.

Once the control panel is running you can change provider and model on its **LLM
Configuration** screen instead, and that choice applies to every agent at once —
see [One brain, four agents](#one-brain-four-agents). The keys stay in `.env`
either way; the screen never sees them.

### 4. Give it something to spend

Any coupon from Friends Kitchen's own admin screens works. To mint one from the
command line, pick a running campaign and generate against it:

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/41/coupons \
  -H "Content-Type: application/json" \
  -d '{"quantity":1,"amount":1500,"codePrefix":"AGENT"}'
```

---

## Running it

```bash
# Coupon plus cash for whatever it does not cover
python run.py "Order two cheeseburgers" --coupon AGENT-8247EA9611 --limit 3000

# Strictly the coupon — no cash at all. If it does not cover the order,
# the agent is expected to stop and say so rather than place it.
python run.py "Get me a Big Mac" --coupon PROBE-BAB0C5A884

# Drive the real website in Chromium instead of the API
python run.py "Order a cheeseburger meal" --coupon AGENT-8247EA9611 --limit 2000 --mode browser
```

| Flag | Meaning |
|---|---|
| `--coupon` | The token to spend. Omit for a cash-only errand. |
| `--limit` | Cash the agent may spend beyond the coupon. Defaults to `0`. |
| `--mode` | `api` (default) or `browser`. |
| `--customer` | Customer id recorded against the coupon redemption. |
| `--quiet` | Suppress the live trace; print only the final report. |

---

## The two modes

Both modes give the agent the same *shape* of tool — browse, add, coupon, pay —
so the model's job does not change when you switch. Only the hands change.

### `--mode api`

Tools call the FastAPI backend directly. Fast, deterministic, and the right
choice for a scheduled or headless run. This is also roughly what real
agent-commerce looks like: machines talking to machines.

### `--mode browser`

Tools drive the React Friends Kitchen in Chromium via Playwright — tapping the welcome
screen, typing in the search box, opening a product, answering "Make it a
meal?", typing the coupon into the coupon field, and pressing Pay. Nothing
touches the API directly.

This is the one to demo. It is slower and it is doing strictly more work, but
it proves the point the brief actually made: *the agent goes to your website and
uses it.*

The browser tools are semantic (`search_menu`, `add_to_cart`, `pay`) rather than
pixel-level. The model decides *what* to do; `agent/browser/friends_kitchen_driver.py`
knows *where* things are, keyed off `data-testid` attributes in the frontend.
That keeps runs reproducible — a restyle cannot break the agent, only a
deliberate change to those hooks can.

---

## How it is put together

```
run.py                       CLI — parses the errand, fills the wallet, starts the agent
server.py                    HTTP front door: starts runs, streams them as SSE (for the UI)
delivery_server.py           The in-house courier — its own service on 8102, with its own card
foodpanda_server.py          The Foodpanda dispatcher agent — its own service on 8103, with a brain
agent/
  config.py                  Environment-driven settings
  wallet.py                  The token and the cash ceiling. Enforced, not suggested.
  location.py                Where the customer is. Validated, then held for the run.
  telemetry.py               OpenTelemetry, off unless FK_OTEL says otherwise
  branches.py                Which branch serves that location, and where a rider collects
  prompts.py                 The brief the agent is given
  friends_kitchen_agent.py             Strands Agent assembly (the brain comes from agent/llm)
  friends_kitchen_api.py               HTTP client for the Friends Kitchen API
  cart.py                    Client-side cart (the API has no cart endpoint)
  llm/                       The central LLM service — one brain for every agent
    store.py                 Which provider and model is selected, and where that is kept
    providers.py             One adapter per provider (OpenRouter, local Ollama, and five more)
    service.py               What agents call: llm.build_model()
    api.py                   /api/llm/* — mounted by all four services
  tools/
    api_tools.py             Ordering via REST
    browser_tools.py         Ordering via the website
    delivery_tools.py        Handing a paid order to a courier, and tracking it
  delivery/
    contract.py              The shapes and the interface — the whole A2A boundary
    courier_agent.py         Friends Kitchen's own delivery agent (the default)
    foodpanda.py             Foodpanda's courier API
    registry.py              Which provider this deployment uses
  browser/
    friends_kitchen_driver.py          Playwright page-object for the Friends Kitchen UI
```

**Framework.** [Strands Agents](https://strandsagents.com) — AWS's open-source
agent SDK. It supplies the agent loop (call the model, run the tool, feed the
result back, repeat); everything above is tools and a prompt. It is
model-agnostic and has MCP and A2A support built in, which is what makes the
next phase cheap.

**Model.** `claude-opus-5` by default, because ordering is a multi-step tool-use
loop where a wrong step spends real money. Set `AGENT_MODEL=claude-sonnet-5` in
`.env` for cheaper demo runs — or change it on the LLM Configuration screen,
which is the subject of the next section.

---

## One brain, four agents

There are four agents on this floor and there is **one** provider-and-model
setting between them. Change it in the control panel's **LLM Configuration**
screen and the ordering agent, the A2A buyer, the A2A merchant and the Foodpanda
dispatcher all follow — no restart, and nothing to edit in any agent.

```
                    LLM Configuration screen  (/llm.html)
                                 │
                                 ▼
                        /api/llm/*   (all four services mount it)
                                 │
                                 ▼
                      agent/llm/service.py   llm.build_model()
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
          OpenRouterProvider            LocalProvider
                    │                         │
                    ▼                         ▼
             OpenRouter API            Ollama on this machine
                    └────────────┬────────────┘
                                 ▼
                          the selected model
                                 │
       ┌──────────────┬──────────┴──────────┬──────────────────┐
       ▼              ▼                     ▼                  ▼
  Ordering agent  A2A buyer          A2A merchant     Foodpanda dispatcher
```

**Where the selection lives.** In `var/llm-config.json`, not in a variable — the
four agents run in four processes, so a module-level singleton could only ever
switch one of them. Each process reads the file when it builds a model client,
so a change reaches a service that is already running and the *next* errand it
takes uses the new brain. It survives a restart, which is the point: nobody
should have to choose a provider twice.

**Where models come from.** Both featured providers are asked rather than
guessed. OpenRouter's catalogue comes from OpenRouter (~400 models, cached ten
minutes); the local list is whatever `ollama pull` has actually put on this
machine, read from the daemon's `/api/tags` and cached twenty seconds. Nothing
is hardcoded in the frontend.

**What has not changed.** `AGENT_PROVIDER` and `AGENT_MODEL` still work and are
still what a deployment runs on until somebody makes a choice on that screen.
All seven providers this project supported are still selectable — the two above
are simply the ones with cards. And no key ever reaches the browser: the screen
changes *which* provider is used, never what it is authenticated with.

```
GET    /api/llm/config      what every agent is running on
PUT    /api/llm/config      change it, everywhere
GET    /api/llm/providers   what can be chosen, and whether each is usable here
GET    /api/llm/models      what one provider can run
GET    /api/llm/health      could this provider and model serve a run
POST   /api/llm/test        ask the model a question and read the answer back
```

---

## Delivery, and the second agent

An errand started **with a customer location** becomes a delivery. One started
without it is the counter order this agent has always placed — same tools, same
brief, same result. That is the whole switch:

```
POST /api/agent/runs
{ "instruction": "Order one Big Mac", "cashLimit": 3000,
  "userLocation": { "latitude": 33.5875, "longitude": 72.9950,
                    "label": "Flat 3, Westridge", "source": "browser" } }
```

The flow it produces:

```
detect location → user_location → nearest branch → order → pay
                                                            ↓
        customer  ←──── delivery agent ←──── order + pickup + dropoff
```

**The handover is automatic, not a step the agent takes.** The instant a paid
take-away order exists, `authorize_payment` (or browser mode's `pay`) hands it to
the configured courier and returns the job alongside the charge. Delivery is
therefore a property of a paid take-away order, not something a model has to
remember to ask for — which matters, because the run where it forgets is a
customer left with a paid order and no rider. `arrange_delivery` is still there
as the retry when that automatic handover reported a failure, and it refuses a
second rider for an order that already has one.

A dine-in order is never dispatched, whatever the errand carries: it is eaten at
the restaurant, and `place_order` says so in its result while the choice can
still be corrected.

**Where the location lives.** `agent/location.py`, held for the run the way the
wallet is. It is never passed through the prompt and the delivery tools take no
coordinate arguments — a model that retyped a latitude could move a delivery a
hundred kilometres and nothing would look wrong. `parse()` refuses anything that
is not a place on Earth, including 0°,0°, which is what a device with no fix
reports.

**The customer's own address.** `location.saved()`, built from three variables in
`.env` (`FK_CUSTOMER_ADDRESS`, `FK_CUSTOMER_LAT`, `FK_CUSTOMER_LON`). It is the
same `UserLocation` a device fix produces, so it travels down exactly the same
path — nothing downstream knows which of the two it was handed. Two flows need
it, because neither has a browser to ask:

- the errand console offers it as one click, which is what turning the *Where it
  goes* switch on now fills in — a street a rider can read instead of a
  permission prompt and five decimal places;
- the **A2A console has the same switch**, and a paid take-away order there goes
  to the saved address whenever nobody flips it. The drop it names never enters
  the negotiation: it is held on the console run, and the merchant's handover
  reads it back from the `buyerId` the conversation was opened with — coordinates
  in a message are coordinates a model can retype. See `agent/a2a/delivery.py`.

A run that carries its own fix uses that one. The saved address is the fallback,
never an override.

**Both buying agents hand over the same way.** `agent/delivery/handover.py` is
the one place a delivery message is assembled: the order and its lines re-read
from the restaurant, payment re-derived from the payment ledger, the pickup from
the branch directory. The errand agent calls it from `authorize_payment` and the
A2A merchant from `take_payment`, so the two cannot drift into two different
ideas of when a courier may be sent — and an order bought by either appears on
the same board at `/foodpanda.html`.

**Which restaurant.** The Friends Kitchen API has no branch concept, so the
directory is `FK_BRANCHES` in `.env` and lives in `agent/branches.py`. Blank
means one branch, which is what the API already assumes.

**The handover is a real network call.** `delivery_server.py` is its own service
on 8102 with its own agent card at `/.well-known/agent-card.json`, exactly as
the A2A merchant on 8101 is separate from this server on 8100. It cannot see the
cart, the wallet or the placed order — only what the request carried.

```bash
.venv\Scripts\python delivery_server.py   # always 8102 — the port is fixed in the file
```

**Adding a courier** is a file in `agent/delivery/` and a line in `registry.py`.
Everything upstream speaks `DeliveryRequest`; only the provider has heard of
`pickup_contact`. `DELIVERY_PROVIDER` picks one:

| `DELIVERY_PROVIDER` | Who carries it | Needs |
| --- | --- | --- |
| `internal` | The courier on 8102 — a state machine on a clock | nothing |
| `mock_foodpanda` | The dispatcher **agent** on 8103 — an LLM that decides | a model key |
| `foodpanda` | Foodpanda's real courier API | `FOODPANDA_API_KEY` |

### The delivery agent that thinks

`foodpanda_server.py` is the second agent in this repo, and the difference from
the courier on 8102 is not the port — it is that a model is in charge of the
job. It reads the request through a tool, decides whether the drop is inside its
service radius, **refuses it if not**, and only then assigns a rider and runs the
legs. Its reasoning is streamed, so the console at `/foodpanda.html` shows an
agent working rather than a progress bar.

```bash
.venv\Scripts\python -m uvicorn foodpanda_server:app --port 8103
```

What keeps that safe is `agent/foodpanda/jobs.py`: the model cannot *write* a
status, only ask for the next legal one, and `picked_up → delivered` without a
ride is not in the transition table. The legs are compressed to seconds and
awaited, never skipped. It is a demonstration courier and says so on its own
agent card — for a real one, point `DELIVERY_PROVIDER=foodpanda` at the API.

### Two steps belong to the customer

The dispatcher decides whether it will take a delivery. It does not decide when
the customer wants it. So a job holds twice, and both are requests made from the
board:

```
requested → accepted ──[ Find a rider ]──→ courier_assigned → picked_up
                                                                  │
                          delivered ← in_transit ←──[ Deliver it to me ]
```

| Ask | Waits at | Route |
| --- | --- | --- |
| a rider | `accepted` | `POST /api/foodpanda/jobs/{id}/find-rider` |
| the delivery | `picked_up` | `POST /api/foodpanda/jobs/{id}/deliver` |

The wait is the dispatcher's own: it called `assign_rider`, and that tool has not
returned yet. There is no second copy of the job's progress being kept while the
board catches up, because the delivery genuinely has not moved — which is why the
job's `awaiting` field, not its status, is what lights a button. A request for a
step the job is not holding is a `409`, so a page left open on a finished
delivery cannot send a rider to an order that arrived ten minutes ago.

`MOCK_FOODPANDA_MANUAL_STEPS=false` turns both gates off and a job runs itself
end to end, for an unattended demo. Nothing about the agent's decisions changes
either way, and a job nobody ever attends to fails on
`MOCK_FOODPANDA_OPERATOR_TIMEOUT_SECONDS` rather than holding a rider for ever.

### Sent is not delivered

The one thing this flow will not do is call an order delivered because the
dispatch succeeded:

- The handover returns `status: "requested"` and `delivered: false`, whether it
  came from payment or from `arrange_delivery`. The tool docstrings, the system
  prompt and the agent card all say so separately.
- `DeliveryJob.delivered` is true for exactly one status. A courier reporting a
  word we do not recognise maps to `unknown`, never to `delivered`.
- `POST /api/delivery/jobs` never returns `delivered`; a job reaches it only
  after the journey has actually elapsed.

The in-house courier dispatches to a **simulated** rider — it is not driving a
vehicle. What it does not do is fake the outcome: it refuses what it cannot
honour, and it reports arrival only when the full journey is done.

---

## Safety properties

These are enforced in code, not asked for in the prompt:

- **The agent cannot overspend.** `authorize_payment` (API) and `pay` (browser)
  both check the amount against the wallet first. Over budget is a refused tool
  call; the money never moves.
- **The agent cannot double-pay.** Both payment tools tell it explicitly not to
  retry an unchanged payment, and a failed payment reports the order as placed
  but unpaid rather than looping.
- **The agent cannot invent product ids.** Ids are server-generated; the tools
  reject unknown ones and point it back at the menu.
- **Agent orders are identifiable.** Every order and redemption carries
  `X-Terminal-Id: agent-01` (configurable), so the admin screens can tell agent
  traffic from walk-in traffic.
- **An unpaid order cannot be sent for delivery.** Every handover re-reads the
  order from the restaurant and checks the status *and* the payment ledger before
  dispatching. The delivery agent checks again on its own side.
- **One order cannot get two riders.** The automatic handover happens once, and a
  later `arrange_delivery` in the same run is refused with the job id of the
  first.
- **A courier that will not answer cannot fail a payment.** The automatic
  handover never raises: the money has already moved by the time it runs, so a
  refusal comes back beside the charge rather than turning it into an error.
- **A failed courier cannot lose an order.** Every delivery failure leaves the
  order placed and paid, and says so — the ordering half is untouched by it.

---

## The console stream

Every service publishes everything it says on one stream per process, with no
run id in the URL and nothing to start first:

```
GET /api/agent/console            the last 800 lines, at once
GET /api/agent/console/events     the same log, live, as SSE
```

and the same pair under `/api/a2a`, `/api/delivery` and `/api/foodpanda`.

**Why it exists.** The per-run streams (`/runs/{id}/events`) are addressed to
whoever started the run, which is right for a console driving one errand and
useless to anything watching the floor. The operations dashboard could see that
the ordering agent was busy and never what it was busy with. These streams are
not addressed to anybody: open one at any time and you get what the process is
doing, whether or not a console is attached.

Two things feed each one, and the pairing is the point — a refused HTTP call
lands directly beneath the tool call that made it:

* the events the service already publishes, mirrored at the single `emit` each
  stream funnels through, so a new event kind cannot be added and forgotten;
* the process's Python logging, so httpx, Strands and any traceback appear too.

Lines carry a level, the speaker, the tool, and the run they belong to. Model
text is coalesced rather than emitted a token at a time. The OpenTelemetry
exporter's own retry chatter is the one thing filtered out: with `FK_OTEL=otlp`
set and no collector listening it is two lines every few seconds forever, and
the health endpoint already reports that condition properly. See
`agent/console.py`; the dashboard's end is `src/dashboard/useConsole.ts` in
`friends-kitchen-agent-frontend`.

Nothing about it can fail an errand — the buffer is bounded, listeners that go
away are dropped, and every error inside the module is swallowed.

## Tracing

Off by default. `FK_OTEL=console` in `.env` prints spans to whichever process
you started; `FK_OTEL=otlp` sends them to a collector.

```bash
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
```

Then `FK_OTEL=otlp` and open <http://localhost:16686>.

**What it is for.** Four separate processes is the right shape for this system
and it has one real cost: no single console shows an errand. A run that ends
"paid, no rider" is spread across three windows and two of them have scrolled.
Tracing puts it back together — every hop carries a `traceparent`, every service
continues the trace it was handed, and one errand is one tree:

```
POST /api/a2a/runs
  └─ buyer agent turn
       ├─ tool: talk_to_merchant   → POST /api/a2a/merchant/tasks
       │    └─ merchant agent turn
       │         ├─ tool: browse_menu   → GET  /api/v1/products
       │         └─ tool: take_payment  → POST /api/v1/payments
       │              └─ POST /api/foodpanda/jobs
       │                   └─ dispatcher agent turn
       │                        └─ tool: assign_rider   (the wait, measured)
       └─ tool: verify_order       → GET  /api/v1/orders/number/357
```

Three layers make that, and [`agent/telemetry.py`](agent/telemetry.py) sets up all
three: **Strands** emits the agent spans — the loop, each model call with its
token counts, each tool call; **httpx** is instrumented so an outbound call is a
span *and* carries the context onward; **FastAPI** so an inbound request
continues the trace rather than starting a fresh one. Miss the httpx half and an
errand is four unrelated traces.

**Each process names itself** — `friends-kitchen-ordering-agent`, `-a2a-desk`,
`-courier`, `-foodpanda-dispatcher`. Not `OTEL_SERVICE_NAME`, which the SDK would
normally read: one `.env` configures all four services here, so a name set there
would label the courier's spans as the ordering agent's. `FK_OTEL_SERVICE_PREFIX`
renames the fleet at once.

**Nothing here can fail an errand.** A collector that is down, an endpoint typed
wrong, an exporter that will not import — each is caught, reported through the
health endpoint's `telemetry` field, and the agent runs untraced. Observability
that can take the system down is worse than none.

Two routes are deliberately not traced: `/health`, because polling it every 2.5
seconds would bury the traces you want in the ones you don't, and `**/events`,
because an SSE span lasts as long as the console stays open and measures the
operator's attention rather than the agent's work — which is already traced by
the spans underneath it.

---

## Verified

Run against the live backend and frontend, without the model in the loop, to
check the tools themselves:

| Flow | Result |
|---|---|
| API: browse → add ×2 → coupon → order → redeem → pay | Order **249**, Rs 1500 coupon redeemed, Rs 685 charged, status `paid` |
| Browser: splash → menu → search → add ×2 → checkout → coupon → pay | Order **252**, Rs 1200 coupon redeemed, Rs 985 charged via the real UI |
| Budget guardrail: 2-burger order, Rs 500 limit, no coupon | Payment refused, order **250** left unpaid, `cashSpent` stayed `0` |

And with the model in the loop, for the delivery flow:

| Flow | Result |
|---|---|
| Delivery: locate → order → pay → hand over | Order **333** paid Rs 610, job `fkd_cbdf…` at `requested`, reached `delivered` on its own clock |
| Coupon **and** delivery together | Order **336**, coupon covered Rs 610, `cashSpent` stayed `0`, dispatched to "Office, Westridge" |
| Counter order (no location) | Order **335**, coupon redeemed — no location event, no delivery tools offered, tool sequence unchanged |
| Courier offline mid-errand | Order **337** still placed and paid; agent did not retry and reported "no rider" rather than delivery |
| Unpaid order offered for delivery | Refused on both sides — `arrange_delivery` and the delivery agent |
| Errand agent → the dispatcher agent on 8103 | Order **356** paid Rs 610, on the board at `accepted` awaiting a rider; rider Bilal Rehman, then `delivered` to the saved address |
| A2A negotiation → the same board | Order **357** paid Rs 610, dispatched to the saved address — no drop named, so the fallback carried it |
| Gated steps, by hand | Held at `accepted` until `find-rider`, then at `picked_up` until `deliver`; `delivered` only after both legs elapsed (70.4s) |
| Repeat request on a finished job | `409` — a page left open cannot send a rider to a delivered order |

---

## What comes next: Agent-to-Agent

Right now the agent is a customer. The natural next step is making the
restaurant an agent too, so the two can negotiate rather than one clicking
through the other's UI.

Strands ships both protocols needed for that:

- **MCP** — expose `friends-kitchen-backend` as an MCP server (menu, cart, coupons,
  orders as MCP tools). Any MCP-capable agent could then order without knowing
  anything about Friends Kitchen's HTTP API. This is the smaller step, and it
  mostly means re-hosting `agent/tools/api_tools.py` behind an MCP server.
- **A2A** — give Friends Kitchen its own agent card and let this agent discover and
  negotiate with it: *"here is my budget and my dietary constraints, what can
  you do?"* That is where the coupon stops being a code typed into a box and
  starts being a credential presented between agents.

Neither changes the shape of what is here: the wallet stays the boundary, and
the tools stay the vocabulary.
"# friends-kitchen-agent-backend" 
