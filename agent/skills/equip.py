"""Giving an agent its skills — one call, used identically by all four.

This repo runs four agents in four processes: the ordering agent, the A2A buyer
and merchant, and the courier dispatcher. The standard only means anything if
they all get their skills the same way, so they all call this.

Purely additive. An agent gains four tools and a paragraph naming the skills it
is entitled to; it loses nothing, and an errand that needs no skill never calls
one. With no skills installed, `equip` hands back exactly what it was given —
which is what makes it safe to put in an agent before anybody has written a
skill for it.
"""

from __future__ import annotations

from agent.skills.loader import library
from agent.skills.prompt import skills_brief
from agent.skills.tools import skill_tools_for


def equip(tools: list, system_prompt: str, agent: str | None = None) -> tuple[list, str]:
    """Add the skill tools and the skills brief to an agent being built.

    Args:
        tools: The agent's tools. Not mutated — a new list comes back.
        system_prompt: The agent's brief, which the skills paragraph is
            appended to.
        agent: This agent's short name, matched against each skill's
            `metadata.agents`. A skill that names no agents goes to all of
            them; None means "give me everything installed".

    Returns:
        `(tools, system_prompt)` to pass straight to `Agent(...)`.

        >>> tools, prompt = equip(tools, prompt, agent="ordering")
    """
    available = library().for_agent(agent)
    if not available:
        return list(tools), system_prompt
    return [*tools, *skill_tools_for(agent)], system_prompt + skills_brief(available)
