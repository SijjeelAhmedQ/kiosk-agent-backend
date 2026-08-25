"""Every service's console, as a stream anybody may read.

The three consoles each follow *their own* run: you POST an errand, you are
handed a run id, and you subscribe to that id. That is right for a console and
wrong for an operations board, which is why the dashboard has until now been
able to say only that the ordering agent is *busy* — never what it is doing. The
work was on a stream addressed to whoever started it.

This is the other stream. One per process rather than one per run, with no id in
the URL and nothing to start first:

    GET /api/agent/console          the last few hundred lines, immediately
    GET /api/agent/console/events   the same log, live, as Server-Sent Events

Anything the process says lands on it. A dashboard opened at nine o'clock sees
what the agent did at ten past, without having asked for it, and without one of
the consoles being open in another tab.

**Two sources feed it.**

* *The services' own events.* Every stream in this repo funnels through one
  `emit` — `Run.emit` on 8100, `Stream.emit` for the A2A desk and the delivery
  dispatcher — and each of those mirrors what it publishes to `mirror()` here.
  So this is not a second telemetry system with its own call sites to keep in
  step; it is the existing one, read from the side.
* *Python's logging.* `install()` puts a handler on the root logger, which is
  where httpx and Strands already write. That is the half that catches what no
  `emit` knows about — a connection refused, a traceback, a retry.

**What it will not do.** It never blocks the thing being logged: the ring buffer
forgets its oldest line rather than growing, a listener that has gone away is
dropped, and every failure inside this module is swallowed. A console that can
take an errand down is worse than no console. It also holds nothing that is not
already on a stream this system publishes — no payload is added here that the
run's own events do not carry, and `data` is deliberately a handful of scalars
rather than the whole record.

**Model text is coalesced, on purpose.** Strands streams a token at a time. One
line per token is not a log, it is a flood that pushes everything else off the
screen — so text is held per run and flushed as one line when the agent does
something else, when it gets long, or when the run ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from typing import Any

from sse_starlette.sse import EventSourceResponse

#: How much scrollback a process keeps. Enough that a dashboard opened mid-shift
#: has the last errand in full, small enough that a service left running for a
#: week costs no more memory on the last day than on the first.
KEPT = 800

#: A ping down the wire when nothing has been said, so a dashboard can tell "this
#: service is quiet" from "this service is gone".
PING_SECONDS = 20

#: Held model text is flushed once it reaches this, rather than waiting for the
#: agent to do something else. A paragraph is a line; an essay is several.
TEXT_FLUSH_CHARS = 220

#: Levels, in the order the dashboard ranks them.
LEVELS = ("debug", "info", "warn", "error")


# --------------------------------------------------------------------------- #
# Process identity
# --------------------------------------------------------------------------- #
#: Who this process is. Set by `install()`, and the reason a line does not have
#: to carry its own origin: one process is one service, so the service and its
#: default speaker are facts about the module rather than about the line.
_identity: dict[str, str] = {"service": "unknown", "agent": "operator"}

_lines: "deque[dict[str, Any]]" = deque(maxlen=KEPT)
_listeners: "list[asyncio.Queue]" = []
_lock = threading.Lock()
_seq = 0

#: The event loop, captured the first time a listener attaches — which always
#: happens inside it. Log records arrive from worker threads (Strands runs sync
#: tools off the loop), and `Queue.put_nowait` from another thread is not safe.
_loop: "asyncio.AbstractEventLoop | None" = None

#: Model text held per run, waiting to be flushed as one line — see the module
#: docstring. Keyed by the run this text belongs to.
_pending: dict[str, dict[str, str]] = {}

#: What each speaker last called and last said, per run.
#:
#: Two small pieces of memory that exist because the streams do not carry what
#: the log needs to read well:
#:
#: * A tool *result* on 8100 arrives with a `toolUseId` and no name, so a result
#:   line would read `matched: 3` with nothing saying what matched. The name is
#:   carried forward from the call that opened it — exact here because every
#:   speaker on this floor runs one tool at a time (runs are serialised on 8100,
#:   the merchant holds a turn lock, the dispatcher works one job at a time), and
#:   keyed by speaker as well as run so the buyer and the merchant, which share a
#:   process, cannot be attributed each other's calls.
#: * A finished run emits its text twice — once streamed token by token, then
#:   again whole as `final`. That is right for a console replaying one run and
#:   wrong here, where both land as lines. The second is dropped when it repeats
#:   the first.
#:
#: Both are cleared when a run ends, and capped in case one never does.
_last_tool: dict[tuple[str, str], str] = {}
_last_said: dict[tuple[str, str], str] = {}

#: How many runs' worth of the two dicts above to keep before forgetting the lot.
#: A ceiling rather than a policy: they are only ever read about the run being
#: worked on, so dropping older entries costs nothing but unbounded growth.
_MEMORY_CAP = 256

_installed = False


def identity() -> dict[str, str]:
    """What this process calls itself, for a health endpoint to report."""
    return dict(_identity)


# --------------------------------------------------------------------------- #
# The bus
# --------------------------------------------------------------------------- #
def _publish(line: dict[str, Any]) -> None:
    """Record, then fan out. Never raises, never blocks."""
    global _seq
    with _lock:
        _seq += 1
        line["seq"] = _seq
        _lines.append(line)
        listeners = list(_listeners)

    if not listeners:
        return

    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False

    for queue in listeners:
        if on_loop:
            with suppress(Exception):
                queue.put_nowait(line)
        elif _loop is not None and not _loop.is_closed():
            with suppress(RuntimeError):
                _loop.call_soon_threadsafe(queue.put_nowait, line)


def say(
    text: str,
    *,
    agent: str | None = None,
    level: str = "info",
    kind: str = "note",
    ref: str = "",
    tool: str | None = None,
    ok: bool | None = None,
    source: str = "agent",
    data: dict[str, Any] | None = None,
) -> None:
    """Put one line on this process's console.

    Args:
        text: What to show. One line's worth — this is a log, not a document.
        agent: Who said it, as the dashboard names actors ("ordering", "buyer",
            "merchant", "dispatcher", "courier"). Defaults to this process's own.
        level: One of `LEVELS`.
        kind: What sort of line it is — "tool", "status", "message", "error" —
            which is what the dashboard filters and draws glyphs on.
        ref: The run, task or job this belongs to, so a line can be traced back
            to the errand it came from.
        tool: The tool name, when the line is about one.
        ok: Whether the thing it reports succeeded.
        source: "agent" for a service's own events, "log" for Python logging.
        data: A handful of scalars, not a payload. See the module docstring.
    """
    if not text or not text.strip():
        return
    _publish(
        {
            "at": int(time.time() * 1000),
            "service": _identity["service"],
            "agent": agent or _identity["agent"],
            "level": level if level in LEVELS else "info",
            "kind": kind,
            "text": text.strip()[:1000],
            "ref": ref or "",
            "tool": tool,
            "ok": ok,
            "source": source,
            "data": data or None,
        }
    )


def snapshot(after: int = 0, limit: int = KEPT) -> list[dict[str, Any]]:
    """Everything after `seq`, oldest first — the backlog a page opens with."""
    with _lock:
        lines = [line for line in _lines if line["seq"] > after]
    return lines[-limit:] if limit else lines


def attach() -> "tuple[list[dict[str, Any]], asyncio.Queue]":
    """Take the backlog and a live feed in one go, with no `await` between them.

    The same contract as `agent/a2a/tasks.Stream.attach`, and for the same
    reason: snapshotting and subscribing as two steps sends every line emitted
    in between twice.
    """
    global _loop
    with suppress(RuntimeError):
        _loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    with _lock:
        backlog = list(_lines)
        _listeners.append(queue)
    return backlog, queue


def detach(queue: "asyncio.Queue") -> None:
    """Stop feeding a listener that has gone away."""
    with _lock:
        with suppress(ValueError):
            _listeners.remove(queue)


# --------------------------------------------------------------------------- #
# Mirroring a service's own events
# --------------------------------------------------------------------------- #
#: The speakers an event may name. Anything else falls back to the process's own
#: agent — a line attributed to nobody is worse than one attributed to the
#: service that emitted it.
_SPEAKERS = {"buyer", "merchant", "dispatcher", "courier", "ordering", "operator"}


def _hold(ref: str, agent: str, chunk: str) -> None:
    """Accumulate streamed model text rather than emitting a line per token."""
    if not chunk:
        return
    held = _pending.setdefault(ref, {"agent": agent, "text": ""})
    held["agent"] = agent
    held["text"] += chunk
    if len(held["text"]) >= TEXT_FLUSH_CHARS:
        flush(ref)


def flush(ref: str = "") -> None:
    """Emit whatever text is being held for a run, if any."""
    held = _pending.pop(ref, None)
    if not held:
        return
    text = held["text"].strip()
    if not text:
        return
    _remember(_last_said, (ref, held["agent"]), _echo_key(text))
    say(text, agent=held["agent"], kind="message", ref=ref)


def _echo_key(text: str) -> str:
    """A normalised head of a line, for recognising the same thing said twice.

    Compared on a head rather than in full because the two copies are never
    byte-identical: the streamed one arrives whole, the `final` that repeats it
    has been clipped to a preview. A hundred and twenty characters is far more
    than enough to tell a repeat from two genuinely different sentences.
    """
    return " ".join(text.split())[:120]


def _remember(store: dict[tuple[str, str], str], key: tuple[str, str], value: str) -> None:
    """Record one fact about a run, and stay bounded while doing it."""
    if len(store) >= _MEMORY_CAP:
        store.clear()
    store[key] = value


def _forget(ref: str) -> None:
    """Drop what was being carried for a run that has ended."""
    _pending.pop(ref, None)
    for store in (_last_tool, _last_said):
        for key in [entry for entry in store if entry[0] == ref]:
            store.pop(key, None)


def _preview(value: Any, limit: int = 160) -> str:
    """A payload as one line, short enough to sit in a log."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def mirror(event: dict[str, Any], ref: str = "", agent: str | None = None) -> None:
    """Put a service's own stream event onto the console, in words.

    Called from the one `emit` each stream funnels through, so there is no
    second set of call sites to keep in step with the first. Anything this does
    not recognise still lands — as its type and a preview — because a console
    that silently drops what it was not taught about is a console you cannot
    trust the first time something new breaks.
    """
    try:
        _mirror(event, ref, agent)
    except Exception:  # noqa: BLE001 — a log line is never worth a failed run
        pass


def _mirror(event: dict[str, Any], ref: str, agent: str | None) -> None:
    kind = str(event.get("type") or "")
    speaker = str(event.get("speaker") or "")
    who = speaker if speaker in _SPEAKERS else (agent or _identity["agent"])

    if kind == "text":
        _hold(ref, who, str(event.get("text") or ""))
        return

    # Anything else means the agent has stopped talking, so what it said goes
    # above what it did next rather than below it.
    flush(ref)

    if kind == "status":
        status = str(event.get("status") or "")
        message = _preview(event.get("message"))
        say(
            f"{status}{f' — {message}' if message else ''}" or "status",
            agent=who,
            kind="status",
            ref=ref,
            level="warn" if status in ("failed", "cancelled", "rejected") else "info",
            data={"status": status} if status else None,
        )

    elif kind == "tool":
        name = str(event.get("name") or "tool")
        _remember(_last_tool, (ref, who), name)
        say(f"calls {name}", agent=who, kind="tool", ref=ref, tool=name)

    elif kind == "tool_result":
        ok = event.get("ok")
        summary = _preview(event.get("summary") or event.get("detail"))
        # Named by the call that opened it when the result does not name itself —
        # see `_last_tool` on why that is exact rather than a guess.
        name = event.get("name") or _last_tool.get((ref, who))
        say(
            f"{str(name) + ' → ' if name else ''}{summary or ('ok' if ok else 'failed')}",
            agent=who,
            kind="tool_result",
            ref=ref,
            tool=str(name) if name else None,
            ok=None if ok is None else bool(ok),
            level="warn" if ok is False else "info",
        )

    elif kind in ("say", "final", "message"):
        text = _preview(event.get("text"), 400)
        # A `final` almost always repeats the text that was just streamed. Saying
        # the same sentence twice makes a log look broken — see `_last_said`.
        if text and _echo_key(text) != _last_said.get((ref, who)):
            _remember(_last_said, (ref, who), _echo_key(text))
            say(text, agent=who, kind="message", ref=ref)

    elif kind == "artifact":
        name = str(event.get("name") or "artifact")
        say(f"produced {name}", agent=who, kind="artifact", ref=ref, data={"name": name})

    elif kind == "awaiting":
        step = event.get("step")
        message = _preview(event.get("message"))
        say(
            message or (f"waiting for {step}" if step else "no longer waiting"),
            agent=who,
            kind="waiting" if step else "status",
            ref=ref,
            level="warn" if step else "info",
        )

    elif kind == "error":
        say(
            _preview(event.get("message"), 400) or "failed",
            agent=who,
            kind="error",
            ref=ref,
            level="error",
        )

    elif kind == "browser":
        say(f"browser {event.get('state') or ''}", agent=who, kind="status", ref=ref)

    elif kind == "location":
        restaurant = (event.get("restaurant") or {}).get("name")
        distance = event.get("distanceKm")
        say(
            "delivery location fixed"
            + (f" — nearest branch {restaurant}" if restaurant else "")
            + (f", {distance} km away" if distance is not None else ""),
            agent=who,
            kind="status",
            ref=ref,
        )

    elif kind == "end":
        say("run finished", agent=who, kind="status", ref=ref, level="debug")
        _forget(ref)

    else:
        say(
            f"{kind or 'event'} {_preview(event)}",
            agent=who,
            kind=kind or "note",
            ref=ref,
            level="debug",
        )


# --------------------------------------------------------------------------- #
# Python logging, onto the same bus
# --------------------------------------------------------------------------- #
_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


#: Loggers whose output never reaches the console, however loud it gets.
#:
#: One entry, and it earns its place. With `FK_OTEL=otlp` set and no collector
#: listening, the OTLP exporter retries every span batch and logs a warning and
#: then an error each time — two lines every few seconds, for as long as the
#: process runs. On a dashboard that colours a panel by its worst line, that is
#: a permanently red console reporting a condition which is not a failure of any
#: agent, and which `telemetry.describe()` already reports properly through each
#: service's health endpoint. It is exporter plumbing, and it belongs there
#: rather than in a log of what the agents did.
#:
#: Nothing else is muted. A filter list is a thing that quietly grows until the
#: log no longer shows you what broke.
_MUTED_LOGGERS = ("opentelemetry",)


class _Bridge(logging.Handler):
    """Root-logger records, onto the console bus.

    Never re-enters: a handler that logs while handling a record is how a
    process ends up in a loop with itself, so everything here is guarded and
    `handleError` deliberately does nothing.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            if record.name.startswith(_MUTED_LOGGERS):
                return
            message = record.getMessage()
            # The dashboard's own reading of the console is not news. Without
            # this, every request it makes appears *in* what it is reading.
            if "/console" in message or "/health" in message:
                return
            if record.exc_info and record.exc_info[0] is not None:
                message = f"{message} · {record.exc_info[0].__name__}"
            say(
                message,
                level=_LEVEL_NAMES.get(record.levelno, "info"),
                kind="log",
                source="log",
                data={"logger": record.name},
            )
        except Exception:  # noqa: BLE001
            pass

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: D102
        pass


def install(service: str, agent: str) -> dict[str, str]:
    """Name this process and start capturing its logging.

    Args:
        service: The service id the dashboard groups by — "ordering", "a2a",
            "courier", "dispatcher".
        agent: Who this process speaks as by default, as the dashboard names
            actors. Events that name their own speaker override it.

    Returns:
        `identity()`, safe to hand to a health endpoint.

    Called a second time — uvicorn `--reload` re-imports the module — it renames
    the process and does not add a second handler.
    """
    global _installed
    _identity["service"] = service
    _identity["agent"] = agent

    if _installed:
        return identity()
    _installed = True

    with suppress(Exception):
        root = logging.getLogger()
        root.addHandler(_Bridge(level=logging.INFO))
        # Only if nobody has asked for more: raising the root level here would
        # be this module deciding how noisy an operator's own terminal is.
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)

    say(f"{service} console attached", kind="status", level="debug")
    return identity()


# --------------------------------------------------------------------------- #
# The two routes, so each server needs one line
# --------------------------------------------------------------------------- #
def mount(app: Any, prefix: str) -> None:
    """Register `{prefix}/console` and `{prefix}/console/events` on `app`.

    A factory rather than four copies: these two endpoints are identical in
    every service, and the one thing that differs — the namespace — is the
    argument. `agent/telemetry.py` already excludes `*/events` from tracing, so
    the long-lived stream does not produce a span measuring how long somebody
    left a dashboard open.
    """

    @app.get(f"{prefix}/console")
    def console_backlog(after: int = 0, limit: int = KEPT) -> dict:
        """The scrollback this process still holds, oldest first."""
        lines = snapshot(after=after, limit=max(1, min(limit, KEPT)))
        return {
            "success": True,
            "data": {
                **identity(),
                "items": lines,
                "seq": lines[-1]["seq"] if lines else after,
                "kept": KEPT,
            },
        }

    @app.get(f"{prefix}/console/events")
    async def console_events(after: int = 0):
        """The same log, live. `after` picks up where a dropped stream left off."""

        async def publisher():
            backlog, queue = attach()
            try:
                for line in backlog:
                    if line["seq"] > after:
                        yield {"data": json.dumps(line, default=str)}
                while True:
                    try:
                        line = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                    except asyncio.TimeoutError:
                        yield {
                            "event": "ping",
                            "data": json.dumps({"at": int(time.time() * 1000)}),
                        }
                        continue
                    yield {"data": json.dumps(line, default=str)}
            finally:
                detach(queue)

        return EventSourceResponse(publisher())
