"""OpenTelemetry for the four agents, in one place.

Off by default, and that is the right default: a demo run should not need a
collector, and a tracer nobody is reading is overhead with no reader. Set
`FK_OTEL` and it turns on.

**What this buys, and why it is worth wiring rather than skipping.** This system
is four processes that talk over HTTP on purpose — the buyer calls the merchant,
the merchant calls the courier, both call the restaurant — and that design has
one real cost: no single log tells you what happened. A run that ends in "the
order is paid and has no rider" is spread across three consoles and two of them
have already scrolled.

Tracing is the thing that puts it back together. Every hop carries a
`traceparent` header, every service continues the trace it was handed, and one
errand becomes one tree:

    POST /api/a2a/runs
      └─ buyer agent turn
           ├─ tool: discover_merchant  → GET  /.well-known/agent-card.json
           ├─ tool: talk_to_merchant   → POST /api/a2a/merchant/tasks
           │    └─ merchant agent turn
           │         ├─ tool: browse_menu     → GET  /api/v1/products
           │         └─ tool: take_payment    → POST /api/v1/payments
           │              └─ POST /api/foodpanda/jobs
           │                   └─ dispatcher agent turn
           │                        └─ tool: assign_rider   (the wait, measured)
           └─ tool: verify_order       → GET  /api/v1/orders/number/357

Three layers produce that, and all three are set up here:

* **Strands** emits the agent spans — the loop, each model call with its token
  counts, each tool call with its arguments and result. Those come free once a
  global tracer provider exists, which is what `StrandsTelemetry` installs.
* **httpx** is instrumented so every outbound call is a span *and* carries the
  trace context onward. This is the half that makes it one trace instead of
  four.
* **FastAPI** is instrumented so every inbound request continues the trace the
  caller sent, rather than starting a fresh one.

**The service name is set per process, not read from the environment.** The
usual `OTEL_SERVICE_NAME` would be wrong here: one `.env` configures all four
services in this repo, so a name set there would label the courier's spans as
the ordering agent's. Each process names itself instead, and the operator's
`FK_OTEL_SERVICE_PREFIX` renames the whole fleet at once if these have to sit
beside another deployment's.

**Nothing here raises.** A collector that is down, an endpoint typed wrong, an
exporter that will not import — none of that is a reason for an errand to fail.
Every problem is caught, reported through the health endpoint, and the agent
runs untraced. Observability that can take the system down is worse than no
observability.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """os.getenv, but an empty string in .env reads as "not set"."""
    return os.getenv(name, "").strip() or default


#: What `FK_OTEL` accepts. `otlp` is the one a real deployment uses; `console`
#: is for seeing the spans without running anything to receive them, which is
#: how you check the wiring before you check the collector.
MODES = ("off", "console", "otlp", "both")

#: The default OTLP endpoint. The HTTP exporter appends `/v1/traces` itself, so
#: this is the collector root — which is what Jaeger, Tempo, the OTel Collector
#: and Langfuse all document.
DEFAULT_ENDPOINT = "http://localhost:4318"

#: Set once, so a second import — or a reload in `--reload` mode — does not add
#: a second span processor and export everything twice.
_state: dict[str, Any] = {
    "enabled": False,
    "service": None,
    "mode": "off",
    "exporters": [],
    "endpoint": None,
    "problem": None,
    "done": False,
}


def mode() -> str:
    """Which exporters the operator asked for. `off` unless `FK_OTEL` says otherwise."""
    chosen = _env("FK_OTEL", "off").lower()
    return chosen if chosen in MODES else "off"


def _service_name(service: str) -> str:
    prefix = _env("FK_OTEL_SERVICE_PREFIX", "friends-kitchen")
    return f"{prefix}-{service}" if prefix else service


def setup(service: str, app: Any = None) -> dict[str, Any]:
    """Turn tracing on for this process, if it was asked for.

    Args:
        service: What this process is, unprefixed — "ordering-agent",
            "a2a-desk", "courier", "foodpanda-dispatcher". The prefix comes
            from `FK_OTEL_SERVICE_PREFIX`.
        app: The FastAPI app to instrument, if this process serves one. The CLI
            passes none, and still gets the agent and httpx spans.

    Returns:
        `describe()` — safe to hand straight to a health endpoint.
    """
    if _state["done"]:
        # A second call can still be given an app: uvicorn --reload re-imports
        # the module, and the CLI sets up before it knows it has no server.
        if app is not None and _state["enabled"]:
            _instrument_app(app)
        return describe()

    _state["done"] = True
    _state["service"] = _service_name(service)
    _state["mode"] = mode()

    if _state["mode"] == "off":
        return describe()

    # Set before StrandsTelemetry reads it — see the module docstring on why
    # this is written rather than read.
    os.environ["OTEL_SERVICE_NAME"] = _state["service"]

    try:
        from strands.telemetry import StrandsTelemetry
    except Exception as exc:  # noqa: BLE001 — a missing SDK is not a failed errand
        _state["problem"] = (
            f"OpenTelemetry could not be started ({exc}). Install it with: "
            "pip install -r requirements.txt"
        )
        return describe()

    telemetry = StrandsTelemetry()

    if _state["mode"] in ("console", "both"):
        telemetry.setup_console_exporter()
        _state["exporters"].append("console")

    if _state["mode"] in ("otlp", "both"):
        endpoint = _env("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
        # The SDK reads this itself; setting it back makes the default explicit
        # so the health endpoint reports where spans are actually going rather
        # than "wherever the SDK decided".
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        _state["endpoint"] = endpoint

        # `setup_otlp_exporter` swallows its own failures and logs them, so a
        # collector that is not there costs one warning at startup and nothing
        # afterwards — the batch processor drops spans quietly.
        telemetry.setup_otlp_exporter()
        _state["exporters"].append("otlp")

    _state["enabled"] = bool(_state["exporters"])

    # httpx before the app: this is the half that carries `traceparent` outward,
    # and without it four services produce four unrelated traces.
    _instrument_httpx()
    if app is not None:
        _instrument_app(app)

    return describe()


def _instrument_httpx() -> None:
    """Make every outbound call a span, and make it carry the trace onward.

    Instruments the client class rather than an instance, which matters here:
    `agent/a2a/merchant_client.py` builds a fresh `AsyncClient` per errand on
    purpose, and there is no long-lived object to hand to an instrumentor.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        _note(f"httpx instrumentation failed ({exc}) — client spans will be missing.")


def _instrument_app(app: Any) -> None:
    """Continue the trace an incoming request carried, instead of starting one."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            # The event streams are long-lived by design — a run can hold one
            # open for minutes — and a span that lasts as long as the SSE
            # connection measures the console's attention span, not the agent's.
            # The work itself is already traced by the spans underneath.
            excluded_urls=r".*/events,.*/health",
        )
    except Exception as exc:  # noqa: BLE001
        _note(f"FastAPI instrumentation failed ({exc}) — server spans will be missing.")


def _note(problem: str) -> None:
    """Record a partial failure without losing an earlier one."""
    existing = _state["problem"]
    _state["problem"] = f"{existing} {problem}".strip() if existing else problem


def describe() -> dict[str, Any]:
    """What the health endpoint tells the operator about tracing.

    Deliberately excludes anything credential-shaped: `OTEL_EXPORTER_OTLP_HEADERS`
    usually carries an auth token, so the answer is the endpoint and whether
    headers were set — never what is in them.
    """
    return {
        "enabled": _state["enabled"],
        "mode": _state["mode"],
        "service": _state["service"],
        "exporters": list(_state["exporters"]),
        "endpoint": _state["endpoint"],
        "authHeaders": bool(_env("OTEL_EXPORTER_OTLP_HEADERS")),
        "problem": _state["problem"],
    }
