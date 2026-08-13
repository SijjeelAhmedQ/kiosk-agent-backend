"""The merchant's other pair of hands: the real touchscreen.

Same tool names as `merchant_tools.py`, so the brief and the negotiation do not
change — only what happens behind them. With these, Friends Kitchen at 5173 visibly
fills itself while the two agents talk, which is the difference between being
told the system works and watching it.

**Why this does not just reuse `agent/tools/browser_tools.py`.** Those tools
exist and drive the same screens, but five of their lines read the process-wide
`wallet` singleton: `apply_coupon` takes the code from it and `pay` checks a
budget against it. Both are wrong here. The merchant has no wallet — the buyer
holds the money, on the other side of the wire — and the coupon arrives in a
message rather than from a global. So the *driver* is reused, which is the part
that knows the selectors and the waits, and the tools around it are the
merchant's own.

**What the screen cannot do that the API can.** Friends Kitchen has no
confirmed-but-unpaid state: checkout shows the firm total, and the order is
created, the coupon redeemed and the card charged by one press of the pay
button. So `confirm_order` here reaches the checkout screen and quotes the firm
total, and an order number only exists after `take_payment`. The artifacts say
so rather than inventing one.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from strands import tool

from agent import friends_kitchen_api
from agent.a2a.merchant_tools import MerchantSession, _amount
from agent.a2a.protocol import QUOTE, RECEIPT, Artifact, event_artifact
from agent.browser.friends_kitchen_driver import BrowserError, browser
from agent.friends_kitchen_api import FriendsKitchenApiError
from agent.wallet import rupees

_MONEY = re.compile(r"[\d,]+(?:\.\d+)?")


def _money(text: str | None) -> float:
    """`Rs 1,122` off the screen, as a number.

    Everything Friends Kitchen shows is already formatted for a person, and the
    artifacts have to carry a figure the buyer's wallet can do arithmetic on.
    Zero on an unreadable screen rather than None: the callers all go on to
    compare it, and a None there is a crash instead of a wrong-looking quote.
    """
    if not text:
        return 0.0
    match = _MONEY.search(text)
    return float(match.group().replace(",", "")) if match else 0.0


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


async def _drive(action, *args) -> Any:
    """Run a driver call off the event loop.

    The driver is synchronous and each call is a real browser interaction — a
    second or more. The buyer is waiting on an HTTP request served by this same
    process, so holding the loop for that long would stall the very connection
    the answer has to go back down.
    """
    return await asyncio.to_thread(action, *args)


def build_browser_tools(session: MerchantSession, headless: bool = False) -> list:
    """The merchant's toolset, driving Friends Kitchen in Chromium."""

    async def _ensure_open(order_type: str = "take_away") -> dict | None:
        """Start the browser and get to the menu, once. Returns a failure or None."""
        if session.order.get("tillOpen"):
            return None
        try:
            await _drive(browser.start, headless, 250)
            await _drive(browser.open_menu, order_type)
        except BrowserError as exc:
            return _fail(
                f"Could not open Friends Kitchen ({exc}). Is the Friends Kitchen front end running on 5173?"
            )
        session.order["tillOpen"] = True
        session.order["orderType"] = order_type
        return None

    @tool
    async def open_till(order_type: str = "take_away") -> dict:
        """Open Friends Kitchen and start an order. Do this before anything else.

        The touchscreen asks dine-in or take-away before it will show a menu, so
        unlike the API this has to be settled up front — you cannot change it at
        checkout.

        Args:
            order_type: "dine_in" or "take_away".

        Returns:
            The screen Friends Kitchen is now on.
        """
        if order_type not in ("dine_in", "take_away"):
            return _fail("order_type must be 'dine_in' or 'take_away'.")
        if session.order.get("tillOpen"):
            return {
                "ok": True,
                "note": f"Already open as {session.order.get('orderType')}.",
            }
        problem = await _ensure_open(order_type)
        if problem:
            return problem
        return {"ok": True, "orderType": order_type, "screen": await _drive(browser.screen)}

    @tool
    async def list_categories() -> dict:
        """List the category tiles on the menu rail.

        Returns:
            Category ids and names.
        """
        problem = await _ensure_open()
        if problem:
            return problem
        try:
            return {"ok": True, "categories": await _drive(browser.categories)}
        except BrowserError as exc:
            return _fail(str(exc))

    @tool
    async def browse_menu(search: str = "", category_id: str = "") -> dict:
        """Find products on the screen.

        Only what is currently displayed can be added, so search or open a
        category first — the product cards carry the ids.

        Args:
            search: Type this into the menu's search box.
            category_id: Tap this category tile instead.

        Returns:
            The products now on screen, with their ids and prices.
        """
        problem = await _ensure_open()
        if problem:
            return problem
        try:
            if category_id:
                screen = await _drive(browser.open_category, category_id)
            elif search:
                screen = await _drive(browser.search, search)
            else:
                screen = await _drive(browser.screen)
        except BrowserError as exc:
            return _fail(str(exc))

        products = screen.get("products", [])
        return {"ok": True, "matched": len(products), "products": products}

    @tool
    async def add_to_basket(product_id: str, quantity: int = 1, as_meal: bool = False) -> dict:
        """Tap a product and add it to the basket.

        Args:
            product_id: An id from browse_menu. The card must be on screen.
            quantity: How many, 1 or more.
            as_meal: Answer yes to "Make it a meal?" if Friends Kitchen asks.

        Returns:
            What was added and the basket total now showing.
        """
        if quantity < 1:
            return _fail("Quantity must be at least 1.")
        problem = await _ensure_open()
        if problem:
            return problem

        try:
            await _drive(browser.open_product, product_id)
            added = await _drive(browser.add_open_product, quantity, as_meal)
        except BrowserError as exc:
            return _fail(f"{exc} Call browse_menu to bring the card on screen first.")

        session.order["basketTotal"] = _money(added.get("basketTotal"))
        return {"ok": True, "added": added, "basketTotal": rupees(session.order["basketTotal"])}

    @tool
    async def view_basket() -> dict:
        """Read the basket total off the screen.

        Returns:
            The screen and the running basket total.
        """
        problem = await _ensure_open()
        if problem:
            return problem
        screen = await _drive(browser.screen)
        total = _money(screen.get("basketTotal"))
        if total:
            session.order["basketTotal"] = total
        return {"ok": True, "basketTotal": rupees(total), "screen": screen}

    @tool
    async def send_quote(note: str = "") -> dict:
        """Hand the customer's agent a quote for what is in the basket.

        Send one before asking anyone to commit. The basket total on the menu
        screen excludes tax — the firm figure appears at checkout — so this is
        explicitly an estimate.

        Args:
            note: Anything the other agent should know, such as a substitution.

        Returns:
            The quote as it was sent.
        """
        total = session.order.get("basketTotal", 0.0)
        if not total:
            return _fail("The basket is empty — there is nothing to quote.")

        data = {
            "kind": "estimate",
            "currency": "PKR",
            "basketTotal": _amount(total),
            "subtotal": _amount(total),
            "note": note
            or (
                "Read off the Friends Kitchen basket. Tax is added at checkout, so the firm "
                "total will be higher — confirm_order reports it."
            ),
        }
        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))
        return {"ok": True, "quoteSent": True, "subtotal": data["subtotal"]["text"]}

    @tool
    async def check_coupon(coupon_code: str) -> dict:
        """Note the coupon the customer's agent offered.

        On the touchscreen a coupon can only be tested by typing it into the
        checkout box, which also applies it — so unlike the API there is no way
        to check one without committing to it. This just remembers the code;
        redeem_coupon is what enters it.

        Args:
            coupon_code: The code the other agent sent you.

        Returns:
            Confirmation that the code is held.
        """
        if not coupon_code.strip():
            return _fail("No coupon code was given.")
        session.coupon_code = coupon_code.strip()
        return {
            "ok": True,
            "held": session.coupon_code,
            "note": (
                "Held. Friends Kitchen has no way to test a coupon without applying it, "
                "so it goes in at checkout — call confirm_order, then redeem_coupon."
            ),
        }

    @tool
    async def confirm_order(order_type: str = "", payment_method: str = "card") -> dict:
        """Take the basket to checkout and read the firm total.

        Unlike the API, this does *not* create an order: Friends Kitchen creates it,
        redeems any coupon and charges the card all at once when take_payment
        presses the button. So nothing is on the restaurant's books yet, and
        there is no order number until payment goes through.

        Args:
            order_type: Ignored here — Friends Kitchen settled it at open_till.
            payment_method: "card", "wallet" or "counter", chosen at checkout.

        Returns:
            The firm total including tax, and what is due.
        """
        problem = await _ensure_open()
        if problem:
            return problem
        if order_type and order_type != session.order.get("orderType"):
            return _fail(
                f"This order was started as {session.order.get('orderType')} and the "
                "Friends Kitchen cannot change that at checkout. Say so, or start again."
            )
        if payment_method not in ("card", "wallet", "counter"):
            return _fail("payment_method must be 'card', 'wallet' or 'counter'.")

        try:
            screen = await _drive(browser.go_to_checkout)
        except BrowserError as exc:
            return _fail(str(exc))

        due = _money(screen.get("amountDue"))
        session.order["paymentMethod"] = payment_method
        session.order["amountDue"] = due

        data = {
            "kind": "firm",
            "currency": "PKR",
            "orderType": session.order.get("orderType"),
            "paymentMethod": payment_method,
            "total": _amount(due),
            "amountDue": _amount(due),
            "note": (
                "Firm, from the Friends Kitchen checkout screen — tax included. Nothing is on "
                "the restaurant's books yet: this Friends Kitchen creates the order, redeems "
                "the coupon and charges in one step, so the order number arrives "
                "with the receipt."
            ),
        }
        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {
            "ok": True,
            "amountDue": rupees(due),
            "orderNumber": None,
            "next": "Enter a coupon with redeem_coupon if one was offered, then take_payment.",
        }

    @tool
    async def redeem_coupon(coupon_code: str = "") -> dict:
        """Type the coupon into the checkout box and apply it.

        Args:
            coupon_code: Defaults to the code held from check_coupon.

        Returns:
            Whether it took, and the revised amount due.
        """
        code = (coupon_code or session.coupon_code or "").strip()
        if not code:
            return _fail("No coupon code — the customer's agent has not offered one.")
        if "amountDue" not in session.order:
            return _fail("The coupon box is on the checkout screen — call confirm_order first.")

        try:
            result = await _drive(browser.enter_coupon, code)
        except BrowserError as exc:
            return _fail(str(exc))

        if not result.get("applied"):
            return _fail(
                result.get("problem")
                or "Friends Kitchen did not accept that coupon, and said nothing about why."
            )

        was = session.order["amountDue"]
        due = _money(result.get("amountDue"))
        session.order["amountDue"] = due

        data = {
            "kind": "firm",
            "currency": "PKR",
            "couponCode": code,
            "couponDiscount": _amount(round(was - due, 2)),
            "total": _amount(was),
            "amountDue": _amount(due),
            "note": "Coupon applied at Friends Kitchen. This is what is left to pay.",
        }
        artifact = session.task.add_artifact(Artifact(name=QUOTE, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {
            "ok": True,
            "redeemed": rupees(round(was - due, 2)),
            "detail": result.get("detail"),
            "amountDue": rupees(due),
        }

    @tool
    async def take_payment() -> dict:
        """Press the pay button. This places the order and charges it.

        One press does everything the API does in three steps, so there is no
        way back after it. Only call it when the customer's agent has said to.

        Returns:
            The receipt, with the order number Friends Kitchen finally shows.
        """
        if "amountDue" not in session.order:
            return _fail("Not at checkout yet — call confirm_order first.")

        try:
            result = await _drive(browser.pay, session.order.get("paymentMethod", "card"))
        except BrowserError as exc:
            # The driver raises this when the payment screen never settles, and
            # its message already says not to try again. Pass it through whole.
            return _fail(str(exc))

        if not result.get("paid"):
            return _fail(
                f"Friends Kitchen refused the payment: {result.get('problem')}. Nothing was charged."
            )

        charged = _money(result.get("charged")) or session.order["amountDue"]
        number = result.get("orderNumber") or ""
        session.order["orderNumber"] = number
        session.order["amountDue"] = 0

        data = {
            "currency": "PKR",
            "orderNumber": number,
            "status": "paid",
            "charged": _amount(charged),
            "total": _amount(charged),
            "paymentMethod": session.order.get("paymentMethod", "card"),
        }
        artifact = session.task.add_artifact(Artifact(name=RECEIPT, data=data))
        session.task.stream.emit(event_artifact(artifact))

        return {"ok": True, "approved": True, "orderNumber": number, "charged": rupees(charged)}

    @tool
    async def look_up_order(order_number: str = "") -> dict:
        """Look an order up in the restaurant's records.

        Reads the API rather than the screen: Friends Kitchen shows a receipt, not a
        history, and this is for answering questions after the fact.

        Args:
            order_number: Defaults to this conversation's order.

        Returns:
            Its status and totals.
        """
        number = order_number or session.order.get("orderNumber", "")
        if not number:
            return _fail("No order number to look up — nothing has been paid for yet.")

        try:
            detail = await asyncio.to_thread(friends_kitchen_api.get, f"/orders/number/{number}")
        except FriendsKitchenApiError as exc:
            return _fail(str(exc))

        summary = detail.get("summary", {})
        return {
            "ok": True,
            "orderNumber": detail["orderNumber"],
            "status": detail["status"],
            "total": rupees(summary.get("total") or 0),
            "couponDiscount": rupees(summary.get("couponDiscount") or 0),
        }

    return [
        open_till,
        list_categories,
        browse_menu,
        add_to_basket,
        view_basket,
        send_quote,
        check_coupon,
        confirm_order,
        redeem_coupon,
        take_payment,
        look_up_order,
    ]
