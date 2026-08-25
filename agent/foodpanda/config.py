"""Settings for the Foodpanda delivery agent.

Every variable here is prefixed `MOCK_FOODPANDA_`, which keeps it clear of the
two families it sits between:

* `FOODPANDA_API_BASE` / `FOODPANDA_API_KEY` configure `agent/delivery/foodpanda.py`
  — the provider that dispatches to Foodpanda's *real* courier API. Nothing in
  this package reads them, and this package is not that.
* `DELIVERY_*` configures which courier the ordering agent hands orders to.
  `DELIVERY_PROVIDER=mock_foodpanda` is what points it here.

The name says what it is. This is a demonstration courier: a real agent, making
real decisions, moving a job through the real lifecycle — on a clock rather than
on a motorbike. It is honest about that everywhere it reports, and it is not
wired to Foodpanda in any way.

Its *brain* is not configured here at all. Provider and model come from the
central selection in `agent/llm` — the LLM configuration screen — so this agent
switches with every other one. `MOCK_FOODPANDA_PROVIDER` and
`MOCK_FOODPANDA_MODEL` remain as the fallback for a deployment where nobody has
made that choice, which is what a stock .env is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from agent.config import _default_model_for
from agent.llm import llm

load_dotenv()


def _env(name: str, default: str) -> str:
    """os.getenv, but an empty string in .env reads as "not set"."""
    value = os.getenv(name, "")
    return value.strip() or default


def _brain() -> tuple[str, str]:
    """Which provider and model the dispatcher runs on.

    Read at call time, not at import: this service is long-lived, and a switch
    made on the LLM screen while it is running has to reach the next job it
    takes rather than the next time somebody restarts it.

    The order of precedence is the one `agent/a2a/config.py` uses, for the same
    reason — a choice made on the screen has to reach every agent or it has
    reached none:

        1. the central selection, when an operator has made one
        2. otherwise `MOCK_FOODPANDA_PROVIDER` / `MOCK_FOODPANDA_MODEL`
        3. otherwise `AGENT_PROVIDER` / `AGENT_MODEL`

    Which of the three is in force is reported by `/api/foodpanda/health`, so a
    dispatcher running on something other than what the screen says is never
    silent about it.
    """
    active = llm.active()
    if active.source == "central":
        return active.provider, active.model_id

    provider = _env("MOCK_FOODPANDA_PROVIDER", active.provider).lower()
    model = _env("MOCK_FOODPANDA_MODEL", "")
    if not model:
        model = active.model_id if provider == active.provider else _default_model_for(provider)
    return provider, model


def _pinned() -> bool:
    """Whether a `MOCK_FOODPANDA_*` pin is currently deciding the brain."""
    if llm.active().source == "central":
        return False
    return bool(_env("MOCK_FOODPANDA_PROVIDER", "") or _env("MOCK_FOODPANDA_MODEL", ""))


_PORT = _env("MOCK_FOODPANDA_PORT", "8103")


@dataclass(frozen=True)
class FoodpandaSettings:
    # --- The service ------------------------------------------------------- #
    port: int = int(_PORT)

    #: Where this agent can be reached, for its card to advertise. A card that
    #: names localhost is useless to anything off this machine, so it is
    #: configurable even though the demo never needs to change it.
    public_base: str = _env("MOCK_FOODPANDA_PUBLIC_BASE", f"http://localhost:{_PORT}")

    # --- The brain --------------------------------------------------------- #
    # Properties rather than fields: a frozen dataclass field is evaluated once
    # at import, which is exactly how a running dispatcher used to keep serving
    # the provider it started with. These ask `agent.llm` every time they are
    # read, so a switch reaches the next job without a restart.
    @property
    def provider(self) -> str:
        return _brain()[0]

    @property
    def model_id(self) -> str:
        return _brain()[1]

    @property
    def pinned(self) -> bool:
        return _pinned()

    #: Not inherited from AGENT_MAX_TOKENS, for the reason A2A does not inherit
    #: it either: on a reasoning model this is `max_completion_tokens`, which
    #: the model's own thinking is charged against before a word reaches the
    #: tools. A budget that is comfortable for the ordering agent stops this one
    #: mid-dispatch, and a job abandoned between pickup and delivery is a far
    #: worse outcome than the headroom costs.
    max_tokens: int = int(_env("MOCK_FOODPANDA_MAX_TOKENS", "32000"))

    #: low | medium | high | xhigh | max. Dispatching is a short tool-use loop
    #: with one real judgement call in it, so `medium` is enough — this agent
    #: does not need to reason as hard as the one composing an order.
    effort: str = _env("MOCK_FOODPANDA_EFFORT", "medium")

    # --- The ride ---------------------------------------------------------- #
    # How long each leg takes, in seconds. This is the one fictional thing in
    # the package, and it is fictional in exactly one direction: the legs are
    # shorter than a real ride, never skipped. A job reaches `delivered` only
    # after both have actually elapsed.
    pickup_seconds: float = float(_env("MOCK_FOODPANDA_PICKUP_SECONDS", "8"))
    transit_seconds: float = float(_env("MOCK_FOODPANDA_TRANSIT_SECONDS", "12"))

    # --- Who says when ----------------------------------------------------- #
    #: Whether the two visible steps of a delivery wait for a person.
    #:
    #: On — the default — the dispatcher takes the job on its own judgement and
    #: then stops: once at `accepted`, until somebody on the board asks for a
    #: rider, and again once the order is collected, until somebody asks for it
    #: to be taken out. That is what a dispatch desk actually looks like, and it
    #: is what makes the board a control rather than a progress bar.
    #:
    #: Off — `MOCK_FOODPANDA_MANUAL_STEPS=false` — and a job runs start to
    #: finish the moment it lands, which is what an unattended demo wants.
    #: Nothing about the decisions changes either way: the agent still judges
    #: the job, and the gates cannot move it anywhere the lifecycle forbids.
    require_operator: bool = _env("MOCK_FOODPANDA_MANUAL_STEPS", "true").lower() not in (
        "false",
        "0",
        "no",
        "off",
    )

    #: How long a gated step waits before the dispatcher gives up on it. A job
    #: nobody ever attends to must not hold a rider and an open stream for the
    #: life of the process — it fails, and says that a person never answered.
    operator_timeout_seconds: float = float(
        _env("MOCK_FOODPANDA_OPERATOR_TIMEOUT_SECONDS", "900")
    )

    #: Beyond this, the agent should refuse the job. A courier that accepts
    #: everything is not making a decision, and a delivery radius is the most
    #: ordinary reason a real one says no.
    service_radius_km: float = float(_env("MOCK_FOODPANDA_RADIUS_KM", "25"))

    # --- The bill ---------------------------------------------------------- #
    # Rupees, like everything else in this system. Quoted to the ordering agent
    # at dispatch and never changed afterwards.
    fee_base: float = float(_env("MOCK_FOODPANDA_FEE_BASE", "120"))
    fee_per_km: float = float(_env("MOCK_FOODPANDA_FEE_PER_KM", "35"))

    # --- Limits ------------------------------------------------------------ #
    #: Finished jobs are kept so the console can be refreshed and still show
    #: one. This is a demo service; it does not need a database.
    keep_jobs: int = int(_env("MOCK_FOODPANDA_KEEP_JOBS", "50"))


foodpanda_settings = FoodpandaSettings()
