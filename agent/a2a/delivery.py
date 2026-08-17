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

    deliver_to = _drop_for(buyer_id)

    try:
        provider, request, job = await asyncio.to_thread(
            handover.dispatch,
            number,
            deliver_to,
            None,
            str(order.get("orderId") or ""),
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
        "deliveryService": provider.display_name,
        "jobId": job.job_id,
        "status": job.status,
        "delivered": job.delivered,
        "deliveringTo": request.dropoff.address,
        "pickupFrom": request.pickup.name,
        "distanceKm": request.distance_km,
        "etaMinutes": job.eta_minutes,
        "fee": job.fee,
        # Said in the tool result because the merchant repeats it to the buyer,
        # and "a courier has it" is the sentence most likely to be rounded up to
        # "it has arrived". It has not: the job is on the delivery board, and the
        # rider is found when somebody there asks for one.
        "note": (
            f"{provider.display_name} has the order on its board and is waiting for "
            "the customer to ask for a rider. It is not delivered — nothing here "
            "makes it delivered."
        ),
    }
