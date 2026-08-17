"""The agent's hands, for the half of the errand that happens after payment.

Three tools, and the order they are meant to be used in is the order they
appear: find out where the customer is and which branch serves them, hand the
paid order to a delivery agent, then ask that agent where it has got to.

The middle step is not usually the agent's to take. `auto_dispatch` is called by
the payment tools the instant a take-away order is bought, so the handover
happens whether or not the model thinks to ask for it — `arrange_delivery` is
left as the retry, not the route. See that function for why.

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

from agent import branches, location
from agent.delivery import handover, registry
from agent.delivery.contract import (
    DeliveryError,
    DeliveryJob,
    DeliveryRejected,
)
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
def dispatch_now(order_number: str = "", notes: str | None = None) -> dict:
    """The handover itself — the same work whether a tool asked for it or not.

    Not a `@tool`: this is what `arrange_delivery` does when the model calls it
    and what `auto_dispatch` does when payment triggers it, and having one body
    is what keeps the two paths from drifting into two different ideas of when a
    courier may be sent.
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

    user_location = location.current()
    if user_location is None:
        return _fail(
            "This errand has no customer location, so there is nowhere to deliver to. "
            "It is a counter order — report it as placed and paid, and stop there."
        )

    try:
        provider, request, job = handover.dispatch(
            number,
            user_location,
            (notes or "").strip() or None,
            order_id_fallback=current_order().get("orderId", ""),
        )
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


def auto_dispatch(order_number: str = "") -> dict | None:
    """Hand a paid take-away order over the moment it is bought, unasked.

    Called by the payment tools, not by the model. That is the point: a delivery
    that depends on the agent remembering to arrange one is a delivery that goes
    missing on the run where it forgets, and the customer is left with a paid
    order and no rider. Payment succeeding is the fact that makes an order
    dispatchable, so payment succeeding is what dispatches it.

    Returns None when there is nothing to do — a counter errand with no location,
    or a job already arranged — so the caller can leave its own result untouched.
    Never raises: the money has already moved by the time this runs, and a
    courier that will not answer must not turn a successful payment into a failed
    tool call. A failure comes back as `{"ok": False, "error": …}` for the agent
    to report alongside the order it did buy.
    """
    if location.current() is None:
        return None
    if _job.get("jobId"):
        return None

    try:
        return dispatch_now(order_number)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return _fail(
            f"The order is paid, but handing it to the delivery service failed: {exc} "
            "Report it as bought but without a rider."
        )


@tool
def arrange_delivery(order_number: str = "", notes: str = "") -> dict:
    """Hand a paid order to the delivery service — only if it was not already.

    You do not normally need this. A paid take-away order on a delivery errand
    is handed to the courier automatically, and authorize_payment tells you the
    job it created. Call this only when that automatic handover reported a
    failure and you have a reason to think a second attempt would do better, or
    when you need to send the rider a note.

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
    return dispatch_now(order_number, notes)


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
