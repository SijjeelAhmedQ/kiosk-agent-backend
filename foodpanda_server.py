"""HTTP front door for the Foodpanda delivery agent.

    .venv\\Scripts\\python -m uvicorn foodpanda_server:app --port 8103 --reload

Its own app on its own port, next to — never inside — the errand server on 8100,
the A2A merchant on 8101 and the in-house courier on 8102. The ordering agent
reaches this over real HTTP, which is what makes the handover agent-to-agent
rather than a function call wearing a costume: nothing in this process can see
the cart, the wallet, the menu or the placed order. It knows what the request
carried and nothing else, and that is the boundary
`agent/delivery/contract.py` describes.

Two families of route, and the split matters:

* `/.well-known/agent-card.json` and `POST /api/foodpanda/jobs` are **the
  protocol**. Anything that speaks the delivery contract can call them; the
  Friends Kitchen ordering agent is only the first client.
* `/api/foodpanda/jobs/{id}/events` is **the console**. It streams the
  dispatcher's own reasoning — every tool it called and what came back — so the
  delivery board shows an agent working rather than a progress bar.

**What it is honest about.** A real agent makes the decisions here: whether to
take the job, when the rider rolls, what to tell the restaurant. What is
simulated is the motorbike. The legs are compressed to seconds and awaited, not
skipped, so a job reaches `delivered` only after both have actually elapsed;
`POST` never returns `delivered`; and a request this service will not take comes
back as a refusal rather than a fake success. It is not connected to Foodpanda.
For that, `agent/delivery/foodpanda.py` speaks the real courier API — this is
the demonstration agent the flow can be shown with when no such account exists.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AliasChoices, BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent import telemetry
from agent.foodpanda import jobs as store
from agent.foodpanda.config import foodpanda_settings as fp
from agent.foodpanda.dispatcher import credentials_ready, dispatch
from agent.foodpanda.jobs import DeliveryJob

app = FastAPI(
    title="Foodpanda Delivery Agent",
    version="1.0.0",
    description=(
        "An AI dispatcher that takes a confirmed restaurant order from another "
        "agent, decides whether it can be delivered, and runs it to the customer."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # The delivery board, served alongside the other consoles at /foodpanda.html.
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        # Vite binds the IPv6 loopback on Windows, so a browser opened at the
        # bracketed address sends this origin rather than either of the two
        # above — an origin is the text that was typed, not what it resolved to.
        "http://[::1]:5174",
        # Friends Kitchen itself, so the storefront could show a delivery later
        # without a CORS change.
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Tracing, if the operator asked for it. Off unless FK_OTEL is set, and it never
# raises — see agent/telemetry.py.
telemetry.setup("foodpanda-dispatcher", app)


def _ok(data) -> dict:
    """The envelope every service in this system answers with."""
    return {"success": True, "data": data}


# --------------------------------------------------------------------------- #
# What a request has to contain
# --------------------------------------------------------------------------- #
# The ordering agent's contract validated all of this before it sent. Pydantic
# does it again here, and the repetition is deliberate: this is a network
# boundary, and a service that trusts its caller to have checked is a service
# that breaks the first time something else calls it. The shapes mirror
# `DeliveryRequest.to_message()` — one wire format, whichever courier is
# carrying it — which is what lets a deployment swap 8102 for 8103 with a
# variable rather than a change to the ordering agent.
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

    #: The customer has already asked for this order to be delivered to them.
    #:
    #: The only field on this request that is about a person rather than about a
    #: parcel, and the only one that changes what this service *asks* rather than
    #: what it carries. True and the "Deliver it to me" gate is already open when
    #: the job is created: the ordering agent's own console put that question to
    #: the customer, and putting it to them twice is not a second safeguard, it
    #: is a delivery that stalls because nobody expected to be asked again.
    #:
    #: Absent reads as False, which is what every caller written before this
    #: field sends and what a stranger's agent that has never heard of it sends —
    #: and False is the behaviour this service has always had. Consent can only
    #: ever remove a question here; it never adds one, and it never moves a job.
    #:
    #: Accepted under either spelling. This is a cross-agent boundary and the
    #: field is named after a switch, so `where_it_goes` — the name the ordering
    #: agent uses for it internally — is understood as well as the camelCase the
    #: rest of this wire format uses.
    whereItGoes: bool = Field(
        default=False,
        validation_alias=AliasChoices("whereItGoes", "where_it_goes"),
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@app.get("/.well-known/agent-card.json")
def card() -> dict:
    """Who this agent is and what it can be asked for.

    Unwrapped, unlike everything else here: the well-known URL is the one thing
    a stranger's agent reads, and it should be a card rather than this project's
    envelope around one.
    """
    return {
        "protocolVersion": "0.3.0",
        "protocolVariant": "http+json subset",
        "name": "Foodpanda Delivery (demonstration agent)",
        "description": (
            "An AI dispatcher for a food-delivery service. Give it a paid "
            "restaurant order, where to collect it and where to deliver it; it "
            "decides whether it can make the run, assigns a rider, and carries "
            "the order to the customer."
        ),
        "provider": {"organization": "Foodpanda (demonstration agent)"},
        "version": "1.0.0",
        "url": f"{fp.public_base}/api/foodpanda",
        "preferredTransport": "HTTP+JSON",
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": "dispatch_delivery",
                "name": "Deliver an order",
                "description": (
                    "Take a paid order, decide whether it is within the service "
                    "area, collect it from the restaurant and deliver it to the "
                    "customer. Refuses an order that is not paid for, and "
                    "refuses a drop outside the delivery radius rather than "
                    "sending a rider on a run it cannot finish."
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
            "statuses": list(store.LIFECYCLE),
            "terminalSuccess": store.DELIVERED,
            "note": "Dispatch returns `requested`. It never returns `delivered`.",
            # Said on the card because it changes what a caller should expect to
            # see: with this on, a job legitimately sits at `accepted` until the
            # customer asks for a rider, and a caller that reads a pause as a
            # stall would report a working delivery as a broken one.
            "operatorSteps": (
                [
                    {
                        "step": store.RIDER,
                        "waitsAt": store.ACCEPTED,
                        "askAt": f"POST {fp.public_base}/api/foodpanda/jobs/{{jobId}}/find-rider",
                    },
                    {
                        "step": store.DELIVERY,
                        "waitsAt": store.PICKED_UP,
                        "askAt": f"POST {fp.public_base}/api/foodpanda/jobs/{{jobId}}/deliver",
                        # The one gate a caller can answer in advance. Send
                        # `whereItGoes: true` on the job and this step is already
                        # asked for — for the caller whose own customer said
                        # "deliver it to me" before the order was even placed.
                        "preAnsweredBy": "whereItGoes",
                    },
                ]
                if fp.require_operator
                else []
            ),
        },
        "x-service": {
            "radiusKm": fp.service_radius_km,
            "dispatcherModel": f"{fp.provider}/{fp.model_id}",
            # Said on the card itself, where anything reading this agent will
            # see it, rather than only in a README nobody fetches.
            "simulation": (
                "The dispatcher is a real AI agent making real decisions. The "
                "ride is simulated: legs are compressed to seconds and awaited, "
                "never skipped. Not connected to Foodpanda."
            ),
        },
        "x-currency": {"code": "PKR", "display": "Rs 1,093", "minorUnits": False},
    }


@app.get("/api/foodpanda/health")
def health() -> dict:
    """Is the delivery agent up, is its brain configured, what is it carrying?"""
    ready, problem = credentials_ready()
    active = [job for job in store.jobs.values() if not job.done]

    return _ok(
        {
            "foodpanda": "ok",
            "service": "Foodpanda Delivery (demonstration agent)",
            "dispatcher": {
                "provider": fp.provider,
                "model": fp.model_id,
                "ready": ready,
                "problem": problem,
            },
            "radiusKm": fp.service_radius_km,
            "legSeconds": {"pickup": fp.pickup_seconds, "transit": fp.transit_seconds},
            # Whether the board is a control or a window. False and a job runs
            # itself from request to doorstep; true — the default — and it holds
            # for a rider request and then for a delivery request.
            "operatorSteps": fp.require_operator,
            "activeJobs": len(active),
            "totalJobs": len(store.jobs),
            # Where this process's spans go, if anywhere. Never a credential:
            # OTEL_EXPORTER_OTLP_HEADERS usually carries a token, so the answer
            # is whether headers are set, not what is in them.
            "telemetry": telemetry.describe(),
        }
    )


# --------------------------------------------------------------------------- #
# The job lifecycle
# --------------------------------------------------------------------------- #
@app.post("/api/foodpanda/jobs", status_code=201)
async def create_job(payload: JobIn) -> dict:
    """Take on a delivery, and set the dispatcher working on it.

    Two refusals happen here rather than in the agent, because they are not
    judgement calls. An unpaid order must never become a rider's problem — the
    restaurant will not release food nobody has bought — and an order with no
    lines has nothing to collect. Everything past that is the dispatcher's to
    decide, and it decides it out loud.

    Returns `requested`, always. What comes back from this call is a job that
    has been *taken in*, not one that has been delivered, and the agent has only
    just started reading it.
    """
    ready, problem = credentials_ready()
    if not ready:
        # Refuse the job rather than take it and sit on it. A courier that
        # accepts work it cannot start leaves the restaurant believing an order
        # is on its way.
        raise HTTPException(
            status_code=503,
            detail=(
                f"The Foodpanda dispatcher has no usable model credentials, so it "
                f"cannot take jobs. {problem or ''}".strip()
            ),
        )

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

    job = DeliveryJob(
        request=payload.model_dump(),
        order_id=payload.order.orderId or "",
        order_number=payload.order.orderNumber,
        items=[item.model_dump() for item in payload.order.items],
        pickup=payload.pickup.model_dump(),
        dropoff=payload.dropoff.model_dump(),
        branch_id=payload.branchId,
        distance_km=payload.distanceKm,
        notes=payload.notes,
        where_it_goes=payload.whereItGoes,
    )
    store.jobs.put(job.id, job)

    # The first entry on the board, before the agent has read anything: the job
    # exists and is waiting on a dispatcher. Emitted here so a console that
    # attaches immediately sees the request land rather than starting mid-story.
    job.timeline.append(
        {
            "status": store.REQUESTED,
            "message": (
                "Delivery request received from the Friends Kitchen ordering agent."
                + (
                    " The customer has already asked for it to be delivered to them."
                    if job.where_it_goes
                    else ""
                )
            ),
            "at": job.created_at,
            "elapsedSeconds": 0.0,
        }
    )
    job.stream.emit({"type": "status", **job.timeline[0], "job": job.to_view()})

    # Said on the stream in its own right, not only folded into the line above.
    # The board's whole account of a delivery is this log, and "why was I never
    # asked to send it out?" is a question it has to be able to answer months
    # later — so the consent that answered it is an entry, with a time on it.
    if job.where_it_goes:
        job.stream.emit(
            {
                "type": "awaiting",
                "step": None,
                "message": (
                    "\"Deliver it to me\" came with the order — the customer asked "
                    "for this delivery when they placed it, so the dispatcher will "
                    "not stop to ask again."
                ),
                "at": job.created_at,
                "job": job.to_view(),
            }
        )

    job.task = asyncio.create_task(dispatch(job))

    return _ok(job.to_view())


@app.get("/api/foodpanda/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """Where a delivery has got to."""
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    return _ok(job.to_view())


@app.get("/api/foodpanda/jobs/{job_id}/events")
async def stream_job(job_id: str):
    """Watch one delivery: the dispatcher's tool calls, its words, its statuses.

    Backlog first, then live — so the board can be opened late, or reloaded
    mid-ride, and still show the whole job.
    """
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    return EventSourceResponse(store.publish(job.stream))


@app.post("/api/foodpanda/jobs/{job_id}/find-rider")
async def find_rider(job_id: str) -> dict:
    """Ask for a rider on a job the dispatcher has accepted.

    The customer's half of the delivery. The dispatcher decided whether it would
    take the job; this is the person on the other end saying "now, please" — and
    it is a request, not a command: it opens the gate the dispatcher is waiting
    at, and the dispatcher is still the one that assigns anybody.

    Returns the job as it stands the instant the request lands, which is still
    `accepted`. The rider appears on the stream a moment later.
    """
    return _ok(_release(job_id, store.RIDER))


@app.post("/api/foodpanda/jobs/{job_id}/deliver")
async def deliver(job_id: str) -> dict:
    """Ask for the collected order to be brought out.

    The second half of the same idea: the rider is holding the food at the
    restaurant, and this is the customer asking for it. Returns the job at
    `picked_up` — the ride starts after this answers, and `delivered` arrives on
    the stream when it has actually been ridden.
    """
    return _ok(_release(job_id, store.DELIVERY))


def _release(job_id: str, step: str) -> dict:
    """Open one gate, or say why it cannot be opened.

    Shared by the two routes above because the failure cases are identical and
    worth answering identically: 404 for a job nobody has, 409 for a step this
    job is not sitting at — which is what a page that was left open on a
    finished delivery would ask for.
    """
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    try:
        job.release(step)
    except store.NotWaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return job.to_view()


@app.post("/api/foodpanda/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Call a delivery off, if it has not already happened."""
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such delivery job.")
    if job.status == store.DELIVERED:
        raise HTTPException(
            status_code=409,
            detail="This order has already been delivered — it cannot be cancelled.",
        )
    if job.done:
        raise HTTPException(
            status_code=409,
            detail=f"This job is already {job.status}.",
        )

    # The status moves first, then the agent is stopped. The other order leaves
    # a window where the dispatcher has been killed and the board still says a
    # rider is on the way.
    job.advance(store.CANCELLED, "Cancelled by the operator.")
    if job.task is not None and not job.task.done():
        job.task.cancel()
    store.close(job.stream)

    return _ok(job.to_view())


@app.get("/api/foodpanda/jobs")
def list_jobs() -> dict:
    """Everything this agent is carrying or has carried, newest first."""
    return _ok({"items": [job.to_view() for job in store.newest_first()]})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("foodpanda_server:app", host="0.0.0.0", port=fp.port, reload=True)
