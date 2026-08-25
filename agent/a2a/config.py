"""Settings for the A2A flow, kept apart from the errand flow's own.

Every variable here is prefixed `A2A_`, so a .env that configures the existing
agent on 8100 keeps configuring exactly that and nothing else. Where a value
would be the same either way — where the restaurant lives, how long to wait on
it — this reads the base settings rather than inventing a second answer.

Its brain is the one thing it no longer decides for itself.

Neither agent decides its own brain any more. Both follow the central selection
in `agent/llm` — the LLM configuration screen — so a provider changed in one
place changes here too, with nothing in this file to edit.

`A2A_BUYER_PROVIDER` and `A2A_MERCHANT_PROVIDER` still work, and still do the
thing they were added for: running the two sides on *different* keys, so one
free-tier rate limit cannot end a negotiation halfway through with the order
placed and unpaid. What changed is where they sit in the order of precedence:

    1. the central selection, when an operator has made one on the LLM screen
    2. otherwise `A2A_<ROLE>_PROVIDER` / `A2A_<ROLE>_MODEL`
    3. otherwise `AGENT_PROVIDER` / `AGENT_MODEL`

A choice made on the screen wins, because a switch that reached three agents
out of four would be worse than no switch at all. Until one is made, a .env
that pins these two keeps behaving exactly as it did. Which rule is in force is
reported by `/api/a2a/health` either way — a side running on something other
than what the screen says must never be silent about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# `_default_model_for` is private to agent.config, and imported anyway: the
# alternative is a second copy of the per-provider model table, which would
# drift the first time one of those defaults is updated.
from agent.config import _default_model_for
from agent.llm import llm

load_dotenv()


def _env(name: str, default: str) -> str:
    """os.getenv, but an empty string in .env reads as "not set"."""
    value = os.getenv(name, "")
    return value.strip() or default


def _brain(role: str) -> tuple[str, str]:
    """Which provider and model one side of the conversation runs on.

    Read at call time rather than at import, which is the change that makes a
    provider switch reach a service that is already running: this process holds
    no copy of the answer, so the next agent it builds gets the current one.
    """
    active = llm.active()
    if active.source == "central":
        return active.provider, active.model_id

    provider = _env(f"A2A_{role}_PROVIDER", active.provider).lower()
    model = _env(f"A2A_{role}_MODEL", "")
    if not model:
        model = active.model_id if provider == active.provider else _default_model_for(provider)
    return provider, model


def _pinned(role: str) -> bool:
    """Whether a `A2A_<ROLE>_*` pin is currently deciding this side.

    False once an operator has chosen centrally, because at that point the pin
    is present in .env but no longer in force — and the health report has to
    say which of the two the agent is actually running on.
    """
    if llm.active().source == "central":
        return False
    return bool(_env(f"A2A_{role}_PROVIDER", "") or _env(f"A2A_{role}_MODEL", ""))


@dataclass(frozen=True)
class A2ASettings:
    # --- The service ------------------------------------------------------- #
    port: int = int(_env("A2A_PORT", "8101"))

    # Where this service can be reached, for the agent card to advertise. A card
    # that names localhost is useless to anything off this machine, so it is
    # configurable even though the demo never needs to change it.
    public_base: str = _env("A2A_PUBLIC_BASE", f"http://localhost:{_env('A2A_PORT', '8101')}")

    # --- The two brains ---------------------------------------------------- #
    # Properties rather than fields, and that is the whole mechanism: a frozen
    # dataclass field is evaluated once at import, which is exactly how a
    # running service used to keep serving the provider it started with. These
    # ask `agent.llm` every time they are read, so the four existing call sites
    # — both agents and both health endpoints — follow a switch without
    # knowing one happened.
    @property
    def buyer_provider(self) -> str:
        return _brain("BUYER")[0]

    @property
    def buyer_model(self) -> str:
        return _brain("BUYER")[1]

    @property
    def buyer_pinned(self) -> bool:
        return _pinned("BUYER")

    @property
    def merchant_provider(self) -> str:
        return _brain("MERCHANT")[0]

    @property
    def merchant_model(self) -> str:
        return _brain("MERCHANT")[1]

    @property
    def merchant_pinned(self) -> bool:
        return _pinned("MERCHANT")

    # Not inherited from AGENT_MAX_TOKENS, and the gap is deliberate. On a
    # reasoning model this is `max_completion_tokens`, which the model's own
    # thinking is charged against before a single word reaches the tools — so a
    # budget that is comfortable for the errand agent stops the merchant
    # mid-sentence here, and Strands raises rather than truncating. A dead
    # conversation costs far more than the headroom does.
    max_tokens: int = int(_env("A2A_MAX_TOKENS", "32000"))

    # --- The merchant's hands ---------------------------------------------- #
    # "api"     — the merchant orders through the REST API. Fast, and what CI
    #             would use if there were any.
    # "browser" — the merchant drives the real Friends Kitchen at 5173 in Chromium, so the
    #             touchscreen visibly fills itself while the two agents talk.
    #             Slower, and needs the Friends Kitchen front end running.
    merchant_hands: str = _env("A2A_MERCHANT_HANDS", "api").lower()

    # Browser hands only. Off by default, because the entire reason to choose
    # this mode is to watch the screen fill itself — a headless run of it is
    # slower than the API and shows nobody anything.
    browser_headless: bool = _env("A2A_BROWSER_HEADLESS", "false").lower() == "true"

    # --- Limits ------------------------------------------------------------ #
    # A negotiation that has gone thirty messages without a receipt is stuck in
    # a loop, and the two agents will happily keep being polite at each other
    # until someone's token budget runs out.
    max_turns: int = int(_env("A2A_MAX_TURNS", "16"))

    # How long a buyer waits on a merchant reply before giving up, in seconds.
    reply_timeout: float = float(_env("A2A_REPLY_TIMEOUT", "180"))

    # Finished tasks are kept so the console can be refreshed and still show the
    # transcript. This is a demo service; it does not need a database.
    keep_tasks: int = int(_env("A2A_KEEP_TASKS", "50"))


a2a_settings = A2ASettings()
