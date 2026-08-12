"""The restaurant's own agent — its one turn of the conversation.

`take_turn` is the seam the whole service is built around: the endpoint records
what the buyer said, calls this, and streams whatever comes out.

One Strands agent per conversation, kept on the task, because a merchant that
forgot the basket between messages would be no use in a negotiation — the buyer
asking "can you drop the drink?" only means anything to something that remembers
what it quoted. That agent and its tools close over a session that belongs to
the task and to nothing else, so two buyers negotiating at once cannot see each
other's baskets.
"""

from __future__ import annotations

import json

from strands import Agent

from agent.a2a.config import a2a_settings
from agent.a2a.models import MissingApiKey, build_model, credentials_ready
from agent.a2a.merchant_tools import MerchantSession, build_tools
from agent.a2a.prompts import merchant_prompt
from agent.a2a.protocol import (
    COMPLETED,
    INPUT_REQUIRED,
    RECEIPT,
    REJECTED,
    WORKING,
    Message,
    event_message,
    event_status,
    parts_data,
    parts_text,
)
from agent.a2a.tasks import MerchantTask
from agent.a2a.trace import run_turn


def _session(task: MerchantTask) -> MerchantSession:
    """This conversation's basket, order and agent — built once, then reused."""
    existing = task.session.get("merchant")
    if existing is not None:
        return existing

    ready, problem = credentials_ready(
        a2a_settings.merchant_provider, a2a_settings.merchant_model, "merchant"
    )
    if not ready:
        raise MissingApiKey(problem or "The merchant has no usable model credentials.")

    session = MerchantSession(task=task)

    # Which hands. The tool *names* are the same either way, so the brief and
    # the negotiation do not change — only whether the merchant is calling the
    # restaurant's API or tapping its touchscreen.
    at_the_screen = a2a_settings.merchant_hands == "browser"
    if at_the_screen:
        from agent.a2a.merchant_browser_tools import build_browser_tools

        tools = build_browser_tools(session, headless=a2a_settings.browser_headless)
    else:
        tools = build_tools(session)

    session_agent = Agent(
        model=build_model(
            a2a_settings.merchant_provider,
            a2a_settings.merchant_model,
            a2a_settings.max_tokens,
        ),
        tools=tools,
        system_prompt=merchant_prompt(at_the_screen=at_the_screen),
        name="friends-kitchen-ordering-desk",
        description="Takes orders from other agents, quotes them, and gets paid.",
        callback_handler=None,
    )
    task.session["merchant"] = session
    task.session["agent"] = session_agent
    return session


def _as_prompt(message: Message) -> str:
    """One buyer message as the merchant's model should read it.

    Data parts are appended as labelled JSON rather than folded into the prose.
    A coupon code is the case that matters: transcribed into a sentence it is a
    string a model can mistype, and a mistyped coupon fails as "not found" three
    tool calls later.
    """
    text = parts_text(message.parts).strip()
    figures = parts_data(message.parts)
    if not figures:
        return text

    blob = json.dumps(figures if len(figures) > 1 else figures[0], indent=2)
    return f"{text}\n\nThe customer's agent also sent this, exactly as written:\n{blob}"


async def take_turn(task: MerchantTask, incoming: Message) -> Message:
    """Answer one buyer message. Returns the merchant's reply.

    The caller has already recorded `incoming` and emitted it. This owns
    everything after that: the state transition, any artifacts, and the reply.
    """
    task.state = WORKING
    task.stream.emit(event_status(WORKING))

    # A conversation this long is going in circles, and both agents will keep
    # being polite at each other until someone's token budget runs out.
    turns = sum(1 for m in task.messages if m.role == "buyer")
    if turns > a2a_settings.max_turns:
        reply = Message.say(
            "merchant",
            f"We have gone {turns} messages without finishing. I am closing this "
            "order unfinished — start a new one if you still want it.",
        )
        task.record(reply)
        task.stream.emit(event_message(reply))
        task.state = REJECTED
        task.stream.emit(event_status(REJECTED))
        return reply

    session = _session(task)
    said = await run_turn(task.session["agent"], _as_prompt(incoming), "merchant", task.stream)

    reply = task.record(Message.say("merchant", said or "(no reply)"))
    task.stream.emit(event_message(reply))

    # Paid is the only finished state a merchant can reach on its own. Anything
    # else leaves the conversation open, including a confirmed-but-unpaid order:
    # the buyer may still be deciding, and closing the task would strand it.
    paid = any(a.name == RECEIPT for a in task.artifacts)
    task.state = COMPLETED if paid else INPUT_REQUIRED
    task.stream.emit(event_status(task.state))

    return reply
