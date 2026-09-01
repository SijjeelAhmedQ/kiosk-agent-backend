"""Handing a negotiated order to the delivery agent.

The errand flow on 8100 has always done this: payment succeeding is what makes
an order dispatchable, so payment succeeding is what dispatches it. This is the
same rule for the other flow that buys food — the merchant in an A2A
negotiation — and it exists as its own module for one reason worth stating.

**The negotiation has no customer in it.** Two agents talk on a port; there is no
browser to ask for a location and no person to type one. So the drop comes from
the *console* that sent the buyer out — the operator's "Where it goes" — and
falls back to the customer's saved address (`agent/location.saved()`) when no
drop was named, which is what this flow always did. That is the whole of the
difference between this handover and the errand one. Everything else — reading
the order back, re-deriving that it is paid, choosing the branch, validating the
message — is `agent/delivery/handover.py`, shared, so the two flows cannot drift
into two different ideas of what a courier may be sent.

The drop is looked up rather than passed down the negotiation, and deliberately:
coordinates in a message are coordinates a model can retype. The merchant knows
only the opaque `buyerId` the conversation was opened with, which is the console
run's id, and the address is read from that run here at the last moment.

The customer's consent travels the same way and for a sharper version of the
same reason. The console's "Where it goes" switch is the customer saying *bring
it to me*; this reads it off the run and puts it on the delivery request, so the
delivery agent knows it has already been asked and does not stop at its own
"Deliver it to me" gate to ask a second time. A consent that had crossed as
prose would be a consent a model could invent — so it never enters the
conversation at all.

Nothing here raises. By the time it runs the money has moved, and a courier that
will not answer must not turn a completed sale into a failed tool call. A failure
comes back as a sentence for the merchant to pass on to the buyer's agent, which
is what a restaurant would do: your order is bought, and it has no rider yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent import location
from agent.a2a import tasks as store
from agent.delivery import handover
from agent.delivery.contract import DeliveryError


def _drop_for(buyer_id: str | None) -> location.UserLocation:
    """Where this conversation's order goes.

    The console run named by `buyer_id`, if it named a drop; otherwise the saved
    address. A buyer that is not this console — a stranger's agent, which is the
    whole point of the protocol half of this service — has no run to look up, and
    lands on the saved address like every negotiation did before.
    """
    run = store.console_runs.get(buyer_id) if buyer_id else None
    return getattr(run, "drop", None) or location.saved()


def _requested(buyer_id: str | None) -> bool:
    """Did this errand ask for the order to be delivered at all?

    The gate that has to come before the drop is looked up, because
    `location.saved()` never returns nothing: ask it where to deliver and it
    always answers with the machine owner's address. So a handover that only
    ever asked "where" could not tell "bring it to my flat" apart from "nobody
    said anything about delivery", and the second one was silently being treated
    as the first — an order bought with no destination chosen went to the
    courier anyway, addressed to whoever this laptop belongs to.

    Either half of the console's delivery question counts as asking. A drop is
    the operator naming a destination, and the "Where it goes" switch is the
    customer saying bring it to me; the switch on its own is a request against
    the saved address, which is exactly what that address is for.

    Neither, or no run at all, is not a request. That includes a stranger's
    agent, which is the case worth being explicit about: it has named no address
    and thrown no switch, and the saved one belongs to somebody it has never
    met. An order it bought is a take-away order waiting to be collected, which
    is the honest reading of a negotiation in which delivery never came up.
    """
    run = store.console_runs.get(buyer_id) if buyer_id else None
    if run is None:
        return False
    return bool(getattr(run, "drop", None)) or bool(getattr(run, "where_it_goes", False))


def _consent_for(buyer_id: str | None) -> bool:
    """Did the customer behind this conversation ask for it to be delivered?

    Looked up here rather than passed down the negotiation, for the same reason
    the drop is: what crosses between the two agents is an order, and a consent
    that travelled as prose through a model is a consent that can be invented by
    one. The console recorded the answer on the run when the operator threw the
    switch; this reads it back at the moment it is needed.

    False for anything that is not one of this console's runs — a stranger's
    agent has said nothing about a "Deliver it to me" button it has never seen,
    and the delivery agent should ask rather than assume.
    """
    run = store.console_runs.get(buyer_id) if buyer_id else None
    return bool(getattr(run, "where_it_goes", False))


async def hand_over(
    order: dict[str, Any], buyer_id: str | None = None
) -> dict[str, Any] | None:
    """Dispatch a paid take-away order to the delivery agent.

    Args:
        order: The merchant session's order — `orderNumber`, `orderId` and
            `orderType`, as `confirm_order` recorded them.
        buyer_id: Whoever opened the conversation. The console puts its run id
            here, which is how the operator's chosen drop is found.

    Returns:
        What the merchant should tell the buyer about the delivery, or None when
        there is nothing to hand over: a dine-in order is eaten where it was
        bought, and an order that was never confirmed has no number to collect
        against.
    """
    number = str(order.get("orderNumber") or "")
    if not number or order.get("orderType") != "take_away":
        return None

    # Nobody asked for this to be delivered. Not an error and not silence: the
    # merchant's brief tells it a paid take-away order goes to the courier by
    # itself, so saying nothing here is how it ends up describing a delivery
    # that was never arranged. This is the third answer — bought, and waiting to
    # be collected.
    if not _requested(buyer_id):
        return {
            "ok": True,
            "arranged": False,
            "note": (
                "No delivery was asked for on this errand — no address was given "
                "and nobody asked for it to be brought to them. The order is "
                "bought and is waiting to be collected. No courier has it and "
                "none was called."
            ),
        }

    deliver_to = _drop_for(buyer_id)
    where_it_goes = _consent_for(buyer_id)

    try:
        provider, request, job = await asyncio.to_thread(
            handover.dispatch,
            number,
            deliver_to,
            None,
            str(order.get("orderId") or ""),
            where_it_goes,
        )
    except DeliveryError as exc:
        return {
            "ok": False,
            "error": (
                f"{exc} The order is placed and paid — it just has no rider yet. "
                "Tell the customer's agent that plainly rather than trying again."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        return {
            "ok": False,
            "error": (
                f"The order is paid, but handing it to the delivery service failed: "
                f"{exc} Report it as bought but without a rider."
            ),
        }

    return {
        "ok": True,
        # Told apart from the "nobody asked" answer above, which is also ok:
        # both are successful outcomes and only one of them has a courier in it.
        "arranged": True,
        "deliveryService": provider.display_name,
        "jobId": job.job_id,
        "status": job.status,
        "delivered": job.delivered,
        "deliveringTo": request.dropoff.address,
        "pickupFrom": request.pickup.name,
        "distanceKm": request.distance_km,
        "etaMinutes": job.eta_minutes,
        "fee": job.fee,
        # The consent that went with it, reported back so the console's trace
        # shows what was handed over rather than only where. Worth its own field
        # on a result a person reads: "the customer will not be asked again" is a
        # promise this service just made on their behalf.
        "whereItGoes": where_it_goes,
        # Said in the tool result because the merchant repeats it to the buyer,
        # and "a courier has it" is the sentence most likely to be rounded up to
        # "it has arrived". It has not: the job is on the delivery board, and the
        # rider is found when somebody there asks for one.
        "note": (
            f"{provider.display_name} has the order on its board and is waiting for "
            "the customer to ask for a rider. It is not delivered — nothing here "
            "makes it delivered."
            + (
                " The customer already asked for it to be delivered to them, so "
                "the delivery agent will not stop to ask again."
                if where_it_goes
                else " They will be asked to confirm the delivery on the delivery "
                "board before it is brought out."
            )
        ),
    }
