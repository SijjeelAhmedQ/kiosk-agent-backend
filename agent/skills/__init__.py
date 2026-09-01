"""Agent Skills for this repo — the standard, and the runtime that runs it.

A skill is a folder of instructions with a scripted component: the Markdown
tells an agent *what* to do, and the script bundled beside it is *how*, so that
a task with a defined procedure produces the same result on every run.
`skills/` is where they live. This package is what turns that folder into
something an agent can actually use:

    from agent.skills import equip, library, skills_brief

    library()                          # every skill on disk, discovered once
    skills_brief()                     # the block that goes in a system prompt
    tools, prompt = equip(tools, prompt, agent="ordering")

The other half of the standard — the catalogue an author reads and the
validator CI runs — is `skills/_shared/list_skills.py` and
`skills/_shared/validate_skills.py`. Those run under any Python and import
nothing from here, because a skill script must not need this repository
importable to do its job.

The specification is at <https://agentskills.io/specification>.
"""

from __future__ import annotations

from agent.skills.equip import equip
from agent.skills.loader import (
    DEFAULT_ROOT,
    Library,
    Skill,
    SkillFormatError,
    SkillNotFound,
    discover,
    library,
    reload,
)
from agent.skills.prompt import skills_brief
from agent.skills.runner import ScriptError, ScriptResult, run_script
from agent.skills.tools import SKILL_TOOLS, skill_tools_for

SPEC_URL = "https://agentskills.io/specification"

__all__ = [
    "DEFAULT_ROOT",
    "SKILL_TOOLS",
    "SPEC_URL",
    "Library",
    "ScriptError",
    "ScriptResult",
    "Skill",
    "SkillFormatError",
    "SkillNotFound",
    "discover",
    "equip",
    "library",
    "reload",
    "run_script",
    "skill_tools_for",
    "skills_brief",
]
