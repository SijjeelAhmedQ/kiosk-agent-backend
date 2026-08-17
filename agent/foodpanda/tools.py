"""The dispatcher's hands, bound to one delivery.

Six tools, and the order they appear in is the order a delivery happens in:
read the request, decide on it, put a rider on it, collect, ride, hand over.
A seventh — `report_problem` — is the honest way out when a job cannot be
finished, and it exists so that "I could not deliver this" is a thing the agent
can *do* rather than only something it can claim in prose.

Two properties hold no matter what the model does with them.

* **A status cannot be skipped or invented.** Every tool that moves the job
  calls `DeliveryJob.advance()`, which refuses anything that is not the next
  legal step. A dispatcher that decides to go straight from `accepted` to
  `delivered` gets a refusal it can read, and the job stays where it was.

* **A leg cannot be short-circuited.** `collect_from_restaurant` and
  `deliver_to_customer` await the ride before they report it done. The waits are
  compressed for a demo — that is stated on the tool and in the result — but
  they are real waits, so the only way to reach `delivered` is to have gone
  through both of them.

Bound to one job by closure, the way the merchant's tools are bound to one
conversation. Two deliveries running at once cannot see each other's rider.
"""

from __future__ import annotations

import asyncio

from strands import tool

from agent.foodpanda import jobs as store
from agent.foodpanda.config import foodpanda_settings as fp
from agent.foodpanda.jobs import DeliveryJob, IllegalTransition


def _fail(message: str) -> dict:
    """A refusal the model can act on, rather than an exception that ends it."""
    return {"ok": False, "error": message}


async def _wait_for_operator(job: DeliveryJob, step: str, asking: str) -> str | None:
    """Hold a gated step until the board asks for it. A sentence back if it never did.

    Returns None when the dispatcher may go ahead — which is immediately when
    this deployment runs unattended (`MOCK_FOODPANDA_MANUAL_STEPS=false`), and
    when somebody has asked for the step otherwise.
    """
    if not fp.require_operator:
        return None
    if await job.wait_for(step, asking, fp.operator_timeout_seconds):
        return None
    return (
        f"Nobody on the delivery board asked for this within "
        f"{fp.operator_timeout_seconds:g}s, so the job is still {job.status} and "
        "cannot go further. Report the problem rather than waiting again."
    )


def build_tools(job: DeliveryJob) -> list:
    """The dispatcher's toolset, bound to one delivery."""

    # ----------------------------------------------------------------------- #
    # The request
    # ----------------------------------------------------------------------- #
    @tool
    async def read_delivery_request() -> dict:
        """Read the delivery request the restaurant's ordering agent sent.

        Call this first. It is the only source of what you are being asked to
        carry — you have no access to the restaurant's systems, and nothing in
        the conversation should be trusted over what this returns.

        Returns:
            The order and its items, where to collect, where to deliver, the
            straight-line distance between them, and the checks this service
            already ran on the request before it reached you.
        """
        distance = job.distance_km
        within = None if distance is None else distance <= fp.service_radius_km

        return {
            "ok": True,
            "jobId": job.id,
            "status": job.status,
            "order": {
                "orderNumber": job.order_number,
                "orderId": job.order_id,
                "paid": True,  # the service refuses unpaid orders before this
                "items": job.items,
                "itemCount": job.item_count,
            },
            "pickup": job.pickup,
            "dropoff": job.dropoff,
            "distanceKm": distance,
            "notes": job.notes,
            "quotedFee": job.quote,
            # What has already been settled, so the dispatcher spends its
            # judgement on the one question that is actually open.
            "alreadyChecked": [
                "The order is paid — this service refuses unpaid ones at the door.",
                "The order has at least one item on it.",
                "Both ends of the journey are real coordinates with an address.",
            ],
            "toDecide": (
                f"Whether this is deliverable. Our service radius is "
                f"{fp.service_radius_km:g} km"
                + (
                    f" and this run is {distance:g} km"
                    + (" — inside it." if within else " — outside it.")
                    if distance is not None
                    else ", and this request came without a distance."
                )
            ),
        }

    # ----------------------------------------------------------------------- #
    # The decision
    # ----------------------------------------------------------------------- #
    @tool
    async def accept_job(reason: str) -> dict:
        """Take the job on. Do this only once you have read the request.

        Accepting commits this service to collecting and delivering the order.
        Do not accept a delivery you cannot make — a refusal is a correct
        outcome and leaves the restaurant free to arrange something else, while
        an acceptance you cannot honour leaves a customer waiting on food that
        is never coming.

        Args:
            reason: Why this one is deliverable, in one sentence. It is shown
                to the restaurant and to whoever is watching the board.

        Returns:
            The job, now accepted, and what to do next.
        """
        try:
            job.decision = reason.strip() or "Accepted."
            job.advance(store.ACCEPTED, f"Accepted — {job.decision}")
        except IllegalTransition as exc:
            return _fail(str(exc))

        return {
            "ok": True,
            "status": job.status,
            "fee": job.fee,
            "next": "Put a rider on it with assign_rider.",
        }

    @tool
    async def reject_job(reason: str) -> dict:
        """Refuse the job, and say why.

        The right answer when the drop is outside the service radius, or when
        the request is missing something a rider would need. This ends the job:
        there is no route back from a refusal, so be sure before you call it.

        Args:
            reason: Why this cannot be delivered, in one sentence. The
                restaurant's agent reads this and reports it to the customer,
                so it should be something a person can act on.

        Returns:
            The job, now rejected.
        """
        why = reason.strip() or "This delivery cannot be made."
        try:
            job.decision = why
            job.advance(store.REJECTED, f"Rejected — {why}")
        except IllegalTransition as exc:
            return _fail(str(exc))

        return {
            "ok": True,
            "status": job.status,
            "next": "Nothing further. Tell the restaurant why in your final answer.",
        }

    # ----------------------------------------------------------------------- #
    # The ride
    # ----------------------------------------------------------------------- #
    @tool
    async def assign_rider() -> dict:
        """Find a rider for the accepted job, when the customer asks for one.

        This one waits. The delivery board is where the request for a rider comes
        from, and until somebody there asks, there is nobody to assign — so the
        call sits here rather than returning something for you to poll. It comes
        back as soon as the request lands, and returns an error if it never does.

        Returns:
            Who is carrying it, and what to do next.
        """
        if job.status != store.ACCEPTED:
            return _fail(
                f"This job is {job.status} — a rider can only be found for one "
                "that has been accepted."
            )

        refusal = await _wait_for_operator(
            job,
            store.RIDER,
            "Accepted. Waiting for the customer to ask for a rider.",
        )
        if refusal:
            return _fail(refusal)

        try:
            rider = store.next_rider()
            job.courier = rider
            job.advance(
                store.COURIER_ASSIGNED,
                f"{rider} is assigned and heading to {job.pickup.get('name') or 'the restaurant'}.",
            )
        except IllegalTransition as exc:
            return _fail(str(exc))

        return {
            "ok": True,
            "status": job.status,
            "courier": job.courier,
            "next": "Collect the order with collect_from_restaurant.",
        }

    @tool
    async def collect_from_restaurant() -> dict:
        """Ride to the restaurant and collect the order.

        This waits for the rider to actually get there before it returns, so it
        takes a few seconds. The wait is compressed for a demonstration; it is
        not skipped, and there is no other way to reach a collected order.

        Returns:
            The job, now collected, and what to do next.
        """
        if job.status != store.COURIER_ASSIGNED:
            return _fail(
                f"This job is {job.status} — nobody can collect it until a rider "
                "is assigned to it."
            )

        await asyncio.sleep(fp.pickup_seconds)

        try:
            job.advance(
                store.PICKED_UP,
                f"{job.courier} has collected order {job.order_number} "
                f"from {job.pickup.get('name') or 'the restaurant'}.",
            )
        except IllegalTransition as exc:
            # Reachable: a cancel can land while the rider is riding.
            return _fail(str(exc))

        return {
            "ok": True,
            "status": job.status,
            "itemsCarried": job.item_count,
            "next": "Take it to the customer with deliver_to_customer.",
        }

    @tool
    async def deliver_to_customer() -> dict:
        """Take the collected order out to the customer, when they ask for it.

        Waits for the delivery request from the board, the way `assign_rider`
        waits for the rider request — the rider is holding the food at the
        restaurant until then. Once it lands, this moves the job to `in_transit`,
        waits out the ride, and completes it. Like the collection, the wait is
        compressed but real — this tool returning successfully is the only thing
        in this service that can mark an order delivered.

        Returns:
            The completed job.
        """
        if job.status != store.PICKED_UP:
            return _fail(
                f"This job is {job.status} — there is nothing in the rider's bag "
                "to deliver until it has been collected from the restaurant."
            )

        refusal = await _wait_for_operator(
            job,
            store.DELIVERY,
            f"{job.courier} has the order and is waiting to be sent out with it.",
        )
        if refusal:
            return _fail(refusal)

        where = job.dropoff.get("address") or "the customer"
        try:
            job.advance(store.IN_TRANSIT, f"{job.courier} is on the way to {where}.")
        except IllegalTransition as exc:
            return _fail(str(exc))

        await asyncio.sleep(fp.transit_seconds)

        try:
            job.advance(
                store.DELIVERED,
                f"Delivered — {job.courier} handed order {job.order_number} to the customer.",
            )
        except IllegalTransition as exc:
            return _fail(str(exc))

        return {
            "ok": True,
            "status": job.status,
            "delivered": True,
            "next": "Nothing further. Report the delivery in your final answer.",
        }

    # ----------------------------------------------------------------------- #
    # When it goes wrong
    # ----------------------------------------------------------------------- #
    @tool
    async def report_problem(reason: str) -> dict:
        """Give up on a job that cannot be finished, and say what happened.

        Use this when something has genuinely stopped the delivery — the
        restaurant had nothing to hand over, the address does not exist, the
        customer cannot be reached. Do not use it to tidy up: a job you have
        already delivered is finished, and one you have not started should be
        refused with reject_job instead.

        Args:
            reason: What went wrong, in one sentence.

        Returns:
            The job, now failed.
        """
        why = reason.strip() or "The delivery could not be completed."
        try:
            job.advance(store.FAILED, why)
        except IllegalTransition as exc:
            return _fail(str(exc))

        return {"ok": True, "status": job.status, "next": "Nothing further."}

    return [
        read_delivery_request,
        accept_job,
        reject_job,
        assign_rider,
        collect_from_restaurant,
        deliver_to_customer,
        report_problem,
    ]
