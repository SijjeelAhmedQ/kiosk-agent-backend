"""One delivery, from the request landing to the food in a hand.

The job is the agent's world. It holds what the ordering agent sent, where the
job has got to, and the stream the console watches — and it is the thing that
makes an LLM safe to put in charge of a delivery, because **the agent cannot
write a status; it can only ask for one.** Every transition goes through
`advance()`, which refuses anything that is not the next legal step.

That is the difference between this and the courier on 8102. There, the status
is derived from a clock and the service is a simulator with no opinions. Here a
model decides — whether to take the job, when the rider rolls, what to tell the
restaurant — and this module is what stops those decisions from including
"declare it delivered". A dispatcher that hallucinated a completed drop would
get a refusal from `advance()`, not a delivered order.

The publish/subscribe half is imported from `agent/a2a/tasks.py` rather than
written again. `Stream` is a general-purpose append-only log with late joiners,
the semantics it gets right are subtle (see `Stream.attach`), and two copies of
subtle code drift.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Reused, not re-implemented — see the module docstring. Importing this pulls in
# nothing that runs: `tasks` builds two empty registries and no server.
from agent.a2a.tasks import Stream, close, publish  # noqa: F401  (re-exported)
from agent.foodpanda.config import foodpanda_settings as fp

# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #
# The same words `agent/delivery/contract.py` normalises onto, in the order they
# happen. Both ends of the wire using one vocabulary is what lets the ordering
# agent read this agent's answer without translating it.
REQUESTED = "requested"
ACCEPTED = "accepted"
COURIER_ASSIGNED = "courier_assigned"
PICKED_UP = "picked_up"
IN_TRANSIT = "in_transit"
DELIVERED = "delivered"
REJECTED = "rejected"
FAILED = "failed"
CANCELLED = "cancelled"

#: The happy path, in order, for anything that has to show or advertise it —
#: the agent card, the console's timeline. The failure states are deliberately
#: not in it: this is the journey, not the vocabulary.
LIFECYCLE = (REQUESTED, ACCEPTED, COURIER_ASSIGNED, PICKED_UP, IN_TRANSIT, DELIVERED)

#: What may follow what. A dispatcher that asks for anything else is refused —
#: which is the whole safety story of this package in one dict.
_NEXT: dict[str, frozenset[str]] = {
    REQUESTED: frozenset({ACCEPTED, REJECTED, FAILED, CANCELLED}),
    ACCEPTED: frozenset({COURIER_ASSIGNED, FAILED, CANCELLED}),
    COURIER_ASSIGNED: frozenset({PICKED_UP, FAILED, CANCELLED}),
    PICKED_UP: frozenset({IN_TRANSIT, FAILED, CANCELLED}),
    IN_TRANSIT: frozenset({DELIVERED, FAILED, CANCELLED}),
    DELIVERED: frozenset(),
    REJECTED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

#: Nothing more will happen to a job in one of these.
TERMINAL = frozenset({DELIVERED, REJECTED, FAILED, CANCELLED})

#: The two steps a person on the board asks for, and what each one is called on
#: the wire. They are the steps a customer can see happening — a rider being
#: found, and the food leaving the restaurant — which is why these two and not
#: the others are the ones a dispatch desk holds.
RIDER = "rider"
DELIVERY = "delivery"
GATES = (RIDER, DELIVERY)


class NotWaiting(RuntimeError):
    """Somebody asked for a step this job is not sitting at."""


#: The riders this service dispatches. Named people rather than "Rider #3",
#: because a customer waiting on food is told a name and the console should
#: show what the customer would be told.
RIDERS = (
    "Ayesha Khan",
    "Bilal Rehman",
    "Hina Sadiq",
    "Omar Farooq",
    "Zainab Malik",
    "Danish Iqbal",
)


class IllegalTransition(RuntimeError):
    """The dispatcher asked for a status that cannot follow the current one."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
@dataclass
class DeliveryJob:
    """One delivery request and everything that has happened to it since."""

    #: Exactly what the ordering agent sent, untouched. Kept whole so the
    #: console can show the operator the message rather than this service's
    #: reading of it — the two disagreeing is precisely the bug worth catching.
    request: dict[str, Any]

    order_id: str = ""
    order_number: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    pickup: dict[str, Any] = field(default_factory=dict)
    dropoff: dict[str, Any] = field(default_factory=dict)
    branch_id: str | None = None
    distance_km: float | None = None
    notes: str | None = None

    #: The customer already asked for this to be brought to them.
    #:
    #: Sent by the ordering agent, off its own console's "Where it goes" switch.
    #: It answers exactly one question — the one the `delivery` gate below would
    #: otherwise hold the job to ask: *do you want this delivered to you?* True
    #: and that gate opens at construction, so the rider is not left standing in
    #: the restaurant waiting for a person to press a button they already
    #: pressed. It does not touch the `rider` gate, and it does not let the job
    #: skip a step: the ride still happens, and `deliver_to_customer` is still
    #: the only thing that can mark an order delivered.
    #:
    #: False means nobody has consented *here* — not that delivery is unwanted —
    #: and the board is asked, exactly as it was before this field existed.
    where_it_goes: bool = False

    id: str = field(default_factory=lambda: f"fp_{uuid.uuid4().hex[:10]}")
    status: str = REQUESTED
    courier: str | None = None

    #: Why the dispatcher took the job, or why it would not. In its own words —
    #: this is the one field on the record the model authors.
    decision: str | None = None

    #: What the dispatcher said at the end, for the console and the trace.
    final_text: str = ""
    error: str | None = None

    created_at: str = field(default_factory=_now)
    started: float = field(default_factory=time.monotonic)

    #: Every status this job has been in, with what was said at the time.
    timeline: list[dict[str, Any]] = field(default_factory=list)

    #: Which step this job is holding at, waiting for a person: "rider",
    #: "delivery", or None when nothing is waiting on anybody. On the view, so
    #: the board can light exactly one button and the ordering agent can say
    #: "waiting to be sent out" rather than reporting a silence.
    awaiting: str | None = None

    #: One latch per gated step, opened by the board. Never re-closed: a step
    #: that has been asked for stays asked for, so a released gate cannot be
    #: taken back by a second request or a reconnecting page.
    gates: dict[str, asyncio.Event] = field(
        default_factory=lambda: {name: asyncio.Event() for name in GATES}
    )

    stream: Stream = field(default_factory=Stream)
    task: Any = None  # asyncio.Task, typed loosely to keep asyncio out of here

    def __post_init__(self) -> None:
        """Open the gate the customer has already opened.

        Consent that arrived with the request is consent, and a gate is nothing
        more than a record of one. Setting the event here rather than teaching
        `wait_for` about the flag means every route into that wait behaves the
        same — including a dispatcher that gets there before the request has
        finished being read — and it keeps the property that matters: a gate,
        once open, is never closed again.

        Only the `delivery` gate. Asking for a rider is a separate request about
        timing rather than about consent, and a customer who said "deliver it to
        me" has not said "and start now".

        The stream is labelled here too, so every line this job mirrors onto the
        process console names the job it came from — see `agent/console.py`.
        """
        self.stream.ref = self.id

        if self.where_it_goes:
            self.gates[DELIVERY].set()

    # -- reading it -------------------------------------------------------- #
    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    @property
    def delivered(self) -> bool:
        """Did the food actually reach the customer?

        One status, and only one. The ordering agent asks the same question of
        its own copy of the answer; both agreeing is the point.
        """
        return self.status == DELIVERED

    @property
    def item_count(self) -> int:
        return sum(int(item.get("quantity") or 0) for item in self.items)

    @property
    def quote(self) -> str:
        """What this ride would cost, in the rupees everything else is priced in."""
        distance = self.distance_km if self.distance_km is not None else 3.0
        return f"Rs {fp.fee_base + fp.fee_per_km * distance:,.0f}"

    @property
    def fee(self) -> str | None:
        """What is actually owed for this ride — the quote, once it is agreed.

        None until the job is taken on, and None for ever if it is refused. A
        price on a delivery nobody agreed to make reads as a charge, and the
        restaurant would be right to pass it on to a customer who is not getting
        any food. The dispatcher still sees `quote` while it is deciding.
        """
        return None if self.status in (REQUESTED, REJECTED) else self.quote

    @property
    def eta_seconds(self) -> int | None:
        """How much riding is left, from where the job actually is.

        Derived from the legs still to run rather than counted down from a
        promise made at dispatch, so a job that sat waiting for a rider reports
        a later arrival instead of an optimistic one that quietly expires.
        """
        if self.done:
            return None
        remaining = {
            REQUESTED: fp.pickup_seconds + fp.transit_seconds,
            ACCEPTED: fp.pickup_seconds + fp.transit_seconds,
            COURIER_ASSIGNED: fp.pickup_seconds + fp.transit_seconds,
            PICKED_UP: fp.transit_seconds,
            IN_TRANSIT: fp.transit_seconds,
        }.get(self.status)
        return None if remaining is None else int(remaining)

    @property
    def eta_minutes(self) -> int | None:
        """The ETA in the unit the delivery contract carries.

        Ceiling, and never zero: a job a few seconds from arriving reads as
        "1 minute" rather than "0", because a zero ETA on something that has
        not landed is the kind of small lie that erodes a whole readout.
        """
        seconds = self.eta_seconds
        return None if seconds is None else max(1, -(-seconds // 60))

    @property
    def message(self) -> str:
        """The most recent thing said about this job, for a one-line readout."""
        return self.timeline[-1]["message"] if self.timeline else ""

    # -- moving it --------------------------------------------------------- #
    def advance(self, status: str, message: str) -> dict[str, Any]:
        """Move the job on one step. Raises `IllegalTransition` if it cannot.

        The refusal is the feature. Tools call this; the model calls tools; so
        the model's only route to a status is through the transition table, and
        `picked_up → delivered` — a rider who never rode — is not in it.
        """
        if status not in _NEXT:
            raise IllegalTransition(f"{status!r} is not a delivery status.")
        if status not in _NEXT[self.status]:
            allowed = ", ".join(sorted(_NEXT[self.status])) or "nothing — it is finished"
            raise IllegalTransition(
                f"This job is {self.status}. It cannot go to {status} from there. "
                f"What can follow: {allowed}."
            )

        self.status = status
        # Nothing is waiting on a person any more: either the gate opened and
        # this is the step it was holding, or the job was called off underneath
        # it. Cleared here rather than in the tools so no route out of a wait
        # can leave the board showing a button for a job that has moved on.
        self.awaiting = None
        entry = {
            "status": status,
            "message": message,
            "at": _now(),
            "elapsedSeconds": round(time.monotonic() - self.started, 1),
        }
        self.timeline.append(entry)
        self.stream.emit({"type": "status", **entry, "job": self.to_view()})
        return entry

    # -- waiting on a person ----------------------------------------------- #
    async def wait_for(self, step: str, asking: str, timeout: float) -> bool:
        """Hold here until the board asks for `step`. False if nobody ever did.

        The wait is real and it is the dispatcher's own: the agent called a tool,
        and the tool has not come back yet. That is what makes the gate honest —
        there is no second copy of the job's progress being kept somewhere while
        the board catches up, because the delivery genuinely has not moved.

        Returns True the moment the step is asked for, and True immediately if it
        was asked for before the dispatcher got here — a person who clicked ahead
        of the agent has still said yes.
        """
        gate = self.gates[step]
        if gate.is_set():
            return True

        self.awaiting = step
        self.stream.emit(
            {
                "type": "awaiting",
                "step": step,
                "message": asking,
                "at": _now(),
                "job": self.to_view(),
            }
        )

        try:
            await asyncio.wait_for(gate.wait(), timeout)
        except asyncio.TimeoutError:
            self.awaiting = None
            return False
        return True

    def release(self, step: str) -> None:
        """The board has asked for `step`. Let the dispatcher through.

        Raises `NotWaiting` when the job is not sitting at that step, so a
        stale page cannot send a rider to a delivery that has already gone out.
        A step already released is not an error — the second click is the same
        instruction as the first.
        """
        if step not in self.gates:
            raise NotWaiting(f"{step!r} is not something this job waits for.")
        if self.done:
            # Before the idempotence check below, or a page left open on a
            # finished delivery gets a cheerful 200 for asking to send a rider to
            # an order that arrived ten minutes ago.
            raise NotWaiting(f"This job is already {self.status}. Nothing is waiting.")
        if self.gates[step].is_set():
            return
        if self.awaiting != step:
            raise NotWaiting(
                f"This job is {self.status} and is not waiting to be asked for "
                f"{step}." + (f" It is waiting for {self.awaiting}." if self.awaiting else "")
            )

        self.gates[step].set()
        self.awaiting = None
        self.stream.emit(
            {
                "type": "awaiting",
                "step": None,
                "message": (
                    "The operator asked for a rider."
                    if step == RIDER
                    else "The operator asked for the order to be delivered."
                ),
                "at": _now(),
                "job": self.to_view(),
            }
        )

    def say(self, text: str) -> None:
        """The dispatcher's own words, into the console's transcript."""
        if text.strip():
            self.stream.emit({"type": "say", "speaker": "dispatcher", "text": text.strip()})

    def fail(self, reason: str) -> None:
        """Something broke that was not the dispatcher's decision.

        A job already in a terminal state keeps the state it reached — a model
        that fell over *after* delivering has still delivered, and rewriting
        that to `failed` would be the same lie in the other direction.
        """
        self.error = reason
        self.stream.emit({"type": "error", "message": reason})
        if not self.done:
            self.advance(FAILED, reason)

    # -- the wire ---------------------------------------------------------- #
    def to_view(self) -> dict[str, Any]:
        """The job as both the ordering agent and the console read it.

        `delivered` is spelled out rather than left implicit in the status,
        because `requested` and `delivered` are one field apart and a caller
        that reads acceptance as arrival tells a customer their food is there
        when it is not.
        """
        return {
            "jobId": self.id,
            "orderId": self.order_id,
            "orderNumber": self.order_number,
            "status": self.status,
            "delivered": self.delivered,
            "done": self.done,
            # "rider" | "delivery" | null — which step, if any, is waiting to be
            # asked for. The board lights one button off this and nothing else.
            "awaiting": self.awaiting,
            "message": self.message,
            "decision": self.decision,
            "courier": {"name": self.courier} if self.courier else None,
            "etaMinutes": self.eta_minutes,
            "etaSeconds": self.eta_seconds,
            "fee": self.fee,
            "trackingUrl": f"/api/foodpanda/jobs/{self.id}",
            "pickup": self.pickup,
            "dropoff": self.dropoff,
            "items": self.items,
            "itemCount": self.item_count,
            "distanceKm": self.distance_km,
            "notes": self.notes,
            # On the view so the board can say why it is not asking. A missing
            # button is indistinguishable from a broken one unless something
            # says the question was already answered.
            "whereItGoes": self.where_it_goes,
            "branchId": self.branch_id,
            "createdAt": self.created_at,
            "timeline": self.timeline,
            "finalText": self.final_text,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# The board
# --------------------------------------------------------------------------- #
class _Registry(OrderedDict):
    """A dict that forgets its oldest entry once it gets too big.

    Same three lines as the A2A registry, and kept here rather than shared
    because that one is bounded by `A2A_KEEP_TASKS` and this one should be
    bounded by its own number.
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def put(self, key: str, value: Any) -> Any:
        self[key] = value
        while len(self) > self._limit:
            self.popitem(last=False)
        return value


jobs: _Registry = _Registry(fp.keep_jobs)


def newest_first() -> list[DeliveryJob]:
    """Everything this agent is carrying or has carried, newest first."""
    return sorted(jobs.values(), key=lambda job: job.started, reverse=True)


def next_rider() -> str:
    """Whose turn it is. Round-robin over the roster, which is fair enough."""
    return RIDERS[len(jobs) % len(RIDERS)]
