"""The delivery agent — the second half of `order → deliver`.

    .venv\\Scripts\\python delivery_server.py
    .venv\\Scripts\\python -m uvicorn delivery_server:app --port 8102 --reload

Either way it answers on 8102 — running the file needs no port because the
port is not a choice this service leaves open; see `PORT` at the bottom.

Its own app on its own port, for the same reason the A2A merchant on 8101 is
not inside the errand server on 8100: the ordering agent reaches this over real
HTTP, so the handover is an agent-to-agent call that would work just as well if
this process were a courier company's own service on the other side of the
country. Nothing here can see the cart, the wallet or the placed order — it
knows only what the request carried, which is exactly the boundary the contract
in `agent/delivery/contract.py` describes.

Its card is at `/.well-known/agent-card.json`, so the ordering side discovers
what it can be asked for rather than being told in a prompt.

**What it is honest about.** This service dispatches to a simulated courier: it
accepts a job, assigns a rider, and moves the job through pickup and transit on
a clock. It is not connected to a vehicle. What it is *not* is a service that
lies about the outcome — a job reaches `delivered` only after the full journey
has elapsed, `POST` never returns `delivered`, and a request it cannot honour
comes back as a refusal rather than a fake success. Point
`DELIVERY_PROVIDER=foodpanda` at a real courier API and the ordering side does
not change; only which service answers these calls does.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# The only thing this service borrows from the ordering side, and it is
# infrastructure rather than domain: agent/telemetry.py reads two environment
# variables and installs a tracer. It cannot see the cart, the wallet or the
# placed order, so the boundary this file's docstring describes still holds.
from agent import console, telemetry
from agent.llm import api as llm_api


async def _watch_the_clock() -> None:
    """Say out loud what this service's jobs are doing while nobody is asking.

    Every other agent on this floor emits as it works, because it is doing the
    work. This one does not work at all — a job's status is a function of how
    long ago it was taken (see `Job.status`), which is what makes it a courier
    with no scheduler to fall behind. The cost is that a rider can collect an
    order and set off and arrive without the process ever having said anything.

    So the console gets its own reader of that clock: one tick a second over the
    handful of live jobs, one line each time a job's derived status changes. It
    announces rather than decides — nothing here can move a delivery, and if
    this task were to die the deliveries would carry on exactly as before.
    """
    seen: dict[str, str] = {}
    while True:
        with suppress(Exception):
            for job in list(_jobs.values()):
                status = job.status
                if seen.get(job.id) == status:
                    continue
                # A job present at startup is not news: reporting the status it
                # is already in would fill the log with a history nobody watched.
                if job.id in seen:
                    console.say(
                        f"{job.order_number}: {job.message}",
                        kind="delivery",
                        ref=job.id,
                        level="warn" if status == "cancelled" else "info",
                        data={"status": status, "orderNumber": job.order_number},
                    )
                seen[job.id] = status
            for gone in set(seen) - set(_jobs):
                seen.pop(gone, None)
        await asyncio.sleep(1)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    ticker = asyncio.create_task(_watch_the_clock())
    try:
        yield
    finally:
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker


app = FastAPI(
    title="Friends Kitchen Delivery Agent",
    version="1.0.0",
    description=(
        "Takes a confirmed restaurant order, collects it, and delivers it to "
        "the customer. Speaks the delivery contract the ordering agent uses."
    ),
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # The agent control panel, so it can show a delivery without a proxy.
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Tracing, if the operator asked for it. Off unless FK_OTEL is set, and it never
# raises — see agent/telemetry.py.
telemetry.setup("courier", app)

# The process console: every line this service produces, on a stream anybody may
# read — `/api/delivery/console` for the scrollback, `/api/delivery/console/events`
# to follow it live. Installed next to tracing and for the same reason it sits
# here rather than in a startup hook: the logging handler has to be on the root
# logger before anything at module scope has a chance to log.
console.install("courier", "courier")
console.mount(app, "/api/delivery")

# The LLM configuration endpoints, mounted the way the console's are and for the
# same reason: they are identical in every service, the selection behind them is
# a file all four processes read, and a screen that could only be reached while
# one particular service happened to be up would be the wrong place to fix a
# configuration problem. See `agent/llm/api.py`.
llm_api.mount(app, "/api/llm")


def _ok(data: Any) -> dict:
    """The envelope every service in this system answers with."""
    return {"success": True, "data": data}


# --------------------------------------------------------------------------- #
# What a request has to contain
# --------------------------------------------------------------------------- #
# Pydantic does the checking the ordering agent's contract already did, again.
# That is deliberate: this is a network boundary, and a service that trusts its
# caller to have validated is a service that breaks the first time something
# else calls it.
class PlaceIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: str = Field(min_length=1, max_length=400)
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=400)


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=999)
    note: str | None = Field(default=None, max_length=200)


class OrderIn(BaseModel):
    orderId: str | None = Field(default=None, max_length=100)
    orderNumber: str = Field(min_length=1, max_length=100)
    status: str = Field(default="unknown", max_length=40)
    paid: bool = False
    itemCount: int | None = None
    items: list[ItemIn] = Field(default_factory=list)


class JobIn(BaseModel):
    order: OrderIn
    pickup: PlaceIn
    dropoff: PlaceIn
    branchId: str | None = Field(default=None, max_length=100)
    distanceKm: float | None = Field(default=None, ge=0, le=20_000)
    notes: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
# How long each leg takes, in seconds. Short enough to watch happen in a demo,
# and it is the only fictional thing here — every status this produces is one a
# real courier reports, and it produces them in the order a real one would.
_ACCEPT_AFTER = 2
_ASSIGN_AFTER = 6
_PICKUP_AFTER = 25
_TRANSIT_AFTER = 40
_DELIVERED_AFTER = 90

_RIDERS = ("Ayesha K.", "Bilal R.", "Hina S.", "Omar F.", "Zainab M.")


@dataclass
class Job:
    id: str
    order_number: str
    pickup: dict
    dropoff: dict
    items: list[dict]
    notes: str | None
    distance_km: float | None
    created: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    courier: str | None = None

    @property
    def age(self) -> float:
        return time.monotonic() - self.created

    @property
    def status(self) -> str:
        """Where this job has got to, derived from the clock.

        Derived rather than stored, so there is no scheduler to fall behind and
        no way for a job to be marked delivered by anything other than the
        passage of the whole journey.
        """
        if self.cancelled:
            return "cancelled"
        age = self.age
        if age < _ACCEPT_AFTER:
            return "requested"
        if age < _ASSIGN_AFTER:
            return "accepted"
        if age < _PICKUP_AFTER:
            return "courier_assigned"
        if age < _TRANSIT_AFTER:
            return "picked_up"
        if age < _DELIVERED_AFTER:
            return "in_transit"
        return "delivered"

    @property
    def eta_minutes(self) -> int | None:
        if self.status in ("delivered", "cancelled"):
            return None
        # Ceiling division, so a job 1 second from arriving reads "1 minute"
        # rather than "0" — a zero ETA on something that has not landed yet
        # is the kind of small lie that erodes trust in the whole readout.
        remaining = max(0.0, _DELIVERED_AFTER - self.age)
        return max(1, int(-(-remaining // 60)))

    @property
    def message(self) -> str:
        return {
            "requested": "We have the job and are finding a rider.",
            "accepted": "Accepted — assigning a rider now.",
            "courier_assigned": f"{self.courier} is on the way to the restaurant.",
            "picked_up": f"{self.courier} has collected the order.",
            "in_transit": f"{self.courier} is on the way to the customer.",
            "delivered": "Handed to the customer.",
            "cancelled": "This delivery was cancelled.",
        }.get(self.status, "")

    def to_view(self) -> dict:
        status = self.status
        return {
            "jobId": self.id,
            "orderNumber": self.order_number,
            "status": status,
            # Stated rather than left to the reader. The ordering side derives
            # this itself too — both agreeing is the point.
            "delivered": status == "delivered",
            "message": self.message,
            "courier": {"name": self.courier} if self.courier else None,
            "etaMinutes": self.eta_minutes,
            "fee": self.fee,
            "trackingUrl": f"/api/delivery/jobs/{self.id}",
            "pickup": self.pickup,
            "dropoff": self.dropoff,
            "items": self.items,
            "notes": self.notes,
        }

    @property
    def fee(self) -> str:
        """What the ride costs, in the rupees everything else here is priced in."""
        base = 120.0
        per_km = 35.0
        distance = self.distance_km if self.distance_km is not None else 3.0
        return f"Rs {base + per_km * distance:,.0f}"


_jobs: dict[str, Job] = {}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@app.get("/.well-known/agent-card.json")
def card() -> dict:
    """Who this agent is and what it can be asked for.

    Unwrapped, unlike everything else here: the well-known URL is the one thing
    a stranger's agent reads, and it should be a card rather than this
    project's envelope around one.
    """
    return {
        "protocolVersion": "0.3.0",
        "protocolVariant": "http+json subset",
        "name": "Friends Kitchen Delivery",
        "description": (
            "Collects a paid restaurant order and delivers it to the customer. "
            "Give it the order, where to collect and where to deliver; it "
            "answers with a job you can track to completion."
        ),
        "provider": {"organization": "Friends Kitchen"},
        "version": "1.0.0",
        "url": "/api/delivery",
        "preferredTransport": "HTTP+JSON",
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": "dispatch_delivery",
                "name": "Deliver an order",
                "description": (
                    "Take a paid order, collect it from the restaurant and "
                    "deliver it to the customer's location. Refuses an order "
                    "that is not paid for."
                ),
                "tags": ["delivery", "courier", "logistics"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "track_delivery",
                "name": "Track a delivery",
                "description": (
                    "Report where a delivery has got to: requested, accepted, "
                    "courier_assigned, picked_up, in_transit, delivered."
                ),
                "tags": ["delivery", "tracking"],
            },
        ],
        # Not in the spec, and the single most useful field on this card: a job
        # is only finished at `delivered`, and a caller that reads acceptance as
        # arrival will tell a customer their food is there when it is not.
        "x-lifecycle": {
            "statuses": [
                "requested",
                "accepted",
                "courier_assigned",
                "picked_up",
                "in_transit",
                "delivered",
            ],
            "terminalSuccess": "delivered",
            "note": "Dispatch returns `requested`. It never returns `delivered`.",
        },
        "x-currency": {"code": "PKR", "display": "Rs 1,093", "minorUnits": False},
    }


@app.get("/api/delivery/health")
def health() -> dict:
    """Is the delivery agent up, and how much is it carrying?"""
    return _ok(
        {
            "delivery": "ok",
            "service": "Friends Kitchen Delivery",
            "activeJobs": sum(1 for job in _jobs.values() if job.status != "delivered"),
            "totalJobs": len(_jobs),
            # Where this process's spans go, if anywhere. Never a credential.
            "telemetry": telemetry.describe(),
        }
    )


# --------------------------------------------------------------------------- #
# The job lifecycle
# --------------------------------------------------------------------------- #
@app.post("/api/delivery/jobs", status_code=201)
def create_job(payload: JobIn) -> dict:
    """Take on a delivery.

    Refuses an unpaid order. The restaurant will not release food that has not
    been bought, so accepting the job would only put a rider outside a counter
    that has nothing for them — and it would let the ordering side believe the
    order was on its way.
    """
    if not payload.order.paid:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Order {payload.order.orderNumber} is not paid "
                f"({payload.order.status}). We only collect orders the restaurant "
                "has been paid for."
            ),
        )

    if not payload.order.items:
        raise HTTPException(
            status_code=422,
            detail="The order has no items on it — there is nothing to collect.",
        )

    job = Job(
        id=f"fkd_{uuid.uuid4().hex[:10]}",
        order_number=payload.order.orderNumber,
        pickup=payload.pickup.model_dump(),
        dropoff=payload.dropoff.model_dump(),
        items=[item.model_dump() for item in payload.order.items],
        notes=payload.notes,
        distance_km=payload.distanceKm,
    )
    # Named up front so the rider is a fact about the job from the moment it is
    # taken, even though the status does not admit to one for a few seconds.
    job.courier = _RIDERS[len(_jobs) % len(_RIDERS)]
    _jobs[job.id] = job
    console.say(
        f"took order {job.order_number} — {job.courier} riding, {job.fee}",
        kind="status",
        ref=job.id,
        data={"orderNumber": job.order_number, "courier": job.courier},
    )

    return _ok(job.to_view())


@app.get("/api/delivery/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """Where a delivery has got to."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    return _ok(job.to_view())


@app.post("/api/delivery/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Call a delivery off, if it has not already happened."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    if job.status == "delivered":
        raise HTTPException(
            status_code=409,
            detail="This order has already been delivered — it cannot be cancelled.",
        )
    job.cancelled = True
    console.say(
        f"order {job.order_number} called off",
        kind="status",
        level="warn",
        ref=job.id,
        data={"orderNumber": job.order_number},
    )
    return _ok(job.to_view())


@app.get("/api/delivery/jobs")
def list_jobs() -> dict:
    """Everything this agent is carrying, newest first."""
    jobs = sorted(_jobs.values(), key=lambda job: job.created, reverse=True)
    return _ok({"items": [job.to_view() for job in jobs]})


# --------------------------------------------------------------------------- #
# The port
# --------------------------------------------------------------------------- #
#: 8102, and not read from anywhere. The ordering side reaches this service at
#: `DELIVERY_BASE_URL`, which names 8102; `agent/config.py`, the README and the
#: courier provider in `agent/delivery/courier_agent.py` all name 8102 too. A
#: courier that came up on uvicorn's default 8000 is therefore one no paid order
#: can reach — the order is placed and charged and nothing carries it, which is
#: the worst failure this system has. One number, in one place, spares it.
PORT = 8102


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("delivery_server:app", host="0.0.0.0", port=PORT, reload=True)
