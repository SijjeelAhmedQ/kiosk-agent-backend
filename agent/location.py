"""Where the customer is, for the errand that has to reach them.

The ordering agent could always finish its job at the counter. Delivery is what
makes the customer's own position matter: it decides which branch the order is
placed at, and it is half of the message the delivery agent needs.

The location is held here the way the wallet is held in agent/wallet.py — one
per process, set by the server before the agent starts and read by the tools.
That is the same "one errand per process" metaphor, and it keeps the coordinates
out of the prompt, where a model could round them.

Nothing here trusts what it is handed. A browser's geolocation is a `dict` off
the wire, and a swapped latitude and longitude is a delivery to the wrong
hemisphere — so `parse()` refuses rather than passes it on.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Any


class InvalidLocation(ValueError):
    """The coordinates handed in are not a place on Earth."""


# Null Island — 0°,0° in the Gulf of Guinea. It is what a device with no fix
# reports often enough to be worth naming, and a delivery dispatched there is
# the failure this check exists to prevent.
_NULL_ISLAND_TOLERANCE = 1e-7


@dataclass(frozen=True)
class UserLocation:
    """A fix on the customer, and how it was come by.

    Frozen: this is evidence, not state. A run that wants a different location
    gets a different object, so nothing downstream can quietly move the customer
    between the branch being chosen and the courier being called.
    """

    latitude: float
    longitude: float

    accuracy_m: float | None = None
    """The device's own claim about its precision, in metres, when it made one."""

    label: str | None = None
    """What the customer calls this place — a flat number, an office name."""

    source: str = "browser"
    """browser | manual — how the fix was come by, for the audit trail."""

    def display(self) -> str:
        """The location as a line in a tool result or a courier's note."""
        where = f"{self.latitude:.5f}, {self.longitude:.5f}"
        if self.label:
            return f"{self.label} ({where})"
        return where

    def to_view(self) -> dict[str, Any]:
        """The wire form — what crosses to the UI and to the delivery agent."""
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracyMeters": self.accuracy_m,
            "label": self.label,
            "source": self.source,
        }


def _coordinate(raw: Any, name: str, limit: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InvalidLocation(f"{name} must be a number, not {raw!r}.") from None
    # NaN fails both comparisons, which is the point — it is not a place.
    if not -limit <= value <= limit:
        raise InvalidLocation(f"{name} must be between -{limit:g} and {limit:g}, not {value}.")
    return value


def parse(raw: Any) -> UserLocation | None:
    """Read a location off the wire, or refuse it.

    Returns None for "the caller sent none", which is a legitimate errand — the
    agent orders at the counter as it always did. Raises for "the caller sent
    something, and it is not a location", because silently dropping a malformed
    fix would place the order at the default branch while the operator believes
    it went to the nearest one.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidLocation("A location must be an object with latitude and longitude.")

    latitude = _coordinate(raw.get("latitude"), "latitude", 90)
    longitude = _coordinate(raw.get("longitude"), "longitude", 180)

    if abs(latitude) < _NULL_ISLAND_TOLERANCE and abs(longitude) < _NULL_ISLAND_TOLERANCE:
        raise InvalidLocation(
            "0, 0 is not a real fix — it is what a device reports when it has "
            "none. Allow location access, or type an address instead."
        )

    accuracy = raw.get("accuracyMeters", raw.get("accuracy"))
    if accuracy is not None:
        try:
            accuracy = float(accuracy)
        except (TypeError, ValueError):
            accuracy = None
        else:
            # A negative or absurd radius says the device is guessing about its
            # own guess. Drop the claim rather than the fix.
            if accuracy < 0 or accuracy > 1_000_000:
                accuracy = None

    label = raw.get("label")
    label = label.strip()[:200] if isinstance(label, str) and label.strip() else None

    source = raw.get("source")
    source = source if source in ("browser", "manual") else "manual"

    return UserLocation(
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy,
        label=label,
        source=source,
    )


def haversine_km(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float
) -> float:
    """Great-circle distance in kilometres.

    Straight-line, not road distance — good enough to pick the nearest branch of
    a handful, and honest about being an estimate wherever it is reported.
    """
    earth_radius_km = 6371.0088
    d_lat = radians(to_lat - from_lat)
    d_lon = radians(to_lon - from_lon)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(from_lat)) * cos(radians(to_lat)) * sin(d_lon / 2) ** 2
    )
    return round(2 * earth_radius_km * asin(sqrt(a)), 3)


# --------------------------------------------------------------------------- #
# The customer's own address
# --------------------------------------------------------------------------- #
# One address, held once, read by everything that needs somewhere to deliver to
# when no browser was there to ask. Built from configuration rather than written
# here — see `FK_CUSTOMER_ADDRESS` in agent/config.py.
#
# It is deliberately a `UserLocation` like any other and not a special case: the
# branch chooser, the delivery contract and the courier all take one of these,
# so a saved address that arrived from .env and a fix that arrived from a phone
# travel down exactly the same path. `source` is "manual" because that is what
# it is — somebody typed it, once, into a file.
def saved() -> UserLocation:
    """The customer's saved address — where "deliver it to me" means.

    Used by the A2A flow, which has no location UI at all, and offered as one
    click by the errand console instead of a permission prompt. A run that
    carries its own fix uses that one; this is the fallback, never an override.
    """
    from agent.config import settings

    return UserLocation(
        latitude=settings.customer_latitude,
        longitude=settings.customer_longitude,
        label=settings.customer_address,
        source="manual",
    )


# --------------------------------------------------------------------------- #
# The location this errand is going to
# --------------------------------------------------------------------------- #
# Module-level, and deliberately so: the tools read it the way they read the
# wallet, rather than the agent carrying coordinates through its prompt and
# handing them back to a tool that has to trust them.
_current: UserLocation | None = None

# Did the customer ask for this errand to be brought to them?
#
# Held next to the location and not inside it, because the two are different
# questions and they come apart. `_current` is *where* the order goes — and on
# this service it now always goes somewhere, since a take-away order handed to a
# courier has to. This is whether the customer said "deliver it to me", which is
# the consent the delivery agent otherwise stops and asks for on its own board
# with the rider already holding the food.
#
# False is not "do not deliver". It is "nobody answered that here", and it
# leaves the delivery agent to ask, which is what it has always done.
_where_it_goes: bool = False


def remember(location: UserLocation | None, where_it_goes: bool = False) -> None:
    """Set the location and the consent for the run about to start.

    Args:
        location: Where the order goes. None clears it.
        where_it_goes: Did the customer ask for it to be brought to them?
            Defaults to False — no consent recorded — which is what every caller
            written before the switch existed passes, and it makes the delivery
            agent ask.
    """
    global _current, _where_it_goes
    _current = location
    _where_it_goes = where_it_goes


def current() -> UserLocation | None:
    """The location this errand was sent with, or None if it was sent without."""
    return _current


def consented() -> bool:
    """Did the customer ask for this errand to be delivered to them?

    Read at the handover and sent across with the request, so the delivery agent
    does not put a question to somebody who has already answered it.
    """
    return _where_it_goes


def reset() -> None:
    """Forget the previous errand's location and consent.

    Called at the top of every run for the same reason the wallet is reset: the
    server outlives any single errand, and yesterday's customer must not receive
    today's order — nor yesterday's answer to a question about it.
    """
    remember(None)
