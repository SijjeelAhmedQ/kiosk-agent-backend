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

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent import kiosk_api
from agent.config import settings
from agent.tools import api_tools, browser_tools
from agent.wallet import wallet

app = FastAPI(
    title="Friends Kitchen Ordering Agent",
    version="1.0.0",
    description="Runs the ordering agent and streams its progress to the kiosk UI.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # The agent's own control panel (kiosk-agent-ui).
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        # The kiosk itself, so it could embed a status widget later.
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
class StartRunIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    couponCode: str | None = Field(default=None, max_length=40)
    cashLimit: float = Field(default=0, ge=0, le=1_000_000)
    mode: Literal["api", "browser"] = "api"
    customerId: str | None = Field(default=None, max_length=50)
    headless: bool = True


@dataclass
class Run:
    id: str
    instruction: str
    mode: str
    status: str = "queued"          # queued | running | done | failed | cancelled
    events: list[dict] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None

    def emit(self, event: dict) -> None:
        """Record and publish. Recording is what lets a browser that reconnects
        (or opens the page late) still see the whole trace."""
        self.events.append(event)
        self.queue.put_nowait(event)


_runs: dict[str, Run] = {}


# --------------------------------------------------------------------------- #
# Turning Strands events into something a UI can render
# --------------------------------------------------------------------------- #
def _tool_summary(result: Any) -> tuple[bool, str]:
    """Boil a tool's return value down to a line of status text."""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False, str(result.get("error", "failed"))
        for key in ("orderNumber", "matched", "added", "applied", "paid", "valid"):
            if key in result:
                return True, f"{key}: {result[key]}"
        return True, "ok"
    return True, str(result)[:200]


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
        ok, summary = _tool_summary(payload)
        if result.get("status") == "error":
            ok = False
        out.append({"toolUseId": result.get("toolUseId"), "ok": ok, "summary": summary})
    return out


async def _drive(run: Run, payload: StartRunIn) -> None:
    """Run one errand, publishing events as it goes."""
    async with _run_lock:
        run.status = "running"
        run.emit({"type": "status", "status": "running"})

        # Fresh wallet and empty cart — this process outlives any single errand.
        wallet.reset(payload.couponCode, payload.cashLimit, payload.customerId)
        api_tools.reset()
        browser_tools.reset()

        try:
            from agent.kiosk_agent import build_agent

            agent = build_agent(wallet, mode=payload.mode, callback_handler=None)

            if payload.mode == "browser":
                # Launch before the model starts so a failure here is reported
                # as "could not open the browser", not as a confusing tool error.
                from agent.browser.kiosk_driver import browser

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
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.emit({"type": "error", "message": str(exc)})
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
    from agent.kiosk_agent import credentials_ready

    ready, problem = credentials_ready()

    return {
        "success": True,
        "data": {
            "agent": "ok",
            "restaurantApi": kiosk_api.health(),
            # Named for the provider-agnostic question the UI actually asks:
            # "can this thing run?" A local Ollama needs no key and is ready.
            "hasApiKey": ready,
            "credentialProblem": problem,
            "provider": settings.provider,
            "model": settings.model_id,
            "kioskWeb": settings.web_base,
            "busy": _run_lock.locked(),
        },
    }


@app.get("/api/agent/coupons")
def spendable_coupons() -> dict:
    """Coupons the agent could actually spend, for the control panel's picker.

    Proxied rather than fetched from the browser: the kiosk API only allows the
    kiosk's own origin, and widening its CORS so a second app can read it is a
    bigger change than forwarding one read from here — where the restaurant's
    address is already configured.
    """
    items: list[dict] = []
    for status in ("unused", "partially_redeemed"):
        try:
            page = kiosk_api.get("/coupons", status=status, limit=100)
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
        # Replay first: the UI subscribes a beat after POSTing, and without this
        # the opening events are gone by the time it connects.
        for event in list(run.events):
            yield {"data": json.dumps(event)}
            if event.get("type") == "end":
                return

        while True:
            try:
                event = await asyncio.wait_for(run.queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            yield {"data": json.dumps(event)}
            if event.get("type") == "end":
                return

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
