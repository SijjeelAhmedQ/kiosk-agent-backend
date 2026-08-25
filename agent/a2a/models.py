"""Building a model client for one named role, through the central service.

This file used to hold a second copy of the ordering agent's seven-branch model
factory, because that one read the process-wide `settings` singleton and could
only ever build the one model the errand flow was configured for.

`agent/llm` is that missing thing: a provider registry that takes the provider
and model as arguments, with the *selected* one as the default. So both copies
are gone and this module is what it always wanted to be — the A2A package's
name for a call it does not own, with the one thing that genuinely belongs to
this package kept: an error message that says *which side* of the conversation
is missing credentials, which is useless advice when there are two agents and
only one of them is unconfigured.

The two public names are unchanged. `agent/foodpanda/dispatcher.py` imports both
of them and did not need touching.
"""

from __future__ import annotations

from typing import Any

from agent.llm import MissingApiKey, llm

__all__ = ["MissingApiKey", "build_model", "credentials_ready"]


def credentials_ready(provider: str, model_id: str, role: str) -> tuple[bool, str | None]:
    """Can this side of the conversation actually run? `(ready, what_is_missing)`.

    `role` is "buyer" or "merchant" (or "dispatcher", for the Foodpanda agent),
    and appears in the message for the reason it always did: with several agents
    on one floor, "no API key" is useless without saying whose.
    """
    ready, problem = llm.credentials_ready(provider, model_id)
    if problem:
        problem = f"The {role} agent cannot run. {problem}"
    return ready, problem


def build_model(provider: str, model_id: str, max_tokens: int, effort: str = "high") -> Any:
    """A Strands model client for `provider`.

    Every branch that used to be here is now an adapter in
    `agent/llm/providers.py`, including the awkward bits: OpenAI's reasoning
    models reject `max_tokens` and want `max_completion_tokens`, Groq is the
    OpenAI client with the base URL moved, Ollama takes `max_tokens` directly
    rather than through `params`.
    """
    return llm.build_model(provider, model_id, max_tokens, effort)
