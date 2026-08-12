"""Settings for the A2A flow, kept apart from the errand flow's own.

Every variable here is prefixed `A2A_`, so a .env that configures the existing
agent on 8100 keeps configuring exactly that and nothing else. Where a value
would be the same either way — where the restaurant lives, how long to wait on
it — this reads the base settings rather than inventing a second answer.

The two agents get their own provider and model. That is not decoration: they
are two conversational parties, and running both on one free-tier key means one
rate limit ends the negotiation halfway through, with the order placed and
unpaid. Pointing them at different providers is the cheap way out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from agent.config import settings as base

# `_default_model_for` is private to agent.config, and imported anyway: the
# alternative is a second copy of the per-provider model table, which would
# drift the first time one of those defaults is updated.
from agent.config import _default_model_for

load_dotenv()


def _env(name: str, default: str) -> str:
    """os.getenv, but an empty string in .env reads as "not set"."""
    value = os.getenv(name, "")
    return value.strip() or default


def _brain(role: str) -> tuple[str, str]:
    """Which provider and model one side of the conversation runs on.

    Falls back to the errand flow's provider, so a working .env needs no new
    variables to try A2A — and setting `A2A_BUYER_PROVIDER` alone still gets a
    sensible model id, because the default follows the provider.
    """
    provider = _env(f"A2A_{role}_PROVIDER", base.provider).lower()
    model = _env(f"A2A_{role}_MODEL", "")
    if not model:
        model = base.model_id if provider == base.provider else _default_model_for(provider)
    return provider, model


_BUYER_PROVIDER, _BUYER_MODEL = _brain("BUYER")
_MERCHANT_PROVIDER, _MERCHANT_MODEL = _brain("MERCHANT")


@dataclass(frozen=True)
class A2ASettings:
    # --- The service ------------------------------------------------------- #
    port: int = int(_env("A2A_PORT", "8101"))

    # Where this service can be reached, for the agent card to advertise. A card
    # that names localhost is useless to anything off this machine, so it is
    # configurable even though the demo never needs to change it.
    public_base: str = _env("A2A_PUBLIC_BASE", f"http://localhost:{_env('A2A_PORT', '8101')}")

    # --- The two brains ---------------------------------------------------- #
    buyer_provider: str = _BUYER_PROVIDER
    buyer_model: str = _BUYER_MODEL
    merchant_provider: str = _MERCHANT_PROVIDER
    merchant_model: str = _MERCHANT_MODEL
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
    # "browser" — the merchant drives the real kiosk at 5173 in Chromium, so the
    #             touchscreen visibly fills itself while the two agents talk.
    #             Slower, and needs the kiosk frontend running.
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
