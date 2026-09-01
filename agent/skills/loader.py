"""Finding skills on disk, and handing them out one layer at a time.

Progressive disclosure is the shape of the format, so it is the shape of this
module:

1. `Skill.summary()` — name and description only, for every skill that exists.
   This is what goes into the system prompt at startup, so it has to stay
   small; a dozen skills is a couple of hundred tokens.
2. `Skill.instructions()` — the whole `SKILL.md` body, read when the agent has
   decided the skill is relevant.
3. `Skill.read_file()` — a bundled reference or asset, read only when the
   instructions send the agent to it.

Skills are looked for in `AGENT_SKILLS_PATH` first (a `PATH`-style list, so a
deployment can add a shared skill library without moving this repo's own), then
this repo's `skills/`. Duplicates are dropped so the same skill is not reported
twice when a path names the default explicitly.

A malformed skill is collected as an error and skipped rather than raised over:
one person's broken `SKILL.md` should not stop everybody else's agent from
starting. `skills/_shared/validate_skills.py` is where a broken skill is meant
to be fatal, and that is what runs in CI.

Nothing here imports `skills/_shared`. That library is written for skill
scripts, which run under whatever Python is to hand and must not need this
repository importable; this module is the other direction — the application
reading `skills/` as data — and the two stay independent on purpose. Delete
`skills/` and every function here still answers, with an empty library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

#: The file that makes a directory a skill.
SKILL_FILE = "SKILL.md"

#: The directory names the specification gives a meaning to. A skill may hold
#: anything else besides; these are the three worth listing separately, because
#: an agent asking "what can I run" means `scripts/`.
CONVENTIONAL_DIRS = ("scripts", "references", "assets")

#: This repo's own skill tree: `<repo>/skills`.
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "skills"

#: Names that are directories in a skill tree but never skills themselves.
_PRIVATE_PREFIXES = ("_", ".")


class SkillNotFound(LookupError):
    """No skill of that name is available to this agent."""


class SkillFormatError(ValueError):
    """A `SKILL.md` that cannot be read far enough to be catalogued."""


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------
# A deliberately small parser rather than PyYAML, and not out of dependency
# squeamishness: `version: 1.0` is a float to YAML and `owner: yes` is a
# boolean, while the spec says every value in a `SKILL.md` is a string — so a
# YAML parser quietly accepts files another implementation would reject. The
# subset below is the spec's own example syntax, which makes "what this parser
# accepts" a readable answer to "how should a skill be written".

_BLOCK = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):\s*(?P<style>[|>])(?P<chomp>[+-]?)\s*$")
_PAIR = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):\s*(?P<value>.*)$")


def _scalar(raw: str) -> str:
    """One value, with its quotes taken off if it wore any."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split a `SKILL.md` into its metadata block and its instructions.

    Accepts `key: value` (bare or quoted), one level of nested map — which is
    what `metadata:` is — and `|` / `>` block scalars for the long
    descriptions. Anything else raises, naming the line: a file with one bad
    field still parses far enough to say which field is bad.

    A byte-order mark is tolerated. Windows editors add one without being
    asked, and a skill that fails to load over an invisible character is a bad
    first experience.
    """
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(
            "SKILL.md must begin with a frontmatter block opened by `---` on the first line."
        )

    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillFormatError("The frontmatter block is never closed by `---`.") from None

    data: dict[str, object] = {}
    nested: dict[str, str] | None = None
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        block = _BLOCK.match(line)
        if block:
            gathered: list[str] = []
            while index < end and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                gathered.append(lines[index].strip())
                index += 1
            joined = (
                "\n".join(gathered)
                if block["style"] == "|"
                else " ".join(part for part in gathered if part)
            )
            data[block["key"]] = joined.strip() if block["chomp"] == "-" else joined
            nested = None
            continue

        pair = _PAIR.match(line)
        if not pair:
            raise SkillFormatError(f"line {index}: expected `key: value`, got {line.strip()!r}")

        key, value = pair["key"], _scalar(pair["value"])
        if pair["indent"]:
            if nested is None:
                raise SkillFormatError(f"line {index}: unexpected indentation")
            nested[key] = value
            continue

        if value == "":
            nested = {}
            data[key] = nested
        else:
            nested = None
            data[key] = value

    return data, "\n".join(lines[end + 1 :]).strip()


# ---------------------------------------------------------------------------
# One skill
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Skill:
    """One skill directory, read as far as it has been asked for."""

    path: Path
    name: str
    description: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def agents(self) -> tuple[str, ...]:
        """The agents this skill belongs to, or `()` meaning every one of them.

        A skill may name them in `metadata.agents` as a comma-separated list —
        `agents: "ordering, buyer"`. Scoping exists because a skill an errand
        cannot use is a step the errand can waste: the courier has no coupons
        and should not be told how to work out their coverage. Naming none is
        the right default for a skill about a reporting format or the standard
        itself, which applies wherever it is read.
        """
        raw = self.metadata.get("agents", "")
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def belongs_to(self, agent: str | None) -> bool:
        """Is this skill in `agent`'s library? None asks for everything."""
        return agent is None or not self.agents or agent in self.agents

    def summary(self) -> dict[str, str]:
        """Layer 1 — the hundred tokens an agent holds from startup."""
        return {"name": self.name, "description": self.description}

    def instructions(self) -> str:
        """Layer 2 — the whole `SKILL.md` body."""
        _, body = parse_frontmatter((self.path / SKILL_FILE).read_text(encoding="utf-8"))
        return body

    def resources(self) -> dict[str, list[str]]:
        """What is on disk under the conventional directories, by relative path.

        Paths, not contents. This is the index an agent reads to decide what to
        open; opening it is a separate act and a later one.
        """
        found: dict[str, list[str]] = {}
        for directory in CONVENTIONAL_DIRS:
            root = self.path / directory
            if not root.is_dir():
                continue
            files = sorted(
                str(item.relative_to(self.path)).replace("\\", "/")
                for item in root.rglob("*")
                if item.is_file()
                and item.name != "__init__.py"
                and "__pycache__" not in item.parts
            )
            if files:
                found[directory] = files
        return found

    def resolve(self, relative: str) -> Path:
        """One of this skill's own files, by path relative to the skill root.

        Confined to the skill directory: a path that climbs out with `..` or a
        symlink is refused rather than followed. A skill is a unit of trust,
        and it does not get to read the rest of the machine through this door.
        """
        target = (self.path / relative).resolve()
        root = self.path.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"{relative!r} is outside the skill directory.")
        return target

    def read_file(self, relative: str) -> str:
        """Layer 3 — one bundled file, read only when something asks for it."""
        target = self.resolve(relative)
        if not target.is_file():
            raise FileNotFoundError(f"{relative!r} is not a file in skill {self.name!r}.")
        return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Library:
    """Every skill this process can see, and whatever would not load."""

    skills: tuple[Skill, ...] = ()
    errors: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Skill]:
        return iter(self.skills)

    def __len__(self) -> int:
        return len(self.skills)

    def __bool__(self) -> bool:
        return bool(self.skills)

    def for_agent(self, agent: str | None) -> "Library":
        """The subset one agent is entitled to. See `Skill.agents`."""
        return Library(tuple(s for s in self.skills if s.belongs_to(agent)), self.errors)

    def get(self, name: str) -> Skill:
        for skill in self.skills:
            if skill.name == name:
                return skill
        known = ", ".join(s.name for s in self.skills) or "none"
        raise SkillNotFound(f"No skill named {name!r}. Available: {known}.")

    def names(self) -> list[str]:
        return [skill.name for skill in self.skills]


def roots(paths: Iterable[Path | str] | None = None) -> list[Path]:
    """Every directory to look in, in precedence order."""
    if paths is None:
        configured = os.getenv("AGENT_SKILLS_PATH", "").strip()
        found = [Path(p) for p in configured.split(os.pathsep) if p.strip()]
        found.append(DEFAULT_ROOT)
    else:
        found = [Path(p) for p in paths]

    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in found:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def discover(paths: Iterable[Path | str] | None = None) -> Library:
    """Read every skill tree and catalogue what is in it, metadata only."""
    skills: list[Skill] = []
    errors: list[str] = []
    claimed: set[str] = set()

    for root in roots(paths):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name[:1] in _PRIVATE_PREFIXES:
                continue
            manifest = entry / SKILL_FILE
            if not manifest.is_file():
                continue
            try:
                data, _ = parse_frontmatter(manifest.read_text(encoding="utf-8"))
            except (OSError, SkillFormatError) as exc:
                errors.append(f"{entry.name}: {exc}")
                continue

            name = str(data.get("name") or entry.name)
            if name in claimed:
                # An earlier root wins, which is what makes AGENT_SKILLS_PATH
                # an override rather than a second listing of the same skill.
                continue
            raw = data.get("metadata")
            metadata = (
                {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
            )
            claimed.add(name)
            skills.append(
                Skill(
                    path=entry,
                    name=name,
                    description=str(data.get("description") or ""),
                    metadata=metadata,
                )
            )

    return Library(tuple(skills), tuple(errors))


_cached: Library | None = None


def library() -> Library:
    """Every skill on disk, discovered once and kept.

    Cached because the alternative is re-reading the whole skill tree on every
    tool call, and a skill is a file on disk that does not change under a
    running errand. `reload()` is there for the times it does — dropping a
    skill in without restarting the service.
    """
    global _cached
    if _cached is None:
        _cached = discover()
    return _cached


def reload() -> Library:
    """Read the skill trees again. Returns the fresh library."""
    global _cached
    _cached = discover()
    return _cached
