"""The merchant's hands, built fresh for each conversation.

Two things make this different from `agent/tools/api_tools.py`, which does the
same job for the errand flow, and they are why this is a separate file rather
than an import:

**No module-level state.** Those tools keep the cart and the placed order in
module globals, which is correct for one-errand-per-process and wrong here —
two buyers can be negotiating at the same time, and their baskets must not meet.
So the tools are closures built by `build_tools(session)`, and every piece of
state hangs off the session.

**No wallet.** The merchant is not the one with a budget. It quotes honestly and
charges what it says it will; refusing to overspend is the *buyer's* job, done
in Python on the other side of the wire. A merchant that policed the customer's
money would be doing the one thing this whole exercise is meant to demonstrate
the buyer doing for itself.

Everything is async because the buyer is waiting on an HTTP request in this same
process while these run — see agent/a2a/merchant_client.py. The restaurant's
client is synchronous and shared, so calls go out through a worker thread rather
than through a second HTTP client with its own idea of the base URL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from strands import tool

from agent import kiosk_api
from agent.a2a.protocol import QUOTE, RECEIPT, Artifact, event_artifact
from agent.a2a.tasks import MerchantTask
from agent.cart import Cart
from agent.kiosk_api import KioskApiError
from agent.wallet import money, rupees


@dataclass
class MerchantSession:
    """Everything one conversation accumulates."""

    task: MerchantTask
    cart: Cart = field(default_factory=Cart)

    # The order once placed: {orderId, orderNumber, paymentMethod, total,
    # amountDue}. Empty until then, which is what the tools check.
    order: dict[str, Any] = field(default_factory=dict)

    # The last coupon the buyer offered. Held so `redeem_coupon` does not have
    # to ask the model to carry a code between two tool calls.
    coupon_code: str | None = None

    # The restaurant's tax rate, fetched once. Quoting without it hands the
    # other agent a subtotal and lets it discover the real total only after the
    # order is on the books — which is exactly how a buyer ends up committed to
    # something it cannot pay for.
    tax_rate: float | None = None


async def _get(path: str, **params: Any) -> Any:
    return await asyncio.to_thread(kiosk_api.get, path, **params)


async def _post(path: str, body: dict[str, Any]) -> Any:
    return await asyncio.to_thread(kiosk_api.post, path, body)


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _slim(product: dict) -> dict:
    """A product as the merchant should see it — no artwork, no plumbing.

    `imgBase64` photos would swamp the context window, so they never leave here.
    """
    return {
        "productId": product["id"],
        "name": product["name"],
        "description": product.get("description", ""),
        "price": rupees(product["price"]),
        "calories": product.get("calories", 0),
        "categoryId": product.get("categoryId"),
        "isMealEligible": product.get("isMealEligible", False),
    }


def _amount(value: float | int | None) -> dict[str, Any]:
    """One figure, twice: the number for arithmetic, the text for reading.

    Artifacts cross the wire to another agent, and that agent needs both. Its
    wallet has to do real arithmetic against the total, so a formatted string
    alone is useless; its *model* reads the same artifact, and a bare `1093` is
    exactly the number a model reports as "$10.93". Sending both costs a few
    bytes and removes the choice.
    """
    number = float(value or 0)
    return {"amount": round(number, 2), "text": rupees(number)}


async def _tax_rate(session: MerchantSession) -> float:
    """The restaurant's tax rate, fetched once per conversation.

    Falls back to zero rather than failing the quote: an estimate without tax is
    worse than one with it, and better than no quote at all.
    """
    if session.tax_rate is None:
        try:
            settings = await _get("/settings")
            session.tax_rate = float(settings.get("TaxRate", 0) or 0)
        except (KioskApiError, TypeError, ValueError):
            session.tax_rate = 0.0
    return session.tax_rate


def build_tools(session: MerchantSession) -> list:
    """The merchant's toolset, bound to one conversation."""

    # ----------------------------------------------------------------------- #
    # Menu
    # ----------------------------------------------------------------------- #
    @tool
    async def list_categories() -> dict:
        """List the menu's categories (Burgers, Sides, Drinks, and so on).

        Returns:
            Category ids and names, for narrowing a later browse_menu call.
        """
        try:
            categories = await _get("/categories")
        except KioskApiError as exc:
            return _fail(str(exc))
        return {
            "ok": True,
            "categories": [{"categoryId": c["id"], "name": c["name"]} for c in categories],
        }

    @tool
    async def browse_menu(search: str = "", category_id: str = "") -> dict:
        """Look up what the restaurant sells.

        Call this before adding anything — product ids are generated by the
        restaurant and cannot be guessed from a name.

        Args:
            search: Case-insensitive match against product name and description.
            category_id: Restrict to one category id from list_categories.

        Returns:
            Matching products with their ids, names and prices as `Rs 450`.
        """
        try:
            products = await (
                _get(f"/categories/{category_id}/products") if category_id else _get("/products")
            )
        except KioskApiError as exc:
            return _fail(str(exc))

        items = [_slim(p) for p in products]

        if search:
            needle = search.lower()
            matched = [
                item
                for item in items
                if needle in item["name"].lower() or needle in item["description"].lower()
            ]
            # An empty result is a dead end, so hand back the whole menu instead
            # and say why — the nearest match is a judgement call, not a lookup.
            if not matched:
                return {
                    "ok": True,
                    "matched": 0,
                    "note": f"Nothing matched {search!r}. Showing the full menu instead.",
                    "products": items,
                }
            items = matched

        return {"ok": True, "matched": len(items), "products": items}

    # ----------------------------------------------------------------------- #
    # Basket
    # ----------------------------------------------------------------------- #
    @tool
    async def add_to_basket(product_id: str, quantity: int = 1, as_meal: bool = False) -> dict:
        """Put an item in this customer's basket.

        Args:
            product_id: An id from browse_menu — not the product's name.
            quantity: How many, 1 or more.
            as_meal: Upgrade to a meal (adds fries and a drink for an upcharge).
                Only valid when the product's isMealEligible is true.

        Returns:
            The basket after the addition.
        """
        if quantity < 1:
            return _fail("Quantity must be at least 1.")

        try:
            product = await _get(f"/products/{product_id}")
        except KioskApiError as exc:
            return _fail(
                f"{exc} Call browse_menu for a valid product id — ids are not product names."
            )

        if as_meal and not product.get("isMealEligible", False):
            return _fail(f"{product['name']} cannot be made a meal. Add it on its own.")

        line = session.cart.add(
            product_id=product["id"],
            name=product["name"],
            unit_price=product["price"],
            quantity=quantity,
            is_meal=as_meal,
        )
        return {"ok": True, "added": line.to_view(), "basket": session.cart.to_view()}

    @tool
    async def remove_from_basket(line_id: str) -> dict:
        """Take a line back out of the basket.

        Use this when the customer's agent asks for something cheaper rather
        than placing a second, smaller order.

        Args:
            line_id: The lineId shown by view_basket.

        Returns:
            The basket after the removal.
        """
        if not session.cart.remove(line_id):
            return _fail(f"No basket line {line_id!r}. Call view_basket for the current lineIds.")
        return {"ok": True, "basket": session.cart.to_view()}

    @tool
    async def view_basket() -> dict:
        """Show what is in the basket right now, with an estimated subtotal.

        Returns:
            The basket lines, item count and estimated subtotal in rupees.
        """
        return {"ok": True, "basket": session.cart.to_view()}

    # ----------------------------------------------------------------------- #
    # Quoting — the artifact the other agent checks against its budget
    # ----------------------------------------------------------------------- #
    @tool
    async def send_quote(note: str = "") -> dict:
        """Hand the customer's agent a priced quote for the current basket.

        Send one before asking anyone to commit. This is an *estimate*: the
        restaurant re-prices at checkout, so `confirm_order` is what produces
        the firm figure. Tax is included here at the going rate, because the
        other agent is deciding against a budget and a subtotal alone would let
        it agree to something it cannot afford.

        Args:
            note: Anything the other agent should know — a substitution you
                made, an item that was unavailable.

        Returns:
            The quote as it was sent.
        """
        if not session.cart.lines:
            return _fail("The basket is empty — there is nothing to quote.")

        rate = await _tax_rate(session)
        subtotal = session.cart.subtotal
        tax = round(subtotal * rate, 2)

        data = {
            "kind": "estimate",
            "currency": "PKR",
            "lines": [
                {
                    "lineId": line.line_id,
                    "productId": line.product_id,
                    "name": line.name,
                    "quantity": line.quantity,
                    "unitPrice": _amount(line.unit_price),
                    "lineTotal": _amount(line.line_total),
                    "isMeal": line.is_meal,
                }
                for line in session.cart.lines
            ],
            "itemCount": session.cart.item_count,
            "subtotal": _amount(subtotal),
            "estimatedTax": _amount(tax),
            "estimatedTotal": _amount(subtotal + tax),
            "taxRate": rate,
            "note": note
            or (
                f"Estimate including tax at {rate:.0%}. The restaurant re-prices at "
                "checkout, and a meal upcharge would be added there."
            ),
        }

        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))
        return {
            "ok": True,
            "quoteSent": True,
            "subtotal": data["subtotal"]["text"],
            "estimatedTotal": data["estimatedTotal"]["text"],
        }

    # ----------------------------------------------------------------------- #
    # Coupons
    # ----------------------------------------------------------------------- #
    @tool
    async def check_coupon(coupon_code: str) -> dict:
        """Check a coupon the customer's agent has offered, against the basket.

        Worth doing before committing to anything: a product coupon only applies
        when a product it covers is actually in the basket, and this says so
        while the basket can still be changed.

        Args:
            coupon_code: The code the other agent sent you.

        Returns:
            Whether it is valid, what it would cover in rupees, and why not if not.
        """
        if not coupon_code.strip():
            return _fail("No coupon code was given.")
        if not session.cart.lines:
            return _fail("The basket is empty, so there is nothing for a coupon to apply to.")

        try:
            result = await _post(
                "/coupons/validate",
                {
                    "couponCode": coupon_code.strip(),
                    "orderAmount": session.cart.subtotal,
                    "allowPartial": True,
                    "cartLines": session.cart.to_coupon_lines(),
                },
            )
        except KioskApiError as exc:
            return _fail(str(exc))

        session.coupon_code = coupon_code.strip()
        return {
            "ok": True,
            "valid": result.get("valid", False),
            "reason": result.get("reasonMessage"),
            "couponType": result.get("couponType"),
            "wouldCover": rupees(result.get("applicableAmount") or 0),
            "remainingBalance": money(result.get("remainingBalance")),
            "matchedProductIds": result.get("matchedProductIds", []),
        }

    @tool
    async def redeem_coupon(coupon_code: str = "") -> dict:
        """Redeem a coupon against the order you have already confirmed.

        Do this after confirm_order and before take_payment. A product coupon
        discounts the line it covers and is spent in full; a value coupon draws
        what it can and keeps the rest.

        Args:
            coupon_code: Defaults to the last code checked in this conversation.

        Returns:
            How much came off and what is still to pay, in rupees.
        """
        if not session.order:
            return _fail("No order has been confirmed yet — call confirm_order first.")

        code = (coupon_code or session.coupon_code or "").strip()
        if not code:
            return _fail("No coupon code — the customer's agent has not offered one.")

        try:
            redemption = await _post(
                "/coupons/redeem",
                {"couponCode": code, "orderId": session.order["orderId"], "allowPartial": True},
            )
        except KioskApiError as exc:
            return _fail(
                f"{exc} The order is confirmed and unpaid — the customer's agent can "
                "still pay the full amount."
            )

        session.order["amountDue"] = redemption["amountDue"]

        # A revised firm quote, not just a sentence about one. The other agent
        # decides whether to pay by doing arithmetic on the amount due, and
        # after a redemption the figure it already holds is out of date — so the
        # new one has to arrive the same way the old one did.
        data = {
            "kind": "firm",
            "currency": "PKR",
            "orderNumber": session.order["orderNumber"],
            "couponCode": code,
            "couponStatus": redemption["status"],
            "couponDiscount": _amount(redemption["redeemedAmount"]),
            "remainingCouponBalance": _amount(redemption.get("remainingBalance")),
            "total": _amount(redemption["orderTotal"]),
            "amountDue": _amount(redemption["amountDue"]),
            "note": "Coupon applied. This is what is left to pay.",
        }
        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {
            "ok": True,
            "redeemed": rupees(redemption["redeemedAmount"]),
            "couponStatus": redemption["status"],
            "remainingCouponBalance": money(redemption.get("remainingBalance")),
            "orderTotal": rupees(redemption["orderTotal"]),
            "amountDue": rupees(redemption["amountDue"]),
        }

    # ----------------------------------------------------------------------- #
    # Checkout
    # ----------------------------------------------------------------------- #
    @tool
    async def confirm_order(order_type: str = "take_away", payment_method: str = "card") -> dict:
        """Ring the basket up. Creates the order and returns the firm total.

        This is the first step that writes anything to the restaurant's books,
        so do it only once the customer's agent has agreed to the quote. It does
        *not* take payment — the coupon goes on next, then take_payment.

        A firm quote artifact goes out automatically, because the total here
        includes tax and may differ from the estimate.

        Args:
            order_type: "dine_in" or "take_away".
            payment_method: "card", "wallet" or "counter".

        Returns:
            The order number, the real total, and what is left to pay.
        """
        if not session.cart.lines:
            return _fail("The basket is empty. Add something before confirming.")
        if session.order:
            return _fail(
                f"Order {session.order['orderNumber']} is already confirmed for this "
                "conversation. Do not create a second one."
            )
        if order_type not in ("dine_in", "take_away"):
            return _fail("order_type must be 'dine_in' or 'take_away'.")
        if payment_method not in ("card", "wallet", "counter"):
            return _fail("payment_method must be 'card', 'wallet' or 'counter'.")

        try:
            placed = await _post(
                "/orders",
                {
                    "orderType": order_type,
                    "paymentMethod": payment_method,
                    "lines": session.cart.to_api_lines(),
                },
            )
        except KioskApiError as exc:
            return _fail(str(exc))

        summary = placed["summary"]
        session.order.update(
            {
                "orderId": placed["orderId"],
                "orderNumber": placed["orderNumber"],
                "paymentMethod": placed["paymentMethod"],
                "total": summary["total"],
                "amountDue": summary.get("amountDue") or summary["total"],
            }
        )

        data = {
            "kind": "firm",
            "currency": "PKR",
            "orderNumber": placed["orderNumber"],
            "orderType": order_type,
            "paymentMethod": placed["paymentMethod"],
            "lines": [
                {
                    "name": line.name,
                    "quantity": line.quantity,
                    "lineTotal": _amount(line.line_total),
                }
                for line in session.cart.lines
            ],
            "subtotal": _amount(summary["subtotal"]),
            "tax": _amount(summary["tax"]),
            "total": _amount(summary["total"]),
            "amountDue": _amount(session.order["amountDue"]),
            "note": "Confirmed and unpaid. Apply a coupon if you have one, then pay.",
        }
        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {
            "ok": True,
            "orderNumber": placed["orderNumber"],
            "status": placed["status"],
            "subtotal": rupees(summary["subtotal"]),
            "tax": rupees(summary["tax"]),
            "total": rupees(summary["total"]),
            "amountDue": rupees(session.order["amountDue"]),
            "next": "Redeem a coupon if one was offered, then call take_payment.",
        }

    @tool
    async def take_payment() -> dict:
        """Charge whatever is still owed and close the order.

        Call this only when the customer's agent has said to. A receipt artifact
        goes out on success.

        Returns:
            The approval, the amount charged, and the receipt.
        """
        if not session.order:
            return _fail("No order has been confirmed yet — call confirm_order first.")

        try:
            result = await _post(
                "/payments",
                {
                    "orderNumber": session.order["orderNumber"],
                    "method": session.order["paymentMethod"],
                },
            )
        except KioskApiError as exc:
            return _fail(str(exc))

        if not result.get("approved", False):
            return _fail(
                f"The payment was declined ({result.get('status')}). The order is "
                "confirmed but unpaid."
            )

        charged = result.get("amount", session.order["amountDue"])
        session.order["amountDue"] = 0

        data = {
            "currency": "PKR",
            "orderNumber": result["orderNumber"],
            "status": "paid",
            "charged": _amount(charged),
            "total": _amount(session.order.get("total")),
            "transactionRef": result.get("transactionRef"),
            "paymentMethod": session.order["paymentMethod"],
        }
        artifact = session.task.add_artifact(Artifact(name=RECEIPT, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {
            "ok": True,
            "approved": True,
            "orderNumber": result["orderNumber"],
            "charged": rupees(charged),
            "transactionRef": result.get("transactionRef"),
        }

    @tool
    async def look_up_order(order_number: str = "") -> dict:
        """Look an order up, to answer a question about one already placed.

        Args:
            order_number: Defaults to this conversation's order.

        Returns:
            Its status, totals and payment attempts.
        """
        number = order_number or session.order.get("orderNumber", "")
        if not number:
            return _fail("No order number to look up.")

        try:
            detail = await _get(f"/orders/number/{number}")
        except KioskApiError as exc:
            return _fail(str(exc))

        return {
            "ok": True,
            "orderNumber": detail["orderNumber"],
            "status": detail["status"],
            "total": rupees(detail["summary"]["total"]),
            "couponDiscount": rupees(detail["summary"].get("couponDiscount") or 0),
            "amountDue": rupees(detail["summary"].get("amountDue") or 0),
            "items": [
                {
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "lineTotal": rupees(line["lineTotal"]),
                }
                for line in detail["lines"]
            ],
        }

    return [
        list_categories,
        browse_menu,
        add_to_basket,
        remove_from_basket,
        view_basket,
        send_quote,
        check_coupon,
        confirm_order,
        redeem_coupon,
        take_payment,
        look_up_order,
    ]
