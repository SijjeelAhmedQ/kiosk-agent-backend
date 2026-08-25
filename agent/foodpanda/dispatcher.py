"""Assembling the dispatcher and running it on one delivery.

Strands supplies the loop — model call, tool dispatch, feed the result back —
so this module only decides which brain, which hands, and which brief. One
agent per job, built when the job arrives and thrown away when it lands: a
dispatcher has nothing worth remembering between deliveries, and an agent that
carried one job's rider into the next would be a bug rather than a memory.

The model client and the tool-call narration are both borrowed from the A2A
package. That is not laziness about layering — `build_model` is the only
provider-agnostic model factory in this codebase, and `run_turn` is what makes
this agent's tool calls render identically to the other two agents' in a
console. A third copy of either would drift.
"""

from __future__ import annotations

from strands import Agent

from agent.a2a.models import MissingApiKey, build_model
from agent.a2a.models import credentials_ready as _a2a_credentials_ready
from agent.a2a.trace import run_turn
from agent.foodpanda import jobs as store
from agent.foodpanda.config import foodpanda_settings as fp
from agent.foodpanda.jobs import DeliveryJob
from agent.foodpanda.prompts import dispatcher_prompt
from agent.foodpanda.tools import build_tools
from agent.reasoning import DropReasoningContent

__all__ = ["MissingApiKey", "credentials_ready", "dispatch"]


def credentials_ready() -> tuple[bool, str | None]:
    """Can the dispatcher actually run? `(ready, what_is_missing)`.

    The check itself is the A2A one — same providers, same keys, same
    which-model-belongs-to-which-vendor trap. Only the *name of the variable to
    fix* differs, so that is the only thing rewritten here. Duplicating the key
    table to change two words in a sentence would guarantee the two copies
    disagree the first time a provider is added.
    """
    ready, problem = _a2a_credentials_ready(fp.provider, fp.model_id, "dispatcher")
    if problem:
        problem = problem.replace("A2A_DISPATCHER_PROVIDER", "MOCK_FOODPANDA_PROVIDER")
        problem = problem.replace("A2A_DISPATCHER_MODEL", "MOCK_FOODPANDA_MODEL")
    return ready, problem


def build_dispatcher(job: DeliveryJob) -> Agent:
    """A dispatcher with hands on exactly one delivery."""
    ready, problem = credentials_ready()
    if not ready:
        raise MissingApiKey(problem or "The dispatcher has no usable model credentials.")

    return Agent(
        model=build_model(fp.provider, fp.model_id, fp.max_tokens, fp.effort),
        tools=build_tools(job),
        system_prompt=dispatcher_prompt(),
        name="foodpanda-delivery-dispatcher",
        description="Takes paid restaurant orders from other agents and delivers them.",
        callback_handler=None,
        hooks=[DropReasoningContent()],
    )


def _briefing(job: DeliveryJob) -> str:
    """What the dispatcher is told when a job lands on its desk.

    Deliberately thin. The request itself is not transcribed into this — it is
    read through a tool, so the coordinates the rider is sent to are the ones
    that arrived over the wire rather than ones a model retyped. A retyped
    latitude is a delivery to the wrong hemisphere and nothing about it looks
    wrong on the way past.
    """
    return (
        f"A delivery request has come in from the Friends Kitchen ordering agent. "
        f"It is job {job.id}, for order {job.order_number}. "
        "Read it, decide on it, and if you take it, run it to the customer."
    )


async def dispatch(job: DeliveryJob) -> None:
    """Run one delivery from request to outcome.

    Owns the two things the agent cannot be trusted to do itself: emitting what
    it said, and making sure the job ends up somewhere terminal. A model that
    stops talking halfway through would otherwise leave the job sitting at
    `accepted` for ever, and the ordering agent politely polling a delivery that
    nobody is riding.
    """
    try:
        agent = build_dispatcher(job)
        job.final_text = await run_turn(agent, _briefing(job), "dispatcher", job.stream)
        job.say(job.final_text)
    except Exception as exc:  # noqa: BLE001 — the restaurant needs a reason, not a traceback
        job.fail(str(exc))
    else:
        if not job.done:
            # It ran out of things to say without finishing the job. Whatever it
            # was thinking, the delivery is not made, and that is what the board
            # and the ordering agent have to be told.
            job.fail(
                f"The dispatcher stopped while the job was still {job.status}. "
                "Nobody is carrying this order."
            )
    finally:
        store.close(job.stream)
