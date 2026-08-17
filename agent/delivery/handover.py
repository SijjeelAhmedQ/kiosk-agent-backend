"""Turning a paid order into a delivery request, for whichever flow paid for it.

Two agents in this project buy food: the errand agent on 8100 and the A2A
merchant on 8101. Both end the same way — an order the restaurant has been paid
for and a customer who is not standing at the counter — so both hand over
through here rather than each assembling its own message.

The rule this module exists to hold is that **nothing is taken from the agent's
memory**. The order and its lines are re-read from the restaurant, the payment
is re-derived from the payment ledger, the pickup point comes from the branch
directory and the drop from a `UserLocation` the caller was given. A model that
retyped a latitude could move a delivery a hundred kilometres and nothing about
the message would look wrong on the way past, so the message is built from
facts every time.

What is *not* here: when to hand over, and what to tell whoever asked. Those
differ between the two flows — the errand agent dispatches the instant payment
succeeds and reports it in a tool result, the merchant does it inside a
negotiation and reports it to another agent — so they stay with their callers.
"""

from __future__ import annotations

from agent import branches, friends_kitchen_api
from agent.delivery import registry
from agent.delivery.contract import (
    DeliveryItem,
    DeliveryJob,
    DeliveryProvider,
    DeliveryRejected,
    DeliveryRequest,
    Place,
)
from agent.friends_kitchen_api import FriendsKitchenApiError
from agent.location import UserLocation


def build_request(
    order_number: str,
    deliver_to: UserLocation,
    notes: str | None = None,
    order_id_fallback: str = "",
) -> DeliveryRequest:
    """Assemble the message from facts, not from what an agent remembers.

    Args:
        order_number: The order to collect. Read back from the restaurant here.
        deliver_to: Where the customer is — their own fix, or the saved address.
        notes: Anything the rider needs. Free text from the customer.
        order_id_fallback: Used only if the restaurant's own record has no id.

    Raises:
        DeliveryRejected: The order could not be read back, which leaves no
            honest way to confirm it is paid for.
    """
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

    branch, distance_km = branches.nearest(deliver_to)

    return DeliveryRequest(
        order_id=str(detail.get("orderId") or order_id_fallback or ""),
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
            latitude=deliver_to.latitude,
            longitude=deliver_to.longitude,
            address=deliver_to.label or deliver_to.display(),
            note=deliver_to.label,
        ),
        items=items,
        notes=notes or None,
        branch_id=branch.id,
        distance_km=distance_km,
    )


def dispatch(
    order_number: str,
    deliver_to: UserLocation,
    notes: str | None = None,
    order_id_fallback: str = "",
) -> tuple[DeliveryProvider, DeliveryRequest, DeliveryJob]:
    """Build the message and hand it to the configured delivery agent.

    Returns the provider that took it, the request as sent, and the job it
    answered with — all three, because a caller reporting a handover needs to
    name the service and the address as well as the job id.

    Raises:
        DeliveryRejected: The request was not fit to send.
        DeliveryUnavailable: The courier could not be reached, or refused it.
    """
    provider = registry.get()
    request = build_request(order_number, deliver_to, notes, order_id_fallback)
    return provider, request, provider.dispatch(request)
