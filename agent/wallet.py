"""The token the agent is sent to the shop with.

The brief was "like giving money to a child and saying go buy this" — so the
agent carries a wallet, not an open account. The wallet holds a coupon (the
restaurant's own value/product token) and a hard cash ceiling for whatever the
coupon does not cover.

The ceiling is enforced *here*, in Python, not in the system prompt. A prompt is
a request; this is a refusal. `authorize_payment` calls `spend()` before it
charges anything, and an over-budget order comes back as a tool error the agent
has to deal with rather than a silent overspend.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a charge would take the agent past its cash ceiling."""


def rupees(amount: float) -> str:
    """Money as the kiosk writes it: `Rs 1,093`, `Rs 2,500.75`.

    Every figure in this system is already a rupee amount, never paisa — a model
    shown a bare `1093` reads it as a minor unit and reports "$10.93". So each
    one that reaches the agent goes through here. Trailing `.00` is dropped: the
    menu is priced in whole rupees, and "Rs 3,000.00" invites the same misread.
    """
    return f"Rs {amount:,.2f}".removesuffix(".00")


def money(amount: float | int | None) -> str | None:
    """`rupees()` for figures that may be absent.

    Coupon balances and discounts come back as None when the restaurant has
    nothing to say about them, and "Rs None" would be worse than no answer.
    """
    if amount is None:
        return None
    return rupees(amount)


@dataclass
class Wallet:
    """One errand's worth of spending authority."""

    coupon_code: str | None = None

    # Cash the agent may spend on top of the coupon, for the whole run.
    # 0 means "the coupon or nothing" — a strict token-only errand.
    spend_limit: float = 0.0

    # Who the coupon belongs to, recorded on the redemption ledger.
    customer_id: str | None = None

    spent: float = field(default=0.0, init=False)
    coupon_redeemed: float = field(default=0.0, init=False)

    @property
    def remaining(self) -> float:
        return round(self.spend_limit - self.spent, 2)

    def check(self, amount: float) -> None:
        """Would this charge fit? Raises rather than returning a bool so a
        caller cannot forget to look at the answer."""
        if amount < 0:
            raise BudgetExceeded(f"Refusing a negative charge of {rupees(amount)}.")
        if amount > self.remaining + 1e-9:
            raise BudgetExceeded(
                f"This charge is {rupees(amount)} but the wallet has only "
                f"{rupees(self.remaining)} left of its {rupees(self.spend_limit)} limit. "
                "Remove items or use a coupon that covers more, then try again."
            )

    def spend(self, amount: float) -> float:
        """Deduct `amount`, or refuse. Returns what is left."""
        self.check(amount)
        self.spent = round(self.spent + amount, 2)
        return self.remaining

    def record_coupon(self, amount: float) -> None:
        """Coupon value is not cash — it is tracked, never charged."""
        self.coupon_redeemed = round(self.coupon_redeemed + amount, 2)

    def reset(
        self,
        coupon_code: str | None = None,
        spend_limit: float = 0.0,
        customer_id: str | None = None,
    ) -> None:
        """Re-issue the wallet for a fresh errand.

        The server reuses one process across runs, so last run's spend must not
        count against this one's ceiling.
        """
        self.coupon_code = coupon_code
        self.spend_limit = spend_limit
        self.customer_id = customer_id
        self.spent = 0.0
        self.coupon_redeemed = 0.0

    def summary(self) -> dict[str, float | str | None]:
        """Machine-readable: raw numbers, for the frontend to render itself."""
        return {
            "couponCode": self.coupon_code,
            "couponRedeemed": self.coupon_redeemed,
            "cashLimit": self.spend_limit,
            "cashSpent": self.spent,
            "cashRemaining": self.remaining,
        }

    def display(self) -> dict[str, str | None]:
        """The same figures as the agent should read them — `Rs 1,093`.

        Tools hand this back, not `summary()`: a bare `1093` in a tool result is
        exactly the number a model reports as "$10.93".
        """
        return {
            "couponCode": self.coupon_code,
            "couponRedeemed": rupees(self.coupon_redeemed),
            "cashLimit": rupees(self.spend_limit),
            "cashSpent": rupees(self.spent),
            "cashRemaining": rupees(self.remaining),
        }


# The single wallet for this run. `run.py` fills it in before the agent starts;
# the tools read it. One errand per process, which is exactly the metaphor.
wallet = Wallet()
