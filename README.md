# Friends Kitchen — Ordering Agent

A standalone AI agent that goes to Friends Kitchen and places an order for you,
carrying a coupon and a spending limit.

There are two ways to drive it — a control panel, or the command line.

**The UI** (recommended). Start the server here, then run the panel:

```bash
.venv\Scripts\python -m uvicorn server:app --port 8100
```

…and `npm run dev` in **`kiosk-agent-ui`** (port 5174). Write the errand, pick a
coupon, watch the agent work. See [that app's README](../kiosk-agent-ui/README.md).

**The CLI**, for scripting and quick checks:

```bash
python run.py "Order two cheeseburgers" --coupon AGENT-8247EA9611 --limit 3000
```

It is a separate application from the kiosk. It has its own process, its own
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
# Backend — from kiosk-backend/
.venv\Scripts\python -m uvicorn app.main:app --port 8000

# Frontend — from kiosk-frontend/, only needed for --mode browser
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
needs its own. `AGENT_PROVIDER` picks which one: `groq` is the free default
here (a key from [console.groq.com](https://console.groq.com/keys), no card),
and `anthropic` is the best tool-caller if you have credits at
[console.anthropic.com](https://console.anthropic.com/settings/keys). `gemini`
and `ollama` also work; see `.env.example` for all five.

### 4. Give it something to spend

Any coupon from the kiosk's own admin screens works. To mint one from the
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

Tools drive the React kiosk in Chromium via Playwright — tapping the welcome
screen, typing in the search box, opening a product, answering "Make it a
meal?", typing the coupon into the coupon field, and pressing Pay. Nothing
touches the API directly.

This is the one to demo. It is slower and it is doing strictly more work, but
it proves the point the brief actually made: *the agent goes to your website and
uses it.*

The browser tools are semantic (`search_menu`, `add_to_cart`, `pay`) rather than
pixel-level. The model decides *what* to do; `agent/browser/kiosk_driver.py`
knows *where* things are, keyed off `data-testid` attributes in the frontend.
That keeps runs reproducible — a restyle cannot break the agent, only a
deliberate change to those hooks can.

---

## How it is put together

```
run.py                       CLI — parses the errand, fills the wallet, starts the agent
server.py                    HTTP front door: starts runs, streams them as SSE (for the UI)
agent/
  config.py                  Environment-driven settings
  wallet.py                  The token and the cash ceiling. Enforced, not suggested.
  prompts.py                 The brief the agent is given
  kiosk_agent.py             Strands Agent + Anthropic model assembly
  kiosk_api.py               HTTP client for the kiosk API
  cart.py                    Client-side cart (the API has no cart endpoint)
  tools/
    api_tools.py             Ordering via REST
    browser_tools.py         Ordering via the website
  browser/
    kiosk_driver.py          Playwright page-object for the kiosk UI
```

**Framework.** [Strands Agents](https://strandsagents.com) — AWS's open-source
agent SDK. It supplies the agent loop (call the model, run the tool, feed the
result back, repeat); everything above is tools and a prompt. It is
model-agnostic and has MCP and A2A support built in, which is what makes the
next phase cheap.

**Model.** `claude-opus-5` by default, because ordering is a multi-step tool-use
loop where a wrong step spends real money. Set `AGENT_MODEL=claude-sonnet-5` in
`.env` for cheaper demo runs.

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
  `X-Kiosk-Id: agent-01` (configurable), so the admin screens can tell agent
  traffic from walk-in traffic.

---

## Verified

Run against the live backend and frontend, without the model in the loop, to
check the tools themselves:

| Flow | Result |
|---|---|
| API: browse → add ×2 → coupon → order → redeem → pay | Order **249**, Rs 1500 coupon redeemed, Rs 685 charged, status `paid` |
| Browser: splash → menu → search → add ×2 → checkout → coupon → pay | Order **252**, Rs 1200 coupon redeemed, Rs 985 charged via the real UI |
| Budget guardrail: 2-burger order, Rs 500 limit, no coupon | Payment refused, order **250** left unpaid, `cashSpent` stayed `0` |

---

## What comes next: Agent-to-Agent

Right now the agent is a customer. The natural next step is making the
restaurant an agent too, so the two can negotiate rather than one clicking
through the other's UI.

Strands ships both protocols needed for that:

- **MCP** — expose `kiosk-backend` as an MCP server (menu, cart, coupons,
  orders as MCP tools). Any MCP-capable agent could then order without knowing
  anything about Friends Kitchen's HTTP API. This is the smaller step, and it
  mostly means re-hosting `agent/tools/api_tools.py` behind an MCP server.
- **A2A** — give the kiosk its own agent card and let this agent discover and
  negotiate with it: *"here is my budget and my dietary constraints, what can
  you do?"* That is where the coupon stops being a code typed into a box and
  starts being a credential presented between agents.

Neither changes the shape of what is here: the wallet stays the boundary, and
the tools stay the vocabulary.
"# kiosk-agent-backend" 
