"""Running a skill's script, which is the half that makes it a standard.

A skill's Markdown tells an agent *what* to do; the model still decides how to
do it. The script is the part that does not vary. Ask a model for 2 + 2 and it
answers 4 nearly every time; ask it for `Rs 1,093 - 3 + 16% tax` across a
forty-step errand and the answer is whatever it improvises that run. So where a
task has a defined procedure, the procedure lives in `scripts/` and is executed
rather than reproduced from memory.

Four properties this runner guarantees, because they are what make the result
the same from a laptop and from a container:

* Python scripts run under `sys.executable` — the interpreter already running
  this agent, not whatever `python` happens to mean on the box.
* The working directory is the skill root, so `assets/report.md` resolves the
  same way everywhere, and `PYTHONPATH` carries this repo so a script may
  import the modules that already do the work.
* Output is captured as UTF-8 with replacement. A Windows console defaults to
  cp1252 and this repo's figures are full of rupee signs and em dashes.
* Every run is bounded by a timeout and an output cap, so a script that hangs
  or prints a megabyte fails the errand's step instead of the process.

The interpreter is chosen by extension rather than by the executable bit or a
shebang: Windows has neither, and "runs on my machine but not in CI" is the
failure this whole standard exists to prevent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent.skills.loader import Skill

#: Seconds before a script is killed, unless `AGENT_SKILL_TIMEOUT` says
#: otherwise. Read per call rather than at import: at import it would be fixed
#: before `.env` was necessarily loaded, which is the kind of setting that
#: reads as configured and quietly is not.
DEFAULT_TIMEOUT = 60

#: How much of a script's output is passed on. Anything past this is a dump,
#: not a result, and a result is what the agent asked for.
MAX_OUTPUT_CHARS = 20_000

#: How to run a file, by extension. `.py` is preferred and is what the house
#: rules ask for — every machine running this agent already has the Python it
#: is running on.
INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable],
    ".js": ["node"],
    ".sh": ["bash"],
    ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
}

#: This repository, so a script can `import agent...` and use the same
#: `rupees()` the tools do rather than a second copy that rounds differently.
AGENT_ROOT = Path(__file__).resolve().parents[2]


class ScriptError(RuntimeError):
    """The script could not be run: bad path, missing interpreter, timeout."""


@dataclass(frozen=True)
class ScriptResult:
    """What came back from one run of one script."""

    exit_code: int
    output: str
    #: The output parsed as JSON when it is JSON. Scripts here are asked to
    #: print JSON precisely so the agent is handed a structured result instead
    #: of prose it has to read back and re-interpret.
    data: object | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _timeout() -> int:
    raw = os.getenv("AGENT_SKILL_TIMEOUT", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def _environment(skill: Skill) -> dict[str, str]:
    """The parent environment, plus the three things a script may rely on."""
    env = dict(os.environ)
    env["SKILL_DIR"] = str(skill.path.resolve())
    env["SKILL_NAME"] = skill.name
    env["AGENT_ROOT"] = str(AGENT_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{AGENT_ROOT}{os.pathsep}{existing}" if existing else str(AGENT_ROOT)
    )
    return env


def run_script(
    skill: Skill,
    script: str,
    args: Sequence[str] = (),
    stdin: str | None = None,
    timeout: int | None = None,
) -> ScriptResult:
    """Run one of a skill's scripts and hand back what it printed.

    Args:
        skill: The loaded skill the script belongs to.
        script: Path to the script, relative to the skill root — normally
            `scripts/something.py`.
        args: Command-line arguments, passed through untouched.
        stdin: Text piped in, for input too large or too structured to pass as
            an argument.
        timeout: Seconds before the script is killed. None takes
            `AGENT_SKILL_TIMEOUT`, or 60.

    Raises:
        ScriptError: The path escapes the skill, does not exist, or names a
            language this machine cannot run.
    """
    try:
        target = skill.resolve(script)
    except ValueError as exc:
        raise ScriptError(str(exc)) from None
    if not target.is_file():
        available = ", ".join(skill.resources().get("scripts", [])) or "none"
        raise ScriptError(
            f"Skill {skill.name!r} has no script {script!r}. It has: {available}."
        )

    interpreter = INTERPRETERS.get(target.suffix.lower())
    if interpreter is None:
        raise ScriptError(
            f"Cannot run {script!r}: no interpreter for {target.suffix!r}. "
            f"Skill scripts may be {', '.join(sorted(INTERPRETERS))} — prefer .py, "
            "which runs the same on every machine this agent is deployed to."
        )

    try:
        completed = subprocess.run(  # noqa: S603 — the path is confined to the skill
            [*interpreter, str(target), *args],
            cwd=str(skill.path),
            env=_environment(skill),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or _timeout(),
        )
    except FileNotFoundError:
        raise ScriptError(
            f"Cannot run {script!r}: {interpreter[0]!r} is not installed on this machine."
        ) from None
    except subprocess.TimeoutExpired:
        raise ScriptError(
            f"{script!r} was still running after {timeout or _timeout()}s and was stopped."
        ) from None
    except OSError as exc:
        raise ScriptError(f"Could not run {script!r}: {exc}") from None

    # stderr matters even on success — a script that warns should be heard —
    # but stdout comes first, because that is where the answer is.
    output = (completed.stdout or "").strip()
    noise = (completed.stderr or "").strip()
    if noise:
        output = f"{output}\n{noise}".strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n… output truncated."

    data: object | None = None
    stdout = (completed.stdout or "").strip()
    if stdout.startswith(("{", "[")):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None

    return ScriptResult(exit_code=completed.returncode, output=output, data=data)
