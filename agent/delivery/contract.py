"""The contract between the ordering agent and a delivery agent.

This module is the whole of the boundary. The ordering agent knows these
shapes and the `DeliveryProvider` protocol; it knows nothing about Foodpanda,
about HTTP, or about how any particular courier numbers its jobs. A new
provider is a new file next to this one, named in `registry.py` — not a change
to the ordering tools, the prompt, or the restaurant API.

Two rules run through it:

* **A request is validated before it leaves.** Coordinates, quantities and the
  order's own payment state are checked here, once, so no provider has to
  re-derive whether it was handed something sane — and so a bad fix is refused
  on this side of the network rather than becoming a courier at the wrong
  address.

* **Sent is not delivered.** `DeliveryJob.status` is whatever the delivery
  agent last said, and `delivered` is true only for the one terminal status
  that means it. Dispatching successfully moves an order to `requested`, and
  nothing in this module will let that be read as arrival.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
class DeliveryError(RuntimeError):
    """Anything that stopped a delivery being arranged."""


class DeliveryRejected(DeliveryError):
    """We refused to send this request — it was not fit to send.

    Raised on our side of the wire: a missing fix, an unpaid order, an empty
    basket. Distinct from `DeliveryUnavailable` because the fix is different —
    this one is a bad request, not a bad connection.
    """


class DeliveryUnavailable(DeliveryError):
    """The delivery agent could not be reached, or would not take the job."""


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #
# Every status a job can be in, in the order they normally happen. A provider
# reporting anything outside this list has it mapped to `unknown` rather than
# passed through — an unrecognised word must never read as progress.
STATUSES = (
    "requested",         # the delivery agent has the job, nothing assigned yet
    "accepted",          # it has agreed to run it
    "courier_assigned",  # a rider has it
    "picked_up",         # collected from the restaurant
    "in_transit",        # on the way to the customer
    "delivered",         # in the customer's hands — the only success
    "failed",
    "cancelled",
    "unknown",
)

#: The one status that means the food arrived. Kept as a constant rather than a
#: string literal in three files, because "which of these counts as done" is
#: exactly the question this module exists to answer once.
DELIVERED = "delivered"

_TERMINAL = frozenset({DELIVERED, "failed", "cancelled"})


def normalise_status(raw: Any) -> str:
    """A provider's word for where a job is, mapped onto ours.

    Unknown words become `unknown`, never `delivered`. A courier API that
    invents a status should leave the order looking unfinished, which is the
    truthful reading, rather than closing it.
    """
    if not isinstance(raw, str):
        return "unknown"
    word = raw.strip().lower().replace("-", "_").replace(" ", "_")

    # The spellings the common courier APIs use for the same handful of states.
    aliases = {
        "pending": "requested",
        "created": "requested",
        "new": "requested",
        "confirmed": "accepted",
        "assigned": "courier_assigned",
        "rider_assigned": "courier_assigned",
        "collected": "picked_up",
        "picked": "picked_up",
        "on_the_way": "in_transit",
        "out_for_delivery": "in_transit",
        "delivering": "in_transit",
        "completed": DELIVERED,
        "dropped_off": DELIVERED,
        "canceled": "cancelled",
        "rejected": "failed",
        "error": "failed",
    }
    word = aliases.get(word, word)
    return word if word in STATUSES else "unknown"


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Place:
    """One end of the journey — where to collect, or where to deliver."""

    latitude: float
    longitude: float
    address: str
    name: str | None = None
    phone: str | None = None
    note: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phone": self.phone,
            "note": self.note,
        }


@dataclass(frozen=True)
class DeliveryItem:
    """One line of the order, as the courier needs to see it.

    Prices are absent on purpose. A courier needs to know what it is carrying
    and how much of it — the bill is settled at the restaurant, and passing
    money figures to a third party that has no use for them is data sent for
    no reason.
    """

    name: str
    quantity: int
    note: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {"name": self.name, "quantity": self.quantity, "note": self.note}


@dataclass(frozen=True)
class DeliveryRequest:
    """Everything a delivery agent needs to collect an order and deliver it."""

    order_id: str
    order_number: str
    order_status: str
    paid: bool

    pickup: Place
    dropoff: Place
    items: list[DeliveryItem]

    #: Free text from the customer — a gate code, "leave at reception".
    notes: str | None = None

    #: Which branch, for a provider that keeps its own store directory.
    branch_id: str | None = None

    #: Straight-line km between the two ends, when it is known. An estimate,
    #: and labelled as one wherever it is shown.
    distance_km: float | None = None

    def validate(self) -> None:
        """Refuse a request that should not be sent. Raises `DeliveryRejected`.

        Called by every provider before it opens a connection, so the checks
        cannot be skipped by adding a provider that forgets them.
        """
        if not self.order_number.strip():
            raise DeliveryRejected("The order has no order number to deliver against.")

        if not self.items:
            raise DeliveryRejected(
                "The order has no items on it — there is nothing to collect."
            )

        for item in self.items:
            if item.quantity < 1:
                raise DeliveryRejected(
                    f"{item.name!r} has a quantity of {item.quantity}, which cannot be delivered."
                )

        # An unpaid order must not become a courier's problem. The restaurant
        # would not release the food, and the customer would be waiting on a
        # rider sent to collect something nobody has bought.
        if not self.paid:
            raise DeliveryRejected(
                f"Order {self.order_number} is not paid ({self.order_status}). "
                "Pay for it before arranging delivery."
            )

        for place, what in ((self.pickup, "pickup"), (self.dropoff, "delivery")):
            if not -90 <= place.latitude <= 90 or not -180 <= place.longitude <= 180:
                raise DeliveryRejected(
                    f"The {what} coordinates are not a place on Earth "
                    f"({place.latitude}, {place.longitude})."
                )
            if not place.address.strip():
                raise DeliveryRejected(f"The {what} point has no address.")

    def to_message(self) -> dict[str, Any]:
        """The request as it crosses to the other agent.

        One dict, one shape, whichever provider is carrying it — that is what
        makes swapping Foodpanda for an in-house courier a config change. A
        provider that needs a different wire format translates *from* this, and
        that translation lives in the provider.
        """
        return {
            "order": {
                "orderId": self.order_id,
                "orderNumber": self.order_number,
                "status": self.order_status,
                "paid": self.paid,
                "itemCount": sum(item.quantity for item in self.items),
                "items": [item.to_message() for item in self.items],
            },
            "pickup": self.pickup.to_message(),
            "dropoff": self.dropoff.to_message(),
            "branchId": self.branch_id,
            "distanceKm": self.distance_km,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# The answer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeliveryJob:
    """What the delivery agent said back, in our words rather than its own."""

    provider: str
    job_id: str
    status: str
    message: str | None = None
    courier: str | None = None
    eta_minutes: int | None = None
    fee: str | None = None
    tracking_url: str | None = None

    #: The provider's own payload, kept for the trace. Never read for meaning —
    #: anything that decides something has a field of its own above.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        """Has the food actually reached the customer?

        The only place in this system that answers that question. Note what it
        is not: "did the dispatch succeed" — an accepted job is a courier on
        its way, and reading that as arrival is the specific mistake this
        property exists to make impossible.
        """
        return self.status == DELIVERED

    @property
    def settled(self) -> bool:
        """Is there any point asking again? True once nothing more will change."""
        return self.status in _TERMINAL

    def to_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "jobId": self.job_id,
            "status": self.status,
            "delivered": self.delivered,
            "message": self.message,
            "courier": self.courier,
            "etaMinutes": self.eta_minutes,
            "fee": self.fee,
            "trackingUrl": self.tracking_url,
        }


# --------------------------------------------------------------------------- #
# The interface a delivery service has to meet
# --------------------------------------------------------------------------- #
@runtime_checkable
class DeliveryProvider(Protocol):
    """One delivery service, as the ordering agent sees it.

    Two methods and three facts. A provider that can do more — scheduled slots,
    live rider positions — exposes it through `DeliveryJob.raw` rather than
    widening this, because every widening is a thing the other providers then
    have to answer for.
    """

    #: Stable key used in config and in tool results. Lowercase, no spaces.
    name: str

    #: What to call this service in front of a person.
    display_name: str

    @property
    def configured(self) -> bool:
        """Can this provider actually be used right now?

        False means "credentials or an address are missing" — which the health
        endpoint reports, so an operator finds out before an errand rather than
        at the handover.
        """
        ...

    def dispatch(self, request: DeliveryRequest) -> DeliveryJob:
        """Hand the order over. Raises `DeliveryError` if it could not be."""
        ...

    def status(self, job_id: str) -> DeliveryJob:
        """Ask where a job has got to. Raises `DeliveryError` if unreachable."""
        ...
