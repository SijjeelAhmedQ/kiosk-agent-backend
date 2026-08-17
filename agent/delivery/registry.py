"""Which delivery service this deployment uses.

One place that knows the provider names, so adding a third is a line here and a
file next to it — not a change anywhere in the ordering agent. `DELIVERY_PROVIDER`
in .env picks one; the health endpoint reports which was picked and whether it
is usable.
"""

from __future__ import annotations

from agent.config import settings
from agent.delivery.contract import DeliveryProvider
from agent.delivery.courier_agent import CourierAgentProvider
from agent.delivery.foodpanda import FoodpandaProvider
from agent.delivery.mock_foodpanda import MockFoodpandaDeliveryAgent

#: Every provider this build can dispatch through, by config name. Built once —
#: the objects hold configuration, not per-job state, so one instance each is
#: right and a second would only be a second thing to keep in step.
_PROVIDERS: dict[str, DeliveryProvider] = {
    CourierAgentProvider.name: CourierAgentProvider(),
    MockFoodpandaDeliveryAgent.name: MockFoodpandaDeliveryAgent(),
    FoodpandaProvider.name: FoodpandaProvider(),
}

#: Used when the configured one is unknown. The in-house courier needs no
#: credentials, so it is the one that always has a chance of working.
FALLBACK = CourierAgentProvider.name


def names() -> list[str]:
    """Every provider name that can be put in DELIVERY_PROVIDER."""
    return list(_PROVIDERS)


def get(name: str | None = None) -> DeliveryProvider:
    """The provider to dispatch through.

    An unknown name falls back rather than raising: a typo in .env should leave
    delivery working through the default and say so in the health report, not
    take down a service whose main job is ordering.
    """
    key = (name or settings.delivery_provider).strip().lower()
    return _PROVIDERS.get(key) or _PROVIDERS[FALLBACK]


def describe() -> dict:
    """What the health endpoint tells the operator about delivery.

    Deliberately excludes anything credential-shaped — the answer is whether a
    key is present, never what it is.
    """
    configured_name = settings.delivery_provider.strip().lower()
    provider = get()
    problem = getattr(provider, "problem", None)

    return {
        "provider": provider.name,
        "displayName": provider.display_name,
        "configured": provider.configured,
        "problem": (
            f"DELIVERY_PROVIDER is {configured_name!r}, which is not one of "
            f"{', '.join(names())} — using {provider.display_name} instead."
            if configured_name not in _PROVIDERS
            else problem
        ),
        "available": names(),
    }
