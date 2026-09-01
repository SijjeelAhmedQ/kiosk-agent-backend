"""Shared plumbing for the Friends Kitchen skill tree.

Not a skill — the leading underscore on the parent directory says so, and a
spec-compliant discoverer looks for `SKILL.md` and will not find one here.

What lives here is the small amount of code that every skill's scripts would
otherwise each carry a copy of: where the four services listen, how to talk to
them, and how to read a `SKILL.md`. None of it reaches into the application.
The skills are an orchestration layer over the running system, so the scripts
call the same HTTP endpoints and the same entry points a person or the control
panel would, and the agent package underneath is left exactly as it is.
"""

from __future__ import annotations

__all__ = ["discovery", "frontmatter", "http", "services", "spec"]
