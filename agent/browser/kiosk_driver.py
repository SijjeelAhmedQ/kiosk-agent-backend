"""Driving the kiosk's actual website in Chromium.

Two things shape this module.

**It runs on its own thread.** Strands drives tools from inside an asyncio loop,
and Playwright's sync API refuses to run there. Rather than rewrite every tool
as a coroutine, the browser lives on one dedicated thread and calls are marshalled
to it — so `search("burger")` from a tool looks like a plain blocking call and
Playwright never sees an event loop.

**It talks in screens, not clicks.** The agent is given "search the menu", "tap
this product", "pay" — the things a customer does — rather than raw coordinates.
That keeps the browser toolset the same shape as the API toolset, and it keeps
the model out of the business of guessing where a button rendered.

Selectors are `data-testid` attributes added to the kiosk frontend. They are a
deliberate contract: styling can change freely, these cannot.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from agent.config import settings

# How long to wait for a screen to arrive before calling it a failure.
NAV_TIMEOUT = 15_000
STEP_TIMEOUT = 8_000


class BrowserError(RuntimeError):
    """Something on the screen was not where it should be."""


class _BrowserThread(threading.Thread):
    """Owns the Playwright instance and executes every call in one place."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="kiosk-browser")
        self._jobs: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._ready = threading.Event()
        self.playwright = None

    def run(self) -> None:
        with sync_playwright() as pw:
            self.playwright = pw
            self._ready.set()
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                fn, future = job
                try:
                    future.set_result(fn())
                except Exception as exc:  # surfaced to the caller, not swallowed
                    future.set_exception(exc)

    def call(self, fn: Callable[[], Any]) -> Any:
        if not self.is_alive():
            self.start()
        self._ready.wait(10)
        future: Future = Future()
        self._jobs.put((fn, future))
        return future.result()

    def shutdown(self) -> None:
        if self.is_alive():
            self._jobs.put(None)
            self.join(timeout=5)


class KioskBrowser:
    """The kiosk website, as a set of things a customer can do."""

    def __init__(self) -> None:
        self._thread = _BrowserThread()
        self._browser = None
        self._page: Page | None = None
        self.started = False

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def start(self, headless: bool = False, slow_mo: int = 250) -> str:
        """Open the browser at the kiosk's front screen.

        `slow_mo` is deliberately non-zero by default: this gets demoed to
        people, and a run that completes in 400ms looks like nothing happened.
        """
        result = self._thread.call(lambda: self._start(headless, slow_mo))
        self.started = True
        return result

    def _start(self, headless: bool, slow_mo: int) -> str:
        pw = self._thread.playwright
        self._browser = pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(STEP_TIMEOUT)
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        self._page = context.new_page()
        self._page.goto(settings.web_base, wait_until="domcontentloaded")
        return self._page.title() or "Friends Kitchen"

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._thread.call(lambda: self._browser.close())
            except Exception:
                pass
        self._thread.shutdown()
        # A finished thread cannot be started again, so the next run needs a
        # fresh one. Without this, run two of a long-lived server dies with
        # "threads can only be started once".
        self._thread = _BrowserThread()
        self._browser = None
        self._page = None
        self.started = False

    # ----------------------------------------------------------------- #
    # Small helpers, all running on the browser thread
    # ----------------------------------------------------------------- #
    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("The browser is not open yet.")
        return self._page

    def _visible(self, testid: str) -> bool:
        return self.page.locator(f"[data-testid='{testid}']").first.is_visible()

    def _click(self, testid: str, what: str) -> None:
        target = self.page.locator(f"[data-testid='{testid}']").first
        try:
            target.click(timeout=STEP_TIMEOUT)
        except PlaywrightTimeout:
            raise BrowserError(
                f"Could not tap {what} — it is not on the screen. "
                f"The page is at {self.page.url}. Read the screen before trying again."
            ) from None

    def _text(self, testid: str, default: str = "") -> str:
        locator = self.page.locator(f"[data-testid='{testid}']").first
        if locator.count() == 0 or not locator.is_visible():
            return default
        return (locator.inner_text() or default).strip()

    def _route(self) -> str:
        """Which screen are we on, by URL."""
        path = self.page.url.replace(settings.web_base, "").split("?")[0] or "/"
        return path

    # ----------------------------------------------------------------- #
    # Reading the screen
    # ----------------------------------------------------------------- #
    def screen(self) -> dict:
        return self._thread.call(self._screen)

    def _screen(self) -> dict:
        route = self._route()
        names = {
            "/": "splash",
            "/order-type": "order type",
            "/menu": "menu",
            "/cart": "cart",
            "/checkout": "checkout",
            "/payment": "payment in progress",
            "/complete": "order complete",
        }
        state: dict[str, Any] = {"screen": names.get(route, route), "path": route}

        if route == "/menu":
            state["products"] = self._products()
            state["basketTotal"] = self._text("basket-total")
            state["productModalOpen"] = self._visible("product-modal")
            state["mealQuestionOpen"] = self._visible("meal-step")
        elif route == "/checkout":
            state["amountDue"] = self._text("checkout-due")
            state["couponApplied"] = self._text("coupon-applied") or None
            state["couponProblem"] = self._text("coupon-notice") or None
        elif route == "/complete":
            state["orderNumber"] = self._text("order-number")
        elif route == "/payment":
            state["paymentFailed"] = self._visible("payment-error")

        return state

    def _products(self) -> list[dict]:
        cards = self.page.locator("[data-testid='product-card']")
        out = []
        for i in range(min(cards.count(), 40)):
            card = cards.nth(i)
            out.append(
                {
                    "productId": card.get_attribute("data-product-id"),
                    "name": card.get_attribute("data-product-name"),
                    "price": card.get_attribute("data-product-price"),
                }
            )
        return out

    # ----------------------------------------------------------------- #
    # The journey
    # ----------------------------------------------------------------- #
    def open_menu(self, order_type: str) -> dict:
        return self._thread.call(lambda: self._open_menu(order_type))

    def _open_menu(self, order_type: str) -> dict:
        if self._route() == "/":
            self._click("splash-start", "the welcome screen")
            self.page.wait_for_url("**/order-type", timeout=NAV_TIMEOUT)
        if self._route() == "/order-type":
            self._click(f"order-type-{order_type}", f"the {order_type} option")
            self.page.wait_for_url("**/menu", timeout=NAV_TIMEOUT)
        # The grid renders once the menu request lands.
        self.page.locator("[data-testid='product-card']").first.wait_for(timeout=NAV_TIMEOUT)
        return self._screen()

    def categories(self) -> list[dict]:
        return self._thread.call(self._categories)

    def _categories(self) -> list[dict]:
        cards = self.page.locator("[data-testid='category-card']")
        return [
            {
                "categoryId": cards.nth(i).get_attribute("data-category-id"),
                "name": cards.nth(i).get_attribute("data-category-name"),
            }
            for i in range(cards.count())
        ]

    def open_category(self, category_id: str) -> dict:
        return self._thread.call(lambda: self._open_category(category_id))

    def _open_category(self, category_id: str) -> dict:
        target = self.page.locator(f"[data-category-id='{category_id}']").first
        if target.count() == 0:
            raise BrowserError(f"No category tile for {category_id!r} on the rail.")
        target.click()
        self.page.wait_for_timeout(400)
        return self._screen()

    def search(self, term: str) -> dict:
        return self._thread.call(lambda: self._search(term))

    def _search(self, term: str) -> dict:
        field = self.page.locator("[data-testid='menu-search']").first
        field.fill(term)
        # Redux filters synchronously; this is for the re-render, not a request.
        self.page.wait_for_timeout(500)
        return self._screen()

    def open_product(self, product_id: str) -> dict:
        return self._thread.call(lambda: self._open_product(product_id))

    def _open_product(self, product_id: str) -> dict:
        card = self.page.locator(f"[data-product-id='{product_id}']").first
        if card.count() == 0:
            raise BrowserError(
                f"No product card for {product_id!r} on this screen. "
                "Search for it or switch category first."
            )
        card.click()
        self.page.locator("[data-testid='product-modal']").wait_for(timeout=STEP_TIMEOUT)
        return {"opened": product_id, "modal": self._modal_state()}

    def _modal_state(self) -> dict:
        modal = self.page.locator("[data-testid='product-modal']")
        return {"open": modal.is_visible(), "text": (modal.inner_text() or "")[:600]}

    def add_open_product(self, quantity: int, as_meal: bool) -> dict:
        return self._thread.call(lambda: self._add_open_product(quantity, as_meal))

    def _add_open_product(self, quantity: int, as_meal: bool) -> dict:
        modal = self.page.locator("[data-testid='product-modal']")
        if not modal.is_visible():
            raise BrowserError("No product is open. Tap a product card first.")

        # The stepper starts at 1, so tap "+" one time fewer than the quantity.
        plus = modal.locator("button[aria-label='Increase']").first
        for _ in range(max(0, quantity - 1)):
            plus.click()

        self._click("modal-add", "the Add to order button")

        # Meal-eligible products interrupt with "Make it a meal?" — answer it.
        meal_step = self.page.locator("[data-testid='meal-step']")
        asked_about_meal = False
        try:
            meal_step.wait_for(timeout=2_000)
            asked_about_meal = True
            self._click("meal-yes" if as_meal else "meal-no", "the meal answer")
        except PlaywrightTimeout:
            if as_meal:
                # It went straight into the basket, so it was never eligible.
                return {
                    "added": True,
                    "asMeal": False,
                    "note": "This product cannot be a meal — it was added on its own.",
                    "basketTotal": self._text("basket-total"),
                }

        self.page.wait_for_timeout(400)
        return {
            "added": True,
            "quantity": quantity,
            "asMeal": as_meal and asked_about_meal,
            "basketTotal": self._text("basket-total"),
        }

    def go_to_checkout(self) -> dict:
        return self._thread.call(self._go_to_checkout)

    def _go_to_checkout(self) -> dict:
        if self._route() == "/checkout":
            return self._screen()
        self._click("basket-checkout", "the Checkout button")
        self.page.wait_for_url("**/checkout", timeout=NAV_TIMEOUT)
        # Wait for the summary to paint, or the first read of the total comes
        # back empty and the agent thinks the order is free.
        self.page.locator("[data-testid='checkout-due']").wait_for(timeout=STEP_TIMEOUT)
        return self._screen()

    def enter_coupon(self, code: str) -> dict:
        return self._thread.call(lambda: self._enter_coupon(code))

    def _enter_coupon(self, code: str) -> dict:
        if self._route() != "/checkout":
            raise BrowserError("The coupon box is on the checkout screen. Go there first.")
        if self._visible("coupon-applied"):
            return {"applied": True, "note": "A coupon is already applied.",
                    "detail": self._text("coupon-applied")}

        self.page.locator("[data-testid='coupon-input']").first.fill(code)
        self._click("coupon-apply", "the Apply button")
        # Validation is a round trip to the restaurant.
        self.page.wait_for_timeout(1_500)

        applied = self._text("coupon-applied")
        problem = self._text("coupon-notice")
        return {
            "applied": bool(applied),
            "detail": applied or None,
            "problem": problem or None,
            "amountDue": self._text("checkout-due"),
        }

    def pay(self, method: str) -> dict:
        return self._thread.call(lambda: self._pay(method))

    def _pay(self, method: str) -> dict:
        if self._route() != "/checkout":
            raise BrowserError("Payment starts from the checkout screen.")

        # A coupon that covers everything removes the picker — there is nothing
        # to choose, and the button says "Place order" instead.
        picker = self.page.locator(f"[data-testid='payment-method-{method}']")
        if picker.count() > 0 and picker.first.is_visible():
            picker.first.click()

        due = self._text("checkout-due")
        self._click("checkout-proceed", "the pay button")

        # The payment screen places the order, redeems the coupon and charges,
        # then lands on either the receipt or an error.
        try:
            self.page.wait_for_selector(
                "[data-testid='order-complete'], [data-testid='payment-error']",
                timeout=NAV_TIMEOUT,
            )
        except PlaywrightTimeout:
            raise BrowserError(
                "The payment screen never finished. The order may or may not have gone through — "
                "do not pay again; report this."
            ) from None

        if self._visible("payment-error"):
            return {
                "paid": False,
                "problem": self.page.locator("[data-testid='payment-error']").inner_text()[:400],
            }

        return {
            "paid": True,
            "orderNumber": self._text("order-number"),
            "charged": due,
        }


browser = KioskBrowser()
