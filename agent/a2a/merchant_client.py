"""The buyer's side of the wire: an A2A client for whatever the card points at.

Everything here is async, and that is not a style preference. The merchant
currently runs in this same process, so a buyer tool that made a *blocking* call
to the merchant's endpoint would hold the event loop that the merchant's handler
needs in order to answer — a deadlock, not a slow request. Async all the way
down means the loop is free while the buyer waits, and it stays correct
unchanged on the day the merchant moves to another machine.

The client knows nothing about ordering. It fetches a card, opens a task, sends
messages, and hands back whatever came out. What those messages *mean* is the
buyer agent's business.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent.a2a.config import a2a_settings

CARD_PATH = "/.well-known/agent-card.json"


class MerchantUnreachable(RuntimeError):
    """The other agent did not answer, in words the buyer can act on."""


class MerchantRefused(RuntimeError):
    """The other agent answered, and the answer was no."""


def _client() -> httpx.AsyncClient:
    # A fresh client per errand rather than a module-level one: a shared client
    # is process state, and keeping this package free of process state is the
    # promise the whole design rests on.
    return httpx.AsyncClient(timeout=a2a_settings.reply_timeout)


async def _unwrap(response: httpx.Response) -> Any:
    """This service answers `{success, data}`; a refusal carries `detail`."""
    try:
        payload = response.json()
    except ValueError:
        raise MerchantUnreachable(
            f"The merchant returned {response.status_code} with a non-JSON body."
        ) from None

    if response.status_code >= 400 or payload.get("success") is False:
        raise MerchantRefused(
            payload.get("detail")
            or payload.get("message")
            or f"The merchant refused with {response.status_code}."
        )
    return payload.get("data", payload)


class MerchantConnection:
    """One conversation with one merchant.

    Holds the discovered card and the task id, so the buyer's tools do not have
    to carry either around in prose — the single most reliable way to lose an id
    is to ask a language model to remember it.
    """

    def __init__(self, base_url: str | None = None, buyer_id: str | None = None) -> None:
        self.base_url = (base_url or a2a_settings.public_base).rstrip("/")
        self.card: dict[str, Any] | None = None
        self.endpoint: str | None = None
        self.task_id: str | None = None

        # Who is buying, named on the conversation the buyer opens. The console
        # sets it to the run id, which is what lets the merchant's side find the
        # run that started it — see `agent/a2a/delivery.py` for the one thing it
        # looks up there. Nothing about the errand travels in it: it is an
        # opaque id, and the drop it leads to never crosses the wire.
        self.buyer_id = buyer_id

    async def discover(self) -> dict[str, Any]:
        """Fetch the agent card and remember where it says to send messages.

        The endpoint comes off the card rather than being assembled here. That
        is the whole reason a card exists, and it is what lets the buyer be
        pointed at a different restaurant without a code change.
        """
        url = f"{self.base_url}{CARD_PATH}"
        try:
            async with _client() as http:
                card = await _unwrap(await http.get(url))
        except httpx.HTTPError as exc:
            raise MerchantUnreachable(
                f"No agent answered at {url} ({exc}). Is the A2A service running "
                f"on port {a2a_settings.port}?"
            ) from exc

        self.card = card
        self.endpoint = str(card.get("url") or f"{self.base_url}/api/a2a/merchant")
        return card

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.endpoint:
            await self.discover()
        try:
            async with _client() as http:
                return await _unwrap(await http.post(f"{self.endpoint}{path}", json=body))
        except httpx.TimeoutException as exc:
            raise MerchantUnreachable(
                f"The merchant did not reply within {a2a_settings.reply_timeout:.0f}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise MerchantUnreachable(f"Could not reach the merchant ({exc}).") from exc

    async def open(
        self,
        message: str,
        data: dict[str, Any] | None = None,
        buyer_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Say the first thing, and get a task id back."""
        reply = await self._post(
            "/tasks",
            {
                "message": message,
                "data": data,
                "buyerId": buyer_id or self.buyer_id,
                "messageId": message_id,
            },
        )
        self.task_id = reply.get("taskId")
        return reply

    async def send(
        self,
        message: str,
        data: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Say the next thing in a conversation already under way."""
        if not self.task_id:
            return await self.open(message, data, message_id=message_id)
        return await self._post(
            f"/tasks/{self.task_id}/messages",
            {"message": message, "data": data, "messageId": message_id},
        )
