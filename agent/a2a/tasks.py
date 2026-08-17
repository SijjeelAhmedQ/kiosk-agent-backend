"""Where a conversation lives while it is happening, and who gets to watch it.

Two records, because there are two things with a lifetime here:

* `MerchantTask` — one conversation, from the merchant's side. Holds the
  transcript and the artifacts, and serialises turns so a buyer that sends twice
  in a row cannot have the merchant thinking about both at once.
* `ConsoleRun` — one errand, from the operator's side. Holds the stream the A2A
  console renders: both agents' messages, the tool calls underneath them, and
  the buyer's final report.

The publish/subscribe half is the same design the errand flow uses on 8100 —
a queue per listener rather than one shared queue, and backlog-plus-live handed
over in a single call with no `await` between them. Both halves of that are
load-bearing and neither is obvious, so the reasoning is written out on
`Stream.attach` rather than left to be rediscovered.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from agent.a2a.config import a2a_settings
from agent.a2a.protocol import (
    SUBMITTED,
    TERMINAL_STATES,
    Artifact,
    Message,
    event_end,
)
from agent.location import UserLocation


class Stream:
    """An append-only event log that listeners can join late."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.listeners: list[asyncio.Queue] = []

    def emit(self, event: dict[str, Any]) -> None:
        """Record, then publish. Recording is what lets a console that opens the
        page late — or reloads it — still see the whole conversation."""
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)

    def attach(self) -> tuple[list[dict[str, Any]], asyncio.Queue]:
        """Take the backlog and a live feed in one go.

        Together, and with no `await` between them, is the point: that is what
        makes "everything so far" and "everything from now on" meet exactly
        once. Snapshotting and subscribing as two separate steps duplicates
        every event emitted before the listener connected.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self.listeners.append(queue)
        return list(self.events), queue

    def detach(self, queue: asyncio.Queue) -> None:
        """Stop feeding a listener that has gone away, so a page left open
        through a dozen reconnects does not accumulate queues forever."""
        with suppress(ValueError):
            self.listeners.remove(queue)

    @property
    def finished(self) -> bool:
        return any(event.get("type") == "end" for event in self.events)


# --------------------------------------------------------------------------- #
# The merchant's side of a conversation
# --------------------------------------------------------------------------- #
@dataclass
class MerchantTask:
    """One buyer, one order, however many messages it takes."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    buyer_id: str | None = None
    state: str = SUBMITTED
    messages: list[Message] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None
    stream: Stream = field(default_factory=Stream)

    # One turn at a time. The merchant holds a cart while it thinks, and two
    # overlapping turns would interleave their edits to it.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Anything the merchant wants to keep between turns that is not part of the
    # transcript — the cart, the placed order, its own tool state.
    session: dict[str, Any] = field(default_factory=dict)

    def record(self, message: Message) -> Message:
        message.task_id = self.id
        self.messages.append(message)
        return message

    def add_artifact(self, artifact: Artifact) -> Artifact:
        self.artifacts.append(artifact)
        return artifact

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_wire(self, include_history: bool = True) -> dict[str, Any]:
        """The task as the A2A spec shapes it: id, state, history, artifacts."""
        wire: dict[str, Any] = {
            "taskId": self.id,
            "state": self.state,
            "artifacts": [a.to_wire() for a in self.artifacts],
        }
        if self.error:
            wire["error"] = self.error
        if include_history:
            wire["history"] = [m.to_wire() for m in self.messages]
        return wire

    def latest_reply(self, artifacts_since: int = 0, messages_since: int = 0) -> dict[str, Any]:
        """The merchant's most recent turn, alone.

        What the buyer's tool actually wants back from a send: the whole history
        on every call would put the conversation in the model's context twice
        over, once as its own memory and once as a tool result.

        `artifacts_since` is the artifact count from before this turn, so only
        what the merchant produced *just now* goes back. Resending the whole
        list every time is worse than verbose: an estimate quote and the firm
        quote that superseded it are the same shape, and a buyer handed both
        will sooner or later check its budget against the wrong one.

        `messages_since` does the same for the reply, and matters more. A turn
        that dies before the merchant says anything would otherwise hand back
        the *previous* merchant message — the buyer reads a stale answer as a
        fresh one, believes it was understood, and asks its next question into
        a conversation that is already dead.
        """
        reply = next(
            (
                m
                for m in reversed(self.messages[messages_since:])
                if m.role == "merchant"
            ),
            None,
        )
        return {
            "taskId": self.id,
            "state": self.state,
            "message": reply.to_wire() if reply else None,
            "artifacts": [a.to_wire() for a in self.artifacts[artifacts_since:]],
            **({"error": self.error} if self.error else {}),
        }


# --------------------------------------------------------------------------- #
# The operator's side of an errand
# --------------------------------------------------------------------------- #
@dataclass
class ConsoleRun:
    """One errand as the A2A console follows it."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    instruction: str = ""
    status: str = "queued"          # queued | running | done | failed | cancelled
    final_text: str = ""
    error: str | None = None
    merchant_task_id: str | None = None
    wallet: dict[str, Any] = field(default_factory=dict)
    stream: Stream = field(default_factory=Stream)
    task: asyncio.Task | None = None

    # Where this errand's delivery goes, when the console named a drop. Held on
    # the run rather than in a module-level singleton the way the errand flow on
    # 8100 holds its location: with API hands two negotiations may overlap in
    # this process, and one shared address between them is how the second
    # customer receives the first one's dinner. None means "wherever the service
    # delivers to by default" — see `agent/a2a/delivery.py`.
    drop: UserLocation | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "runId": self.id,
            "status": self.status,
            "instruction": self.instruction,
            "merchantTaskId": self.merchant_task_id,
            "finalText": self.final_text,
            "error": self.error,
            "wallet": self.wallet,
            "events": self.stream.events,
        }


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #
class _Registry(OrderedDict):
    """A dict that forgets its oldest finished entry once it gets too big.

    This service keeps transcripts in memory so the console can be refreshed and
    still show one — which is the right call for a demo and the wrong one for a
    process that runs for a week. Bounding it is three lines; a database is not
    the lesson this project is teaching.
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def put(self, key: str, value: Any) -> Any:
        self[key] = value
        while len(self) > self._limit:
            self.popitem(last=False)
        return value


merchant_tasks: _Registry = _Registry(a2a_settings.keep_tasks)
console_runs: _Registry = _Registry(a2a_settings.keep_tasks)


# --------------------------------------------------------------------------- #
# Serving a stream
# --------------------------------------------------------------------------- #
async def publish(stream: Stream):
    """Yield an SSE body for one stream: backlog first, then live.

    The 20-second ping is not decoration — proxies and browsers both drop an
    idle event stream, and a merchant thinking through a long menu can easily
    go quiet for that long.
    """
    import json

    backlog, queue = stream.attach()
    try:
        for event in backlog:
            yield {"data": json.dumps(event)}
            if event.get("type") == "end":
                return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            yield {"data": json.dumps(event)}
            if event.get("type") == "end":
                return
    finally:
        stream.detach(queue)


def close(stream: Stream) -> None:
    """End a stream once, however many ways its owner can finish."""
    if not stream.finished:
        stream.emit(event_end())
