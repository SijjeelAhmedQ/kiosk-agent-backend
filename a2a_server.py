"""HTTP front door for agent-to-agent ordering.

    .venv\\Scripts\\python -m uvicorn a2a_server:app --port 8101 --reload

Its own app on its own port, next to — never inside — the errand service on
8100. That is a deliberate cost: two processes to start instead of one. What it
buys is a guarantee that is otherwise only a promise, because the cart, the
wallet, the placed order and the browser are all module-level singletons in this
codebase. Separate processes mean separate singletons, so an A2A negotiation
cannot reach into an errand that is running next door, and this service falling
over is not something 8100 can even observe.

Two families of route, and the split matters:

* `/.well-known/agent-card.json` and `/api/a2a/merchant/*` are **the protocol**.
  This is the restaurant's agent, reachable by anything that can speak A2A. The
  buyer here is only its first client.
* `/api/a2a/runs/*` is **the console**. It sends the buyer out and streams both
  sides of the resulting conversation into one transcript.

The buyer reaches the merchant through the first family, over real HTTP, even
though today both live in this process. See agent/a2a/merchant_client.py.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from agent import console, friends_kitchen_api, location, telemetry
from agent.a2a import tasks as store
from agent.a2a.buyer_agent import run_errand
from agent.a2a.config import a2a_settings
from agent.a2a.merchant_agent import take_turn
from agent.a2a.models import credentials_ready
from agent.a2a.protocol import (
    CANCELLED,
    FAILED,
    MerchantMessageIn,
    MerchantTaskIn,
    Message,
    StartRunIn,
    agent_card,
    event_error,
    event_message,
    event_status,
)
from agent.a2a.tasks import ConsoleRun, MerchantTask

app = FastAPI(
    title="Friends Kitchen A2A",
    version="1.0.0",
    description=(
        "Agent-to-agent ordering. A buyer agent negotiates with the restaurant's "
        "own agent and pays out of a wallet it cannot exceed."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # The A2A console, served alongside the errand UI at /a2a.html.
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        # Friends Kitchen, so it could show a negotiation later without a CORS change.
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Tracing, if the operator asked for it. Off unless FK_OTEL is set, and it never
# raises — see agent/telemetry.py. Called here rather than in a startup hook so
# httpx is instrumented before any module-level client can be built.
telemetry.setup("a2a-desk", app)

# The process console: every line this service produces, on a stream anybody may
# read — `/api/a2a/console` for the scrollback, `/api/a2a/console/events`
# to follow it live. Installed next to tracing and for the same reason it sits
# here rather than in a startup hook: the logging handler has to be on the root
# logger before anything at module scope has a chance to log.
console.install("a2a", "buyer")
console.mount(app, "/api/a2a")


def _ok(data) -> dict:
    """The envelope every endpoint in this system answers with."""
    return {"success": True, "data": data}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@app.get("/.well-known/agent-card.json")
def card() -> dict:
    """Who the merchant is. Fetched by the buyer before it says anything.

    Unwrapped, unlike everything else here: the well-known URL is the one thing
    a stranger's agent reads, and it should be the card the spec describes
    rather than this project's envelope around one.
    """
    return agent_card()


@app.get("/api/a2a/health")
def health() -> dict:
    """What is and is not ready, for the console to say out loud.

    Both sides are reported separately because they are configured separately:
    "no API key" is useless advice when there are two agents and only one of
    them is missing one.
    """
    buyer_ready, buyer_problem = credentials_ready(
        a2a_settings.buyer_provider, a2a_settings.buyer_model, "buyer"
    )
    merchant_ready, merchant_problem = credentials_ready(
        a2a_settings.merchant_provider, a2a_settings.merchant_model, "merchant"
    )

    return _ok(
        {
            "a2a": "ok",
            "restaurantApi": friends_kitchen_api.health(),
            "buyer": {
                "provider": a2a_settings.buyer_provider,
                "model": a2a_settings.buyer_model,
                "ready": buyer_ready,
                "problem": buyer_problem,
            },
            "merchant": {
                "provider": a2a_settings.merchant_provider,
                "model": a2a_settings.merchant_model,
                "hands": a2a_settings.merchant_hands,
                "ready": merchant_ready,
                "problem": merchant_problem,
            },
            "busy": _browser_lock.locked(),
            # The customer's saved address, so the console's "Where it goes" can
            # offer "deliver to my address" as one click rather than a permission
            # prompt — and so that the same address the handover falls back to is
            # the one shown on screen. Served from here rather than written into
            # the UI so there is one copy of it: change FK_CUSTOMER_ADDRESS and
            # both consoles follow.
            "customer": location.saved().to_view(),
            # Where this process's spans go, if anywhere. Never a credential:
            # OTEL_EXPORTER_OTLP_HEADERS usually carries a token, so the answer
            # is whether headers are set, not what is in them.
            "telemetry": telemetry.describe(),
        }
    )


# Spendable first, then the spent ones in the order the picker greys them out.
# Friends Kitchen computes these and they are mutually exclusive, so a coupon appears
# under exactly one — this never returns the same code twice.
_COUPON_STATUSES = ("unused", "partially_redeemed", "fully_redeemed", "expired", "cancelled")


@app.get("/api/a2a/coupons")
def coupons() -> dict:
    """Every coupon the restaurant has, for the console's picker.

    Proxied rather than fetched from the browser, because the Friends Kitchen API allows
    only Friends Kitchen's own origin. The errand service on 8100 forwards this same
    read for its own console; this service does its own so that the A2A console
    works whether or not 8100 is running.
    """
    items: list[dict] = []
    for status in _COUPON_STATUSES:
        try:
            page = friends_kitchen_api.get("/coupons", status=status, limit=100)
            items.extend(page.get("items", []))
        except Exception:  # noqa: BLE001 — a missing picker is a nuisance, a 500 is a wall
            continue

    keep = (
        "couponCode", "couponType", "status", "remainingBalance",
        "originalAmount", "productName", "expiryDate",
    )
    return _ok({"items": [{k: c.get(k) for k in keep} for c in items]})


# --------------------------------------------------------------------------- #
# The protocol — the merchant's endpoint
# --------------------------------------------------------------------------- #
@app.post("/api/a2a/merchant/tasks", status_code=201)
async def open_task(payload: MerchantTaskIn) -> dict:
    """A buyer opens a conversation."""
    task = MerchantTask(buyer_id=payload.buyerId)
    store.merchant_tasks.put(task.id, task)
    return _ok(await _run_turn(task, payload.message, payload.data, payload.messageId))


@app.post("/api/a2a/merchant/tasks/{task_id}/messages")
async def send_message(task_id: str, payload: MerchantMessageIn) -> dict:
    """A buyer says the next thing."""
    task = store.merchant_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task.")
    if task.done:
        raise HTTPException(
            status_code=409,
            detail=f"This conversation is already {task.state}. Open a new task.",
        )
    if not payload.message and not payload.data:
        raise HTTPException(status_code=422, detail="Send text, data, or both — not neither.")

    return _ok(await _run_turn(task, payload.message, payload.data, payload.messageId))


async def _run_turn(
    task: MerchantTask,
    message: str,
    data: dict | None,
    message_id: str | None = None,
) -> dict:
    """Record what the buyer said, let the merchant answer, hand back the reply.

    The turn lock is per task, so two merchants can be thinking about two
    different orders at once — but one buyer cannot have the merchant working on
    two of its own messages, which would interleave edits to a single cart.
    """
    async with task.turn_lock:
        before = len(task.artifacts)
        said_before = len(task.messages)
        incoming = task.record(Message.say("buyer", message, data, message_id))
        task.stream.emit(event_message(incoming))

        try:
            await take_turn(task, incoming)
        except Exception as exc:  # noqa: BLE001 — the buyer needs a reason, not a traceback
            task.state = FAILED
            task.error = str(exc)
            task.stream.emit(event_error(task.error))
            task.stream.emit(event_status(FAILED))

        if task.done:
            store.close(task.stream)

        return task.latest_reply(artifacts_since=before, messages_since=said_before)


@app.get("/api/a2a/merchant/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = store.merchant_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return _ok(task.to_wire())


@app.get("/api/a2a/merchant/tasks/{task_id}/events")
async def stream_task(task_id: str):
    """Watch one conversation from the merchant's side.

    Not what the console uses — that follows the run, which carries both sides.
    This is here because an A2A merchant that cannot be observed by its own
    client is only half an implementation, and it is what to open when the
    question is "what did the merchant actually think it was told?".
    """
    task = store.merchant_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return EventSourceResponse(store.publish(task.stream))


# --------------------------------------------------------------------------- #
# The console — sending the buyer out
# --------------------------------------------------------------------------- #
# Only browser hands need serialising: Chromium is one per process, and two
# negotiations driving the same window would fight over it. With API hands each
# task carries its own cart, so runs may overlap.
_browser_lock = asyncio.Lock()


async def _drive(run: ConsoleRun, payload: StartRunIn) -> None:
    """Own the asyncio-shaped concerns so the buyer agent does not have to."""
    if a2a_settings.merchant_hands == "browser":
        await _browser_lock.acquire()
    try:
        await run_errand(run, payload)
    except asyncio.CancelledError:
        run.status = "cancelled"
        run.stream.emit(event_status(CANCELLED))
        raise
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = str(exc)
        run.stream.emit(event_error(run.error))
        run.stream.emit(event_status(FAILED))
    finally:
        if a2a_settings.merchant_hands == "browser":
            # Close the window before releasing the lock, so the next run opens
            # a clean one rather than inheriting a basket. Best effort: a
            # browser that has already died must not turn a finished run into a
            # failed one.
            with suppress(Exception):
                from agent.browser.friends_kitchen_driver import browser

                await asyncio.to_thread(browser.close)
            run.stream.emit({"type": "browser", "state": "closed"})
            if _browser_lock.locked():
                _browser_lock.release()
        store.close(run.stream)


@app.post("/api/a2a/runs", status_code=201)
async def start_run(payload: StartRunIn) -> dict:
    if not payload.couponCode and payload.cashLimit <= 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "Give the buyer a coupon, a cash limit, or both — it cannot "
                "negotiate with neither."
            ),
        )

    # Validated at the edge, before a run exists to carry it. A swapped latitude
    # and longitude is a delivery to the wrong hemisphere, so a fix that is not a
    # place on Earth is a 422 here rather than an address the operator believes in
    # and a courier is sent to.
    try:
        drop = location.parse(payload.userLocation.model_dump()) if payload.userLocation else None
    except location.InvalidLocation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    run = ConsoleRun(
        instruction=payload.instruction,
        drop=drop,
        # The switch, not the address. `drop` is where it goes; this is whether
        # the customer asked for it to come to them — and it is what stops the
        # delivery agent asking them again at the far end of the handover.
        where_it_goes=payload.whereItGoes,
    )
    store.console_runs.put(run.id, run)
    run.stream.emit(event_status("queued"))
    run.task = asyncio.create_task(_drive(run, payload))

    return _ok({"runId": run.id, "status": run.status})


@app.get("/api/a2a/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.console_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    return _ok(run.to_wire())


@app.get("/api/a2a/runs/{run_id}/events")
async def stream_run(run_id: str):
    run = store.console_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    return EventSourceResponse(store.publish(run.stream))


@app.post("/api/a2a/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    run = store.console_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    if run.task and not run.task.done():
        run.task.cancel()
    return _ok({"runId": run.id, "status": "cancelling"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("a2a_server:app", host="0.0.0.0", port=a2a_settings.port, reload=True)
