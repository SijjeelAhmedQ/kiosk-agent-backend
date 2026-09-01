"""Assembling the ordering agent.

Strands supplies the loop — model call, tool dispatch, feed the result back,
repeat — so this module only has to decide which brain, which tools, and which
brief. Swapping `mode` swaps the toolset and nothing else, which is the whole
point of keeping the API and browser tools shaped the same way.

The brain is not decided here at all any more: it comes from `agent.llm`, which
every agent in this repo reads. Change the provider or the model on the LLM
configuration screen and this agent follows on its next errand, with nothing in
this file to update.
"""

from __future__ import annotations

from strands import Agent

from agent.config import settings
from agent.llm import MissingApiKey as _MissingApiKey
from agent.llm import llm
from agent.location import UserLocation
from agent.prompts import system_prompt
from agent.reasoning import DropReasoningContent
from agent.skills import equip
from agent.wallet import Wallet


#: Raised when the selected provider has no usable credentials. Re-exported
#: rather than redefined so that `except MissingApiKey` catches the same class
#: whichever module a caller imported it from — `run.py` catches this one.
MissingApiKey = _MissingApiKey


def credentials_ready() -> tuple[bool, str | None]:
    """Can the selected provider actually run? `(ready, what_is_missing)`.

    Split out from `_model()` so the health endpoint can answer the question
    without constructing a client — the control panel needs to *say* what is
    missing, not discover it by failing a run.

    The check itself now lives in the central LLM service, because the answer
    depends on which provider is selected and that is no longer a fact about
    this module. Same signature, same callers.
    """
    return llm.credentials_ready()


def _model():
    """The model client for whatever the LLM configuration screen selected.

    Seven provider branches used to live here. They live in
    `agent/llm/providers.py` now, one adapter each, and this function is the
    one line that asks for the selected one — which is what makes changing the
    provider on one screen change it for this agent, the A2A pair and the
    Foodpanda dispatcher at once.
    """
    return llm.build_model(max_tokens=settings.max_tokens, effort=settings.effort)


def build_agent(
    wallet: Wallet,
    mode: str = "api",
    callback_handler=None,
    deliver_to: UserLocation | None = None,
) -> Agent:
    """Wire up an agent for one errand.

    Args:
        wallet: The coupon and cash ceiling this run is allowed to use.
        mode: "api" to order through the REST API, "browser" to drive the
            actual website in Chromium.
        callback_handler: Strands streaming callback; None silences the
            built-in printer so the CLI can render its own output.
        deliver_to: Where the customer is, when the errand is a delivery. None
            is a counter order and builds exactly the agent this function built
            before delivery existed — same tools, same brief. The delivery
            tools are added only when there is somewhere to deliver to, because
            a tool an errand cannot use is a tool it can waste a step on.
    """
    if mode == "browser":
        from agent.tools.browser_tools import BROWSER_TOOLS

        tools = list(BROWSER_TOOLS)
    elif mode == "api":
        from agent.tools.api_tools import API_TOOLS

        tools = list(API_TOOLS)
    else:
        raise ValueError(f"Unknown mode {mode!r} — expected 'api' or 'browser'.")

    service = "the delivery service"
    if deliver_to is not None:
        from agent.delivery import registry
        from agent.tools.delivery_tools import DELIVERY_TOOLS

        tools += DELIVERY_TOOLS
        service = registry.get().display_name

    prompt = system_prompt(
        wallet,
        browser_mode=(mode == "browser"),
        delivery_to=deliver_to.display() if deliver_to else None,
        delivery_service=service,
    )

    # Skills last, so the brief ends with the library the agent may reach for.
    # Additive and safe when `skills/` is empty: with nothing installed this
    # hands back the tools and the prompt untouched, and the errand runs
    # exactly as it did before there was a skill layer. See agent/skills/.
    tools, prompt = equip(tools, prompt, agent="ordering")

    return Agent(
        model=_model(),
        tools=tools,
        system_prompt=prompt,
        name="friends-kitchen-ordering-agent",
        description="Places orders at Friends Kitchen using a coupon and a cash limit.",
        callback_handler=callback_handler,
        hooks=[DropReasoningContent()],
    )
