"""One adapter per place a model can come from.

Every provider answers the same four questions — can you run, what models do you
have, are you healthy, and build me a client — so nothing above this file has to
know whether the model is a service in Singapore or a process on this laptop.

Two of them are the ones the LLM screen puts front and centre — one cloud router
and one way to run a model on this machine:

* `OpenRouterProvider` — the cloud router this system has been using all along.
  Its model list is fetched from OpenRouter, so it is whatever OpenRouter has
  today rather than a list baked into a frontend.
* `LlamaCppProvider` — `llama-server`, llama.cpp's own HTTP server, serving one
  GGUF over an OpenAI-compatible API with llama.cpp's native sampling on top.

The other local runtimes are supported exactly as well, just not led with:

* `LMStudioProvider`, `JanProvider`, `GPT4AllProvider`, `VLLMProvider` — four
  more local servers that speak the OpenAI wire format, so four instances of one
  adapter with different ports.
* `LocalProvider` — a locally running Ollama. Its model list is whatever
  `ollama pull` has actually put on the machine, read from the daemon's own
  `/api/tags`.

None of the six local ones is a fallback for another. Which one runs is the
selection, the same as it is between the cloud providers, and every one of them
reports the models it has actually loaded rather than a list written here.

The rest — Anthropic, Gemini, OpenAI, Groq, Hugging Face — were already
supported through `AGENT_PROVIDER` and still are. They have no listing endpoint
worth calling here, so they report their configured default and say the list is
not dynamic. Removing them would have broken an existing .env.

Featured is a *presentation* fact and lives on the adapter only because the
screen has to be told it: everything unfeatured stays selectable, keeps its
settings, and runs the agents identically once picked.

Nothing in this file returns, logs or accepts an API key. `configured()` answers
whether a key is *present*, which is all a UI ever needs to know.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.config import _default_model_for, canonical_provider, settings
from agent.llm import store


class MissingApiKey(RuntimeError):
    """The chosen provider has no usable credentials."""


class ProviderUnavailable(RuntimeError):
    """The provider is configured but is not answering."""


@dataclass
class Check:
    """One line of a health report, in the words the operator reads."""

    label: str
    ok: bool
    detail: str | None = None

    def to_view(self) -> dict:
        return {"label": self.label, "ok": self.ok, "detail": self.detail}


@dataclass
class Health:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    problem: str | None = None

    def to_view(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [check.to_view() for check in self.checks],
            "problem": self.problem,
        }


@dataclass
class Setting:
    """One knob a provider exposes, described well enough to draw a field from.

    The point of describing them rather than hard-coding a form is that the LLM
    screen stays one screen: it renders whatever the selected adapter declares,
    so a provider with a server URL and a temperature gets a section and a
    provider with neither gets none — without a branch on the provider's name
    anywhere in the frontend.

    Never a credential. A key belongs in .env, out of reach of an endpoint that
    writes a file; `key_env` already tells a UI which variable to put it in.
    """

    key: str
    label: str
    #: "url", "number" or "text". What kind of input to draw and how to coerce.
    kind: str
    default: Any
    help: str = ""
    #: Folded away on the screen. True for the knobs that have a right answer
    #: almost always, so the section stays four fields rather than nine.
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    #: "int" or "float", for the number fields. Decides the coercion.
    number: str = "float"

    def to_view(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "help": self.help,
            "advanced": self.advanced,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "number": self.number,
        }

    def coerce(self, value: Any) -> Any:
        """`value` as this setting's type, or `None` if it cannot be one.

        None rather than a raise, because the two places a bad value arrives
        from are a hand-edited config file and an HTTP payload — and in both the
        right answer is to fall back to the default rather than to take a
        service down or refuse a whole save over one field.
        """
        if value is None:
            return None

        if self.kind == "number":
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if self.minimum is not None:
                number = max(number, self.minimum)
            if self.maximum is not None:
                number = min(number, self.maximum)
            return int(number) if self.number == "int" else number

        text = str(value).strip()
        if not text:
            return None
        if self.kind == "url":
            # A URL with no scheme is the commonest thing typed into a field
            # like this, and httpx rejects it as an opaque UnsupportedProtocol.
            if not text.startswith(("http://", "https://")):
                text = f"http://{text}"
            text = text.rstrip("/")
        return text


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class LLMProvider:
    """What every provider adapter can do.

    Subclasses override `list_models`, `health` and — where the wiring differs
    from the OpenAI-compatible default — `build`.
    """

    #: The name used in configuration and on the wire. Kept as it already was in
    #: `AGENT_PROVIDER`, so an existing .env keeps meaning what it meant.
    name: str = ""
    display_name: str = ""
    #: "cloud" or "local". The screen draws them differently and so should it.
    kind: str = "cloud"
    #: The environment variable holding this provider's key, or None if it needs
    #: no credential. Only ever the *name*; the value never leaves the process.
    key_env: str | None = None
    #: Where an operator goes to get one, for the message when it is missing.
    key_url: str | None = None
    #: Whether `list_models()` asks the provider or reports a configured default.
    dynamic_models: bool = False
    #: The two the LLM screen offers as cards. The others are still selectable.
    featured: bool = False
    #: One line under the name on the card.
    blurb: str = ""
    #: What to do about a prompt that will not fit, for the message somebody
    #: reads when one does not. Empty on the cloud providers: their context
    #: window is the model's and there is no local setting to raise.
    context_hint: str = ""
    #: Model ids of this vendor start with one of these, when that is a
    #: meaningful check. Routers host everybody's models, so theirs is empty.
    families: tuple[str, ...] = ()

    # -- configuration ------------------------------------------------------ #
    def default_model(self) -> str:
        return _default_model_for(self.name)

    def settings_schema(self) -> list[Setting]:
        """The knobs this provider exposes on the LLM screen. None by default.

        A cloud provider configured entirely by one key has nothing to put here
        — its address is the vendor's and its sampling is the model's business.
        The local runtimes are the opposite: where they listen is a fact about
        this machine, so it has to be settable without editing a file.
        """
        return []

    def settings(self) -> dict:
        """This provider's settings as they are actually in force.

        The adapter's defaults — which come from .env — under whatever the LLM
        screen has saved over them, coerced to the declared type. This is what
        `build()`, `health()` and `list_models()` all read, so a URL changed on
        the screen moves every one of them at once and is hard-coded in none.
        """
        saved = store.settings_for(self.name)
        resolved: dict = {}
        for setting in self.settings_schema():
            value = setting.coerce(saved.get(setting.key))
            resolved[setting.key] = setting.default if value is None else value
        return resolved

    def setting(self, key: str) -> Any:
        """One resolved setting, or None if this provider has no such knob."""
        return self.settings().get(key)

    def has_key(self) -> bool:
        return not self.key_env or bool(os.getenv(self.key_env, "").strip())

    def _key(self) -> str:
        return os.getenv(self.key_env or "", "").strip()

    def configured(self, model_id: str | None = None) -> tuple[bool, str | None]:
        """Can this provider run? `(ready, what_is_missing)`.

        Deliberately answerable without building a client or touching the
        network, so a health endpoint can *say* what is missing rather than
        discover it by failing a run.
        """
        if self.key_env and not self.has_key():
            return False, (
                f"{self.key_env} is not set, and the agents are configured for "
                f"{self.display_name}. Get a key at {self.key_url} and put it in "
                "friends-kitchen-agent-backend/.env."
            )

        # A model id left over from another provider is the likeliest mistake
        # when switching, and it fails as an opaque 404 from the vendor.
        model_id = (model_id or "").strip()
        if model_id and self.families and not model_id.lower().startswith(self.families):
            return False, (
                f"{model_id!r} is not a {self.display_name} model. Pick one from the "
                f"model list, or use the default {self.default_model()!r}."
            )
        return True, None

    # -- what it can run ---------------------------------------------------- #
    def list_models(self) -> list[dict]:
        """Every model this provider can serve, best-first.

        The base answer is the configured default and nothing else, which is the
        honest one for a vendor with no catalogue endpoint: a hardcoded list
        would be wrong the week after it was written.
        """
        return [{"id": self.default_model(), "label": self.default_model()}]

    def health(self, model_id: str | None = None) -> Health:
        ready, problem = self.configured(model_id)
        return Health(
            ok=ready,
            checks=[Check("Credentials configured", ready, problem)],
            problem=problem,
        )

    # -- the client --------------------------------------------------------- #
    def build(self, model_id: str, max_tokens: int, effort: str):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# OpenAI-compatible providers — OpenAI itself, and the three routers
# --------------------------------------------------------------------------- #
class _OpenAICompatible(LLMProvider):
    """The OpenAI client with the base URL moved.

    OpenAI, Groq, Hugging Face and OpenRouter all speak the same wire format, so
    this is one branch rather than four. It is the branch
    `agent/friends_kitchen_agent.py` and `agent/a2a/models.py` each used to
    carry a copy of.
    """

    base_url: str = ""

    def build(self, model_id: str, max_tokens: int, effort: str):
        from strands.models.openai import OpenAIModel

        client_args: dict = {"api_key": self._key()}
        if self.base_url:
            client_args["base_url"] = self.base_url

        # `max_completion_tokens`, not `max_tokens`: `params` is spread straight
        # into the request and the reasoning models reject the older name.
        #
        # `effort` is deliberately not forwarded to any of these. OpenAI's
        # reasoning_effort has no equivalent of our xhigh/max, Groq takes it on
        # some models and 400s on the rest, and on the Hugging Face and
        # OpenRouter routers whether it is accepted depends on the backend they
        # picked — so sending it turns a working config into an occasional 400.
        return OpenAIModel(
            client_args=client_args,
            model_id=model_id,
            params={"max_completion_tokens": max_tokens},
        )


class OpenAIProvider(_OpenAICompatible):
    name = "openai"
    display_name = "OpenAI"
    key_env = "OPENAI_API_KEY"
    key_url = "https://platform.openai.com/api-keys"
    blurb = "GPT models, direct"
    families = ("gpt", "o3", "o4")


class GroqProvider(_OpenAICompatible):
    name = "groq"
    display_name = "Groq"
    key_env = "GROQ_API_KEY"
    key_url = "https://console.groq.com/keys"
    blurb = "Open weights on Groq silicon"

    @property
    def base_url(self) -> str:  # type: ignore[override]
        return settings.groq_base_url


class HuggingFaceProvider(_OpenAICompatible):
    name = "huggingface"
    display_name = "Hugging Face"
    key_env = "HF_TOKEN"
    key_url = "https://huggingface.co/settings/tokens"
    blurb = "The Hub's inference router"

    @property
    def base_url(self) -> str:  # type: ignore[override]
        return settings.hf_base_url


class OpenRouterProvider(_OpenAICompatible):
    """The cloud router this system has been running on."""

    name = "openrouter"
    display_name = "OpenRouter"
    kind = "cloud"
    key_env = "OPENROUTER_API_KEY"
    key_url = "https://openrouter.ai/keys"
    dynamic_models = True
    featured = True
    blurb = "Cloud models from every vendor, behind one key"

    #: The catalogue is public and it is long, so it is fetched once and held.
    #: Long enough that opening the screen twice is one call, short enough that
    #: a model released this morning shows up this afternoon.
    _CACHE_SECONDS = 600
    _cache: tuple[float, list[dict]] | None = None

    @property
    def base_url(self) -> str:  # type: ignore[override]
        return settings.openrouter_base_url

    def list_models(self) -> list[dict]:
        cached = type(self)._cache
        if cached and (time.monotonic() - cached[0]) < self._CACHE_SECONDS:
            return cached[1]

        headers = {}
        # The catalogue is readable without a key. The key is sent when there is
        # one only so a key scoped to a subset sees its own subset.
        if self.has_key():
            headers["Authorization"] = f"Bearer {self._key()}"

        try:
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/models",
                headers=headers,
                timeout=settings.http_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(
                "OpenRouter's model list is not reachable. Check the machine's "
                "internet connection and try again."
            ) from exc

        models: list[dict] = []
        for item in payload.get("data") or []:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            pricing = item.get("pricing") or {}
            # OpenRouter prices per token as decimal strings; zero on both sides
            # is what makes a model free, and free is worth surfacing.
            free = str(pricing.get("prompt", "")) in ("0", "0.0") and str(
                pricing.get("completion", "")
            ) in ("0", "0.0")
            models.append(
                {
                    "id": model_id,
                    "label": str(item.get("name") or model_id),
                    "contextLength": item.get("context_length"),
                    "free": free,
                }
            )

        models.sort(key=lambda model: model["id"])
        type(self)._cache = (time.monotonic(), models)
        return models

    def health(self, model_id: str | None = None) -> Health:
        checks = [
            Check(
                "API key configured",
                self.has_key(),
                None if self.has_key() else f"{self.key_env} is not set in .env.",
            )
        ]
        if not self.has_key():
            return Health(
                ok=False,
                checks=checks,
                problem=(
                    "Unable to connect to OpenRouter. Please check the configuration — "
                    f"{self.key_env} is not set."
                ),
            )

        try:
            models = self.list_models()
        except ProviderUnavailable as exc:
            checks.append(Check("Connection successful", False, str(exc)))
            return Health(ok=False, checks=checks, problem=str(exc))

        checks.append(Check("Connection successful", True, f"{len(models)} models offered."))

        wanted = (model_id or "").strip()
        if wanted:
            known = any(model["id"] == wanted for model in models)
            checks.append(
                Check(
                    "Model available",
                    known,
                    None if known else f"OpenRouter does not list {wanted!r}.",
                )
            )
            if not known:
                return Health(
                    ok=False,
                    checks=checks,
                    problem=f"OpenRouter does not offer {wanted!r}. Pick another model.",
                )

        return Health(ok=True, checks=checks)


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    display_name = "Anthropic"
    key_env = "ANTHROPIC_API_KEY"
    key_url = "https://console.anthropic.com/settings/keys"
    blurb = "Claude, direct"
    families = ("claude",)

    def build(self, model_id: str, max_tokens: int, effort: str):
        from strands.models.anthropic import AnthropicModel

        # `params` is spread straight into the Anthropic request. Note what is
        # NOT here: temperature and top_p are rejected by Claude Opus 5, and
        # thinking is on by default on that model.
        return AnthropicModel(
            client_args={"api_key": self._key()},
            model_id=model_id,
            max_tokens=max_tokens,
            params={"output_config": {"effort": effort}},
        )


class GeminiProvider(LLMProvider):
    name = "gemini"
    display_name = "Google Gemini"
    key_env = "GOOGLE_API_KEY"
    key_url = "https://aistudio.google.com/apikey"
    blurb = "Gemini, with a free tier"
    families = ("gemini",)

    def build(self, model_id: str, max_tokens: int, effort: str):
        from strands.models.gemini import GeminiModel

        return GeminiModel(
            client_args={"api_key": self._key()},
            model_id=model_id,
            params={"max_output_tokens": max_tokens},
        )


# --------------------------------------------------------------------------- #
# The local runtime
# --------------------------------------------------------------------------- #
class LocalProvider(LLMProvider):
    """A locally running Ollama — the open models on this machine.

    Ollama was already this project's local runtime (`AGENT_PROVIDER=ollama`,
    `OLLAMA_HOST`), so this adapter talks to that rather than introducing a
    second one. Its model list is not a list at all until it is asked for: it is
    whatever `ollama pull` has actually put on the disk, which is the only
    honest answer and the only one that cannot go stale.
    """

    name = "ollama"
    display_name = "Local Open LLM"
    kind = "local"
    key_env = None
    dynamic_models = True
    blurb = "Open models running on this machine, through Ollama"
    context_hint = (
        "Raise LOCAL_LLM_NUM_CTX, or pick a model with a larger window."
    )

    #: Shorter than the router's: pulling a model is something an operator does
    #: *while* this screen is open, and the list should catch up quickly.
    _CACHE_SECONDS = 20
    _cache: tuple[float, list[dict]] | None = None

    #: How to get it going, for the message somebody reads when it is not.
    start_hint = (
        "Start Ollama with `ollama serve` and pull a model with `ollama pull llama3.1`."
    )

    @property
    def host(self) -> str:
        """Where the runtime is listening. Ollama's own variable, so an existing
        `OLLAMA_HOST` keeps meaning what it meant."""
        return settings.ollama_host.rstrip("/")

    def configured(self, model_id: str | None = None) -> tuple[bool, str | None]:
        """A local runtime needs no key, so there is nothing to check here.

        Whether it is *running* is a health question, not a credentials one —
        and answering it costs a network call, which `configured()` promises
        not to make.
        """
        return True, None

    def _get(self, path: str) -> dict:
        try:
            response = httpx.get(f"{self.host}{path}", timeout=settings.http_timeout)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(
                "Local LLM is not available. Please make sure the local runtime is "
                f"running at {self.host}. {self.start_hint}"
            ) from exc

    def list_models(self) -> list[dict]:
        cached = type(self)._cache
        if cached and (time.monotonic() - cached[0]) < self._CACHE_SECONDS:
            return cached[1]

        payload = self._get("/api/tags")
        models: list[dict] = []
        for item in payload.get("models") or []:
            model_id = str(item.get("name") or item.get("model") or "").strip()
            if not model_id:
                continue
            details = item.get("details") or {}
            models.append(
                {
                    "id": model_id,
                    "label": model_id,
                    "sizeBytes": item.get("size"),
                    "parameterSize": details.get("parameter_size"),
                    "family": details.get("family"),
                }
            )

        models.sort(key=lambda model: model["id"])
        type(self)._cache = (time.monotonic(), models)
        return models

    def health(self, model_id: str | None = None) -> Health:
        try:
            models = self.list_models()
        except ProviderUnavailable as exc:
            return Health(
                ok=False,
                checks=[Check("Runtime available", False, str(exc))],
                problem=str(exc),
            )

        checks = [Check("Runtime available", True, f"Ollama is answering at {self.host}.")]

        if not models:
            problem = (
                "The local runtime is running but has no models installed. "
                "Pull one with `ollama pull llama3.1`."
            )
            checks.append(Check("Model available", False, problem))
            return Health(ok=False, checks=checks, problem=problem)

        checks.append(
            Check("Connection successful", True, f"{len(models)} model(s) installed.")
        )

        wanted = (model_id or "").strip()
        if wanted:
            # Ollama tags are `name:tag`, and a bare `llama3.1` means
            # `llama3.1:latest` — so a bare name matches the tag it is short for.
            installed = any(
                model["id"] == wanted or model["id"].split(":", 1)[0] == wanted
                for model in models
            )
            checks.append(
                Check(
                    "Model available",
                    installed,
                    None
                    if installed
                    else f"{wanted!r} is not installed. Pull it with `ollama pull {wanted}`.",
                )
            )
            if not installed:
                return Health(
                    ok=False,
                    checks=checks,
                    problem=(
                        f"The local runtime does not have {wanted!r}. "
                        f"Pull it with `ollama pull {wanted}`, or pick an installed model."
                    ),
                )

        return Health(ok=True, checks=checks)

    def build(self, model_id: str, max_tokens: int, effort: str):
        from strands.models.ollama import OllamaModel

        # Ollama's config takes max_tokens directly — it has no `params` key,
        # unlike the Anthropic and Gemini providers.
        #
        # The three settings below were all documented in .env and none of them
        # reached the daemon before:
        #
        # * `num_ctx` goes in `options`, which is spread into the request's own
        #   options. Without it the daemon uses its 4096 default and truncates
        #   this system's prompt *silently* — no error, just an agent that was
        #   never told half its brief.
        # * `think` is a top-level request field, so it goes in
        #   `additional_args`. qwen3 and gpt-oss think by default, and on a
        #   CPU-bound box that deliberation is most of the wall clock.
        # * the budget is capped to the local one rather than taking the caller's
        #   figure. `max_tokens` becomes `num_predict`, and the errand budget is
        #   sized for a cloud reasoning model — asking for more than `num_ctx`
        #   can hold makes the daemon shift the window mid-answer.
        return OllamaModel(
            host=self.host,
            model_id=model_id,
            max_tokens=min(max_tokens, settings.ollama_max_tokens),
            options={"num_ctx": settings.ollama_num_ctx},
            additional_args={"think": settings.ollama_think},
        )


# --------------------------------------------------------------------------- #
# The model servers that run on this machine
# --------------------------------------------------------------------------- #
class _LocalServer(LLMProvider):
    """A model server listening on this machine, whichever project wrote it.

    llama.cpp, LM Studio, Jan, GPT4All and vLLM are five programs doing the same
    job: load a model file, listen on a port, answer an HTTP request. So the
    parts that are the same live here — no credential, an address that is a fact
    about *this machine* rather than about a vendor, a model list read from the
    server instead of hardcoded, and a failure that says where it looked.

    What each subclass still owns is its port, its start hint, and how a client
    is built. Four of the five speak the OpenAI wire format and share
    `_LocalOpenAIServer` below; llama.cpp gets its own adapter, because Strands
    ships a client that speaks its native extensions.
    """

    kind = "local"
    key_env = None
    dynamic_models = True
    context_hint = (
        "Restart the server with a larger context window, or pick a model with "
        "a smaller one."
    )

    #: How to get it going, for the message somebody reads when it is not.
    start_hint: str = ""

    #: Short, because starting a server is something an operator does *while*
    #: this screen is open and the list should catch up quickly. Keyed by base
    #: URL as well as by time, so changing the address does not read back a list
    #: fetched from the previous one.
    _CACHE_SECONDS = 15
    _cache: tuple[float, str, list[dict]] | None = None

    # -- configuration ------------------------------------------------------ #
    @property
    def default_base_url(self) -> str:
        """Where this runtime listens when nothing has been configured.

        A property rather than a class attribute because it comes from .env,
        which `agent.config` reads at import — a class attribute here would be
        bound before the settings object exists.
        """
        return ""

    @property
    def default_answer_tokens(self) -> int:
        """The answer budget this runtime runs on before anybody sets one.

        Shared across the four OpenAI-compatible servers, because it is one
        decision about local generation rather than four about four vendors.
        llama.cpp overrides it: it has had `LLAMACPP_MAX_TOKENS` in .env since
        before this screen could set anything, and a variable that reads as
        configuration but is ignored is worse than one that does not exist.
        """
        return settings.local_max_tokens

    def model_hint(self, model_id: str) -> str:
        """What to do about a model id this server has not loaded."""
        return f"Load {model_id!r} in {self.display_name}, or pick one from the model list."

    def settings_schema(self) -> list[Setting]:
        return [
            Setting(
                key="baseUrl",
                label="Server URL",
                kind="url",
                default=self.default_base_url,
                help=f"Where {self.display_name} is listening on this machine.",
            ),
            Setting(
                key="temperature",
                label="Temperature",
                kind="number",
                default=settings.local_temperature,
                help="Higher is more varied. Ordering is tool use, so lower is steadier.",
                minimum=0.0,
                maximum=2.0,
                step=0.05,
            ),
            Setting(
                key="maxTokens",
                label="Max answer tokens",
                kind="number",
                number="int",
                default=self.default_answer_tokens,
                help=(
                    "A ceiling on one reply. Capped against the errand budget, so "
                    "whichever is smaller wins."
                ),
                minimum=1,
                maximum=131072,
                step=64,
            ),
            Setting(
                key="timeout",
                label="Request timeout (seconds)",
                kind="number",
                default=300.0,
                advanced=True,
                help=(
                    "A local model is slower than a hosted one, and the first call "
                    "after a load is the slowest of all. Generous on purpose."
                ),
                minimum=5,
                maximum=3600,
                step=5,
            ),
        ]

    @property
    def host(self) -> str:
        """The address in force — what the screen saved, else what .env says.

        Read through `settings()` every time rather than held, which is what
        makes one URL the only URL: `build`, `health` and `list_models` all ask
        this, and none of them carries a copy of the answer.
        """
        return str(self.setting("baseUrl") or self.default_base_url).rstrip("/")

    def configured(self, model_id: str | None = None) -> tuple[bool, str | None]:
        """A local runtime needs no key, so there is nothing to check here.

        Whether it is *running* is a health question, not a credentials one —
        and answering it costs a network call, which `configured()` promises
        not to make.
        """
        return True, None

    # -- talking to it ------------------------------------------------------ #
    def unreachable(self) -> str:
        """The sentence somebody reads when nothing answered.

        It names the address it tried, because that is the first thing to check
        — on a machine that can run five of these, four are on the wrong port
        for any given one.
        """
        return f"{self.display_name} server is not reachable at {self.host}. {self.start_hint}".strip()

    def _timeout(self) -> float:
        """What a *probe* waits — not what a generation waits, which is longer.

        A model list that has not answered in a few seconds is a server that is
        not there. Waiting the generation timeout to say so would hang the LLM
        screen for minutes on the commonest failure it has.
        """
        return settings.http_timeout

    def _get(self, path: str) -> dict:
        """One GET against the runtime, with every failure said the same way.

        Connection refused, a timeout, a 500 and a body that is not JSON all
        mean the same thing to an operator — nothing is answering at that
        address — so they arrive as one sentence rather than four.
        """
        try:
            response = httpx.get(f"{self.host}{path}", timeout=self._timeout())
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(self.unreachable()) from exc

    def _models_path(self) -> str:
        """Where this runtime publishes its catalogue, relative to `host`."""
        return "/models"

    def _read_models(self, payload: dict) -> list[dict]:
        """An OpenAI-shaped `{"data": [{"id": ...}]}` as this screen's rows.

        Overridden by llama.cpp, which puts real metadata on each entry.
        """
        models: list[dict] = []
        for item in payload.get("data") or []:
            model_id = str(item.get("id") or "").strip()
            if model_id:
                models.append({"id": model_id, "label": model_id})
        return models

    def list_models(self) -> list[dict]:
        """What the server has loaded, asked of the server.

        Never a hardcoded list. Which model a local runtime can serve is a fact
        about what was loaded into it a minute ago, and the only thing that
        knows it is the runtime.
        """
        host = self.host
        cached = type(self)._cache
        if cached and cached[1] == host and (time.monotonic() - cached[0]) < self._CACHE_SECONDS:
            return cached[2]

        models = self._read_models(self._get(self._models_path()))
        models.sort(key=lambda model: model["id"])
        type(self)._cache = (time.monotonic(), host, models)
        return models

    def health(self, model_id: str | None = None) -> Health:
        try:
            models = self.list_models()
        except ProviderUnavailable as exc:
            return Health(
                ok=False,
                checks=[Check("Server reachable", False, str(exc))],
                problem=str(exc),
            )

        checks = [
            Check("Server reachable", True, f"{self.display_name} is answering at {self.host}.")
        ]

        if not models:
            problem = (
                f"{self.display_name} is running at {self.host} but has no model loaded. "
                f"{self.start_hint}"
            ).strip()
            checks.append(Check("Model loaded", False, problem))
            return Health(ok=False, checks=checks, problem=problem)

        checks.append(Check("Connection successful", True, f"{len(models)} model(s) loaded."))

        wanted = (model_id or "").strip()
        if wanted:
            loaded = any(model["id"] == wanted for model in models)
            checks.append(
                Check(
                    "Model available",
                    loaded,
                    None if loaded else f"{wanted!r} is not loaded. {self.model_hint(wanted)}",
                )
            )
            if not loaded:
                problem = (
                    f"{self.display_name} does not have {wanted!r} loaded. "
                    f"{self.model_hint(wanted)}"
                )
                return Health(ok=False, checks=checks, problem=problem)

        return Health(ok=True, checks=checks)


class _LocalOpenAIServer(_LocalServer):
    """A local server that speaks the OpenAI wire format.

    LM Studio, Jan, GPT4All and vLLM all do, so all four are the same OpenAI
    client with the base URL moved — the branch `_OpenAICompatible` already is
    for the hosted ones. What differs from that branch is small and matters:

    * `max_tokens`, not `max_completion_tokens`. The newer name is what the
      hosted reasoning models require and what several of these four reject.
    * a temperature is sent, because on a local model it is much of what decides
      whether a tool call comes out well formed, and there is no vendor default
      tuned for this behind it.
    * the API key is optional. These servers check one only when they were
      started with one, and the OpenAI client refuses to be built without a
      string — so a placeholder goes in when there is nothing to send.
    """

    #: The .env variable holding a key, for a deployment that puts one in front
    #: of a local server. Deliberately not `key_env`: that would make
    #: `configured()` demand it, and a loopback runtime almost never has one.
    optional_key_env: str = ""

    def _optional_key(self) -> str:
        return os.getenv(self.optional_key_env or "", "").strip()

    def build(self, model_id: str, max_tokens: int, effort: str):
        from strands.models.openai import OpenAIModel

        values = self.settings()
        return OpenAIModel(
            client_args={
                "base_url": values["baseUrl"],
                # Never empty: the OpenAI client validates this argument before
                # any request is made, and these servers ignore what they were
                # not started to check.
                "api_key": self._optional_key() or "local",
                "timeout": float(values["timeout"]),
            },
            model_id=model_id,
            params={
                "max_tokens": min(max_tokens, int(values["maxTokens"])),
                "temperature": float(values["temperature"]),
            },
        )


class LlamaCppProvider(_LocalServer):
    """`llama-server`, llama.cpp's own HTTP server.

    It loads one GGUF and serves it over an OpenAI-compatible
    `/v1/chat/completions` — the same `tools` in and `tool_calls` out as the
    hosted providers — which is the whole reason the ordering agent, the A2A
    pair and the Foodpanda dispatcher need no change to run on it.

    Strands ships a client for it rather than only the OpenAI one, and that is
    what this builds: llama.cpp's own sampling parameters (`top_k`,
    `repeat_penalty`, grammars) travel at the top level of the request body,
    where the OpenAI client would drop them, and the native client is the thing
    that knows to put them there.

    Which GGUF is loaded is not this adapter's business, and deliberately so:
    the file, the context window and the GPU offload are decided by the command
    that starts the server — see scripts/llama-server.ps1 — and read back from
    `/v1/models` and `/props` rather than duplicated here.
    """

    name = "llamacpp"
    display_name = "llama.cpp"
    featured = True
    blurb = "A GGUF on this machine, through llama-server"
    context_hint = (
        "Raise LLAMACPP_CTX and restart llama-server — it refuses a request that "
        "will not fit rather than truncating one, so the limit it names is real."
    )
    start_hint = (
        "Start it with scripts/llama-server.ps1, or "
        "`llama-server -m model.gguf --host 127.0.0.1 --port 8080 --jinja`."
    )

    @property
    def default_base_url(self) -> str:
        return settings.llamacpp_base_url

    @property
    def default_answer_tokens(self) -> int:
        return settings.llamacpp_max_tokens

    def model_hint(self, model_id: str) -> str:
        return (
            "Restart llama-server with that GGUF, or pick the model it has loaded "
            "from the list."
        )

    @property
    def host(self) -> str:
        """The server root — never the `/v1` under it.

        Both the Strands client and the paths below append `/v1` themselves, so
        a URL pasted with it already on the end would reach `/v1/v1/models`.
        That is the obvious mistake to make when four of the five local runtimes
        on this screen *do* want it, so it is corrected rather than diagnosed.
        """
        base = str(self.setting("baseUrl") or self.default_base_url).rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    def settings_schema(self) -> list[Setting]:
        """The four every local server has, plus llama.cpp's own sampling.

        The extra three are advanced, and folded away on the screen, because
        their defaults are right for tool use and the one field an operator
        actually opens this section for is the URL.
        """
        return [
            *super().settings_schema(),
            Setting(
                key="topP",
                label="Top P",
                kind="number",
                default=0.95,
                advanced=True,
                help="Nucleus sampling. 1.0 disables it.",
                minimum=0.0,
                maximum=1.0,
                step=0.01,
            ),
            Setting(
                key="topK",
                label="Top K",
                kind="number",
                number="int",
                default=40,
                advanced=True,
                help="Consider only this many tokens at each step. 0 disables it.",
                minimum=0,
                maximum=1000,
                step=1,
            ),
            Setting(
                key="repeatPenalty",
                label="Repeat penalty",
                kind="number",
                default=1.1,
                advanced=True,
                help="Above 1.0 discourages the model from repeating itself.",
                minimum=0.5,
                maximum=2.0,
                step=0.01,
            ),
        ]

    # -- what it has loaded -------------------------------------------------- #
    def _models_path(self) -> str:
        return "/v1/models"

    def props(self) -> dict:
        """`/props` — what the running server says about itself, or `{}`.

        Best-effort on purpose: an older build of llama-server does not have the
        endpoint at all, and nothing here is worth failing a health report over.
        What it is asked for is `chat_template_caps` — see `health` below.
        """
        try:
            payload = self._get("/props")
        except ProviderUnavailable:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_models(self, payload: dict) -> list[dict]:
        """llama.cpp's `/v1/models`, which carries the GGUF's own metadata.

        Recent builds put a `meta` object on each entry — the parameter count,
        the file size, the quantisation, and both context windows. Most of that
        is what this screen's model list already draws for Ollama, so it is
        mapped onto the same keys and drawn by the same code.

        `n_ctx` in preference to `n_ctx_train`: the first is the window the
        server was *started* with and the second is what the weights were
        trained for. A prompt has to fit in the first one.
        """
        models: list[dict] = []
        for item in payload.get("data") or []:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            meta = item.get("meta") or {}
            models.append(
                {
                    "id": model_id,
                    "label": model_id,
                    "sizeBytes": meta.get("size"),
                    "parameterSize": _billions(meta.get("n_params")),
                    "contextLength": meta.get("n_ctx") or meta.get("n_ctx_train"),
                    "quantization": str(meta.get("ftype") or "").strip() or None,
                }
            )
        return models

    def health(self, model_id: str | None = None) -> Health:
        """The shared check, with llama.cpp's own `/health` in front of it.

        Worth the extra call for one state it alone reports: a server that is up
        and *still loading the model* answers 503 there, which is a wait rather
        than a misconfiguration — and telling somebody to check their port while
        a 4 GB file is being read off disk sends them to fix nothing.
        """
        try:
            httpx.get(f"{self.host}/health", timeout=self._timeout()).raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                problem = (
                    f"{self.display_name} is starting up at {self.host} — it is still "
                    "loading the model. Try again in a moment."
                )
                return Health(
                    ok=False,
                    checks=[Check("Server reachable", False, problem)],
                    problem=problem,
                )
            # Any other status is a server answering something unexpected, and
            # the model list below is the better diagnosis. Fall through to it.
        except httpx.HTTPError:
            problem = self.unreachable()
            return Health(
                ok=False,
                checks=[Check("Server reachable", False, problem)],
                problem=problem,
            )

        report = super().health(model_id)
        if not report.ok:
            return report

        # Every agent in this system is a tool-use loop, so a template that
        # cannot emit a tool call is not a limitation here — it is a server that
        # will fail every errand halfway through. llama-server publishes whether
        # its template can, and the commonest reason it cannot is that it was
        # started without `--jinja`: without that flag the GGUF's own template
        # is ignored in favour of a built-in one that has no tool syntax. Worth
        # catching here rather than twenty tool calls into an order.
        caps = self.props().get("chat_template_caps")
        if isinstance(caps, dict):
            tools = bool(caps.get("supports_tool_calls") or caps.get("supports_tools"))
            detail = (
                "The loaded chat template emits tool calls."
                if tools
                else (
                    "The loaded chat template cannot emit tool calls, and every agent "
                    "here needs them. Restart llama-server with --jinja so the GGUF's "
                    "own template is used, or load a model whose template supports "
                    "tools."
                )
            )
            report.checks.append(Check("Tool calling supported", tools, detail))
            if not tools:
                report.ok = False
                report.problem = detail

        return report

    # -- the client ---------------------------------------------------------- #
    def build(self, model_id: str, max_tokens: int, effort: str):
        """Strands' own llama.cpp client, pointed at the configured server.

        `effort` is not forwarded: llama.cpp has no reasoning-effort control,
        and how much a model deliberates is decided by the GGUF that was loaded
        rather than by the request.

        The answer budget is the smaller of the errand's and this provider's,
        for the reason the Ollama adapter caps its own: the errand figure is
        sized for a cloud reasoning model, and asking for more tokens than the
        server's context window can hold is refused outright.
        """
        from strands.models.llamacpp import LlamaCppModel

        values = self.settings()
        model = LlamaCppModel(
            base_url=self.host,
            # A local generation is slow, and the first one after a load is the
            # slowest of all. The probe timeout would abandon a perfectly
            # healthy first call, so this is the long one.
            timeout=float(values["timeout"]),
            model_id=model_id,
            params={
                "max_tokens": min(max_tokens, int(values["maxTokens"])),
                "temperature": float(values["temperature"]),
                "top_p": float(values["topP"]),
                "top_k": int(values["topK"]),
                "repeat_penalty": float(values["repeatPenalty"]),
            },
        )

        # `--api-key` is the only credential llama-server has, and only a
        # runtime that is not on loopback needs one. Set on the client rather
        # than declared as `key_env`, which would make a server needing no key
        # report itself unconfigured.
        if settings.llamacpp_api_key:
            model.client.headers["Authorization"] = f"Bearer {settings.llamacpp_api_key}"

        return model


class LMStudioProvider(_LocalOpenAIServer):
    name = "lmstudio"
    display_name = "LM Studio"
    blurb = "GGUF models through LM Studio's own server"
    optional_key_env = "LMSTUDIO_API_KEY"
    start_hint = "Load a model in LM Studio and start its server on the Developer tab."

    @property
    def default_base_url(self) -> str:
        return settings.lmstudio_base_url

    def model_hint(self, model_id: str) -> str:
        return "Load it in LM Studio, or pick one it already has from the list."


class JanProvider(_LocalOpenAIServer):
    name = "janai"
    display_name = "Jan.ai"
    blurb = "Jan's local API server"
    optional_key_env = "JAN_API_KEY"
    start_hint = "In Jan, turn on the local API server under Settings."

    @property
    def default_base_url(self) -> str:
        return settings.jan_base_url

    def model_hint(self, model_id: str) -> str:
        return "Download and start it in Jan, or pick one it already has from the list."


class GPT4AllProvider(_LocalOpenAIServer):
    name = "gpt4all"
    display_name = "GPT4All"
    blurb = "GPT4All's desktop API server"
    optional_key_env = "GPT4ALL_API_KEY"
    start_hint = "In GPT4All, turn on the API server under Settings."

    @property
    def default_base_url(self) -> str:
        return settings.gpt4all_base_url

    def model_hint(self, model_id: str) -> str:
        return "Download it in GPT4All, or pick one it already has from the list."


class VLLMProvider(_LocalOpenAIServer):
    """vLLM's OpenAI-compatible server.

    The one of the five that is not a desktop app: it is served rather than
    clicked, one model per process, and its default port is 8000 — which on a
    machine also running this project's restaurant API is taken. So it is the
    likeliest of the five to need its URL changed, which is the field the
    settings section leads with.
    """

    name = "vllm"
    display_name = "vLLM"
    blurb = "A served model, on this machine or the next one"
    optional_key_env = "VLLM_API_KEY"
    start_hint = "Start it with `vllm serve <model>`, then set the URL to its port."

    @property
    def default_base_url(self) -> str:
        return settings.vllm_base_url

    def model_hint(self, model_id: str) -> str:
        return (
            "vLLM serves the model it was started with — restart it with that one, "
            "or pick what it has from the list."
        )


def _billions(parameters: Any) -> str | None:
    """7615616512 -> "7.6B". What Ollama reports directly and llama.cpp counts."""
    try:
        count = float(parameters)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    return f"{count / 1e9:.1f}B" if count >= 1e9 else f"{count / 1e6:.0f}M"


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
#: Built once. These objects hold configuration, not per-call state, so one
#: instance each is right — the same arrangement `agent/delivery/registry.py`
#: uses for couriers, and for the same reason.
_PROVIDERS: dict[str, LLMProvider] = {
    provider.name: provider
    for provider in (
        LlamaCppProvider(),
        LMStudioProvider(),
        JanProvider(),
        GPT4AllProvider(),
        VLLMProvider(),
        LocalProvider(),
        OpenRouterProvider(),
        AnthropicProvider(),
        GeminiProvider(),
        OpenAIProvider(),
        GroqProvider(),
        HuggingFaceProvider(),
    )
}

def names() -> list[str]:
    """Every provider name that can be put in AGENT_PROVIDER or chosen on screen."""
    return list(_PROVIDERS)


def canonical(name: str) -> str:
    """The one spelling of a provider name — `local` and `ollama` are one thing.

    Delegates to `agent.config`, which owns the alias map because the model
    defaults have to canonicalise by the same rules. Two copies of this table
    was the bug: the adapter lookup knew `local` meant Ollama and
    `_default_model_for` did not, so `AGENT_PROVIDER=local` reached the right
    runtime holding the wrong model id.
    """
    return canonical_provider(name)


def get(name: str) -> LLMProvider:
    """The adapter for `name`, or a `ValueError` naming the ones that exist."""
    key = canonical(name)
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise ValueError(
            f"Unknown provider {name!r} — expected one of {', '.join(_PROVIDERS)}."
        )
    return provider


def describe(provider: LLMProvider) -> dict:
    """A provider as the LLM screen draws it.

    Never a key — only whether one is set, and the *name* of the variable to put
    it in when it is not.

    `settings` is the shape of this provider's configuration section and
    `settingValues` is what is currently in force, so the screen draws a section
    for a provider that has one and nothing at all for a provider that does not
    — without knowing any provider's name.
    """
    ready, problem = provider.configured()
    schema = provider.settings_schema()
    return {
        "name": provider.name,
        "displayName": provider.display_name,
        "kind": provider.kind,
        "blurb": provider.blurb,
        "featured": provider.featured,
        "dynamicModels": provider.dynamic_models,
        "requiresKey": provider.key_env is not None,
        "keyEnv": provider.key_env,
        "configured": ready,
        "problem": problem,
        "defaultModel": provider.default_model(),
        "startHint": getattr(provider, "start_hint", "") or None,
        "settings": [setting.to_view() for setting in schema],
        "settingValues": provider.settings() if schema else {},
    }
