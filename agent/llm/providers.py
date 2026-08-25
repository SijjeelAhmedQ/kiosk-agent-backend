"""One adapter per place a model can come from.

Every provider answers the same four questions — can you run, what models do you
have, are you healthy, and build me a client — so nothing above this file has to
know whether the model is a service in Singapore or a process on this laptop.

Two of them are the ones the LLM screen puts front and centre:

* `OpenRouterProvider` — the cloud router this system has been using all along.
  Its model list is fetched from OpenRouter, so it is whatever OpenRouter has
  today rather than a list baked into a frontend.
* `LocalProvider` — a locally running Ollama. Its model list is whatever
  `ollama pull` has actually put on the machine, read from the daemon's own
  `/api/tags`.

The rest — Anthropic, Gemini, OpenAI, Groq, Hugging Face — were already
supported through `AGENT_PROVIDER` and still are. They have no listing endpoint
worth calling here, so they report their configured default and say the list is
not dynamic. Removing them would have broken an existing .env; they are simply
not featured.

Nothing in this file returns, logs or accepts an API key. `configured()` answers
whether a key is *present*, which is all a UI ever needs to know.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

from agent.config import _default_model_for, canonical_provider, settings


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
    #: Model ids of this vendor start with one of these, when that is a
    #: meaningful check. Routers host everybody's models, so theirs is empty.
    families: tuple[str, ...] = ()

    # -- configuration ------------------------------------------------------ #
    def default_model(self) -> str:
        return _default_model_for(self.name)

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
    featured = True
    blurb = "Open models running on this machine, through Ollama"

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
# The registry
# --------------------------------------------------------------------------- #
#: Built once. These objects hold configuration, not per-call state, so one
#: instance each is right — the same arrangement `agent/delivery/registry.py`
#: uses for couriers, and for the same reason.
_PROVIDERS: dict[str, LLMProvider] = {
    provider.name: provider
    for provider in (
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
    """
    ready, problem = provider.configured()
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
    }
