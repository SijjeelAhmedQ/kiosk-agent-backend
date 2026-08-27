"""The central LLM service — the only thing in this system that builds a brain.

Every agent here used to pick its own model client. The ordering agent had a
seven-branch `_model()`, the A2A package had a second copy of it with the
provider passed in, and the Foodpanda dispatcher imported that one. Three call
sites, three chances for a provider switch to reach two of them.

Now there is one:

    agent  ->  llm.build_model()  ->  provider adapter  ->  the selected model

`build_model()` with no arguments builds whatever the central selection says,
which is the only way any agent should ask. The arguments exist for the two
things that legitimately differ per agent and have nothing to do with which
vendor is answering: the token budget and the reasoning effort. A negotiation
needs more headroom than a counter order; neither is a provider choice.

Switching provider or model is `select()`, and it takes effect on the *next*
agent built — in this process and in the other three, because the selection is
a file rather than a variable. Nothing here caches a client.
"""

from __future__ import annotations

from typing import Any

from agent.config import settings
from agent.llm import providers as registry
from agent.llm import store
from agent.llm.providers import (
    Check,
    Health,
    LLMProvider,
    MissingApiKey,
    ProviderUnavailable,
    Setting,
)
from agent.llm.store import Selection

__all__ = [
    "Check",
    "Health",
    "LLMProvider",
    "LLMService",
    "MissingApiKey",
    "ProviderUnavailable",
    "Selection",
    "Setting",
    "llm",
]

#: What a test generation asks for. Short on purpose — the question is whether
#: the pipe works, not whether the model is clever — and worded so that a model
#: which answers at all answers recognisably.
_TEST_PROMPT = "Reply with exactly one word: ok"


class LLMService:
    """The one door between an agent and a model."""

    # -- what is selected --------------------------------------------------- #
    def active(self) -> Selection:
        """The provider and model every agent in this system is running on."""
        return store.active()

    def select(self, provider: str, model_id: str | None = None) -> Selection:
        """Change it, for every agent in every one of the four processes.

        Validated before it is written: an unknown provider or a model id that
        obviously belongs to another vendor is refused here rather than saved
        and discovered as a 404 on somebody's next errand.
        """
        adapter = registry.get(provider)
        chosen = (model_id or "").strip() or adapter.default_model()

        ready, problem = adapter.configured(chosen)
        if not ready:
            raise MissingApiKey(problem or f"{adapter.display_name} is not configured.")

        return store.save(adapter.name, chosen)

    def provider(self, name: str | None = None) -> LLMProvider:
        """The adapter for `name`, or for whatever is currently selected."""
        return registry.get(name or self.active().provider)

    # -- what the LLM screen reads ------------------------------------------ #
    def providers(self) -> list[dict]:
        """Every provider, described. Featured ones first, then alphabetical."""
        described = [registry.describe(registry.get(name)) for name in registry.names()]
        described.sort(key=lambda item: (not item["featured"], item["displayName"].lower()))
        return described

    def models(self, provider: str | None = None) -> list[dict]:
        """What `provider` can run. Raises `ProviderUnavailable` if it will not say."""
        return self.provider(provider).list_models()

    def settings(self, provider: str | None = None) -> dict:
        """One provider's configuration section: its shape and its values.

        Empty `fields` is the honest answer for a provider that has nothing to
        configure here — a cloud vendor's address is not this deployment's
        business — and it is what the screen reads to decide whether to draw a
        section at all.
        """
        adapter = self.provider(provider)
        schema = adapter.settings_schema()
        return {
            "provider": adapter.name,
            "displayName": adapter.display_name,
            "fields": [setting.to_view() for setting in schema],
            "values": adapter.settings() if schema else {},
        }

    def configure(self, provider: str, values: dict) -> dict:
        """Save one provider's settings, and report them as they now stand.

        Validated against the adapter's own schema rather than trusted: a field
        it does not declare is refused by name, and a value it does declare is
        coerced and clamped to the range declared with it. That is what keeps a
        hand-written PUT from putting a string where a temperature goes and
        turning every later `build()` into a TypeError.

        A field sent as null is a *reset* — it drops out of the saved file and
        goes back to what .env says, which is not the same as pinning it to
        whatever that value happens to be today.
        """
        adapter = self.provider(provider)
        schema = {setting.key: setting for setting in adapter.settings_schema()}
        if not schema:
            raise ValueError(f"{adapter.display_name} has no settings to configure.")

        cleaned: dict = {}
        for key, value in values.items():
            setting = schema.get(key)
            if setting is None:
                raise ValueError(
                    f"{adapter.display_name} has no setting called {key!r} — "
                    f"expected one of {', '.join(schema)}."
                )
            if value is None:
                cleaned[key] = None
                continue
            coerced = setting.coerce(value)
            if coerced is None:
                raise ValueError(f"{value!r} is not a usable value for {setting.label}.")
            cleaned[key] = coerced

        store.save_settings(adapter.name, cleaned)

        # Any cached model list was fetched from the *previous* address, so a
        # changed URL must not be answered with what the old one had loaded.
        if "baseUrl" in cleaned:
            type(adapter)._cache = None

        return self.settings(adapter.name)

    def health(self, provider: str | None = None, model_id: str | None = None) -> dict:
        """Whether a provider and model could serve a run right now.

        Defaults to the active selection, so the health strip on every console
        can ask this question without knowing the answer to the previous one.
        """
        selection = self.active()
        name = provider or selection.provider
        wanted = model_id if model_id is not None else (
            selection.model_id if name == selection.provider else None
        )

        try:
            adapter = registry.get(name)
        except ValueError as exc:
            return {
                "provider": name,
                "model": wanted,
                **Health(
                    ok=False,
                    checks=[Check("Provider known", False, str(exc))],
                    problem=str(exc),
                ).to_view(),
            }

        report = adapter.health(wanted)
        return {
            "provider": adapter.name,
            "displayName": adapter.display_name,
            "kind": adapter.kind,
            "model": wanted,
            **report.to_view(),
        }

    def test(self, provider: str | None = None, model_id: str | None = None) -> dict:
        """Actually run something. The only check that proves the whole path.

        Health asks the provider about itself; this asks the *model* a question
        and reads the answer back, through exactly the client an agent would get
        — so a key that is present but rejected, or a model that is listed but
        not actually servable, fails here rather than on the next errand.

        Never raises. A failed test is a state the screen renders, and the
        message it renders is deliberately a sentence rather than a traceback:
        a stack trace from a provider client can carry a request header in it.
        """
        selection = self.active()
        name = provider or selection.provider
        wanted = (model_id or "").strip() or (
            selection.model_id if name == selection.provider else ""
        )

        checks: list[Check] = []

        try:
            adapter = registry.get(name)
        except ValueError as exc:
            return {
                "ok": False,
                "provider": name,
                "model": wanted,
                "checks": [Check("Provider known", False, str(exc)).to_view()],
                "problem": str(exc),
            }

        wanted = wanted or adapter.default_model()

        # 1 — is it configured at all? Cheap, and it produces the best message.
        #     Only *reported* when it fails: a local runtime needs no credential,
        #     so a green "provider connected" here would be a tick for a check
        #     that touched nothing — and the real connectivity is step 2.
        ready, problem = adapter.configured(wanted)
        if not ready:
            checks.append(Check("Provider configured", False, problem))
            return {
                "ok": False,
                "provider": adapter.name,
                "model": wanted,
                "checks": [check.to_view() for check in checks],
                "problem": problem,
            }

        # 2 — is the provider actually answering, and is the model there? For
        #     the two dynamic providers this reaches the network; for the rest
        #     the adapter says so without a call.
        report = adapter.health(wanted)
        checks.extend(report.checks)
        if not report.ok:
            return {
                "ok": False,
                "provider": adapter.name,
                "model": wanted,
                "checks": [check.to_view() for check in checks],
                "problem": report.problem,
            }

        # 3 — the only question that matters: does it answer?
        try:
            reply = self._generate(adapter, wanted)
        except Exception as exc:  # noqa: BLE001 — every provider fails differently
            detail = _explain(exc, adapter)
            checks.append(Check("Test response received", False, detail))
            return {
                "ok": False,
                "provider": adapter.name,
                "model": wanted,
                "checks": [check.to_view() for check in checks],
                "problem": detail,
            }

        checks.append(Check("Test response received", True, reply or None))
        return {
            "ok": True,
            "provider": adapter.name,
            "model": wanted,
            "checks": [check.to_view() for check in checks],
            "problem": None,
            "reply": reply,
        }

    def _generate(self, adapter: LLMProvider, model_id: str) -> str:
        """One tiny completion, through the real client. Blocking — call it in a thread.

        A bare `Agent` with no tools and no brief, because the point is to
        exercise the transport rather than the ordering flow. `max_tokens` is
        the ordering agent's own budget rather than something small: on a
        reasoning model the budget is spent on thinking before a word is
        emitted, and a test that fails only because it was given fifty tokens
        would be a test that lies.
        """
        from strands import Agent

        from agent.reasoning import DropReasoningContent

        probe = Agent(
            model=adapter.build(model_id, settings.max_tokens, settings.effort),
            tools=[],
            system_prompt="You are a connectivity probe. Answer in one word.",
            callback_handler=None,
            hooks=[DropReasoningContent()],
        )
        return str(probe(_TEST_PROMPT)).strip()[:200]

    # -- what agents call --------------------------------------------------- #
    def credentials_ready(
        self, provider: str | None = None, model_id: str | None = None
    ) -> tuple[bool, str | None]:
        """Can the selected brain run? `(ready, what_is_missing)`.

        Split from `build_model()` so a health endpoint can *say* what is
        missing rather than discover it by failing a run — which is what the
        ordering agent's own `credentials_ready()` has always been for.
        """
        selection = self.active()
        name = provider or selection.provider
        wanted = model_id or (selection.model_id if name == selection.provider else None)
        try:
            adapter = registry.get(name)
        except ValueError as exc:
            return False, str(exc)
        return adapter.configured(wanted)

    def build_model(
        self,
        provider: str | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
    ) -> Any:
        """A Strands model client for whatever is selected.

        This is what every agent in this repo calls, and none of them pass a
        provider: that is the whole point. `max_tokens` and `effort` are passed,
        because those are properties of the errand rather than of the vendor.
        """
        selection = self.active()
        name = provider or selection.provider
        wanted = (model_id or "").strip() or (
            selection.model_id if name == selection.provider else ""
        )

        try:
            adapter = registry.get(name)
        except ValueError as exc:
            raise MissingApiKey(str(exc)) from None

        wanted = wanted or adapter.default_model()

        ready, problem = adapter.configured(wanted)
        if not ready:
            raise MissingApiKey(problem or "The model provider is not configured.")

        return adapter.build(
            wanted,
            max_tokens if max_tokens is not None else settings.max_tokens,
            effort or settings.effort,
        )

    # -- what the health endpoints report ----------------------------------- #
    def describe(self) -> dict:
        """The active brain, for every service's `/health`.

        Deliberately excludes anything credential-shaped: the answer is whether
        a key is present, never what it is.
        """
        selection = self.active()
        ready, problem = self.credentials_ready()
        try:
            adapter = registry.get(selection.provider)
            display = adapter.display_name
            kind = adapter.kind
        except ValueError:
            display, kind = selection.provider, "unknown"

        return {
            **selection.to_view(),
            "displayName": display,
            "kind": kind,
            "ready": ready,
            "problem": problem,
        }


def _explain(exc: Exception, adapter: LLMProvider) -> str:
    """A provider failure as one sentence somebody can act on.

    Never the exception's own text verbatim past a point: a client library's
    error can carry the request it was making, headers included. The status is
    the useful part, so that is what is read out of it.
    """
    text = f"{exc}"
    lowered = text.lower()

    if adapter.kind == "local":
        # A local runtime has no credential to get wrong, so almost everything
        # that fails here is the machine rather than the configuration — and
        # collapsing all of it into "not available" sends an operator to check a
        # daemon they can see running. These are the three that actually happen,
        # and each one has a different fix.
        # Each runtime says the fix in its own words — `ollama pull` is not
        # advice a llama.cpp operator can act on, and a port is not a hint at
        # all unless it is the port that was actually tried. `getattr` rather
        # than a method on the base class, so a provider that has nothing
        # better to say falls back to the general sentence.
        hint = adapter.context_hint
        where = getattr(adapter, "host", "its configured address")

        if "out of memory" in lowered or "unable to allocate" in lowered or "oom" in lowered:
            return (
                "The local model could not be loaded: this machine ran out of memory "
                f"for it. Free some RAM, or choose a smaller model. {hint}".strip()
            )
        if "context" in lowered and ("exceed" in lowered or "longer than" in lowered):
            return f"The prompt is longer than the local model's context window. {hint}".strip()
        if "not found" in lowered or "404" in text or "no such model" in lowered:
            model_hint = getattr(adapter, "model_hint", None)
            return (
                f"{adapter.display_name} does not have that model. "
                + (
                    model_hint("that model")
                    if callable(model_hint)
                    else "Pull it with `ollama pull <model>`, or pick one from the model list."
                )
            )
        if "connect" in lowered or "refused" in lowered or "timeout" in lowered:
            unreachable = getattr(adapter, "unreachable", None)
            if callable(unreachable):
                return unreachable()
            return (
                "Local LLM is not available. Please make sure the local runtime is "
                f"running at {where}."
            )
        # Anything else: say it is the runtime, but do not pretend to know which
        # of its many failure modes this was.
        return (
            f"The local runtime at {where} failed to serve that model. Check the "
            "runtime's own log for the reason."
        )
    if "401" in text or "unauthor" in lowered or "invalid api key" in lowered:
        return (
            f"{adapter.display_name} rejected the credentials. Check {adapter.key_env} "
            "in friends-kitchen-agent-backend/.env."
        )
    if "404" in text or "not found" in lowered:
        return f"{adapter.display_name} does not serve that model. Pick another one."
    if "429" in text or "rate limit" in lowered:
        return f"{adapter.display_name} is rate limiting this key. Try again in a moment."
    if "timeout" in lowered or "timed out" in lowered:
        return f"{adapter.display_name} did not answer in time."
    return f"Unable to connect to {adapter.display_name}. Please check the configuration."


#: The service. One per process, holding no state of its own — the selection
#: lives in the file, the adapters hold configuration. Import this, not the
#: class.
llm = LLMService()
