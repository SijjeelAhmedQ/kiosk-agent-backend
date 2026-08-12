"""The wire format the two agents speak.

Shaped after Google's A2A spec — an agent card at a well-known URL, tasks that
move through a small set of states, messages made of typed parts, and artifacts
for the structured output — but only the slice this system needs is implemented.
Claiming full conformance would be a lie; the card says so in `protocolVariant`.

Three decisions worth knowing about, because the rest of the package leans on
them:

**Money never travels as prose.** A quote and a receipt are `data` parts with
real numbers in them. Two language models agreeing in English about what a total
was is not a transaction, and the buyer verifies the receipt against the
restaurant afterwards rather than believing it.

**The merchant is reachable over HTTP even though both agents currently live in
one process.** The buyer's tools go out through httpx to the merchant's own
endpoint, the one the agent card advertises. That costs a loopback round trip
and buys the thing that makes this agent-to-agent rather than two objects
calling each other: moving the merchant to another machine is a change of
environment variable, not of design.

**`input_required` is where the negotiation actually happens.** It is the state
the merchant parks in after quoting — the moment the buyer gets to look at its
wallet and decide whether to offer the coupon, ask for something cheaper, or
walk away.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.a2a.config import a2a_settings

PROTOCOL_VERSION = "0.3"

Role = Literal["buyer", "merchant"]


# --------------------------------------------------------------------------- #
# Task states
# --------------------------------------------------------------------------- #
# Terminal states are the ones a listener can stop on. `rejected` is separate
# from `failed` on purpose: an errand the buyer declined because the money did
# not stretch is a correct outcome, and reporting it as a failure would train
# whoever reads the console to ignore red.
SUBMITTED = "submitted"
WORKING = "working"
INPUT_REQUIRED = "input_required"
COMPLETED = "completed"
FAILED = "failed"
REJECTED = "rejected"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, REJECTED, CANCELLED})


# --------------------------------------------------------------------------- #
# Parts, messages, artifacts
# --------------------------------------------------------------------------- #
def text_part(text: str) -> dict[str, Any]:
    """What one agent says to the other, in words."""
    return {"kind": "text", "text": text}


def data_part(data: dict[str, Any]) -> dict[str, Any]:
    """What one agent hands the other, in numbers."""
    return {"kind": "data", "data": data}


def parts_text(parts: list[dict[str, Any]]) -> str:
    """Every text part of a message, run together — the prose half."""
    return "\n".join(p["text"] for p in parts if p.get("kind") == "text" and p.get("text"))


def parts_data(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every data part of a message — the half that is allowed to carry money."""
    return [p["data"] for p in parts if p.get("kind") == "data" and isinstance(p.get("data"), dict)]


def _stamp() -> float:
    return round(time.time(), 3)


@dataclass
class Message:
    """One turn of the conversation."""

    role: Role
    parts: list[dict[str, Any]]
    task_id: str = ""
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=_stamp)

    @classmethod
    def say(
        cls,
        role: Role,
        text: str,
        data: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> "Message":
        """The common case: something said, optionally with figures attached.

        `message_id` exists so the *sender* mints the id. Without it the buyer
        would file an utterance under one id in its own transcript while the
        merchant recorded the same utterance under another, and correlating the
        two sides of a conversation afterwards would be guesswork.
        """
        parts = [text_part(text)] if text else []
        if data:
            parts.append(data_part(data))
        fields = {"message_id": message_id} if message_id else {}
        return cls(role=role, parts=parts, **fields)

    def to_wire(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "taskId": self.task_id,
            "role": self.role,
            "parts": self.parts,
            "ts": self.ts,
        }


# The artifact names both sides agree on. Anything else is free-form, but these
# two are what the console renders as cards and what the buyer reads with code
# rather than with the model.
QUOTE = "quote"
RECEIPT = "receipt"


@dataclass
class Artifact:
    """A structured result the merchant produces — a quote, or a receipt."""

    name: str
    data: dict[str, Any]
    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=_stamp)

    def to_wire(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "parts": [data_part(self.data)],
            "ts": self.ts,
        }


# --------------------------------------------------------------------------- #
# Requests in
# --------------------------------------------------------------------------- #
class MerchantTaskIn(BaseModel):
    """A buyer opening a conversation with the merchant.

    Note what is *not* here: no coupon, no budget. The merchant is told what
    someone wants to eat, and finds out about the money only if the buyer
    chooses to bring it up. That asymmetry is the point of the exercise.
    """

    message: str = Field(min_length=1, max_length=4000)
    data: dict[str, Any] | None = None
    buyerId: str | None = Field(default=None, max_length=64)
    messageId: str | None = Field(default=None, max_length=64)


class MerchantMessageIn(BaseModel):
    """A buyer's reply inside a conversation already under way."""

    message: str = Field(default="", max_length=4000)
    data: dict[str, Any] | None = None
    messageId: str | None = Field(default=None, max_length=64)


class StartRunIn(BaseModel):
    """The console sending the buyer out on an errand.

    Deliberately the same shape as the errand flow's own `StartRunIn` on 8100,
    minus the mode: the operator fills in the same three things either way, and
    the two forms should not disagree about what an errand is.
    """

    instruction: str = Field(min_length=1, max_length=2000)
    couponCode: str | None = Field(default=None, max_length=40)
    cashLimit: float = Field(default=0, ge=0, le=1_000_000)
    customerId: str | None = Field(default=None, max_length=50)


# --------------------------------------------------------------------------- #
# The agent card
# --------------------------------------------------------------------------- #
def agent_card() -> dict[str, Any]:
    """Who the merchant is and what it can be asked for.

    Served at `/.well-known/agent-card.json`. The buyer fetches this before its
    first message rather than being told where to go in its prompt — discovery
    is cheap here and it is the part of A2A that stops the two agents from being
    hardwired to each other.
    """
    base = a2a_settings.public_base.rstrip("/")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        # An honest label. This implements the card, the task lifecycle, typed
        # parts and artifacts over HTTP+SSE — not JSON-RPC, not push
        # notifications, not authenticated extended cards.
        "protocolVariant": "http+sse subset",
        "name": "Friends Kitchen Ordering Desk",
        "description": (
            "The restaurant's own agent. Knows the menu, the prices, the coupon "
            "rules and how to take payment. Talk to it in plain language; it "
            "answers with a quote you can check, and a receipt when it is done."
        ),
        "provider": {"organization": "Friends Kitchen"},
        "version": "1.0.0",
        "url": f"{base}/api/a2a/merchant",
        "preferredTransport": "HTTP+SSE",
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": "order_food",
                "name": "Order food",
                "description": (
                    "Take an order in plain language, price it from the live menu, "
                    "and place it. Substitutes the nearest match when an item is "
                    "not on the menu, and says which substitution it made."
                ),
                "tags": ["ordering", "menu", "checkout"],
                "examples": [
                    "Two zinger burgers and a large drink, take away.",
                    "Something vegetarian under Rs 800, dine in.",
                ],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "quote_order",
                "name": "Quote an order",
                "description": (
                    "Price a basket without committing to it. Returns a quote "
                    "artifact with lines, subtotal, tax and total in PKR."
                ),
                "tags": ["pricing", "quote"],
            },
            {
                "id": "apply_coupon",
                "name": "Apply a coupon",
                "description": (
                    "Check a coupon code against the current basket and, on "
                    "checkout, redeem it. Value coupons draw down what they can "
                    "and keep the rest; product coupons are spent in full."
                ),
                "tags": ["coupon", "discount"],
            },
        ],
        # Not part of the spec, and the single most useful field on this card:
        # every figure in this system is whole rupees, and a buyer agent that
        # assumes minor units reports Rs 1,093 as $10.93.
        "x-currency": {"code": "PKR", "display": "Rs 1,093", "minorUnits": False},
    }


# --------------------------------------------------------------------------- #
# Console events
# --------------------------------------------------------------------------- #
# What the A2A console renders. Close to the errand flow's event shape on 8100 —
# same `type` discriminator, same `end` sentinel — so the frontend's stream
# handling is familiar, with one addition: `speaker`, because here there are two
# of them and a transcript with no attribution is unreadable.
def event_status(status: str, **extra: Any) -> dict[str, Any]:
    return {"type": "status", "status": status, **extra}


def event_message(message: Message) -> dict[str, Any]:
    return {
        "type": "message",
        "speaker": message.role,
        "text": parts_text(message.parts),
        "data": parts_data(message.parts),
        "messageId": message.message_id,
        "ts": message.ts,
    }


def event_artifact(artifact: Artifact, speaker: Role = "merchant") -> dict[str, Any]:
    return {
        "type": "artifact",
        "speaker": speaker,
        "name": artifact.name,
        "data": artifact.data,
        "artifactId": artifact.artifact_id,
        "ts": artifact.ts,
    }


def event_tool(speaker: Role, tool_use_id: str, name: str) -> dict[str, Any]:
    return {"type": "tool", "speaker": speaker, "toolUseId": tool_use_id, "name": name}


def event_tool_result(speaker: Role, **result: Any) -> dict[str, Any]:
    return {"type": "tool_result", "speaker": speaker, **result}


def event_error(message: str) -> dict[str, Any]:
    return {"type": "error", "message": message}


def event_end() -> dict[str, Any]:
    return {"type": "end"}
