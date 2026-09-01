"""Where the four services listen, and what they answer.

One table, because a skill script that hardcoded `http://localhost:8100` would
be wrong on the first deployment that moved it — and because the environment
variables below are the ones the application already reads, so a skill follows
a `.env` change rather than needing its own copy of the answer.

Nothing here starts, stops or configures a service. It is an address book.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    """One of the running processes a skill can talk to."""

    key: str
    name: str
    #: The port it is documented to listen on, for the "start it with" message.
    port: int
    #: Prefix every one of its own routes sits under, e.g. `/api/agent`.
    prefix: str
    #: How to start it, from the repository root, exactly as the READMEs say.
    start_hint: str
    #: True when the service publishes an A2A agent card at the well-known URL.
    has_agent_card: bool = False

    @property
    def base_url(self) -> str:
        return _base(self.key, self.port)

    @property
    def health_url(self) -> str:
        return f"{self.base_url}{self.prefix}/health"

    @property
    def card_url(self) -> str | None:
        if not self.has_agent_card:
            return None
        return f"{self.base_url}/.well-known/agent-card.json"


#: The environment variable each service's address is already configured by,
#: falling back to the port in its own module docstring.
_ENV = {
    "ordering": ("FK_AGENT_BASE", "FK_AGENT_BASE_URL"),
    "a2a": ("A2A_PUBLIC_BASE", "FK_A2A_BASE"),
    "delivery": ("DELIVERY_BASE_URL",),
    "foodpanda": ("MOCK_FOODPANDA_PUBLIC_BASE", "MOCK_FOODPANDA_BASE_URL"),
}


def _base(key: str, port: int) -> str:
    for name in _ENV.get(key, ()):
        value = os.getenv(name, "").strip()
        if value:
            return value.rstrip("/")
    return f"http://localhost:{port}"


ORDERING = Service(
    key="ordering",
    name="Ordering agent",
    port=8100,
    prefix="/api/agent",
    start_hint=".venv\\Scripts\\python -m uvicorn server:app --port 8100",
)

A2A = Service(
    key="a2a",
    name="A2A ordering desk",
    port=8101,
    prefix="/api/a2a",
    start_hint=".venv\\Scripts\\python -m uvicorn a2a_server:app --port 8101",
    has_agent_card=True,
)

DELIVERY = Service(
    key="delivery",
    name="Friends Kitchen Delivery",
    port=8102,
    prefix="/api/delivery",
    start_hint=".venv\\Scripts\\python -m uvicorn delivery_server:app --port 8102",
    has_agent_card=True,
)

FOODPANDA = Service(
    key="foodpanda",
    name="Foodpanda Delivery (demonstration agent)",
    port=8103,
    prefix="/api/foodpanda",
    start_hint=".venv\\Scripts\\python -m uvicorn foodpanda_server:app --port 8103",
    has_agent_card=True,
)

#: In the order an errand travels through them.
ALL = (ORDERING, A2A, DELIVERY, FOODPANDA)

BY_KEY = {service.key: service for service in ALL}


def restaurant_api() -> str:
    """The Friends Kitchen REST API the agents actually buy from."""
    return os.getenv("FK_API_BASE", "").strip().rstrip("/") or "http://localhost:8000/api/v1"


def get(key: str) -> Service:
    """One service by key, or a `KeyError` that names the ones there are."""
    try:
        return BY_KEY[key]
    except KeyError:
        known = ", ".join(BY_KEY)
        raise KeyError(f"Unknown service {key!r}. Known: {known}.") from None
