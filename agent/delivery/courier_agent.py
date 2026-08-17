"""The in-house delivery agent, reached over HTTP.

This is the default provider, and it talks to `delivery_server.py` — a separate
service on 8102 with its own agent card, exactly as the A2A merchant on 8101 is
a separate service from the errand server on 8100. The ordering agent reaches it
over real HTTP even when both are running on one laptop, because that is what
makes it agent-to-agent rather than a function call wearing a costume.

It is also the provider that needs no credentials, which is what lets the whole
delivery flow be demonstrated without an account anywhere.
"""

from __future__ import annotations

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


class CourierAgentProvider:
    """Friends Kitchen's own delivery agent."""

    name = "internal"
    display_name = "Friends Kitchen Delivery"

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self._base = (base_url or settings.delivery_base).rstrip("/")
        self._timeout = timeout or settings.delivery_timeout

    @property
    def configured(self) -> bool:
        """It needs an address and nothing else — no key, no account."""
        return bool(self._base)

    # -- the wire ---------------------------------------------------------- #
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        """One request, with every failure turned into `DeliveryUnavailable`.

        The ordering agent has one thing it can usefully do about a courier
        that will not answer — report it and leave the order paid but
        undelivered — so every transport failure arrives as one exception type
        carrying a sentence worth showing.
        """
        url = f"{self._base}{path}"
        try:
            response = httpx.request(method, url, json=body, timeout=self._timeout)
        except httpx.TimeoutException:
            raise DeliveryUnavailable(
                f"{self.display_name} did not answer within {self._timeout:g}s."
            ) from None
        except httpx.HTTPError:
            raise DeliveryUnavailable(
                f"{self.display_name} is not reachable at {self._base}. "
                "Start it with: .venv\\Scripts\\python -m uvicorn delivery_server:app --port 8102"
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise DeliveryUnavailable(
                f"{self.display_name} returned {response.status_code} with a non-JSON body."
            ) from None

        if not payload.get("success", False):
            raise DeliveryUnavailable(
                payload.get("detail")
                or payload.get("message")
                or f"{self.display_name} refused the job ({response.status_code})."
            )

        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _job(self, data: dict) -> DeliveryJob:
        return DeliveryJob(
            provider=self.name,
            job_id=str(data.get("jobId") or ""),
            status=normalise_status(data.get("status")),
            message=data.get("message"),
            courier=(data.get("courier") or {}).get("name")
            if isinstance(data.get("courier"), dict)
            else data.get("courier"),
            eta_minutes=_int(data.get("etaMinutes")),
            fee=data.get("fee"),
            tracking_url=data.get("trackingUrl"),
            raw=data,
        )

    # -- the contract ------------------------------------------------------ #
    def dispatch(self, request: DeliveryRequest) -> DeliveryJob:
        request.validate()
        job = self._job(self._call("POST", "/api/delivery/jobs", request.to_message()))
        if not job.job_id:
            raise DeliveryUnavailable(
                f"{self.display_name} accepted the order but returned no job id, "
                "so there is nothing to track it by."
            )
        return job

    def status(self, job_id: str) -> DeliveryJob:
        return self._job(self._call("GET", f"/api/delivery/jobs/{job_id}"))
