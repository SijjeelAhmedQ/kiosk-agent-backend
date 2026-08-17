"""Which Friends Kitchen the order is placed at, and where a courier collects it.

The restaurant's own API has no notion of branches — it is one till, one menu,
one order book. That is fine for a customer standing at the panel, and not
enough for delivery: a courier needs a street address to drive to, and picking
one at random is how an order gets collected from the wrong city.

So the branch directory lives here, in the agent's own configuration, rather
than being invented in the restaurant's database. `FK_BRANCHES` is a JSON list;
without it there is a single branch, which is exactly the situation the
restaurant API already assumes. Nothing about ordering changes either way — the
branch decides where the food is *collected from*, not how it is bought.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.config import settings
from agent.location import UserLocation, haversine_km


@dataclass(frozen=True)
class Branch:
    """One Friends Kitchen, as a courier needs to know it."""

    id: str
    name: str
    address: str
    latitude: float
    longitude: float
    phone: str | None = None

    def to_view(self) -> dict[str, Any]:
        """The wire form — the pickup half of a delivery request."""
        return {
            "branchId": self.id,
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phone": self.phone,
        }


# The single branch, for the common case where nobody has configured a list.
# The coordinates are Karachi's city centre and are a placeholder — set
# FK_BRANCHES in .env with real ones before pointing this at a real courier.
_FALLBACK = Branch(
    id="fk-main",
    name="Friends Kitchen",
    address="Friends Kitchen, Karachi",
    latitude=24.8607,
    longitude=67.0011,
    phone=None,
)


def _read(raw: Any) -> Branch | None:
    """One entry of FK_BRANCHES, or None if it is not usable.

    A branch missing its coordinates is dropped rather than defaulted: a
    guessed pickup point is worse than a shorter list.
    """
    if not isinstance(raw, dict):
        return None
    try:
        latitude = float(raw["latitude"])
        longitude = float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    name = str(raw.get("name") or "Friends Kitchen")
    return Branch(
        id=str(raw.get("id") or raw.get("branchId") or name.lower().replace(" ", "-")),
        name=name,
        address=str(raw.get("address") or name),
        latitude=latitude,
        longitude=longitude,
        phone=str(raw["phone"]) if raw.get("phone") else None,
    )


def _load() -> list[Branch]:
    """The configured branches, or the single fallback.

    Read once at import: the directory is deployment configuration, and a branch
    that appears halfway through a run would be a stranger thing than a restart.
    """
    if not settings.branches_json:
        return [_FALLBACK]
    try:
        raw = json.loads(settings.branches_json)
    except json.JSONDecodeError:
        # A typo in .env should not take the service down — and it should not
        # silently dispatch to the wrong place either. One branch, said plainly
        # by the health endpoint, is the safe middle.
        return [_FALLBACK]

    found = [branch for branch in (_read(item) for item in raw or []) if branch]
    return found or [_FALLBACK]


BRANCHES: list[Branch] = _load()


def default_branch() -> Branch:
    """Where an order goes when nobody said where the customer is."""
    return BRANCHES[0]


def nearest(user_location: UserLocation | None) -> tuple[Branch, float | None]:
    """The branch to order from, and how far the customer is from it in km.

    Returns the default branch and a distance of None when there is no
    location — the errand still runs, it just cannot claim to have chosen.
    Straight-line distance, so it is a ranking, not a delivery estimate.
    """
    if user_location is None:
        return default_branch(), None

    ranked = sorted(
        (
            (
                haversine_km(
                    user_location.latitude,
                    user_location.longitude,
                    branch.latitude,
                    branch.longitude,
                ),
                branch,
            )
            for branch in BRANCHES
        ),
        key=lambda pair: pair[0],
    )
    distance, branch = ranked[0]
    return branch, distance
