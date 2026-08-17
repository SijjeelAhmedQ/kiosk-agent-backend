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

from strands import Agent

from agent.a2a.buyer_tools import BuyerSession, build_tools
from agent.a2a.config import a2a_settings
from agent.a2a.merchant_client import MerchantConnection
from agent.a2a.models import MissingApiKey, build_model, credentials_ready
from agent.a2a.prompts import buyer_prompt
from agent.a2a.protocol import COMPLETED, FAILED, event_error, event_status
from agent.a2a.tasks import ConsoleRun
from agent.a2a.trace import run_turn
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
        )

        said = await run_turn(agent, _errand(payload), "buyer", run.stream)
        run.final_text = said or _fallback_report(session)
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
