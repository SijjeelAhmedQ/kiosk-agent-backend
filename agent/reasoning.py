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
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

__all__ = ["DropReasoningContent"]


def _talks_chat_completions(model: Any) -> bool:
    """Is this agent's brain reached through the Chat Completions API?

    Asked of the model client rather than of the provider name, because there
    are four names for it — openai, groq, huggingface, openrouter — and every
    one of them ends up as the same class. A fifth would too.
    """
    from strands.models.openai import OpenAIModel

    return isinstance(model, OpenAIModel)


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
