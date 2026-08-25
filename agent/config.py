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


def _env_any(*names: str, default: str = "") -> str:
    """The first of `names` that is actually set, else `default`.

    Several settings here have more than one spelling for the same fact — the
    local runtime's address is `LOCAL_LLM_BASE_URL` in this project's own
    vocabulary and `OLLAMA_HOST` in the one Ollama's CLI exports, and both were
    documented as working. Only one of them was ever read, so the other was a
    silent no-op: a .env that set it looked configured and wasn't. This makes
    the precedence explicit and the fallbacks real.
    """
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


# The local runtime is spelled two ways: `ollama` is what AGENT_PROVIDER has
# always said and stays the canonical name, and `local` / `local-llm` are what
# the LLM screen calls it. Both must resolve to the same adapter *and* the same
# default model — `AGENT_PROVIDER=local` used to reach the adapter through
# providers.canonical() but miss _DEFAULT_MODEL, so it asked a local Ollama for
# claude-opus-5 and got an opaque 404.
_PROVIDER_ALIASES: dict[str, str] = {"local": "ollama", "local-llm": "ollama"}


def canonical_provider(name: str) -> str:
    """The one spelling of a provider name that the rest of the system uses."""
    key = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


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
    # OpenRouter is a router too, so its ids are `org/model` like the Hub's.
    # Same model as the groq and huggingface defaults, and for the same reason:
    # it is the open-weight one measured to get tool calls right every time.
    # Append `:free` to use the free-tier routing of a model where one exists.
    "openrouter": "openai/gpt-oss-120b",
    # Small enough to run on a laptop, and one of the better local tool-callers.
    # Overridable, unlike the rest: which local model exists is a fact about the
    # machine rather than about the vendor, so LOCAL_LLM_MODEL / OLLAMA_MODEL
    # decide it. Both were documented in .env and neither was read before.
    "ollama": _env_any("LOCAL_LLM_MODEL", "OLLAMA_MODEL", default="qwen3:4b"),
}


def _default_model_for(provider: str) -> str:
    """The model a provider runs when nothing has pinned one.

    Canonicalises first, so the `local` spelling of the local runtime gets the
    local default instead of falling through to the Anthropic one.
    """
    return _DEFAULT_MODEL.get(canonical_provider(provider), _DEFAULT_MODEL["anthropic"])


@dataclass(frozen=True)
class Settings:
    # --- The restaurant ---------------------------------------------------- #
    api_base: str = _env("FK_API_BASE", "http://localhost:8000/api/v1")
    web_base: str = _env("FK_WEB_BASE", "http://localhost:5173")
    terminal_id: str = _env("FK_TERMINAL_ID", "agent-01")

    # --- The brain --------------------------------------------------------- #
    # anthropic | gemini | openai | groq | huggingface | openrouter | ollama.
    # Strands is model-agnostic, so this is a config choice: gemini, groq and
    # huggingface have free tiers, openrouter fronts free-tier models of its
    # own, and ollama runs locally for nothing — which is what makes a no-cost
    # demo possible.
    # LLM_PROVIDER is accepted alongside AGENT_PROVIDER and wins where both are
    # set: it is the name the LLM screen and this project's docs use, and a
    # variable that reads as configuration but does nothing is worse than one
    # that does not exist. Canonicalised, so `local` and `ollama` are one thing.
    provider: str = canonical_provider(
        _env_any("LLM_PROVIDER", "AGENT_PROVIDER", default="anthropic")
    )

    # Opus 5 is the default because ordering is a multi-step tool-use loop and
    # a wrong step here spends real money. Override to claude-sonnet-5 for a
    # cheaper run. Each provider needs its own model id, so the default follows
    # the provider rather than forcing every switch to set two variables.
    model_id: str = _env(
        "AGENT_MODEL",
        _default_model_for(_env_any("LLM_PROVIDER", "AGENT_PROVIDER", default="anthropic")),
    )
    max_tokens: int = int(_env("AGENT_MAX_TOKENS", "8000"))

    # --- The local runtime ------------------------------------------------- #
    # Where a local Ollama is listening. LOCAL_LLM_BASE_URL is this project's
    # own name for it; OLLAMA_BASE_URL and OLLAMA_HOST are both accepted because
    # .env documented the first and Ollama's own CLI exports the second.
    ollama_host: str = _env_any(
        "LOCAL_LLM_BASE_URL",
        "OLLAMA_BASE_URL",
        "OLLAMA_HOST",
        default="http://localhost:11434",
    )

    # Ollama's own default context is 4096, which silently truncates this
    # system's prompt rather than raising — the agent then behaves as if it had
    # never been told half its brief, which is the hardest kind of failure to
    # read. Set explicitly so the window is a decision rather than a default.
    ollama_num_ctx: int = int(_env_any("LOCAL_LLM_NUM_CTX", "OLLAMA_NUM_CTX", default="8192"))

    # Whether a reasoning model may think before each step. qwen3 and gpt-oss
    # both think by default, and on a CPU-bound local box that deliberation is
    # most of the wall clock — so this defaults off here where it defaults on
    # upstream.
    ollama_think: bool = _env_any(
        "LOCAL_LLM_THINK", "OLLAMA_THINK", default="false"
    ).lower() in ("1", "true", "yes", "on")

    # The local answer budget, which becomes Ollama's `num_predict`. Separate
    # from AGENT_MAX_TOKENS on purpose: that is sized for a cloud reasoning
    # model at 8000, and asking a local daemon to predict more tokens than
    # ollama_num_ctx can hold invites it to shift the window mid-answer.
    ollama_max_tokens: int = int(
        _env_any("LOCAL_LLM_MAX_TOKENS", "OLLAMA_MAX_TOKENS", default="1500")
    )

    # Groq speaks the OpenAI wire format, so it is the OpenAI client pointed
    # somewhere else. Configurable only so a proxy can be slotted in.
    groq_base_url: str = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    # Hugging Face's Inference Providers router is OpenAI-compatible too, so it
    # is the same client again with a third base URL.
    hf_base_url: str = _env("HF_BASE_URL", "https://router.huggingface.co/v1")

    # OpenRouter is OpenAI-compatible as well — the same client, a fourth base
    # URL. One key reaches every vendor it fronts, which is why both A2A agents
    # can sit on it without either needing a second account.
    openrouter_base_url: str = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    # low | medium | high | xhigh | max. Ordering is agentic tool-use, which is
    # what `high` is tuned for; `low` is enough for the happy path.
    effort: str = _env("AGENT_EFFORT", "high")

    # --- The branches ------------------------------------------------------ #
    # A JSON list of the restaurant's locations, which is what turns a
    # customer's coordinates into a place a courier can collect from. The
    # restaurant's own API has no branch concept, so this lives here:
    #   [{"id":"fk-saddar","name":"Friends Kitchen Saddar",
    #     "address":"…","latitude":33.598827,"longitude":73.053810,"phone":"…"}]
    # Blank means one branch — Saddar, Rawalpindi, see agent/branches.py — which
    # is what the API already assumes.
    branches_json: str = _env("FK_BRANCHES", "")

    # --- The customer's own address ---------------------------------------- #
    # Where "deliver it to me" means, for the flows that have no browser to ask.
    # The A2A negotiation has no location UI at all — two agents talking on a
    # port — and the errand console offers this as one click instead of a
    # permission prompt, so the saved address is what most deliveries here go to.
    #
    # It is configuration rather than a literal in the code because it is one
    # person's street: change these three variables and nothing else moves.
    # The coordinates are the address's fix in Westridge, Rawalpindi — they
    # decide which branch collects and how far the rider is told to go, so keep
    # them in step with the street below if the address ever changes.
    customer_address: str = _env(
        "FK_CUSTOMER_ADDRESS",
        "Shabbir Lane, Street No 6 East, opposite Malik Car Parking, "
        "Westridge, Rawalpindi",
    )
    customer_latitude: float = float(_env("FK_CUSTOMER_LAT", "33.5875"))
    customer_longitude: float = float(_env("FK_CUSTOMER_LON", "72.9950"))

    # --- Delivery ---------------------------------------------------------- #
    # Which delivery agent to hand a paid order to:
    #   internal       — the in-house courier on 8102 (delivery_server.py)
    #   mock_foodpanda — the Foodpanda dispatcher agent on 8103, an AI that
    #                    decides and rides (foodpanda_server.py)
    #   foodpanda      — Foodpanda's real courier API, needs FOODPANDA_API_KEY
    # The in-house courier needs no credentials, so it is the default and the
    # fallback — see agent/delivery/registry.py.
    delivery_provider: str = _env("DELIVERY_PROVIDER", "internal")

    # Where the in-house delivery agent listens (delivery_server.py). Its own
    # process on its own port, like the A2A merchant on 8101 — which is what
    # makes the handover a real agent-to-agent call rather than a function one.
    delivery_base: str = _env("DELIVERY_BASE_URL", "http://localhost:8102")

    # Where the Foodpanda demonstration agent listens (foodpanda_server.py).
    # A third process on a third port, for the same reason as the second: an
    # agent reached over HTTP is one that could be run by somebody else.
    mock_foodpanda_base: str = _env("MOCK_FOODPANDA_BASE_URL", "http://localhost:8103")

    # Foodpanda's courier API. The key is FOODPANDA_API_KEY, read from the
    # environment at call time and never returned by a tool.
    foodpanda_base: str = _env("FOODPANDA_API_BASE", "")

    # Couriers are slower to answer than the restaurant is, and a dispatch that
    # times out leaves an order paid but unassigned — so this is its own number
    # rather than sharing FK_HTTP_TIMEOUT.
    delivery_timeout: float = float(_env("DELIVERY_TIMEOUT", "15"))

    # --- Limits ------------------------------------------------------------ #
    http_timeout: float = float(_env("FK_HTTP_TIMEOUT", "20"))
    max_agent_steps: int = int(_env("AGENT_MAX_STEPS", "40"))


settings = Settings()
