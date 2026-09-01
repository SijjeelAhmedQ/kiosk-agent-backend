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
import re

from strands import Agent

from agent.a2a.claims import DENIED, denied_near
from agent.a2a.config import a2a_settings
from agent.a2a.models import MissingApiKey, build_model, credentials_ready
from agent.a2a.merchant_tools import MerchantSession, build_tools
from agent.a2a.prompts import merchant_prompt
from agent.a2a.redaction import strip_ids
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
from agent.reasoning import DropReasoningContent


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
        hooks=[DropReasoningContent()],
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


# --------------------------------------------------------------------------- #
# Keeping the merchant's words true, and the restaurant's plumbing out of them
# --------------------------------------------------------------------------- #
#: "Added one Big Mac to the basket", "the basket now contains two burgers".
#: Deliberately requires the verb: "I notice the basket is empty" is a true
#: sentence about an empty basket and must not read as a claim about a full one.
_CLAIMS_BASKET = re.compile(
    r"\b(?:added|adding|have\s+added|put)\b[^.!?]{0,80}\b(?:basket|cart|order)\b"
    r"|\b(?:basket|cart)\b[^.!?]{0,40}\b(?:now\s+)?(?:contains|holds|has)\b",
    re.IGNORECASE,
)

#: "Order 495 confirmed", "I have placed the order".
_CLAIMS_ORDER = re.compile(
    r"\border\s+(?:number\s+)?\d+\b"
    r"|\b(?:confirmed|placed|created)\b[^.!?]{0,40}\border\b"
    r"|\border\b[^.!?]{0,40}\b(?:is\s+|has\s+been\s+)?(?:confirmed|placed)\b",
    re.IGNORECASE,
)

_CORRECT_BASKET = (
    "Stop and check yourself before you answer. The basket is empty. You have "
    "just described putting something in it, and no add_to_basket call has "
    "returned — so nothing is in it, and the customer's agent is about to be "
    "given a total for a basket that does not exist. Look the item up with "
    "browse_menu, add it with add_to_basket, then send_quote; one call at a "
    "time, waiting for each result. Then answer again, describing only what "
    "the tools actually did."
)

_CORRECT_ORDER = (
    "Stop and check yourself before you answer. No order exists. You have just "
    "described one as placed or confirmed, and no confirm_order call has "
    "returned — there is no order number and nothing is on the restaurant's "
    "books. If they have agreed to the quote, call confirm_order now. If they "
    "have not, answer again and say plainly that the order is not confirmed yet."
)


def _has_basket(session: MerchantSession) -> bool:
    """Is there anything to sell? Asked of both toolsets at once — the API
    tools keep a cart here, the browser tools a total read off the screen."""
    return bool(session.cart.lines) or bool(session.order.get("basketTotal"))


def _is_confirmed(session: MerchantSession) -> bool:
    """Has an order actually been written to the restaurant's books?

    Not `session.order` itself: in browser mode that dict holds the open till
    long before anything is confirmed.
    """
    return bool(session.order.get("orderNumber")) or session.order.get("amountDue") is not None


def _unbacked_claim(said: str, session: MerchantSession) -> str | None:
    """A correction for a reply that describes something that never happened.

    A small local model narrates the step instead of taking it: it reads the
    menu, writes "added one Big Mac to the basket, that will be Rs 530", and
    never calls `add_to_basket`. Every word of that reaches the buyer as fact,
    and the buyer has no way to tell — a described basket looks exactly like a
    real one until payment fails against it.

    So the reply is checked against the session before it is sent, and a claim
    the session does not back buys the merchant one more turn to make it true.
    A model that took its steps properly never sees this, which is the point:
    it costs the good case nothing.
    """
    text = (said or "").strip()
    if not text:
        return None

    if not _has_basket(session) and _CLAIMS_BASKET.search(text) and not DENIED.search(text):
        return _CORRECT_BASKET
    if not _is_confirmed(session) and _CLAIMS_ORDER.search(text) and not DENIED.search(text):
        return _CORRECT_ORDER
    return None


#: The buyer agreeing, in the words a model actually uses to agree. Only ever
#: read against a basket that already exists and an order that does not, which
#: is what keeps "confirm you have the burger" — a question, asked before there
#: is anything to confirm — out of it.
_AGREED = re.compile(
    r"\b(?:confirm|place|finalise|finalize|ring\s+up)\b[^.!?]{0,40}\b(?:the\s+)?order\b"
    r"|\border\b[^.!?]{0,30}\b(?:confirmed|placed)\b"
    r"|\b(?:go\s+ahead|proceed\s+with)\b",
    re.IGNORECASE,
)

#: A whole message that is nothing but agreement. "Yes." on its own is not a
#: pattern worth trusting in general, but it is only ever read here against a
#: basket the merchant has already quoted and an order it has not placed, and
#: at that point in a negotiation there is nothing else a bare yes can mean.
#: Small local models answer this tersely and the long form above misses them.
#: The length cap is what keeps "Yes, but drop the drink" — a change, not an
#: agreement — from reading as one.
_JUST_YES = re.compile(
    r"^\W*(?:yes|yep|yeah|ok|okay|sure|agreed|confirmed)\b[^\n]{0,16}$",
    re.IGNORECASE,
)

_CORRECT_STALL = (
    "Stop and check yourself before you answer. They have just agreed to the "
    "quote, and your reply went out without confirm_order having returned — so "
    "nothing is on the restaurant's books, there is no order number, and there "
    "is no firm total for them to pay against. Asking them to confirm a second "
    "time is the one answer that cannot move this forward: they already did, "
    "and they have no tool that can answer it again any more plainly. Call "
    "confirm_order now and answer with what it returns. If you genuinely "
    "cannot — the basket is wrong, or something is unavailable — say which, "
    "plainly, instead of asking again."
)


def _stalled_confirmation(incoming: Message, session: MerchantSession) -> str | None:
    """A correction for a merchant that was told to confirm and did not.

    The failure this catches is quieter than an invented basket and ends the
    errand just as dead: the buyer says "yes, confirm the order", the merchant
    answers "please confirm if you would like to proceed", and the two of them
    do that until the turn limit. Nothing is claimed falsely, so
    `_unbacked_claim` sees nothing wrong — but no order exists, which means no
    firm quote, which means `authorize_payment` refuses and the buyer goes home
    with the food unbought.

    Read off the session rather than off the merchant's phrasing: what matters
    is not whether the reply sounds like another request for confirmation, but
    that a basket the buyer has agreed to is still not an order.
    """
    if _is_confirmed(session) or not _has_basket(session):
        return None

    asked = (parts_text(incoming.parts) or "").strip()
    if _JUST_YES.match(asked):
        return _CORRECT_STALL

    match = _AGREED.search(asked)
    if not match or denied_near(asked, match):
        return None
    return _CORRECT_STALL


def _redact_ids(said: str, session: MerchantSession) -> str:
    """Take the restaurant's plumbing back out of the merchant's words.

    The rule itself is in `agent.a2a.redaction`, shared with the buyer, because
    an id that reaches the transcript through the buyer's sentence is as visible
    as one that reaches it through the merchant's. What is merchant-specific is
    the second argument: this side knows every id the conversation handled, so
    it can strike a bare one the buyer would have no way to recognise.
    """
    return strip_ids(said, session.seen_ids)


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

    # One chance to put the turn right — a step that was described instead of
    # taken, or one the buyer asked for and did not get. Whichever applies; the
    # false claim first, because its correction already names confirm_order.
    #
    # Bounded at one on purpose: a merchant that will not add to the basket
    # twice running is not going to on the third ask, and the buyer is better
    # served by an honest reply it can act on than by a conversation that never
    # comes back.
    correction = _unbacked_claim(said, session) or _stalled_confirmation(incoming, session)
    if correction:
        said = await run_turn(task.session["agent"], correction, "merchant", task.stream) or said

    # The quote, unasked — the same rule as the delivery handover in
    # `take_payment`. A quote that depends on the merchant remembering to send
    # one is a quote that goes missing on the turn it forgets, and what the buyer
    # loses with it is not a nicety: `walletCheck` is computed from the artifact,
    # so a basket that was only ever described in words is a basket the buyer
    # cannot check against its budget at all. Sending one costs the merchant that
    # did call `send_quote` nothing — see `_quote_now`, which is a no-op unless
    # the basket has moved on from the figure the buyer is already holding.
    if session.quote_unasked is not None:
        await session.quote_unasked()

    reply = task.record(Message.say("merchant", _redact_ids(said, session) or "(no reply)"))
    task.stream.emit(event_message(reply))

    # Paid is the only finished state a merchant can reach on its own. Anything
    # else leaves the conversation open, including a confirmed-but-unpaid order:
    # the buyer may still be deciding, and closing the task would strand it.
    paid = any(a.name == RECEIPT for a in task.artifacts)
    task.state = COMPLETED if paid else INPUT_REQUIRED
    task.stream.emit(event_status(task.state))

    return reply
