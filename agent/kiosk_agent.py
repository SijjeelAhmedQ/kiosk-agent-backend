"""Assembling the ordering agent.

Strands supplies the loop — model call, tool dispatch, feed the result back,
repeat — so this module only has to decide which brain, which tools, and which
brief. Swapping `mode` swaps the toolset and nothing else, which is the whole
point of keeping the API and browser tools shaped the same way.
"""

from __future__ import annotations

import os

from strands import Agent

from agent.config import settings
from agent.prompts import system_prompt
from agent.wallet import Wallet


class MissingApiKey(RuntimeError):
    """The chosen provider has no usable credentials."""


def credentials_ready() -> tuple[bool, str | None]:
    """Can the configured provider actually run? `(ready, what_is_missing)`.

    Split out from `_model()` so the health endpoint can answer the question
    without constructing a client — the control panel needs to *say* what is
    missing, not discover it by failing a run.
    """
    if settings.provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            return False, (
                "ANTHROPIC_API_KEY is not set. Put it in kiosk-agent/.env "
                "(copy .env.example) — the agent needs its own key."
            )
    elif settings.provider == "gemini":
        if not os.getenv("GOOGLE_API_KEY", "").strip():
            return False, (
                "GOOGLE_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/apikey and put it in kiosk-agent/.env."
            )
    elif settings.provider == "openai":
        if not os.getenv("OPENAI_API_KEY", "").strip():
            return False, (
                "OPENAI_API_KEY is not set. Get a key at "
                "https://platform.openai.com/api-keys and put it in kiosk-agent/.env."
            )
    elif settings.provider == "ollama":
        # Nothing to check — a local Ollama needs no key. If the daemon is down
        # the first tool call fails with a connection error, which says so.
        return True, None
    else:
        return False, (
            f"Unknown AGENT_PROVIDER {settings.provider!r} — "
            "expected anthropic, gemini, openai or ollama."
        )

    # A model id left over from another provider is the likeliest mistake when
    # switching, and it fails as an opaque 404 from the vendor. Name it instead.
    # Tuples because OpenAI's reasoning models dropped the gpt- prefix, so that
    # provider needs several — startswith takes a tuple either way.
    families: dict[str, tuple[str, ...]] = {
        "anthropic": ("claude",),
        "gemini": ("gemini",),
        "openai": ("gpt", "o3", "o4"),
    }
    expected = families.get(settings.provider)
    if expected and not settings.model_id.lower().startswith(expected):
        return False, (
            f"AGENT_MODEL is {settings.model_id!r}, which is not a {settings.provider} model. "
            f"Blank AGENT_MODEL in kiosk-agent/.env to use the default, or set a {expected[0]}-* id."
        )

    return True, None


def _model():
    """Build the model client for whichever provider is configured.

    Strands is model-agnostic, so the provider is a config choice rather than a
    code change — which is what makes a free demo (Gemini's free tier, or a
    fully local Ollama) possible without touching the tools or the prompt.
    """
    ready, problem = credentials_ready()
    if not ready:
        raise MissingApiKey(problem or "The model provider is not configured.")

    if settings.provider == "gemini":
        from strands.models.gemini import GeminiModel

        return GeminiModel(
            client_args={"api_key": os.getenv("GOOGLE_API_KEY", "").strip()},
            model_id=settings.model_id,
            params={"max_output_tokens": settings.max_tokens},
        )

    if settings.provider == "openai":
        from strands.models.openai import OpenAIModel

        # `max_completion_tokens`, not `max_tokens`: `params` is spread straight
        # into the request and the reasoning models reject the older name.
        # AGENT_EFFORT is deliberately not forwarded — OpenAI's reasoning_effort
        # has no equivalent of our xhigh/max, and sending one is a 400.
        return OpenAIModel(
            client_args={"api_key": os.getenv("OPENAI_API_KEY", "").strip()},
            model_id=settings.model_id,
            params={"max_completion_tokens": settings.max_tokens},
        )

    if settings.provider == "ollama":
        from strands.models.ollama import OllamaModel

        # Ollama's config takes max_tokens directly — it has no `params` key,
        # unlike the Anthropic and Gemini providers.
        return OllamaModel(
            host=settings.ollama_host,
            model_id=settings.model_id,
            max_tokens=settings.max_tokens,
        )

    from strands.models.anthropic import AnthropicModel

    # `params` is spread straight into the Anthropic request. Note what is NOT
    # here: temperature and top_p are rejected by Claude Opus 5, and thinking is
    # on by default on that model, so neither needs configuring.
    return AnthropicModel(
        client_args={"api_key": os.getenv("ANTHROPIC_API_KEY", "").strip()},
        model_id=settings.model_id,
        max_tokens=settings.max_tokens,
        params={"output_config": {"effort": settings.effort}},
    )


def build_agent(wallet: Wallet, mode: str = "api", callback_handler=None) -> Agent:
    """Wire up an agent for one errand.

    Args:
        wallet: The coupon and cash ceiling this run is allowed to use.
        mode: "api" to order through the REST API, "browser" to drive the
            actual website in Chromium.
        callback_handler: Strands streaming callback; None silences the
            built-in printer so the CLI can render its own output.
    """
    if mode == "browser":
        from agent.tools.browser_tools import BROWSER_TOOLS

        tools = BROWSER_TOOLS
    elif mode == "api":
        from agent.tools.api_tools import API_TOOLS

        tools = API_TOOLS
    else:
        raise ValueError(f"Unknown mode {mode!r} — expected 'api' or 'browser'.")

    return Agent(
        model=_model(),
        tools=tools,
        system_prompt=system_prompt(wallet, browser_mode=(mode == "browser")),
        name="friends-kitchen-ordering-agent",
        description="Places orders at Friends Kitchen using a coupon and a cash limit.",
        callback_handler=callback_handler,
    )
