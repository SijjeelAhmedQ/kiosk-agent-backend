"""Thin HTTP client for the Friends Kitchen API.

Every endpoint answers `{success, data, message}` on the way out and
`{success: false, code, message}` on the way in, so unwrapping and error
translation belong in one place rather than in each tool.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent.config import settings


class FriendsKitchenApiError(RuntimeError):
    """A refusal from the restaurant, in words the agent can act on."""

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


_client = httpx.Client(
    base_url=settings.api_base,
    timeout=settings.http_timeout,
    headers={"X-Terminal-Id": settings.terminal_id},
)


def _unwrap(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        raise FriendsKitchenApiError(
            f"The restaurant returned {response.status_code} with a non-JSON body.",
            status=response.status_code,
        ) from None

    if not payload.get("success", False):
        raise FriendsKitchenApiError(
            payload.get("message") or f"Request failed with {response.status_code}.",
            code=payload.get("code"),
            status=response.status_code,
        )
    return payload.get("data")


def get(path: str, **params: Any) -> Any:
    return _unwrap(_client.get(path, params={k: v for k, v in params.items() if v is not None}))


def post(path: str, body: dict[str, Any]) -> Any:
    return _unwrap(_client.post(path, json=body))


def health() -> bool:
    """Is the restaurant open? Checked once at startup so a dead backend is a
    clear message instead of the agent's first tool call failing mysteriously."""
    try:
        response = httpx.get(
            settings.api_base.rsplit("/api/", 1)[0] + "/health",
            timeout=5,
        )
        return response.status_code == 200 and response.json().get("success", False)
    except Exception:
        return False
