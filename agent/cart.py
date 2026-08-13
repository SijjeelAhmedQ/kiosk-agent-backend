"""The agent's cart, held here rather than on the server.

The Friends Kitchen API has no cart endpoint — `POST /orders` takes the whole basket in
one shot and re-prices it server-side. So the agent needs somewhere to
accumulate lines between "add two burgers" and "check out", and this is it.

Prices are cached alongside each line purely so the agent can talk about totals
before placing the order. They are advisory: the database re-prices everything
at checkout, and `place_order` reports what it actually charged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from agent.wallet import rupees


@dataclass
class CartLine:
    line_id: str
    product_id: str
    name: str
    unit_price: float
    quantity: int
    is_meal: bool = False
    modifier_ids: list[str] = field(default_factory=list)

    @property
    def line_total(self) -> float:
        return round(self.unit_price * self.quantity, 2)

    def to_api(self) -> dict:
        return {
            "lineId": self.line_id,
            "productId": self.product_id,
            "quantity": self.quantity,
            "isMeal": self.is_meal,
            "modifiers": [{"modifierId": m} for m in self.modifier_ids],
        }

    def to_view(self) -> dict:
        """What the agent reads. Prices go out as `Rs 450`, never bare numbers."""
        return {
            "lineId": self.line_id,
            "productId": self.product_id,
            "name": self.name,
            "quantity": self.quantity,
            "unitPrice": rupees(self.unit_price),
            "lineTotal": rupees(self.line_total),
            "isMeal": self.is_meal,
        }


@dataclass
class Cart:
    lines: list[CartLine] = field(default_factory=list)

    def add(
        self,
        product_id: str,
        name: str,
        unit_price: float,
        quantity: int,
        is_meal: bool = False,
        modifier_ids: list[str] | None = None,
    ) -> CartLine:
        """Adds a line, merging into an identical one if it is already there —
        so "add a burger" twice reads as quantity 2, not two separate lines."""
        modifier_ids = sorted(modifier_ids or [])
        for line in self.lines:
            same = (
                line.product_id == product_id
                and line.is_meal == is_meal
                and sorted(line.modifier_ids) == modifier_ids
            )
            if same:
                line.quantity += quantity
                return line

        line = CartLine(
            line_id=f"L{uuid.uuid4().hex[:8]}",
            product_id=product_id,
            name=name,
            unit_price=unit_price,
            quantity=quantity,
            is_meal=is_meal,
            modifier_ids=modifier_ids,
        )
        self.lines.append(line)
        return line

    def remove(self, line_id: str) -> bool:
        before = len(self.lines)
        self.lines = [line for line in self.lines if line.line_id != line_id]
        return len(self.lines) != before

    def clear(self) -> None:
        self.lines = []

    @property
    def subtotal(self) -> float:
        return round(sum(line.line_total for line in self.lines), 2)

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    def to_api_lines(self) -> list[dict]:
        return [line.to_api() for line in self.lines]

    def to_coupon_lines(self) -> list[dict]:
        """What `POST /coupons/validate` wants: product and quantity only."""
        return [
            {"productId": line.product_id, "quantity": line.quantity}
            for line in self.lines
        ]

    def to_view(self) -> dict:
        return {
            "lines": [line.to_view() for line in self.lines],
            "itemCount": self.item_count,
            "estimatedSubtotal": rupees(self.subtotal),
            "note": "Tax and any meal upcharge are added by the restaurant at checkout.",
        }


cart = Cart()
