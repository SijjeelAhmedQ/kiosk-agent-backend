"""HTTP front door for the ordering agent, so the React UI can drive it.

    .venv\\Scripts\\python -m uvicorn server:app --port 8100 --reload

The shape is: `POST /api/agent/runs` starts an errand and hands back a run id;
`GET /api/agent/runs/{id}/events` streams what the agent is doing as
Server-Sent Events. The UI shows that trace live rather than making someone
watch a terminal.

**Runs are serialised, deliberately.** The wallet, the cart and the browser are
one-per-process singletons — that is the right model for "one agent, one errand"
and the wrong one for concurrency. Rather than pretend otherwise, a second run
waits for the first to finish. The UI is told it is queued.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent import branches, friends_kitchen_api, location
from agent.config import settings
from agent.delivery import registry as delivery_registry
from agent.location import InvalidLocation
from agent.tools import api_tools, browser_tools, delivery_tools
from agent.wallet import wallet

app = FastAPI(
    title="Friends Kitchen Ordering Agent",
    version="1.0.0",
    description="Runs the ordering agent and streams its progress to the Friends Kitchen UI.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # The agent's own control panel (friends-kitchen-agent-frontend).
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        # Friends Kitchen itself, so it could embed a status widget later.
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# One errand at a time — see the module docstring.
_run_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #
class UserLocationIn(BaseModel):
    """Where the customer is, as the browser reports it.

    Optional on the run, and its absence is not an error: an errand without a
    location is the counter order this service has always placed. Its *presence*
    turns the errand into a delivery.
    """

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracyMeters: float | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    source: Literal["browser", "manual"] = "browser"


class StartRunIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    couponCode: str | None = Field(default=None, max_length=40)
    cashLimit: float = Field(default=0, ge=0, le=1_000_000)
    mode: Literal["api", "browser"] = "api"
    customerId: str | None = Field(default=None, max_length=50)
    headless: bool = True
    userLocation: UserLocationIn | None = None


@dataclass
class Run:
    id: str
    instruction: str
    mode: str
    status: str = "queued"          # queued | running | done | failed | cancelled
    events: list[dict] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    # One queue per listener, not one per run: a single shared queue means two
    # tabs steal each other's events, and replaying the backlog to a listener
    # whose queue already holds it sends everything twice.
    listeners: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None

    def emit(self, event: dict) -> None:
        """Record and publish. Recording is what lets a browser that reconnects
        (or opens the page late) still see the whole trace."""
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)

    def attach(self) -> tuple[list[dict], asyncio.Queue]:
        """Take the backlog and a live feed in one go.

        Together, and with no `await` between them, is the point: that is what
        makes "everything so far" and "everything from now on" meet exactly
        once. Snapshotting and subscribing as two steps is what used to
        duplicate every event emitted before the UI connected.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self.listeners.append(queue)
        return list(self.events), queue

    def detach(self, queue: asyncio.Queue) -> None:
        """Stop feeding a listener that has gone away, so a page left open
        through a dozen reconnects does not accumulate queues forever."""
        with suppress(ValueError):
            self.listeners.remove(queue)


_runs: dict[str, Run] = {}


# --------------------------------------------------------------------------- #
# Turning Strands events into something a UI can render
# --------------------------------------------------------------------------- #
# A browse of the whole menu hands back forty products, and the trace shows the
# first few of any list. The rest is bandwidth spent on something nobody sees.
_LIST_CAP = 6


def _shrink(value: Any) -> Any:
    """Cut a tool result down to what a timeline row can hold.

    Lists are capped and long strings clipped. The counts a result carries
    alongside its list (`matched`, `itemCount`) are left alone, so the UI can
    still say "and 34 more" off a list it only received the head of.
    """
    if isinstance(value, dict):
        return {key: _shrink(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink(item) for item in value[:_LIST_CAP]]
    if isinstance(value, str):
        return value[:400]
    return value


def _tool_detail(payload: Any) -> dict | None:
    """A tool's return value as a dict the UI can read field by field.

    Strands puts tool results on the wire as text, so a tool that returned a
    dict arrives here as a JSON string. Parsing it back is what lets the trace
    say "Big Mac® × 1 — Rs 530" rather than printing the JSON at the operator.
    """
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{"):
            with suppress(json.JSONDecodeError):
                payload = json.loads(text)
    return _shrink(payload) if isinstance(payload, dict) else None


def _tool_summary(result: Any) -> tuple[bool, str]:
    """Boil a tool's return value down to a line of status text.

    The UI writes its own sentence from `detail` when it recognises the tool, so
    this is the fallback for anything it does not — and, more importantly, the
    channel a refusal comes down: the wallet turning a payment away says why
    here.
    """
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False, str(result.get("error", "failed"))
        for key in ("orderNumber", "matched", "added", "applied", "paid", "valid"):
            if key in result:
                return True, f"{key}: {result[key]}"
        return True, "ok"
    return True, str(result)[:200]


def _explain(exc: Exception) -> str:
    """Say what went wrong in a sentence the operator can act on.

    A run dies most often at the provider, not in the tools — a free tier out of
    tokens, a key with no credit. Those arrive as one useful sentence wrapped in
    a stringified error body, and putting the whole body on screen buries it.
    """
    text = str(exc)

    # OpenAI-compatible clients (Groq included) stringify failures as
    # "Error code: 429 - {'error': {'message': '…', 'code': '…'}}" — a Python
    # repr rather than JSON, so json.loads is the wrong tool here.
    match = re.search(r"Error code: \d+ - (\{.*\})\s*$", text, re.DOTALL)
    if match:
        with suppress(Exception):
            error = ast.literal_eval(match.group(1))["error"]
            message = error.get("message")
            if message and error.get("code") == "rate_limit_exceeded":
                # The limit is per model, so the fix is usually a model id
                # rather than a wait — worth saying, since it is not obvious.
                return (
                    f"{message} This budget is per model: either wait it out, or point "
                    "AGENT_MODEL at a model that still has room and restart the agent."
                )
            if message:
                return message

    return text


def _extract_tool_results(message: dict) -> list[dict]:
    """Pull toolResult blocks out of a Strands message.

    Tool results come back on the *user* turn, which is how the agent loop feeds
    them to the model. That is also the only place the outcome of a call is
    visible, so it is where the UI's green ticks and red crosses come from.
    """
    out = []
    for block in message.get("content", []) or []:
        result = block.get("toolResult") if isinstance(block, dict) else None
        if not result:
            continue
        payload: Any = None
        for item in result.get("content", []) or []:
            if "json" in item:
                payload = item["json"]
                break
            if "text" in item:
                payload = item["text"]
                break
        detail = _tool_detail(payload)
        ok, summary = _tool_summary(detail if detail is not None else payload)
        if result.get("status") == "error":
            ok = False
        out.append(
            {
                "toolUseId": result.get("toolUseId"),
                "ok": ok,
                "summary": summary,
                "detail": detail,
            }
        )
    return out


async def _drive(run: Run, payload: StartRunIn) -> None:
    """Run one errand, publishing events as it goes."""
    async with _run_lock:
        run.status = "running"
        run.emit({"type": "status", "status": "running"})

        # Fresh wallet, empty cart, no leftover location or delivery — this
        # process outlives any single errand, and yesterday's customer must not
        # receive today's order.
        wallet.reset(payload.couponCode, payload.cashLimit, payload.customerId)
        api_tools.reset()
        browser_tools.reset()
        delivery_tools.reset()
        location.reset()

        try:
            from agent.friends_kitchen_agent import build_agent

            # Validated at the edge, before the model sees anything. A bad fix
            # becomes a failed run with a sentence, rather than an order placed
            # at the default branch while the operator believes otherwise.
            user_location = (
                location.parse(payload.userLocation.model_dump())
                if payload.userLocation
                else None
            )
            location.remember(user_location)

            if user_location is not None:
                branch, distance_km = branches.nearest(user_location)
                run.emit(
                    {
                        "type": "location",
                        "userLocation": user_location.to_view(),
                        "restaurant": branch.to_view(),
                        "distanceKm": distance_km,
                        "deliveryService": delivery_registry.get().display_name,
                    }
                )

            agent = build_agent(
                wallet,
                mode=payload.mode,
                callback_handler=None,
                deliver_to=user_location,
            )

            if payload.mode == "browser":
                # Launch before the model starts so a failure here is reported
                # as "could not open the browser", not as a confusing tool error.
                from agent.browser.friends_kitchen_driver import browser

                await asyncio.to_thread(browser.start, payload.headless, 250)
                run.emit({"type": "browser", "state": "opened", "headless": payload.headless})

            announced: set[str] = set()

            async for event in agent.stream_async(payload.instruction):
                tool_use = event.get("current_tool_use") or {}
                tool_id = tool_use.get("toolUseId")
                if tool_id and tool_id not in announced and tool_use.get("name"):
                    announced.add(tool_id)
                    run.emit(
                        {
                            "type": "tool",
                            "toolUseId": tool_id,
                            "name": tool_use["name"],
                        }
                    )

                text = event.get("data")
                if text:
                    run.emit({"type": "text", "text": text})

                message = event.get("message")
                if isinstance(message, dict):
                    for result in _extract_tool_results(message):
                        run.emit({"type": "tool_result", **result})

                result = event.get("result")
                if result is not None:
                    run.final_text = str(result).strip()

            run.status = "done"
            run.emit(
                {
                    "type": "final",
                    "text": run.final_text,
                    "wallet": wallet.summary(),
                }
            )

        except asyncio.CancelledError:
            run.status = "cancelled"
            run.emit({"type": "status", "status": "cancelled"})
            raise
        except InvalidLocation as exc:
            # Its own branch because it is the one failure here that is the
            # caller's to fix, and `_explain` would only wrap a sentence that
            # is already written for a person.
            run.status = "failed"
            run.error = f"That is not a usable delivery location: {exc}"
            run.emit({"type": "error", "message": run.error})
        except Exception as exc:
            run.status = "failed"
            run.error = _explain(exc)
            run.emit({"type": "error", "message": run.error})
        finally:
            if payload.mode == "browser":
                with suppress(Exception):
                    await asyncio.to_thread(browser_tools.reset)
                run.emit({"type": "browser", "state": "closed"})
            run.emit({"type": "end"})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/agent/health")
def health() -> dict:
    """Everything the UI needs to tell the operator what is not ready."""
    from agent.friends_kitchen_agent import credentials_ready

    ready, problem = credentials_ready()

    return {
        "success": True,
        "data": {
            "agent": "ok",
            "restaurantApi": friends_kitchen_api.health(),
            # Named for the provider-agnostic question the UI actually asks:
            # "can this thing run?" A local Ollama needs no key and is ready.
            "hasApiKey": ready,
            "credentialProblem": problem,
            "provider": settings.provider,
            "model": settings.model_id,
            "friendsKitchenWeb": settings.web_base,
            "busy": _run_lock.locked(),
            # Which courier a delivery would go to, and whether it could. Never
            # a credential — the answer is "a key is present", not what it is.
            "delivery": delivery_registry.describe(),
            "branches": len(branches.BRANCHES),
        },
    }


@app.get("/api/agent/branches")
def branch_list() -> dict:
    """Where the restaurant has counters a courier can collect from.

    The control panel shows these so an operator can see which branch a
    customer's location resolved to, rather than taking the agent's word for it.
    """
    return {
        "success": True,
        "data": {"items": [branch.to_view() for branch in branches.BRANCHES]},
    }


# Spendable first, then the spent ones in the order the picker greys them out.
# Friends Kitchen computes these, and they are mutually exclusive — a coupon appears
# under exactly one of them, so this never returns the same code twice.
_COUPON_STATUSES = ("unused", "partially_redeemed", "fully_redeemed", "expired", "cancelled")


@app.get("/api/agent/coupons")
def coupons() -> dict:
    """Every coupon the restaurant has, for the control panel's picker.

    Spent, expired and cancelled ones come back too: the picker shows them
    greyed out with the reason, which answers "why is this code not working?"
    where hiding them only raises the question.

    Proxied rather than fetched from the browser: the Friends Kitchen API only allows the
    Friends Kitchen's own origin, and widening its CORS so a second app can read it is a
    bigger change than forwarding one read from here — where the restaurant's
    address is already configured.
    """
    items: list[dict] = []
    for status in _COUPON_STATUSES:
        try:
            page = friends_kitchen_api.get("/coupons", status=status, limit=100)
            items.extend(page.get("items", []))
        except Exception:
            # A missing picker is a nuisance; a 500 here would block the page.
            continue

    keep = (
        "couponCode", "couponType", "status", "remainingBalance",
        "originalAmount", "productName", "expiryDate",
    )
    return {
        "success": True,
        "data": {"items": [{k: c.get(k) for k in keep} for c in items]},
    }


@app.post("/api/agent/runs", status_code=201)
async def start_run(payload: StartRunIn) -> dict:
    if not payload.couponCode and payload.cashLimit <= 0:
        raise HTTPException(
            status_code=422,
            detail="Give the agent a coupon, a cash limit, or both — it cannot buy anything with neither.",
        )

    run = Run(id=uuid.uuid4().hex[:12], instruction=payload.instruction, mode=payload.mode)
    _runs[run.id] = run
    run.emit({"type": "status", "status": "queued", "queued": _run_lock.locked()})
    run.task = asyncio.create_task(_drive(run, payload))

    return {"success": True, "data": {"runId": run.id, "status": run.status}}


@app.get("/api/agent/runs/{run_id}/events")
async def stream_run(run_id: str):
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")

    async def publisher():
        # Backlog first: the UI subscribes a beat after POSTing, and without
        # this the opening events are gone by the time it connects.
        backlog, queue = run.attach()
        try:
            for event in backlog:
                yield {"data": json.dumps(event)}
                if event.get("type") == "end":
                    return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"data": json.dumps(event)}
                if event.get("type") == "end":
                    return
        finally:
            run.detach(queue)

    return EventSourceResponse(publisher())


@app.get("/api/agent/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    return {
        "success": True,
        "data": {
            "runId": run.id,
            "status": run.status,
            "instruction": run.instruction,
            "mode": run.mode,
            "finalText": run.final_text,
            "error": run.error,
            "wallet": wallet.summary(),
            "events": run.events,
        },
    }


@app.post("/api/agent/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    if run.task and not run.task.done():
        run.task.cancel()
    return {"success": True, "data": {"runId": run.id, "status": "cancelling"}}
