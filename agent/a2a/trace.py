"""Turning a Strands run into events a transcript can render.

The errand service on 8100 does this too, in `server.py`. That version cannot be
imported — importing it would build the 8100 FastAPI app as a side effect and
tie this service's lifetime to that one's module — so the small parsing helpers
are repeated here, and the useful part is new: `run_turn` is shared by *both*
agents, so a buyer's tool call and a merchant's tool call look identical in the
console apart from who made it.

One deliberate omission: streamed text deltas are not emitted. Both agents'
words reach the transcript as whole messages, and a delta stream on top of that
renders every sentence twice.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from agent.a2a.protocol import Role, event_tool, event_tool_result
from agent.a2a.tasks import Stream

# A browse of the whole menu hands back forty products, and a timeline row shows
# the first few of any list. The rest is bandwidth spent on something nobody sees.
_LIST_CAP = 6


def _shrink(value: Any) -> Any:
    """Cut a tool result down to what a timeline row can hold.

    Lists are capped and long strings clipped. Counts that travel alongside a
    list (`matched`, `itemCount`) are left alone, so the console can still say
    "and 34 more" off a list it only received the head of.
    """
    if isinstance(value, dict):
        return {key: _shrink(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink(item) for item in value[:_LIST_CAP]]
    if isinstance(value, str):
        return value[:400]
    return value


def _detail(payload: Any) -> dict | None:
    """A tool's return value as a dict the console can read field by field.

    Strands puts tool results on the wire as text, so a tool that returned a
    dict arrives here as a JSON string. Parsing it back is what lets a row say
    "Zinger Burger x 2 — Rs 1,060" rather than printing JSON at the operator.
    """
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{"):
            with suppress(json.JSONDecodeError):
                payload = json.loads(text)
    return _shrink(payload) if isinstance(payload, dict) else None


def _summary(result: Any) -> tuple[bool, str]:
    """Boil a tool's return value down to a line of status text.

    Also the channel a refusal comes down: a coupon the restaurant will not
    honour says why here.
    """
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False, str(result.get("error", "failed"))
        for key in ("orderNumber", "quoteSent", "matched", "added", "redeemed", "valid"):
            if key in result:
                return True, f"{key}: {result[key]}"
        return True, "ok"
    return True, str(result)[:200]


def extract_tool_results(message: dict) -> list[dict]:
    """Pull toolResult blocks out of a Strands message.

    Tool results come back on the *user* turn — that is how the agent loop feeds
    them to the model — so it is the only place the outcome of a call is visible.
    """
    out = []
    for block in message.get("content", []) or []:
        result = block.get("toolResult") if isinstance(block, dict) else None
        if not result:
            continue
        payload: Any = None
        for item in result.get("content", []) or []:
            if "json" in item:
                payload = item["json"]
                break
            if "text" in item:
                payload = item["text"]
                break
        detail = _detail(payload)
        ok, summary = _summary(detail if detail is not None else payload)
        if result.get("status") == "error":
            ok = False
        out.append(
            {
                "toolUseId": result.get("toolUseId"),
                "ok": ok,
                "summary": summary,
                "detail": detail,
            }
        )
    return out


async def run_turn(agent, prompt: str, speaker: Role, stream: Stream) -> str:
    """Run one agent turn, narrating its tool calls. Returns the final text."""
    announced: set[str] = set()
    final = ""

    async for event in agent.stream_async(prompt):
        use = event.get("current_tool_use") or {}
        tool_id = use.get("toolUseId")
        if tool_id and tool_id not in announced and use.get("name"):
            announced.add(tool_id)
            stream.emit(event_tool(speaker, tool_id, use["name"]))

        message = event.get("message")
        if isinstance(message, dict):
            for result in extract_tool_results(message):
                stream.emit(event_tool_result(speaker, **result))

        result = event.get("result")
        if result is not None:
            final = str(result).strip()

    return final
