"""Talking to the services, over the standard library only.

`urllib` rather than `httpx`, deliberately. Skill scripts should run under any
Python that is to hand — a spec-compliant agent host will not necessarily be
inside this repository's virtual environment — and the moment a script needs a
package installed, the deterministic thing it was written to do stops being
deterministic. Nothing here is on a hot path.

Every endpoint in this system answers `{"success": true, "data": ...}`, or
`{"detail": "..."}` when it refuses. `unwrap` is that envelope, and it is the
reason this module exists at all: a script that reached for `["data"]` directly
would turn a refusal — which carries a sentence written for a person — into a
`KeyError`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

DEFAULT_TIMEOUT = 30.0


class ServiceError(RuntimeError):
    """The service answered, and the answer was no."""


class ServiceDown(RuntimeError):
    """The service did not answer at all."""


def request(
    method: str,
    url: str,
    body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """One call, unwrapped. Raises `ServiceError` / `ServiceDown` on failure."""
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    call = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return unwrap(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        raise ServiceError(f"{method.upper()} {url} -> {exc.code}: {_detail(exc)}") from None
    except urllib.error.URLError as exc:
        raise ServiceDown(f"{url} is not answering ({exc.reason}).") from None
    except TimeoutError:
        raise ServiceDown(f"{url} did not answer within {timeout:g}s.") from None
    except json.JSONDecodeError:
        raise ServiceError(f"{method.upper()} {url} returned a body that is not JSON.") from None


def get(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return request("GET", url, timeout=timeout)


def post(url: str, body: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return request("POST", url, body=body, timeout=timeout)


def unwrap(payload: Any) -> Any:
    """The `data` out of the envelope, or the refusal raised as a sentence.

    An unwrapped body — the well-known agent card is the one in this system —
    passes through as it is, because a card is not wrapped and never was.
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("success") is False:
        raise ServiceError(payload.get("detail") or payload.get("message") or "refused.")
    if "data" in payload and payload.get("success") is True:
        return payload["data"]
    return payload


def _detail(exc: urllib.error.HTTPError) -> str:
    """The sentence out of a refusal body, or the status reason if there is none."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — a body we cannot read is not a second failure
        return exc.reason or "no detail"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or body)
    return str(body)


def reachable(url: str, timeout: float = 3.0) -> tuple[bool, str | None]:
    """`(up, why_not)` for one health URL. Never raises."""
    try:
        get(url, timeout=timeout)
    except (ServiceDown, ServiceError) as exc:
        return False, str(exc)
    return True, None


def stream(url: str, timeout: float = DEFAULT_TIMEOUT) -> Iterator[dict]:
    """Follow a Server-Sent Events endpoint, yielding each decoded `data:` line.

    The services stream a run this way and the control panel reads it with an
    `EventSource`. A script that only polled the final state would miss the
    trace, which is the interesting half of watching an agent work.

    Stops after `{"type": "end"}`, which every run emits, so a caller does not
    have to decide for itself when a finished run is finished.
    """
    call = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        response = urllib.request.urlopen(call, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise ServiceError(f"GET {url} -> {exc.code}: {_detail(exc)}") from None
    except urllib.error.URLError as exc:
        raise ServiceDown(f"{url} is not answering ({exc.reason}).") from None

    with response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            blob = line[5:].strip()
            if not blob:
                continue
            try:
                event = json.loads(blob)
            except json.JSONDecodeError:
                continue
            yield event
            if isinstance(event, dict) and event.get("type") == "end":
                return
