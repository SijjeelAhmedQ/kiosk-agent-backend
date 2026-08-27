"""Keeping reasoning blocks out of a history the Chat Completions API cannot take.

A reasoning model streams its thinking back as a `reasoningContent` block, and
Strands stores that block on the assistant message like any other. Anthropic and
Gemini want it back on the next turn — the signature on it is what lets them
carry a thought across a tool call — so keeping it is right for them.

The OpenAI Chat Completions API has nowhere to put it. Strands drops the block
when it formats the request and says so:

    reasoningContent is not supported in multi-turn conversations with the
    Chat Completions API.

Which is a warning about something already handled, logged once per message
holding one, on every turn after the first. An errand is a dozen tool calls, the
root logger is mirrored into `agent.console`, and so the dashboard's console
fills with a line that describes no problem and hides the ones that do.

`AGENT_PROVIDER=openrouter` — the current setting — is that API, as are groq,
huggingface and openai itself. So drop the block as the message is added, for
those providers only: the request that goes on the wire is byte for byte the one
Strands was going to send anyway, minus a warning about the difference.

The local runtimes are that API too, and on one of them the same block is worse
than a warning. LM Studio, Jan, GPT4All and vLLM are all reached through the
OpenAI client and behave exactly as the hosted four do. llama.cpp is reached
through its own client, and that one does not drop what it cannot format — it
raises `TypeError: content_type=<reasoningContent> | unsupported type` and ends
the errand. A local thinking model (qwen3 with thinking left on, say) is enough
to produce one, so llama.cpp is named here as well, and for a stronger reason
than tidy logs.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

__all__ = ["DropReasoningContent"]


def _talks_chat_completions(model: Any) -> bool:
    """Is this agent's brain reached through the Chat Completions API?

    Asked of the model client rather than of the provider name, because there
    are eight names for it — openai, groq, huggingface, openrouter, and the four
    local servers that speak the same wire format — and every one of them ends
    up as the same class. A ninth would too.

    llama.cpp is the one that does not: Strands ships a native client for it, so
    it has to be named. It is the same API underneath, and the block is even
    less welcome there — see the module docstring.
    """
    from strands.models.llamacpp import LlamaCppModel
    from strands.models.openai import OpenAIModel

    return isinstance(model, (OpenAIModel, LlamaCppModel))


class DropReasoningContent(HookProvider):
    """Strips `reasoningContent` from messages on their way into history."""

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(MessageAddedEvent, self._strip)

    def _strip(self, event: MessageAddedEvent) -> None:
        if not _talks_chat_completions(event.agent.model):
            return

        content = event.message.get("content") or []
        kept = [block for block in content if "reasoningContent" not in block]
        if len(kept) == len(content):
            return

        # A turn that was *only* thinking is left alone. It should not happen —
        # there is always text or a tool call beside the reasoning — but an
        # assistant message with no content at all is a stranger thing to hand
        # the next request than the warning this exists to silence.
        if not kept:
            return

        # The same dict the agent appended, so this edits the history itself and
        # not a copy of it.
        event.message["content"] = kept
