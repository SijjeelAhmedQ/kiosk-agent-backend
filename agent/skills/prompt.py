"""What the agent is told about its skills.

This is layer 1 of progressive disclosure: every skill's name and description
and nothing else, so a dozen skills cost a couple of hundred tokens rather than
a dozen documents. The agent opens the ones it needs.

The rules underneath the list are the point of the whole standard. A model that
reads a skill and then does the task from memory has gained nothing over a
model that never read it — so the brief is explicit that a script named by a
skill is executed, and that its output *is* the answer.
"""

from __future__ import annotations

from agent.skills.loader import Library, library

_HEADER = """\

# Your skills

You have skills: packaged procedures for tasks this team has already decided
how to do. Each one is a folder of instructions, and most carry a script that
performs the task exactly.

{catalogue}

**How to use them.**

1. Before starting a task a skill covers, call `open_skill` with its name and
   follow what it says. The description above is a label, not the instructions.
2. When a skill names a script, run it with `run_skill_script`. Do not work out
   what the script would return and report that instead — the script's output
   *is* the answer, and reproducing it from your own reasoning is the one thing
   these skills exist to prevent.
3. If a skill points you at a file under `references/` or `assets/`, read it
   with `read_skill_file` when you get to the step that needs it.
4. If a script fails, report what it said. Do not fall back to doing the task by
   hand, and do not re-run an unchanged command that has already failed.

A task no skill covers, you handle as you normally would.
"""


def catalogue(skills: Library) -> str:
    """One line per skill: the name an agent calls, and when to call it."""
    return "\n".join(f"- **{skill.name}** — {skill.description}" for skill in skills)


def skills_brief(skills: Library | None = None) -> str:
    """The skills section of a system prompt, or `""` when there are none.

    Empty rather than a "you have no skills" paragraph on purpose: an agent
    told about a capability it does not have spends steps looking for it.
    """
    available = library() if skills is None else skills
    if not available:
        return ""
    return _HEADER.format(catalogue=catalogue(available))
