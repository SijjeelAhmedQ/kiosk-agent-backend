"""The customer's agent — the one that is sent out with a coupon and a limit.

The mirror of `merchant_agent.take_turn`: the console starts a run, this drives
it, and the run's stream carries both sides of the conversation so the whole
negotiation reads in one place.

Unlike the merchant, this agent runs as a single continuous turn. It is not
answering messages; it is conducting an errand, and it decides for itself how
many exchanges that takes. The loop lives inside Strands — every call to
`talk_to_merchant` is one tool call in one long agent turn.

The wallet is built here, from the class rather than from the errand flow's
module-level singleton, and it is the only thing in this package that can stop
money moving.
"""

from __future__ import annotations

import re

from strands import Agent

from agent.a2a.buyer_tools import BuyerSession, build_tools
from agent.a2a.claims import denied_near
from agent.a2a.config import a2a_settings
from agent.a2a.merchant_client import MerchantConnection
from agent.a2a.models import MissingApiKey, build_model, credentials_ready
from agent.a2a.prompts import buyer_prompt
from agent.a2a.protocol import COMPLETED, FAILED, event_error, event_status
from agent.a2a.tasks import ConsoleRun
from agent.a2a.trace import run_turn
from agent.reasoning import DropReasoningContent
from agent.wallet import Wallet


def _errand(payload) -> str:
    """The instruction as the buyer's model should receive it.

    The wallet is already in the system prompt; what goes here is the job. Kept
    separate so a change to how spending authority is described cannot
    accidentally rewrite what was ordered.
    """
    return (
        f"{payload.instruction.strip()}\n\n"
        "Start by reading the merchant's card, then negotiate the order. "
        "Verify the order with the restaurant before you report back."
    )


# --------------------------------------------------------------------------- #
# Keeping the buyer's report true
# --------------------------------------------------------------------------- #
# The merchant's words are checked against its basket before they are sent. This
# is the same check on the other side, and it guards the more dangerous sentence
# of the two: the merchant lying reaches one agent, which has tools to find out.
# The buyer's report reaches a person, who has nothing to check it against and
# every reason to believe it.
#
# Observed on llama.cpp with qwen3-4b-instruct-2507: three tool calls — discover,
# talk, talk — and then "The order was confirmed and payment authorized. The
# receipt shows Rs 609.50. After verifying with the restaurant's records, the
# order matches." No order was placed, nothing was charged, and `verify_order`
# was never called.

#: "payment was authorized", "I paid", "the receipt shows", "Rs 609.50 was
#: charged". Deliberately broad: this only ever runs when the wallet says
#: nothing was spent, so the cost of matching too much is one wasted turn and
#: the cost of matching too little is an operator told they bought dinner.
_CLAIMS_PAID = re.compile(
    r"\bpayment\s+(?:was\s+|is\s+)?(?:authoris|authoriz)ed\b"
    r"|\b(?:authoris|authoriz)ed\s+(?:the\s+)?payment\b"
    r"|\bwas\s+paid\b|\bi\s+paid\b|\bpaid\s+for\s+it\b"
    r"|\b(?:was\s+|were\s+)?charged\b"
    r"|\breceipt\s+(?:shows|says|confirms)\b",
    re.IGNORECASE,
)

#: "the order was confirmed", "order number 495".
_CLAIMS_ORDER_PLACED = re.compile(
    r"\border\s+(?:was\s+|has\s+been\s+)?(?:confirmed|placed)\b"
    r"|\bconfirmed\s+the\s+order\b"
    r"|\border\s+number\s+\S+",
    re.IGNORECASE,
)

#: "I verified with the restaurant", "the records match the receipt".
_CLAIMS_VERIFIED = re.compile(
    r"\bverif(?:ied|ying)\b[^.!?]{0,60}\b(?:restaurant|record|order)\b"
    r"|\brecords?\b[^.!?]{0,40}\bmatch(?:es|ed)?\b",
    re.IGNORECASE,
)

_CORRECT_REPORT = (
    "Stop. Do not write that report — every load-bearing sentence in it is "
    "false, and it is about to be read by the person who sent you out.\n\n"
    "What your wallet and your tools actually say happened: {facts}\n\n"
    "You described steps you never took. Saying a thing is not doing it, and "
    "the tools are the only record of what was done. If you still want to "
    "finish the errand, take the steps for real, one call at a time, waiting "
    "for each result: `talk_to_merchant` to ask it to confirm the order, then "
    "`authorize_payment`, then `verify_order`. If you cannot finish it, say so "
    "plainly and say what stopped you. Either way the next thing you write must "
    "describe only what the tools returned."
)


def _facts(session: BuyerSession) -> str:
    """What actually happened, in the words the report should have used."""
    from agent.wallet import rupees

    parts = []
    order = session.receipt.get("orderNumber") or session.firm_quote.get("orderNumber")
    parts.append(f"order {order} is on the restaurant's books" if order else "no order exists")
    parts.append(
        f"{rupees(session.wallet.spent)} has been charged"
        if session.paid
        else "nothing has been paid"
    )
    if not session.receipt:
        parts.append("no receipt was ever sent")
    return "; ".join(parts) + "."


def _unbacked_report(said: str, session: BuyerSession) -> str | None:
    """A correction for a report that describes an errand that did not happen.

    Bounded at one, like the merchant's. A model that invents a receipt once
    will usually take the steps when told the record disagrees; one that invents
    a second time is handled below, where the facts stop being negotiable.
    """
    text = (said or "").strip()
    if not text:
        return None

    for pattern, untrue in (
        (_CLAIMS_PAID, not session.paid),
        (_CLAIMS_ORDER_PLACED, not session.firm_quote and not session.receipt),
        (_CLAIMS_VERIFIED, not session.verified),
    ):
        match = pattern.search(text)
        if untrue and match and not denied_near(text, match):
            return _CORRECT_REPORT.format(facts=_facts(session))
    return None


def _truthful(said: str, session: BuyerSession) -> str:
    """The report, with the record put in front of it if it still disagrees.

    The last line of defence, and the one that does not ask the model for
    anything. A corrected agent that goes back and pays leaves this a no-op; an
    agent that writes the same invented receipt twice gets its report kept —
    losing it would hide whatever true detail is in there — under a heading that
    says which parts of it the tools do not support. An operator reading "paid"
    when nothing was paid is the one outcome this whole file exists to prevent.
    """
    if not said or not _unbacked_report(said, session):
        return said
    return (
        "[the agent's report below claims steps its tools never took — "
        f"what actually happened: {_facts(session)}]\n\n{said}"
    )


async def run_errand(run: ConsoleRun, payload) -> None:
    """Send the buyer out, and narrate what happens into `run.stream`."""
    run.status = "running"
    run.stream.emit(event_status("running"))

    # This errand's spending authority, and nothing else's. The `Wallet` class
    # is shared with the errand flow on 8100; the `wallet` singleton next to it
    # deliberately is not.
    wallet = Wallet()
    wallet.reset(payload.couponCode, payload.cashLimit, payload.customerId)
    run.wallet = wallet.summary()

    # The connection carries the run's id as the buyer id, so the conversation it
    # opens can be traced back to the errand that started it. The merchant's
    # handover is the one place that matters: it reads the drop off this run
    # rather than being told an address in a message.
    session = BuyerSession(
        run=run, wallet=wallet, merchant=MerchantConnection(buyer_id=run.id)
    )

    try:
        ready, problem = credentials_ready(
            a2a_settings.buyer_provider, a2a_settings.buyer_model, "buyer"
        )
        if not ready:
            raise MissingApiKey(problem or "The buyer has no usable model credentials.")

        agent = Agent(
            model=build_model(
                a2a_settings.buyer_provider,
                a2a_settings.buyer_model,
                a2a_settings.max_tokens,
            ),
            tools=build_tools(session),
            system_prompt=buyer_prompt(wallet),
            name="friends-kitchen-buying-agent",
            description="Buys food from another agent, out of a wallet it cannot exceed.",
            callback_handler=None,
            hooks=[DropReasoningContent()],
        )

        said = await run_turn(agent, _errand(payload), "buyer", run.stream)

        # One chance to make the report true, on the same terms as the
        # merchant's: a claim the session does not back buys the agent one more
        # turn to go and make it so.
        correction = _unbacked_report(said, session)
        if correction:
            said = await run_turn(agent, correction, "buyer", run.stream) or said

        run.final_text = _truthful(said, session) or _fallback_report(session)
        run.merchant_task_id = session.merchant.task_id
        run.status = "done"
        run.stream.emit(
            {
                "type": "final",
                "text": run.final_text,
                "wallet": wallet.summary(),
                "paid": session.paid,
                "orderNumber": session.receipt.get("orderNumber"),
            }
        )
        run.stream.emit(event_status(COMPLETED))

    except Exception as exc:  # noqa: BLE001 — the console needs a sentence, not a traceback
        run.merchant_task_id = session.merchant.task_id
        run.status = "failed"
        run.error = _explain(exc)
        run.stream.emit(event_error(run.error))

        # A run can die *after* the money has moved — a provider's daily token
        # budget running out between paying and reporting is the ordinary way
        # that happens. Reporting only the error would hide a completed
        # purchase behind a red banner, so the facts go out either way.
        run.final_text = _fallback_report(session)
        run.stream.emit(
            {
                "type": "final",
                "text": run.final_text,
                "wallet": wallet.summary(),
                "paid": session.paid,
                "orderNumber": session.receipt.get("orderNumber"),
                "afterError": True,
            }
        )
        run.stream.emit(event_status(FAILED))

    finally:
        run.wallet = wallet.summary()


def _fallback_report(session: BuyerSession) -> str:
    """A report assembled from the facts, for when the agent gives none.

    A model that talks itself into a corner — usually on discovering it cannot
    afford what it just agreed to — sometimes stops without saying anything at
    all. An empty report is the worst possible outcome of that, because the
    silence is indistinguishable from success and a confirmed unpaid order is
    exactly the thing someone needs to be told about. So the facts get written
    out here instead, and labelled as not coming from the agent.
    """
    from agent.wallet import rupees

    wallet = session.wallet
    if session.paid:
        return (
            f"[no report from the agent] Order {session.receipt.get('orderNumber')} was "
            f"paid: {rupees(float(session.receipt.get('charged', {}).get('amount', 0)))} "
            f"charged, {rupees(wallet.coupon_redeemed)} covered by the coupon."
        )

    order = session.firm_quote.get("orderNumber")
    if order:
        due = session.firm_quote.get("amountDue", {}).get("text", "an unknown amount")
        return (
            f"[no report from the agent] Order {order} was confirmed with the restaurant "
            f"and NOT paid — {due} is outstanding, against a cash limit of "
            f"{rupees(wallet.spend_limit)}. Someone needs to settle or cancel it."
        )

    return (
        "[no report from the agent] Nothing was ordered and nothing was paid. "
        "The negotiation ended without a confirmed order."
    )


def _explain(exc: Exception) -> str:
    """Say what went wrong in a sentence the operator can act on.

    A run dies most often at the provider, not in the tools — a free tier out of
    tokens, a key with no credit — and with two agents sharing one key that is
    now twice as likely. Those arrive as one useful sentence wrapped in a
    stringified error body, and putting the whole body on screen buries it.
    """
    import ast
    import re
    from contextlib import suppress

    text = str(exc)

    # OpenAI-compatible clients (Groq included) stringify failures as
    # "Error code: 429 - {'error': {...}}" — a Python repr, not JSON.
    match = re.search(r"Error code: \d+ - (\{.*\})\s*$", text, re.DOTALL)
    if match:
        with suppress(Exception):
            error = ast.literal_eval(match.group(1))["error"]
            message = error.get("message")
            if message and error.get("code") == "rate_limit_exceeded":
                return (
                    f"{message} This budget is per model, and A2A runs two agents: "
                    "put the buyer and the merchant on different providers with "
                    "A2A_BUYER_PROVIDER and A2A_MERCHANT_PROVIDER, or wait it out."
                )
            if message:
                return message

    return text
