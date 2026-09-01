"""Finding the skills in this tree, and loading them progressively.

Progressive disclosure is the whole shape of the format, so it is the shape of
this module too:

* `catalogue()` reads only `name` and `description` — the roughly hundred
  tokens per skill that an agent is expected to hold from startup.
* `load()` adds the body, for the one skill that turned out to be relevant.
* Everything under `scripts/`, `references/` and `assets/` stays on disk until
  something asks for it by path. `Skill.resources()` lists what is there
  without reading any of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter as fm
from . import spec

SKILL_FILE = "SKILL.md"

#: The directory names the specification gives a meaning to. A skill may hold
#: anything else besides; these are the three that are worth listing separately
#: because a caller looking for "what can I run" means `scripts/`.
CONVENTIONAL_DIRS = ("scripts", "references", "assets")


@dataclass
class Skill:
    """One skill directory."""

    path: Path
    name: str
    description: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str | None = None

    @property
    def directory_name(self) -> str:
        return self.path.name

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
                if item.is_file() and item.name != "__init__.py"
            )
            if files:
                found[directory] = files
        return found

    def read(self, relative: str) -> str:
        """One resource, by its path relative to the skill root.

        Refuses to leave the skill directory. A skill's own files are the only
        thing a reference in a SKILL.md is allowed to mean.
        """
        target = (self.path / relative).resolve()
        root = self.path.resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"{relative!r} is outside the skill directory.")
        return target.read_text(encoding="utf-8")

    def validate(self) -> spec.Report:
        return spec.validate(self.frontmatter, self.directory_name, self.body)


def skills_root() -> Path:
    """The root of this skill tree — the `skills/` directory two levels up."""
    return Path(__file__).resolve().parents[2]


def iter_skill_dirs(root: Path | str | None = None) -> Iterator[Path]:
    """Every directory under `root` that holds a SKILL.md.

    Directories whose name starts with `_` or `.` are skipped, which is what
    keeps `_shared/` out of the catalogue without it having to be named here.
    """
    base = Path(root) if root is not None else skills_root()
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name[:1] in ("_", "."):
            continue
        if (entry / SKILL_FILE).is_file():
            yield entry


def catalogue(root: Path | str | None = None) -> list[Skill]:
    """Every skill, metadata only — the startup view.

    A skill whose frontmatter will not parse is left out rather than raised
    over: one broken skill should not make the other four undiscoverable. Run
    `validate_skills.py` to be told about it.
    """
    found: list[Skill] = []
    for directory in iter_skill_dirs(root):
        try:
            data, _ = fm.read(directory / SKILL_FILE)
        except (OSError, fm.FrontmatterError):
            continue
        found.append(
            Skill(
                path=directory,
                name=str(data.get("name", directory.name)),
                description=str(data.get("description", "")),
                frontmatter=data,
            )
        )
    return found


def load(name: str, root: Path | str | None = None) -> Skill:
    """One skill in full — frontmatter and body.

    Args:
        name: The skill's `name`, which is also its directory name.

    Raises:
        FileNotFoundError: when no skill of that name is in the tree.
    """
    for directory in iter_skill_dirs(root):
        if directory.name != name:
            continue
        data, body = fm.read(directory / SKILL_FILE)
        return Skill(
            path=directory,
            name=str(data.get("name", directory.name)),
            description=str(data.get("description", "")),
            frontmatter=data,
            body=body,
        )

    known = ", ".join(entry.name for entry in iter_skill_dirs(root)) or "none"
    raise FileNotFoundError(f"No skill named {name!r}. Known skills: {known}.")
