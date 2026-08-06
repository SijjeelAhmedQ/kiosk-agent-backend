"""Settings for the ordering agent.

Everything here is environment-driven so the same agent binary can be pointed at
a local kiosk, a staging one, or a colleague's machine without a code change.
See .env.example for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    """os.getenv, but an empty string in .env reads as "not set"."""
    value = os.getenv(name, "")
    return value.strip() or default


# A sensible model per provider, so switching providers is one variable rather
# than two. `.get` rather than `[]`: an unknown provider should reach the
# readable error in kiosk_agent.credentials_ready(), not die on a KeyError here.
_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-5",
    # Small enough to run on a laptop, and one of the better local tool-callers.
    "ollama": "qwen3:8b",
}


def _default_model_for(provider: str) -> str:
    return _DEFAULT_MODEL.get(provider, _DEFAULT_MODEL["anthropic"])


@dataclass(frozen=True)
class Settings:
    # --- The restaurant ---------------------------------------------------- #
    api_base: str = _env("KIOSK_API_BASE", "http://localhost:8000/api/v1")
    web_base: str = _env("KIOSK_WEB_BASE", "http://localhost:5173")
    kiosk_id: str = _env("KIOSK_ID", "agent-01")

    # --- The brain --------------------------------------------------------- #
    # anthropic | gemini | openai | ollama. Strands is model-agnostic, so this
    # is a config choice: gemini has a free tier and ollama runs locally for
    # nothing, which is what makes a no-cost demo possible.
    provider: str = _env("AGENT_PROVIDER", "anthropic").lower()

    # Opus 5 is the default because ordering is a multi-step tool-use loop and
    # a wrong step here spends real money. Override to claude-sonnet-5 for a
    # cheaper run. Each provider needs its own model id, so the default follows
    # the provider rather than forcing every switch to set two variables.
    model_id: str = _env(
        "AGENT_MODEL", _default_model_for(_env("AGENT_PROVIDER", "anthropic").lower())
    )
    max_tokens: int = int(_env("AGENT_MAX_TOKENS", "8000"))

    # Where a local Ollama is listening.
    ollama_host: str = _env("OLLAMA_HOST", "http://localhost:11434")

    # low | medium | high | xhigh | max. Ordering is agentic tool-use, which is
    # what `high` is tuned for; `low` is enough for the happy path.
    effort: str = _env("AGENT_EFFORT", "high")

    # --- Limits ------------------------------------------------------------ #
    http_timeout: float = float(_env("KIOSK_HTTP_TIMEOUT", "20"))
    max_agent_steps: int = int(_env("AGENT_MAX_STEPS", "40"))


settings = Settings()
