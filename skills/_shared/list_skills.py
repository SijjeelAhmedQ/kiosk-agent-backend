#!/usr/bin/env python3
"""The skill catalogue — what an agent is meant to hold in context from startup.

    python skills/_shared/list_skills.py
    python skills/_shared/list_skills.py --json
    python skills/_shared/list_skills.py --show friends-kitchen-ordering

Without arguments it prints one line per skill: the `name` and `description`,
and nothing else. That is the first stage of progressive disclosure, and the
whole reason a description is required to say *when* to use a skill and not
only what it does — this listing is all an agent has to go on when it decides
whether to open one.

`--show` is the second stage: the full SKILL.md body for one skill, plus an
index of the files under `scripts/`, `references/` and `assets/` that the body
may point at. Those files stay on disk until something reads them by name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fkskills import discovery, terminal  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="list_skills",
        description="List the Friends Kitchen agent skills, or print one in full.",
    )
    parser.add_argument("--root", default=None, help="The skills/ directory to read.")
    parser.add_argument("--show", metavar="NAME", default=None, help="Print one skill in full.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser.parse_args()


def main() -> int:
    terminal.utf8()
    args = parse_args()
    root = Path(args.root) if args.root else None

    if args.show:
        try:
            skill = discovery.load(args.show, root)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if args.json:
            print(
                json.dumps(
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "path": str(skill.path),
                        "frontmatter": skill.frontmatter,
                        "resources": skill.resources(),
                        "body": skill.body,
                    },
                    indent=2,
                )
            )
            return 0

        print(f"# {skill.name}\n")
        print(f"{skill.description}\n")
        print(f"path: {skill.path}\n")
        for directory, files in skill.resources().items():
            print(f"{directory}/")
            for item in files:
                print(f"  {item}")
        print()
        print(skill.body or "")
        return 0

    skills = discovery.catalogue(root)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "path": str(skill.path),
                        "resources": skill.resources(),
                    }
                    for skill in skills
                ],
                indent=2,
            )
        )
        return 0

    if not skills:
        print("No skills found.", file=sys.stderr)
        return 2

    for skill in skills:
        print(f"{skill.name}")
        print(f"    {skill.description}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
