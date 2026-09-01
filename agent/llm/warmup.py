"""Read the unchanging half of the prompt into the local runtime, once.

llama-server keeps the tokens it has already processed and re-uses them when the
next request begins with the same ones. Everything the ordering agent starts an
errand with — its brief and the schemas of its thirteen tools — is now the same
on every run (agent/prompts.py explains the split that made it so), which means
those ~2,000 tokens can be read *before* anybody is waiting on them.

Measured on this machine: with a cold slot the first tool call of an errand
lands about forty seconds in, almost all of it that one read. Warmed, it lands
in about two.

Three properties this deliberately has:

* **Only llama.cpp.** A hosted provider has no slot of ours to warm and would
  be charged for the request. Ollama keeps its own cache and needs no help.
* **Never fatal, never noisy.** A warm-up that fails changes nothing except how
  long the first errand takes, so every failure here is swallowed and the reason
  reported to whoever asked for it.
* **The agent's own prompt and the agent's own tools.** `toolset()` is shared
  with `build_agent` rather than copied, because a warm-up that primes a
  slightly different prompt primes nothing at all — the re-use is a prefix
  match, and one different token in the tool list ends it.
"""

from __future__ import annotations

import threading
from typing import Callable

__all__ = ["warm", "warm_in_background"]

#: What the warm-up asks for. One token, because nothing here wants an answer —
#: the point is the prompt that had to be read before it could be produced.
_ANSWER_TOKENS = 1


def _quiet(message: str) -> None:
    """The default reporter: say nothing."""


def _prefix(mode: str = "api", delivery: bool = True):
    """A built agent, for the prompt and the tool specs it would send.

    Built rather than assembled here, and that matters more than it looks: the
    tool registry normalises a schema on the way in — an empty `required: []`
    that the decorated function's own `tool_spec` does not carry — so specs
    read off the functions render as different text from the ones the agent
    sends, and a prefix that differs by one token is a prefix that is not
    re-used. Asking the agent itself cannot drift from what the agent does.

    Delivery by default, and that is the interesting choice here: the delivery
    tools are *appended* to the ordering ones, so the prompt of a counter errand
    is a prefix of the prompt of a delivery errand. Warming the longer one warms
    both — a counter errand re-reads only the tail it does not share, where
    warming the shorter one would leave a delivery errand to re-read everything
    after the point they diverge.

    Where the customer is does not reach the prompt at all any more, so the
    saved address here is standing in for "this errand has one" and nothing
    else — the agent is built and thrown away without being asked anything.
    """
    from agent import location
    from agent.friends_kitchen_agent import build_agent

    return build_agent(
        mode=mode,
        callback_handler=None,
        deliver_to=location.saved() if delivery else None,
    )


def _sample_errand() -> str:
    """A stand-in errand, for the part of the message that is not the system prompt.

    The prompt an errand sends is the invariant brief, then the tool schemas,
    then this — and re-use runs to the first token that differs, so warming with
    a plausible errand rather than a bare "hello" carries the cache past the
    delivery note and the numbered steps as well. What is left to read when the
    real errand arrives is its coupon line, its cash figure, its address and the
    order itself: a hundred-odd tokens rather than nine hundred.

    Cash and a delivery, because that is the shape of the ordering console's
    own default. A coupon errand still re-uses everything down to the third
    numbered step, which is where the two lists part company — worse than a
    match, far better than starting at the top.
    """
    from agent import location
    from agent.friends_kitchen_agent import errand_message
    from agent.wallet import Wallet

    return errand_message(
        "Order one item from the menu.",
        Wallet(coupon_code=None, spend_limit=1000.0),
        deliver_to=location.saved(),
    )


def warm(report: Callable[[str], None] = _quiet, mode: str = "api") -> bool:
    """Send the errand-independent prompt to llama-server and throw the reply away.

    Returns True when the runtime has read it, False when there was nothing to
    warm (another provider is selected) or the attempt failed — the caller has
    nothing to do differently either way, so this is for logs and tests.
    """
    try:
        from agent.llm import llamacpp_launcher

        if not llamacpp_launcher.selected():
            return False
        if not llamacpp_launcher.reachable():
            report("llama.cpp is not answering yet — nothing to warm.")
            return False

        import httpx

        agent = _prefix(mode)
        model = agent.model

        # Formatted by the same client that will format the real request, so
        # what is read now is byte for byte what the errand will send.
        request = model._format_request(
            [{"role": "user", "content": [{"text": _sample_errand()}]}],
            agent.tool_registry.get_all_tool_specs(),
            agent.system_prompt,
        )
        request["stream"] = False
        request.pop("stream_options", None)
        request["max_tokens"] = _ANSWER_TOKENS

        # The client's own address and its own timeout, rather than a second
        # copy of either: a warm-up that reads a different URL from the one the
        # errand will use warms the wrong server, and the first read of a model
        # is the slowest one there is.
        base = str(model.client.base_url).rstrip("/")
        response = httpx.post(
            f"{base}/v1/chat/completions",
            json=request,
            timeout=model.client.timeout,
        )
        response.raise_for_status()

        usage = (response.json() or {}).get("usage") or {}
        read = usage.get("prompt_tokens")
        report(
            f"llama.cpp warmed — {read} prompt tokens cached."
            if read
            else "llama.cpp warmed."
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a warm-up may never fail a run
        report(f"llama.cpp warm-up skipped: {exc}")
        return False


def warm_in_background(report: Callable[[str], None] = _quiet, mode: str = "api") -> None:
    """`warm()` on a daemon thread, for a caller that must not wait for it.

    The server has one slot, so a warm-up and a real errand do not overlap: an
    errand started while this is in flight queues behind it and then re-uses
    what it read, which is the same total work in the same order. What the
    thread buys is the case the warm-up exists for — nobody has asked for an
    errand yet, and the reading happens in the minutes before they do.
    """
    threading.Thread(
        target=warm,
        args=(report, mode),
        name="llamacpp-warmup",
        daemon=True,
    ).start()
