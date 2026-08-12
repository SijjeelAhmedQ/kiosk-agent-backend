"""The buyer's hands — and there are only six of them, on purpose.

This agent cannot browse the menu, cannot add anything to a basket, and has no
idea what a product id looks like. Everything it wants, it has to ask another
agent for in words. That constraint is the whole exercise: give the buyer the
restaurant's tools and you have not built agent-to-agent ordering, you have
built the same single agent with an extra hop in the middle.

Two of these tools are gates rather than actions, and both are gates the model
cannot talk its way through:

* `offer_coupon` sends the code **from the wallet**, never from the model. The
  agent is never told the code and cannot type one, so it cannot invent one,
  leak one, or fat-finger one.
* `authorize_payment` is the only tool that can cause money to move, and it
  checks the amount against the wallet in Python before it sends anything. A
  prompt is a request; this is a refusal.

And one that exists purely out of distrust: `verify_order` asks the restaurant
directly what happened, rather than believing the receipt the other agent sent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from strands import tool

from agent import kiosk_api
from agent.a2a.merchant_client import MerchantConnection, MerchantRefused, MerchantUnreachable
from agent.a2a.protocol import (
    QUOTE,
    RECEIPT,
    TERMINAL_STATES,
    Message,
    event_message,
    parts_text,
)
from agent.a2a.tasks import ConsoleRun
from agent.kiosk_api import KioskApiError
from agent.wallet import BudgetExceeded, Wallet, rupees


@dataclass
class BuyerSession:
    """One errand's worth of state on the buyer's side."""

    run: ConsoleRun
    wallet: Wallet
    merchant: MerchantConnection

    # The most recent *firm* quote the merchant sent — the one with tax in it
    # and an amount due. Estimates are deliberately not kept: paying against an
    # estimate is how an agent overspends while believing it did not.
    firm_quote: dict[str, Any] = field(default_factory=dict)

    receipt: dict[str, Any] = field(default_factory=dict)
    paid: bool = False

    # The merchant's task id is announced to the console once, not per message.
    announced_task: bool = False

    # A redemption is reported in every revised quote that follows it; the
    # wallet must only hear about it the first time.
    coupon_recorded: bool = False


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _recover(session: BuyerSession, problem: str) -> str:
    """Drop a dead conversation so the next message opens a fresh one.

    A merchant task that has failed stays failed — every further message to it
    is refused, and an agent told only "refused" will keep rephrasing into the
    same wall until its token budget is gone. Clearing the task id turns the
    next `talk_to_merchant` into a new conversation, and saying so is what makes
    that a choice rather than a surprise.

    Not done once anything has been paid for: a completed conversation is
    finished, and starting another would order the food twice.
    """
    if session.paid:
        return problem
    session.merchant.task_id = None
    session.firm_quote = {}
    return (
        f"{problem} That conversation is over and cannot be continued. Your next "
        "message will open a fresh one — the merchant will have no memory of the "
        "basket, so state the whole order again."
    )


def _record_coupon(quote: dict[str, Any], session: BuyerSession) -> None:
    """Note what the coupon took off, once.

    Coupon value is not cash and is never checked against the ceiling — but it
    is half of what the errand is judged on, and a report that says "the coupon
    covered nothing" after a redemption is simply wrong. The merchant re-sends
    the revised quote whenever anything changes, so this has to be idempotent.
    """
    discount = quote.get("couponDiscount")
    if not discount or session.coupon_recorded:
        return
    amount = float(discount.get("amount", 0))
    if amount <= 0:
        return
    session.coupon_recorded = True
    session.wallet.record_coupon(amount)
    session.run.wallet = session.wallet.summary()


def _digest(reply: dict[str, Any], session: BuyerSession) -> dict:
    """Turn a merchant reply into something the buyer's model can act on.

    Artifacts are filed on the way through — that is what keeps ids and amounts
    out of the model's memory, where they get paraphrased.
    """
    said = reply.get("message") or {}
    text = "\n".join(
        part["text"] for part in said.get("parts", []) if part.get("kind") == "text"
    )

    quotes: list[dict] = []
    for artifact in reply.get("artifacts", []):
        data = artifact["parts"][0]["data"]
        if artifact["name"] == QUOTE:
            quotes.append(data)
            if data.get("kind") == "firm":
                session.firm_quote = data
                _record_coupon(data, session)
        elif artifact["name"] == RECEIPT:
            session.receipt = data
            session.paid = True

    out: dict[str, Any] = {
        "ok": True,
        "merchantSaid": text,
        "conversationState": reply.get("state"),
    }
    if quotes:
        out["quotes"] = quotes

    verdict = _affordability(session, quotes)
    if verdict:
        out["walletCheck"] = verdict
    return out


def _affordability(session: BuyerSession, quotes: list[dict]) -> dict[str, Any] | None:
    """What the latest quote costs, measured against the wallet — in Python.

    This rides along on every merchant reply rather than waiting to be asked,
    because the failure it prevents is not the model doing arithmetic badly. It
    is the model not doing the arithmetic at all: told a subtotal and a budget
    it will agree to the order, and only discover at the till that tax pushed it
    over — by which time the order is on the restaurant's books and the only
    honest outcome left is an unpaid one.

    So the figure compared here is always the payable one: the amount due after
    any coupon on a firm quote, and the tax-inclusive estimate before that.
    """
    firm = session.firm_quote
    latest = firm or next((q for q in quotes if q.get("kind") == "estimate"), None)
    if not latest:
        return None

    payable_field = "amountDue" if firm else "estimatedTotal"
    payable = float(latest.get(payable_field, {}).get("amount", 0) or 0)
    if payable <= 0:
        return None

    remaining = session.wallet.remaining
    affordable = payable <= remaining + 1e-9
    coupon_pending = bool(session.wallet.coupon_code) and not session.coupon_recorded

    if affordable:
        note = "This fits your cash limit. You may pay for it."
    elif coupon_pending:
        note = (
            "This is more cash than you have, but your coupon has not been applied "
            "yet — offer it and get a revised quote before agreeing to anything."
        )
    else:
        note = (
            "You cannot pay this. Do not let the merchant confirm it — ask for "
            "items to be removed or swapped, then get a new quote."
        )

    return {
        "basis": "firm quote" if firm else "estimate including tax",
        "payable": rupees(payable),
        "cashRemaining": rupees(remaining),
        "affordable": affordable,
        "shortfall": None if affordable else rupees(round(payable - remaining, 2)),
        "couponStillUnapplied": coupon_pending,
        "note": note,
    }


def build_tools(session: BuyerSession) -> list:
    """The buyer's toolset, bound to one errand."""

    async def _send(message: str, data: dict | None = None) -> dict:
        """One exchange, narrated into the console's transcript as it happens.

        Both sides are emitted here rather than left to the merchant's own
        stream, because the console follows the run and a transcript with only
        half a conversation in it is worse than none.
        """
        outgoing = Message.say("buyer", message, data)
        session.run.stream.emit(event_message(outgoing))

        try:
            reply = await session.merchant.send(message, data, message_id=outgoing.message_id)
        except MerchantUnreachable as exc:
            return _fail(str(exc))
        except MerchantRefused as exc:
            return _fail(f"The merchant refused: {_recover(session, str(exc))}")

        # A turn that broke hands back an error and no message. Reading that as
        # a reply — or worse, as the previous reply — is how a buyer ends up
        # negotiating with a conversation that has already died.
        if reply.get("error") or not reply.get("message"):
            problem = reply.get("error") or "The merchant answered with nothing at all."
            return _fail(_recover(session, str(problem)))

        # Said once, the first time there is a task to point at: the console
        # uses it to open a second stream and watch the merchant's own tool
        # calls, which do not come down this one.
        if session.merchant.task_id and not session.announced_task:
            session.announced_task = True
            session.run.stream.emit(
                {
                    "type": "merchant_task",
                    "speaker": "merchant",
                    "taskId": session.merchant.task_id,
                }
            )

        said = reply.get("message") or {}
        if said:
            session.run.stream.emit(
                {
                    "type": "message",
                    "speaker": "merchant",
                    "text": parts_text(said.get("parts", [])),
                    "data": [],
                    "messageId": said.get("messageId"),
                    "ts": said.get("ts"),
                }
            )
        for artifact in reply.get("artifacts", []):
            session.run.stream.emit(
                {
                    "type": "artifact",
                    "speaker": "merchant",
                    "name": artifact["name"],
                    "data": artifact["parts"][0]["data"],
                    "artifactId": artifact.get("artifactId"),
                    "ts": artifact.get("ts"),
                }
            )

        return _digest(reply, session)

    # ----------------------------------------------------------------------- #
    # Finding out who you are dealing with
    # ----------------------------------------------------------------------- #
    @tool
    async def discover_merchant() -> dict:
        """Read the restaurant agent's card before saying anything to it.

        Tells you what it can do and what currency it deals in. Call this first.

        Returns:
            The other agent's name, description and skills.
        """
        try:
            card = await session.merchant.discover()
        except MerchantUnreachable as exc:
            return _fail(str(exc))

        session.run.stream.emit(
            {
                "type": "discovery",
                "speaker": "buyer",
                "name": card.get("name"),
                "skills": [s.get("id") for s in card.get("skills", [])],
                "url": session.merchant.endpoint,
            }
        )
        return {
            "ok": True,
            "name": card.get("name"),
            "description": card.get("description"),
            "skills": [
                {"id": s.get("id"), "description": s.get("description")}
                for s in card.get("skills", [])
            ],
            "currency": card.get("x-currency", {}).get("display"),
        }

    # ----------------------------------------------------------------------- #
    # Talking
    # ----------------------------------------------------------------------- #
    @tool
    async def talk_to_merchant(message: str) -> dict:
        """Say something to the restaurant's agent and read its reply.

        This is how you order. Describe what you want in plain language, ask for
        a quote, ask for something cheaper, ask it to confirm — all of it goes
        through here. You cannot browse the menu yourself; it can.

        Do not use this to authorise payment. That is `authorize_payment`, and
        it is the only tool that may.

        Args:
            message: What to say. One clear request at a time.

        Returns:
            What the merchant said, any quote it sent, and the amount due.
        """
        if not message.strip():
            return _fail("Say something — an empty message tells the merchant nothing.")
        if session.paid:
            return _fail("This order is already paid for. The errand is finished.")
        return await _send(message)

    @tool
    async def offer_coupon(instruction: str = "") -> dict:
        """Offer the merchant the coupon you were sent out with.

        You are not told the code and you do not need it — this tool takes it
        from your wallet and sends it as data, so it cannot be mistyped. Offer
        it once the merchant has quoted, so you know it has something to apply
        the coupon to.

        Args:
            instruction: Anything to say alongside it. Defaults to asking the
                merchant to check what the coupon covers.

        Returns:
            What the merchant said the coupon is worth against this order.
        """
        if not session.wallet.coupon_code:
            return _fail(
                "You were not given a coupon for this errand — pay with your cash limit."
            )
        return await _send(
            instruction or "I am holding a coupon. Check what it covers on this order.",
            {"couponCode": session.wallet.coupon_code},
        )

    # ----------------------------------------------------------------------- #
    # Money
    # ----------------------------------------------------------------------- #
    @tool
    async def check_wallet() -> dict:
        """What you are carrying and what is left of it.

        Returns:
            The coupon you hold, your cash limit, and what remains.
        """
        return {"ok": True, "wallet": session.wallet.display()}

    @tool
    async def authorize_payment() -> dict:
        """Tell the merchant to charge for the order. The only tool that spends.

        Works off the firm quote the merchant sent after confirming the order,
        and refuses outright if the amount due is more than your cash limit. If
        it refuses, do not call it again unchanged — ask the merchant to remove
        something, or offer your coupon, and get a new quote first.

        Returns:
            The receipt, and what is left in your wallet.
        """
        if session.paid:
            return _fail("Already paid. Do not pay twice.")
        if not session.firm_quote:
            return _fail(
                "No firm quote yet. Ask the merchant to confirm the order — an "
                "estimate has no tax in it and is not something to pay against."
            )

        due = float(session.firm_quote.get("amountDue", {}).get("amount", 0))

        try:
            session.wallet.check(due)
        except BudgetExceeded as exc:
            return _fail(str(exc))

        result = await _send("Take the payment for this order.")
        if not result.get("ok", False):
            return result

        if not session.receipt:
            return _fail(
                "The merchant did not send a receipt, so the payment is unconfirmed. "
                "Call verify_order to find out what actually happened."
            )

        charged = float(session.receipt.get("charged", {}).get("amount", due))
        session.wallet.spend(charged)
        session.run.wallet = session.wallet.summary()

        return {
            "ok": True,
            "charged": rupees(charged),
            "orderNumber": session.receipt.get("orderNumber"),
            "transactionRef": session.receipt.get("transactionRef"),
            "wallet": session.wallet.display(),
            "next": "Call verify_order before reporting back.",
        }

    # ----------------------------------------------------------------------- #
    # Distrust
    # ----------------------------------------------------------------------- #
    @tool
    async def verify_order() -> dict:
        """Ask the restaurant directly whether the order really is paid.

        The receipt came from the other agent, and a receipt is a claim. This
        checks it against the restaurant's own records. Do this before you
        report back — if the two disagree, say so plainly.

        Returns:
            The restaurant's own view of the order, and whether it matches the
            receipt you were given.
        """
        number = session.receipt.get("orderNumber")
        if not number:
            return _fail("No receipt to verify — nothing has been paid for yet.")

        try:
            detail = await asyncio.to_thread(kiosk_api.get, f"/orders/number/{number}")
        except KioskApiError as exc:
            return _fail(f"Could not check with the restaurant: {exc}")

        summary = detail.get("summary", {})

        # `amountDue` is *not* an outstanding balance. The restaurant defines it
        # as total minus coupon discount — what the order came to — and it stays
        # at that figure after payment. Whether the money arrived is `status`.
        payable = float(summary.get("amountDue") or 0)
        claimed = float(session.receipt.get("charged", {}).get("amount", 0))
        settled = detail.get("status") == "paid"
        right_amount = abs(claimed - payable) < 0.01

        return {
            "ok": True,
            "orderNumber": detail["orderNumber"],
            "status": detail["status"],
            "paidAccordingToRestaurant": settled,
            "total": rupees(summary.get("total") or 0),
            "couponDiscount": rupees(summary.get("couponDiscount") or 0),
            "shouldHaveBeenCharged": rupees(payable),
            "receiptSaidCharged": rupees(claimed),
            "matches": settled and right_amount,
            "discrepancy": None
            if settled and right_amount
            else (
                f"The restaurant has this order as {detail.get('status')!r}"
                if not settled
                else f"The receipt says {rupees(claimed)} but the order came to {rupees(payable)}."
            ),
            "items": [
                {"name": line["name"], "quantity": line["quantity"]}
                for line in detail.get("lines", [])
            ],
        }

    return [
        discover_merchant,
        talk_to_merchant,
        offer_coupon,
        check_wallet,
        authorize_payment,
        verify_order,
    ]


def conversation_over(state: str | None) -> bool:
    """Has the merchant closed the conversation on its side?"""
    return state in TERMINAL_STATES
