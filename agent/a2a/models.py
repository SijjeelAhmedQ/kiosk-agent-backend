"""Building a model client for one named provider.

`agent.kiosk_agent._model()` does this already and cannot be reused: it reads
the process-wide `settings` singleton, so it can only ever build the *one* model
the errand flow is configured for. A2A has two agents and wants them on
different providers — that is the whole point of splitting the keys — so the
same logic is repeated here with the provider and model passed in.

The duplication is real and deliberate. The alternative is threading arguments
through `kiosk_agent`, which is a change to the flow this package promised not
to touch.
"""

from __future__ import annotations

import os

_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
}

_WHERE: dict[str, str] = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "gemini": "https://aistudio.google.com/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "groq": "https://console.groq.com/keys",
}

# A model id left over from another provider is the likeliest mistake when
# switching, and it fails as an opaque 404 from the vendor. Groq is absent on
# purpose: it hosts other vendors' open weights, so its ids share no prefix.
_FAMILIES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "gemini": ("gemini",),
    "openai": ("gpt", "o3", "o4"),
}


class MissingApiKey(RuntimeError):
    """The chosen provider has no usable credentials."""


def credentials_ready(provider: str, model_id: str, role: str) -> tuple[bool, str | None]:
    """Can this side of the conversation actually run? `(ready, what_is_missing)`.

    `role` is "buyer" or "merchant", and appears in the message because with two
    agents configured separately, "no API key" is useless without saying whose.
    """
    variable = f"A2A_{role.upper()}_PROVIDER"

    if provider == "ollama":
        # Nothing to check — a local Ollama needs no key. If the daemon is down
        # the first tool call fails with a connection error, which says so.
        return True, None

    env_name = _KEY_ENV.get(provider)
    if env_name is None:
        return False, (
            f"Unknown {variable} {provider!r} — expected anthropic, gemini, "
            "openai, groq or ollama."
        )

    if not os.getenv(env_name, "").strip():
        return False, (
            f"{env_name} is not set, and the {role} agent is configured for "
            f"{provider}. Get a key at {_WHERE[provider]} and put it in "
            "kiosk-agent-backend/.env."
        )

    expected = _FAMILIES.get(provider)
    if expected and not model_id.lower().startswith(expected):
        return False, (
            f"A2A_{role.upper()}_MODEL is {model_id!r}, which is not a {provider} "
            f"model. Blank it to use the default, or set a {expected[0]}-* id."
        )

    return True, None


def build_model(provider: str, model_id: str, max_tokens: int, effort: str = "high"):
    """A Strands model client for `provider`.

    Every branch mirrors `kiosk_agent._model()`, including the awkward bits:
    OpenAI's reasoning models reject `max_tokens` and want
    `max_completion_tokens`; Groq is the OpenAI client with the base URL moved;
    Ollama takes `max_tokens` directly rather than through `params`.
    """
    from agent.config import settings as base

    if provider == "gemini":
        from strands.models.gemini import GeminiModel

        return GeminiModel(
            client_args={"api_key": os.getenv("GOOGLE_API_KEY", "").strip()},
            model_id=model_id,
            params={"max_output_tokens": max_tokens},
        )

    if provider in ("openai", "groq"):
        from strands.models.openai import OpenAIModel

        client_args = {
            "api_key": os.getenv(_KEY_ENV[provider], "").strip(),
        }
        if provider == "groq":
            client_args["base_url"] = base.groq_base_url

        # Effort is deliberately not forwarded to either: OpenAI's
        # reasoning_effort has no equivalent of our xhigh/max, and Groq takes it
        # only on some models and 400s on the rest.
        return OpenAIModel(
            client_args=client_args,
            model_id=model_id,
            params={"max_completion_tokens": max_tokens},
        )

    if provider == "ollama":
        from strands.models.ollama import OllamaModel

        return OllamaModel(host=base.ollama_host, model_id=model_id, max_tokens=max_tokens)

    from strands.models.anthropic import AnthropicModel

    # Note what is NOT here: temperature and top_p are rejected by Claude Opus 5,
    # and thinking is on by default on that model.
    return AnthropicModel(
        client_args={"api_key": os.getenv("ANTHROPIC_API_KEY", "").strip()},
        model_id=model_id,
        max_tokens=max_tokens,
        params={"output_config": {"effort": effort}},
    )
