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
# The local runtimes are spelled more than one way. `llama.cpp` is how its own
# project writes itself and `llamacpp` is what fits in an environment variable;
# `lm-studio`, `jan.ai` and the rest are the same story. Every spelling has to
# reach one adapter *and* one default model, which is why this table is here and
# not in `providers.py`: two copies was the original bug.
_PROVIDER_ALIASES: dict[str, str] = {
    "local": "ollama",
    "local-llm": "ollama",
    "llama.cpp": "llamacpp",
    "llama-cpp": "llamacpp",
    "llama_cpp": "llamacpp",
    "llamaserver": "llamacpp",
    "llama-server": "llamacpp",
    "lm-studio": "lmstudio",
    "lm_studio": "lmstudio",
    "lmstudio.ai": "lmstudio",
    "jan": "janai",
    "jan.ai": "janai",
    "jan-ai": "janai",
    "jan_ai": "janai",
    "gpt-4-all": "gpt4all",
    "gpt-4all": "gpt4all",
    "vllm-openai": "vllm",
}


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
    # The five OpenAI-compatible local servers. Every one of them serves
    # whatever GGUF (or, for vLLM, whatever safetensors) it was started with, so
    # the model id is a fact about how the server was launched rather than about
    # the vendor — exactly like Ollama's above, and overridable for the same
    # reason. Each publishes a `/models` endpoint, so the LLM screen offers what
    # the server actually has loaded and this default is only what a deployment
    # runs on before anybody looks.
    #
    # `default` is llama.cpp's own name for "the model this server loaded",
    # which is what llama-server answers with when it was started without `-a`.
    "llamacpp": _env_any("LLAMACPP_MODEL", default="default"),
    "lmstudio": _env_any("LMSTUDIO_MODEL", default="local-model"),
    "janai": _env_any("JAN_MODEL", "JANAI_MODEL", default="local-model"),
    "gpt4all": _env_any("GPT4ALL_MODEL", default="local-model"),
    "vllm": _env_any("VLLM_MODEL", default="local-model"),
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
    # anthropic | gemini | openai | groq | huggingface | openrouter | ollama |
    # llamacpp | lmstudio | janai | gpt4all | vllm.
    # Strands is model-agnostic, so this is a config choice: gemini, groq and
    # huggingface have free tiers, openrouter fronts free-tier models of its
    # own, and the five local runtimes run on this machine for nothing — which
    # is what makes a no-cost demo possible.
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

    # --- llama.cpp --------------------------------------------------------- #
    # `llama-server` is llama.cpp's own HTTP server, and it speaks the same
    # OpenAI-compatible /v1/chat/completions the hosted providers do — which is
    # why nothing in the agent layer changes to use it.
    #
    # Where it listens. 8100-8103 are the four agent services, so the runtime
    # and the agents cannot collide however they are started. LLM_BASE_URL is
    # read as a fallback because .env documented it first.
    #
    # This is the *default*. The LLM screen can override it per deployment, and
    # what it saves lands in var/llm-config.json beside the provider choice —
    # so the URL is configured in one place whichever way it is set.
    llamacpp_base_url: str = _env_any(
        "LLAMACPP_BASE_URL", "LLM_BASE_URL", default="http://localhost:8080"
    )

    # The context window `llama-server` was started with, `-c`. Not a
    # per-request setting — it is fixed when the server loads the model — so
    # this is here for two reasons only: scripts/llama-server.ps1 hands it to
    # the runtime, and the LLM screen shows it beside the answer budget so a
    # budget larger than the window it has to fit in is visible before it is
    # sent. llama-server refuses such a request outright rather than silently
    # shifting the window, which is the one thing it does better than Ollama.
    llamacpp_ctx: int = int(_env("LLAMACPP_CTX", "8192"))

    # How many model layers go on the GPU, `-ngl`. 99 means all of them. Read
    # only by the start script; the server decides nothing per request from it.
    llamacpp_ngl: int = int(_env("LLAMACPP_NGL", "99"))

    # The local answer budget, `max_tokens` on the request. Separate from
    # AGENT_MAX_TOKENS for the same reason Ollama's is: that figure is sized for
    # a cloud reasoning model at 8000, and asking for more tokens than
    # llamacpp_ctx can hold is refused.
    llamacpp_max_tokens: int = int(_env("LLAMACPP_MAX_TOKENS", "2000"))

    # llama-server takes no credential unless it was started with `--api-key`.
    # Blank is the normal case for a loopback runtime.
    llamacpp_api_key: str = _env("LLAMACPP_API_KEY", "")

    # Whether an errand may start `llama-server` itself when llama.cpp is the
    # selected provider and nothing is answering on its port. On by default,
    # because this is the one provider whose process is a window somebody can
    # close: every other runtime here is either hosted or a service that starts
    # on login. Turn it off where the runtime is started some other way — a
    # Windows service, a container, another machine — so that a silent port is
    # a fault to report rather than a second server to launch.
    llamacpp_autostart: bool = _env("LLAMACPP_AUTOSTART", "true").lower() in (
        "1", "true", "yes", "on",
    )

    # How long to wait for a just-started llama-server to finish loading the
    # GGUF, in seconds. Loading is the slow half of starting this runtime and it
    # scales with the file: the 4B is twenty-odd seconds off a warm disk, the
    # 30B mixture-of-experts a good deal more, and a cold disk is slower again.
    llamacpp_autostart_timeout: float = float(_env("LLAMACPP_AUTOSTART_TIMEOUT", "180"))

    # --- the other OpenAI-compatible local servers -------------------------- #
    # Four more ways to run a model on this machine, all of them serving the
    # OpenAI wire format, so all four are the OpenAI client with the base URL
    # moved — the same branch Groq and OpenRouter take. Only the port differs,
    # and each of these is the port its own project ships as the default.
    lmstudio_base_url: str = _env("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    jan_base_url: str = _env_any(
        "JAN_BASE_URL", "JANAI_BASE_URL", default="http://localhost:1337/v1"
    )
    gpt4all_base_url: str = _env("GPT4ALL_BASE_URL", "http://localhost:4891/v1")
    # vLLM's own default port is 8000, which on a machine also running the
    # restaurant API (FK_API_BASE, above) is taken — so this is the one of the
    # four most likely to need changing, either here or on the LLM screen.
    vllm_base_url: str = _env("VLLM_BASE_URL", "http://localhost:8000/v1")

    # A key, for the deployments that put one in front of a local server. vLLM
    # is the one that actually does this — `--api-key` on the serve command — so
    # it is optional everywhere and read at build time rather than demanded by
    # `configured()`, which would make a loopback runtime look unusable.
    lmstudio_api_key: str = _env("LMSTUDIO_API_KEY", "")
    jan_api_key: str = _env_any("JAN_API_KEY", "JANAI_API_KEY", default="")
    gpt4all_api_key: str = _env("GPT4ALL_API_KEY", "")
    vllm_api_key: str = _env("VLLM_API_KEY", "")

    # What a local server is asked for when nothing has been set on the LLM
    # screen. Shared by the five above rather than one variable each: they are
    # the same request to five implementations of the same API, and a knob per
    # runtime would be five things to keep in step for one decision.
    local_temperature: float = float(_env("LOCAL_LLM_TEMPERATURE", "0.7"))
    local_max_tokens: int = int(_env("LOCAL_LLM_ANSWER_TOKENS", "2000"))

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
