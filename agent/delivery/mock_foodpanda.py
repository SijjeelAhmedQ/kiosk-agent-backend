"""Foodpanda's delivery agent, as the ordering agent sees it.

Two things wear the Foodpanda name in this package, and they are not the same
thing:

* `agent/delivery/foodpanda.py` — `FoodpandaProvider`. Speaks Foodpanda's *real*
  courier API, needs `FOODPANDA_API_KEY`, and has no mock path at all. Point it
  at the real service and it dispatches for real.
* this file — `MockFoodpandaDeliveryAgent`. Speaks to `foodpanda_server.py` on
  8103, which is a demonstration delivery agent this project ships: an AI
  dispatcher that decides, assigns a rider, collects and delivers on a
  compressed clock. It needs no account anywhere, which is what lets the whole
  `order → deliver` handover be shown end to end.

`Mock` is in the class name and "demonstration agent" is in the display name for
one reason: every place this provider is named — a tool result, the health
endpoint, the console — should say plainly that no real courier was called.

It is three lines of subclass because 8103 answers the same job shape as the
in-house courier on 8102 does. That is not a coincidence to be tidied away: one
wire format for every courier is what makes `DELIVERY_PROVIDER` a config change
rather than a code change, and it is the contract in `contract.py` doing its job.
"""

from __future__ import annotations

from agent.config import settings
from agent.delivery.courier_agent import CourierAgentProvider


class MockFoodpandaDeliveryAgent(CourierAgentProvider):
    """The demonstration Foodpanda dispatcher on 8103."""

    name = "mock_foodpanda"
    display_name = "Foodpanda (demonstration agent)"
    root = "/api/foodpanda"
    start_hint = ".venv\\Scripts\\python -m uvicorn foodpanda_server:app --port 8103"

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        super().__init__(base_url or settings.mock_foodpanda_base, timeout)
