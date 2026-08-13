"""Settings for the ordering agent.

Everything here is environment-driven so the same agent binary can be pointed at
a local Friends Kitchen, a staging one, or a colleague's machine without a code change.
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
# readable error in friends_kitchen_agent.credentials_ready(), not die on a KeyError here.
_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-5",
    # Groq serves open-weight models on its own silicon. This is the only one
    # measured to emit well-formed tool calls every time — llama-3.3-70b has a
    # bigger token budget but intermittently writes the older `<function=…>`
    # text form, which Groq rejects mid-run as a fatal 400. See .env.example.
    "groq": "openai/gpt-oss-120b",
    # Hugging Face's router fronts many vendors' hardware behind one key, so the
    # ids are the Hub's own (`org/model`). Same model as the groq default, and
    # for the same reason — it is the open-weight one that gets tool calls right.
    # Pin a specific backend by appending it: `openai/gpt-oss-120b:groq`.
    "huggingface": "openai/gpt-oss-120b",
    # Small enough to run on a laptop, and one of the better local tool-callers.
    "ollama": "qwen3:8b",
}


def _default_model_for(provider: str) -> str:
    return _DEFAULT_MODEL.get(provider, _DEFAULT_MODEL["anthropic"])


@dataclass(frozen=True)
class Settings:
    # --- The restaurant ---------------------------------------------------- #
    api_base: str = _env("FK_API_BASE", "http://localhost:8000/api/v1")
    web_base: str = _env("FK_WEB_BASE", "http://localhost:5173")
    terminal_id: str = _env("FK_TERMINAL_ID", "agent-01")

    # --- The brain --------------------------------------------------------- #
    # anthropic | gemini | openai | groq | huggingface | ollama. Strands is
    # model-agnostic, so this is a config choice: gemini, groq and huggingface
    # have free tiers and ollama runs locally for nothing, which is what makes a
    # no-cost demo possible.
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

    # Groq speaks the OpenAI wire format, so it is the OpenAI client pointed
    # somewhere else. Configurable only so a proxy can be slotted in.
    groq_base_url: str = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    # Hugging Face's Inference Providers router is OpenAI-compatible too, so it
    # is the same client again with a third base URL.
    hf_base_url: str = _env("HF_BASE_URL", "https://router.huggingface.co/v1")

    # low | medium | high | xhigh | max. Ordering is agentic tool-use, which is
    # what `high` is tuned for; `low` is enough for the happy path.
    effort: str = _env("AGENT_EFFORT", "high")

    # --- Limits ------------------------------------------------------------ #
    http_timeout: float = float(_env("FK_HTTP_TIMEOUT", "20"))
    max_agent_steps: int = int(_env("AGENT_MAX_STEPS", "40"))


settings = Settings()
