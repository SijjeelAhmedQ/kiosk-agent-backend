"""The agent's hands, version two: the actual touchscreen website.

Same errand, same shape of tool as api_tools — browse, add, coupon, pay — but
every one of these drives Chromium against the React kiosk a customer would use.
Nothing here talks to the REST API directly.

The wallet guardrail still applies. `pay` reads the amount off the checkout
screen and checks it against the cash ceiling before it taps anything, so a
browser run cannot overspend where an API run could not.
"""

from __future__ import annotations

import re

from strands import tool

from agent.browser.kiosk_driver import BrowserError, browser
from agent.wallet import BudgetExceeded, rupees, wallet

_MONEY = re.compile(r"[\d,]+(?:\.\d+)?")

# "Coupon applied — Rs 1,200 off" / "Rs 950 off, tax included" — both shapes the
# kiosk uses to say what a coupon took off.
_DISCOUNT = re.compile(r"([\d,]+(?:\.\d+)?)\s*off")

# What the coupon promised on the checkout screen. The website spends it during
# payment, so it is only banked in the wallet once payment actually succeeds.
_reserved_coupon: dict[str, float] = {"amount": 0.0}


def _money(text: str | None) -> float | None:
    """"Rs 1,250" -> 1250.0. The kiosk renders whole rupees with a symbol."""
    if not text:
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _discount(text: str | None) -> float:
    """How much the applied-coupon panel says is coming off."""
    if not text:
        return 0.0
    match = _DISCOUNT.search(text)
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0


# Everything the kiosk puts a figure in. The DOM is not consistent about it —
# `data-product-price` is a bare `1250`, a rendered total may already carry its
# own prefix — so these are parsed back to numbers and written out one way.
_MONEY_FIELDS = frozenset({"price", "basketTotal", "amountDue", "charged"})


def _as_rupees(value):
    amount = _money(value) if isinstance(value, str) else value
    return value if amount is None else rupees(amount)


def _priced(value):
    """Rewrite every money field in a driver result as `Rs 1,250`.

    Applied to everything on its way out of `_guarded`, so no screen can hand
    the agent a bare number to guess the currency of.
    """
    if isinstance(value, dict):
        return {
            key: _as_rupees(item) if key in _MONEY_FIELDS else _priced(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_priced(item) for item in value]
    return value


def reset() -> None:
    """Forget the previous errand's coupon reservation, and close any browser it
    left open — a stale session would start the next run mid-checkout."""
    _reserved_coupon["amount"] = 0.0
    if browser.started:
        browser.close()


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _guarded(action, *args, **kwargs) -> dict:
    """Run a driver call, turning a missing element into advice rather than a
    stack trace — the agent can act on "read the screen", not on a traceback."""
    try:
        return {"ok": True, **_priced(action(*args, **kwargs) or {})}
    except BrowserError as exc:
        return _fail(str(exc))
    except Exception as exc:  # Playwright timeouts, closed pages, and friends
        return _fail(f"The browser could not do that: {exc}")


# --------------------------------------------------------------------------- #
# Getting in
# --------------------------------------------------------------------------- #
@tool
def open_kiosk(order_type: str = "take_away") -> dict:
    """Walk up to the kiosk: open the website and get as far as the menu.

    Call this once, before anything else.

    Args:
        order_type: "dine_in" or "take_away" — the kiosk asks this on the way in.

    Returns:
        The menu screen, with the products currently on it.
    """
    if order_type not in ("dine_in", "take_away"):
        return _fail("order_type must be 'dine_in' or 'take_away'.")

    if not browser.started:
        try:
            browser.start(headless=False)
        except Exception as exc:
            return _fail(f"Could not open the browser: {exc}")

    return _guarded(browser.open_menu, order_type)


@tool
def read_screen() -> dict:
    """Look at what is on the kiosk screen right now.

    Use this whenever you are unsure where you are, or after something did not
    do what you expected. The screen changes after every tap.

    Returns:
        Which screen is showing and what is on it.
    """
    return _guarded(browser.screen)


# --------------------------------------------------------------------------- #
# The menu
# --------------------------------------------------------------------------- #
@tool
def list_categories() -> dict:
    """Read the category rail down the left of the menu screen.

    Returns:
        The category ids and names you can open.
    """
    try:
        return {"ok": True, "categories": _priced(browser.categories())}
    except Exception as exc:
        return _fail(f"Could not read the category rail: {exc}")


@tool
def open_category(category_id: str) -> dict:
    """Tap a category tile to show its products.

    Args:
        category_id: An id from list_categories.

    Returns:
        The menu screen after the tap.
    """
    return _guarded(browser.open_category, category_id)


@tool
def search_menu(term: str) -> dict:
    """Type into the menu's search box.

    Searching spans every category, so this is usually the quickest way to find
    something the customer named.

    Args:
        term: What to look for, e.g. "burger". Pass an empty string to clear it.

    Returns:
        The products now showing, with their product ids.
    """
    return _guarded(browser.search, term)


@tool
def add_to_cart(product_id: str, quantity: int = 1, as_meal: bool = False) -> dict:
    """Tap a product, set the quantity, and add it to the order.

    The product must be visible on the current screen — search for it or open its
    category first. Meal-eligible products ask "Make it a meal?" and this answers
    that question for you using `as_meal`.

    Args:
        product_id: A productId from search_menu or read_screen.
        quantity: How many, 1 or more.
        as_meal: Answer yes to the meal upgrade.

    Returns:
        Confirmation and the running basket total, in rupees.
    """
    if quantity < 1:
        return _fail("Quantity must be at least 1.")

    opened = _guarded(browser.open_product, product_id)
    if not opened.get("ok"):
        return opened

    return _guarded(browser.add_open_product, quantity, as_meal)


# --------------------------------------------------------------------------- #
# Checkout
# --------------------------------------------------------------------------- #
@tool
def go_to_checkout() -> dict:
    """Tap Checkout on the basket panel.

    Returns:
        The checkout screen, including the amount due in rupees.
    """
    return _guarded(browser.go_to_checkout)


@tool
def apply_coupon() -> dict:
    """Type the coupon you were given into the checkout screen's coupon box and
    apply it.

    The kiosk validates it there and then, and says why if it will not take it.
    Applying only reserves the coupon — it is actually spent when you pay.

    Returns:
        Whether the kiosk accepted it, and the amount now due in rupees.
    """
    if not wallet.coupon_code:
        return _fail("No coupon was issued for this errand.")

    result = _guarded(browser.enter_coupon, wallet.coupon_code)
    if result.get("ok") and result.get("applied"):
        _reserved_coupon["amount"] = _discount(result.get("detail"))
    return result


@tool
def pay(method: str = "card") -> dict:
    """Choose a payment method and complete the order.

    This is the step that spends money. It reads the amount due off the screen
    and refuses if that is more than the cash limit you were sent with — if it
    refuses, go back and remove items or apply the coupon.

    A coupon that covers the whole order leaves nothing to choose: the kiosk
    hides the payment methods and the button becomes "Place order". That is
    normal, and this handles it.

    Args:
        method: "card", "wallet" or "counter".

    Returns:
        The order number and what was charged in rupees, or the reason it
        failed.
    """
    if method not in ("card", "wallet", "counter"):
        return _fail("method must be 'card', 'wallet' or 'counter'.")

    screen = _guarded(browser.screen)
    if not screen.get("ok"):
        return screen
    if screen.get("path") != "/checkout":
        return _fail("You are not on the checkout screen. Call go_to_checkout first.")

    due = _money(screen.get("amountDue"))
    if due is None:
        return _fail(
            "Could not read the amount due off the checkout screen. "
            "Call read_screen and check the order looks right before paying."
        )

    try:
        wallet.check(due)
    except BudgetExceeded as exc:
        return _fail(str(exc))

    result = _guarded(browser.pay, method)
    if not result.get("ok"):
        return result

    if not result.get("paid"):
        return _fail(
            f"The kiosk did not complete the payment: {result.get('problem')}. "
            "Do not try to pay a second time — report this."
        )

    charged = _money(result.get("charged"))
    if charged is None:
        charged = due
    wallet.spend(charged)
    # The website redeemed the coupon during payment, so bank it now rather
    # than at apply time — an abandoned checkout must not look like a spend.
    if _reserved_coupon["amount"]:
        wallet.record_coupon(_reserved_coupon["amount"])
        _reserved_coupon["amount"] = 0.0

    return {
        "ok": True,
        "paid": True,
        "orderNumber": result.get("orderNumber"),
        "charged": rupees(charged),
        "wallet": wallet.display(),
    }


BROWSER_TOOLS = [
    open_kiosk,
    read_screen,
    list_categories,
    open_category,
    search_menu,
    add_to_cart,
    go_to_checkout,
    apply_coupon,
    pay,
]
