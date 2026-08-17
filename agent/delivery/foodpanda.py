"""Foodpanda, as a delivery provider.

Foodpanda's courier API is credentialed and regional, and this codebase has no
account for it. So what this module is, honestly: the contract translated into
the shape Foodpanda's dispatch API uses — a `POST` to create a delivery, a `GET`
to poll it, a bearer token from the environment — behind the same two methods
every other provider implements.

Point `FOODPANDA_API_BASE` and `FOODPANDA_API_KEY` at the real service (or a
sandbox) and this dispatches for real. Leave them unset and `configured` is
False, the registry falls back to the in-house courier, and the health endpoint
says why. What it never does is pretend: there is no mock success path here,
because a provider that fakes a dispatch is a provider that reports an order
delivered when no rider was ever called.

**The key stays here.** It is read from the environment in this process and put
on an outgoing header. It is never returned by a tool, never part of a
`DeliveryJob`, and so never reaches the browser.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agent.config import settings
from agent.delivery.contract import (
    DeliveryJob,
    DeliveryRequest,
    DeliveryUnavailable,
    normalise_status,
)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FoodpandaProvider:
    """Dispatching through Foodpanda's courier API."""

    name = "foodpanda"
    display_name = "Foodpanda"

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self._base = (base_url or settings.foodpanda_base).rstrip("/")
        self._timeout = timeout or settings.delivery_timeout

    @property
    def _key(self) -> str:
        # Read per call rather than cached at construction, so a key added to
        # .env and a restart are the only two steps — not a third that is easy
        # to forget because the object was built at import time.
        return os.getenv("FOODPANDA_API_KEY", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._base and self._key)

    @property
    def problem(self) -> str | None:
        """What is missing, in words the health endpoint can show."""
        if not self._base:
            return "FOODPANDA_API_BASE is not set."
        if not self._key:
            return (
                "FOODPANDA_API_KEY is not set. Put it in friends-kitchen-agent-backend/.env "
                "— it stays on the server and never reaches the browser."
            )
        return None

    # -- the wire ---------------------------------------------------------- #
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.configured:
            raise DeliveryUnavailable(
                self.problem or "Foodpanda is not configured on this server."
            )

        try:
            response = httpx.request(
                method,
                f"{self._base}{path}",
                json=body,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Accept": "application/json",
                },
            )
        except httpx.TimeoutException:
            raise DeliveryUnavailable(
                f"Foodpanda did not answer within {self._timeout:g}s."
            ) from None
        except httpx.HTTPError:
            raise DeliveryUnavailable("Foodpanda is not reachable.") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            # Their refusals carry a sentence worth passing on — it is usually
            # the actionable half ("delivery area not covered").
            raise DeliveryUnavailable(
                str(payload.get("message") or payload.get("error") or "")
                or f"Foodpanda refused the job ({response.status_code})."
            )

        return payload if isinstance(payload, dict) else {}

    def _job(self, data: dict) -> DeliveryJob:
        courier = data.get("courier") if isinstance(data.get("courier"), dict) else {}
        return DeliveryJob(
            provider=self.name,
            job_id=str(data.get("delivery_id") or data.get("id") or ""),
            status=normalise_status(data.get("status")),
            message=data.get("status_message") or data.get("message"),
            courier=courier.get("name"),
            eta_minutes=_int(data.get("estimated_delivery_minutes") or data.get("eta_minutes")),
            fee=str(data["delivery_fee"]) if data.get("delivery_fee") is not None else None,
            tracking_url=data.get("tracking_url"),
            raw=data,
        )

    def _body(self, request: DeliveryRequest) -> dict:
        """Our contract in Foodpanda's field names.

        The whole of the Foodpanda-specific knowledge in this codebase is these
        twenty lines. That is the point of the contract — everything upstream
        speaks `DeliveryRequest`, and only this method has ever heard of
        `pickup_contact`.
        """
        message = request.to_message()
        return {
            "reference_id": request.order_number,
            "pickup": {
                "name": request.pickup.name,
                "address": request.pickup.address,
                "latitude": request.pickup.latitude,
                "longitude": request.pickup.longitude,
                "phone_number": request.pickup.phone,
                "comment": request.pickup.note,
            },
            "dropoff": {
                "address": request.dropoff.address,
                "latitude": request.dropoff.latitude,
                "longitude": request.dropoff.longitude,
                "phone_number": request.dropoff.phone,
                "comment": request.dropoff.note or request.notes,
            },
            "items": [
                {"name": item["name"], "quantity": item["quantity"]}
                for item in message["order"]["items"]
            ],
            "order_value_paid": request.paid,
        }

    # -- the contract ------------------------------------------------------ #
    def dispatch(self, request: DeliveryRequest) -> DeliveryJob:
        request.validate()
        job = self._job(self._call("POST", "/v1/deliveries", self._body(request)))
        if not job.job_id:
            raise DeliveryUnavailable(
                "Foodpanda accepted the order but returned no delivery id, "
                "so there is nothing to track it by."
            )
        return job

    def status(self, job_id: str) -> DeliveryJob:
        return self._job(self._call("GET", f"/v1/deliveries/{job_id}"))
