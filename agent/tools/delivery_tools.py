"""The agent's hands, for the half of the errand that happens after payment.

Three tools, and the order they are meant to be used in is the order they
appear: find out where the customer is and which branch serves them, hand the
paid order to a delivery agent, then ask that agent where it has got to.

What these tools deliberately do *not* do is trust the model for any of it. The
customer's coordinates come from `agent/location.py`, not from an argument —
a model that retyped a latitude could move a delivery a hundred kilometres and
nothing would look wrong. The order's payment state is re-read from the
restaurant rather than taken from what the agent believes it did. And the
handover refuses on both counts before a single byte crosses to the courier.

The one thing worth knowing about `check_delivery`: it reports what the
delivery agent says, and dispatching successfully is not arrival. An order
sits at `requested` until a rider actually completes it, and no tool here will
call that done on the courier's behalf.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from agent import branches, friends_kitchen_api, location
from agent.delivery import registry
from agent.delivery.contract import (
    DeliveryError,
    DeliveryItem,
    DeliveryJob,
    DeliveryRejected,
    DeliveryRequest,
    Place,
)
from agent.friends_kitchen_api import FriendsKitchenApiError
from agent.tools.api_tools import current_order

# The delivery arranged this run: {jobId, provider, status}. Set by
# arrange_delivery, read by check_delivery so the agent never has to carry a
# job id around in prose — the same trick `_order` plays in api_tools.
_job: dict[str, Any] = {}


def reset() -> None:
    """Forget the previous errand's delivery."""
    _job.clear()


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _job_view(job: DeliveryJob, provider_label: str) -> dict:
    """A job as the agent should read it.

    `delivered` is spelled out as a word rather than left implicit in the
    status, because "requested" and "delivered" are one field apart and the
    distinction is the whole point.
    """
    view = job.to_view()
    return {
        "ok": True,
        "deliveryService": provider_label,
        "jobId": job.job_id,
        "status": job.status,
        "delivered": job.delivered,
        "courier": view["courier"],
        "etaMinutes": view["etaMinutes"],
        "fee": view["fee"],
        "trackingUrl": view["trackingUrl"],
        "message": job.message,
    }


# --------------------------------------------------------------------------- #
# Where the customer is
# --------------------------------------------------------------------------- #
@tool
def check_delivery_location() -> dict:
    """Find out where the customer is and which branch should make their order.

    Call this before placing the order when the errand is a delivery: it says
    which Friends Kitchen the food will be collected from, so the order is
    placed at the right one.

    You do not need to supply the customer's position — it was given to this
    errand when it started, and you cannot change it.

    Returns:
        The customer's location, the branch serving it, and how far apart they
        are as the crow flies.
    """
    user_location = location.current()
    branch, distance_km = branches.nearest(user_location)

    if user_location is None:
        return {
            "ok": True,
            "haveLocation": False,
            "note": (
                "This errand was started without a customer location, so this is a "
                "counter order. Place it as normal and do not arrange delivery."
            ),
            "restaurant": branch.to_view(),
        }

    return {
        "ok": True,
        "haveLocation": True,
        "customerLocation": user_location.to_view(),
        "customerLocationText": user_location.display(),
        "restaurant": branch.to_view(),
        "distanceKm": distance_km,
        "note": (
            f"Order from {branch.name} — it is the closest branch to the customer. "
            "The distance is a straight line, not a driving distance."
        ),
    }


# --------------------------------------------------------------------------- #
# The handover
# --------------------------------------------------------------------------- #
def _build_request(order_number: str, notes: str | None) -> DeliveryRequest:
    """Assemble the message from facts, not from what the agent remembers.

    Every field is re-read here: the order and its lines from the restaurant,
    the customer's position from the location this run was started with, the
    branch from the directory. That is what makes `validate()` meaningful —
    it is checking the world, not checking the prompt.
    """
    user_location = location.current()
    if user_location is None:
        raise DeliveryRejected(
            "This errand has no customer location, so there is nowhere to deliver to. "
            "It is a counter order — report it as placed and paid, and stop there."
        )

    try:
        detail = friends_kitchen_api.get(f"/orders/number/{order_number}")
    except FriendsKitchenApiError as exc:
        raise DeliveryRejected(
            f"Could not read order {order_number} back from the restaurant to confirm "
            f"it before arranging delivery: {exc}"
        ) from None

    summary = detail.get("summary") or {}
    status = str(detail.get("status") or "unknown")

    # Two independent readings of "is this bought?", and both have to agree.
    # The status is the restaurant's own word for it; the payment ledger is the
    # arithmetic behind that word.
    #
    # Note which figure is *not* used here: `summary.amountDue` is what the
    # order came to after any coupon — what was owed, not what is still owing.
    # It stays at the full amount after payment, so reading it as a balance
    # refuses every paid order there is.
    approved = sum(
        float(payment.get("amount") or 0)
        for payment in detail.get("payments") or []
        if payment.get("status") == "approved"
    )
    owed = float(summary.get("amountDue") or 0)
    paid = status == "paid" and approved + 1e-9 >= owed

    items = [
        DeliveryItem(
            name=str(line.get("name") or "Item"),
            quantity=int(line.get("quantity") or 1),
        )
        for line in detail.get("lines") or []
    ]

    branch, distance_km = branches.nearest(user_location)

    return DeliveryRequest(
        order_id=str(detail.get("orderId") or current_order().get("orderId") or ""),
        order_number=str(detail.get("orderNumber") or order_number),
        order_status=status,
        paid=paid,
        pickup=Place(
            latitude=branch.latitude,
            longitude=branch.longitude,
            address=branch.address,
            name=branch.name,
            phone=branch.phone,
            note=f"Collect order #{order_number}",
        ),
        dropoff=Place(
            latitude=user_location.latitude,
            longitude=user_location.longitude,
            address=user_location.label or user_location.display(),
            note=user_location.label,
        ),
        items=items,
        notes=notes or None,
        branch_id=branch.id,
        distance_km=distance_km,
    )


@tool
def arrange_delivery(order_number: str = "", notes: str = "") -> dict:
    """Hand the paid order to the delivery service so a rider collects it.

    Call this only after the order is placed *and* paid — it re-reads the order
    from the restaurant and refuses if anything is still owed on it.

    This starts a delivery; it does not complete one. A successful call means a
    courier has the job, not that the food has arrived. Use check_delivery to
    find out where it has got to, and never report an order as delivered on the
    strength of this tool succeeding.

    Args:
        order_number: Defaults to the order placed during this errand.
        notes: Anything the rider needs — a gate code, "leave at reception".

    Returns:
        The delivery job: which service has it, its id, its status, and an ETA
        if the service gave one.
    """
    number = order_number or current_order().get("orderNumber", "")
    if not number:
        return _fail("No order has been placed yet — place and pay for one first.")

    if _job.get("jobId"):
        # Two riders sent to collect one order is a real cost to somebody, and
        # a model that has lost track of what it already did is exactly how it
        # would happen.
        return _fail(
            f"Delivery for this errand is already arranged — job {_job['jobId']} with "
            f"{_job.get('deliveryService')}. Use check_delivery to see where it is."
        )

    provider = registry.get()

    try:
        request = _build_request(number, notes.strip() or None)
        job = provider.dispatch(request)
    except DeliveryRejected as exc:
        # Our own refusal: the request was not fit to send. The order stands,
        # paid, and the agent should say so rather than retrying.
        return _fail(str(exc))
    except DeliveryError as exc:
        return _fail(
            f"{exc} The order is placed and paid — it just has no rider yet. "
            "Report it that way rather than trying again."
        )

    _job.clear()
    _job.update(
        {
            "jobId": job.job_id,
            "provider": job.provider,
            "deliveryService": provider.display_name,
            "status": job.status,
        }
    )

    result = _job_view(job, provider.display_name)
    result["orderNumber"] = request.order_number
    result["pickupFrom"] = request.pickup.name
    result["deliveringTo"] = request.dropoff.address
    result["itemCount"] = sum(item.quantity for item in request.items)
    result["next"] = (
        "A courier has the job. Use check_delivery for its progress — it is not "
        "delivered until the status says so."
    )
    return result


@tool
def check_delivery() -> dict:
    """Ask the delivery service where the order has got to.

    Reports the courier's own status. `delivered` is true only when the food has
    actually reached the customer — anything else means it is still on its way,
    however far along it is.

    Returns:
        The job's current status, its courier and ETA if there are any.
    """
    if not _job.get("jobId"):
        return _fail("No delivery has been arranged for this errand yet.")

    provider = registry.get(_job.get("provider"))

    try:
        job = provider.status(_job["jobId"])
    except DeliveryError as exc:
        # A courier that has stopped answering does not undo the dispatch, so
        # the last known status is still the truthful answer — with the caveat.
        return _fail(
            f"{exc} The delivery was arranged (job {_job['jobId']}) and was last seen "
            f"at {_job.get('status')}, but its current status cannot be read."
        )

    _job["status"] = job.status
    return _job_view(job, _job.get("deliveryService") or provider.display_name)


#: The delivery half of the toolset. Added to the agent only when the errand
#: was started with a location — see agent/friends_kitchen_agent.py. An errand
#: with nowhere to deliver to should not be offered a tool it cannot use.
DELIVERY_TOOLS = [
    check_delivery_location,
    arrange_delivery,
    check_delivery,
]
