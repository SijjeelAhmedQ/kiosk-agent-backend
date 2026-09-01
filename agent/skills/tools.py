"""The four tools that let an agent use a skill.

They map onto the spec's three layers of progressive disclosure, plus the one
thing a layer cannot do — execute:

    list_skills        layer 1 — what exists
    open_skill         layer 2 — the instructions
    read_skill_file    layer 3 — a bundled reference or asset
    run_skill_script   the procedure itself, run

They are built per agent rather than defined once, because a skill can name the
agents it belongs to and the courier has no business being handed the ordering
agent's arithmetic. The library is looked up per call rather than captured at
build time, so reloading skills reaches an agent that is already running.

Every result is a dict with an `ok` flag, in the same shape the ordering tools
use: the control panel reads these straight off the wire, and a refusal here
should read like any other refused step rather than like a crash.
"""

from __future__ import annotations

from strands import tool

from agent.skills.loader import Library, SkillNotFound, library
from agent.skills.runner import ScriptError, run_script


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def skill_tools_for(agent: str | None = None) -> list:
    """The skill tools for one agent, closed over the skills it is entitled to.

    Args:
        agent: This agent's short name, matched against each skill's
            `metadata.agents`. A skill that names no agents goes to all of
            them; None here means "give me everything installed".
    """

    def mine() -> Library:
        return library().for_agent(agent)

    @tool
    def list_skills() -> dict:
        """List the skills available for this errand.

        Returns each skill's name, what it is for, and which of its scripts,
        references and templates exist. Use it when you are unsure whether a
        task already has a defined procedure. Call open_skill to read one.
        """
        available = mine()
        if not available:
            return {"ok": True, "count": 0, "skills": [], "note": "No skills are installed."}
        return {
            "ok": True,
            "count": len(available),
            "skills": [
                {**skill.summary(), **skill.resources()} for skill in available
            ],
        }

    @tool
    def open_skill(name: str) -> dict:
        """Read a skill's full instructions before doing the task it covers.

        Returns the instructions and an index of every file the skill bundles.
        Follow them as written rather than doing the work yourself: where they
        name a script, run it with run_skill_script.

        Args:
            name: The skill's name, as listed by list_skills.
        """
        try:
            skill = mine().get(name)
        except SkillNotFound as exc:
            return _fail(str(exc))
        try:
            return {
                "ok": True,
                "skill": skill.name,
                "description": skill.description,
                "instructions": skill.instructions(),
                **skill.resources(),
            }
        except OSError as exc:
            return _fail(f"Could not read skill {name!r}: {exc}")

    @tool
    def read_skill_file(skill: str, path: str) -> dict:
        """Read one file bundled with a skill — a reference document or a template.

        Only for files the skill's own instructions send you to; there is no
        reason to read one otherwise.

        Args:
            skill: The skill's name.
            path: The file's path relative to the skill, e.g.
                `assets/report.template.md`. Take it from the skill's
                instructions or its file list.
        """
        try:
            found = mine().get(skill)
        except SkillNotFound as exc:
            return _fail(str(exc))
        try:
            return {
                "ok": True,
                "skill": found.name,
                "file": path,
                "content": found.read_file(path),
            }
        except (FileNotFoundError, ValueError) as exc:
            return _fail(str(exc))
        except OSError as exc:
            return _fail(f"Could not read {path!r} from skill {skill!r}: {exc}")

    @tool
    def run_skill_script(
        skill: str,
        script: str,
        args: list[str] | None = None,
        stdin: str = "",
    ) -> dict:
        """Run a skill's script and use what it returns.

        The script's output is the answer. Do not compute the same thing
        yourself and report that instead, and do not adjust what it returns — a
        script exists where the result has to be identical on every run. If it
        fails, report what it said rather than doing the task by hand.

        Args:
            skill: The skill's name.
            script: The script's path relative to the skill, e.g.
                `scripts/order_math.py`.
            args: Command-line arguments, exactly as the skill's instructions
                describe them.
            stdin: Text to pipe in, for input too large or too structured to
                pass as an argument.
        """
        try:
            found = mine().get(skill)
        except SkillNotFound as exc:
            return _fail(str(exc))

        try:
            result = run_script(found, script, args or [], stdin or None)
        except ScriptError as exc:
            return _fail(str(exc))

        if not result.ok:
            return {
                "ok": False,
                "error": result.output or f"{script} exited {result.exit_code}.",
                "skill": found.name,
                "script": script,
                "exitCode": result.exit_code,
            }

        answer: dict = {
            "ok": True,
            "skill": found.name,
            "script": script,
            "exitCode": result.exit_code,
            "output": result.output,
        }
        # A script that prints JSON is handing over a structured result. Pass it
        # through as one rather than making the agent parse its own tool output.
        if isinstance(result.data, dict):
            answer["result"] = result.data
        elif result.data is not None:
            answer["result"] = {"value": result.data}
        return answer

    return [list_skills, open_skill, read_skill_file, run_skill_script]


#: Every skill installed, for a caller that has no agent name to scope by.
SKILL_TOOLS = skill_tools_for()
