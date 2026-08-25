"""The central LLM layer: one selection, one service, one adapter per provider.

    agent  ->  llm.build_model()  ->  provider adapter  ->  the selected model

Import `llm` from here. Nothing above this package should import a Strands model
class, read a provider's API key, or decide which vendor an errand runs on —
those three facts live in `providers.py`, and which one is in force lives in
`store.py`.
"""

from agent.llm.providers import (
    Check,
    Health,
    LLMProvider,
    MissingApiKey,
    ProviderUnavailable,
)
from agent.llm.service import LLMService, llm
from agent.llm.store import Selection

__all__ = [
    "Check",
    "Health",
    "LLMProvider",
    "LLMService",
    "MissingApiKey",
    "ProviderUnavailable",
    "Selection",
    "llm",
]
