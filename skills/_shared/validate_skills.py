#!/usr/bin/env python3
"""Check every skill in this tree against the Agent Skills specification.

    python skills/_shared/validate_skills.py
    python skills/_shared/validate_skills.py --json
    python skills/_shared/validate_skills.py friends-kitchen-ordering

Exits 0 when every skill is valid and 1 when one is not, so it can be a step in
whatever runs checks here without anything having to read its output.

This is the local stand-in for `skills-ref validate`, which is the reference
implementation and worth running too if it is installed. The rules it applies
are in `fkskills/spec.py`, written out from the specification so that this
repository can answer the question offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fkskills import discovery, terminal  # noqa: E402
from fkskills import frontmatter as fm  # noqa: E402
from fkskills import spec  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate_skills",
        description="Validate the Friends Kitchen skills against agentskills.io/specification.",
    )
    parser.add_argument(
        "skill",
        nargs="*",
        help="Skill directory names to check. Default: every skill in the tree.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="The skills/ directory to read. Default: the one this script lives in.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures.",
    )
    return parser.parse_args()


def check(directory: Path) -> spec.Report:
    """One skill directory, read and validated."""
    try:
        data, body = fm.read(directory / discovery.SKILL_FILE)
    except fm.FrontmatterError as exc:
        return spec.Report(skill=directory.name, errors=[str(exc)])
    except OSError as exc:
        return spec.Report(skill=directory.name, errors=[f"SKILL.md is unreadable: {exc}"])

    report = spec.validate(data, directory.name, body)

    # Beyond the frontmatter: a reference the body names and the tree does not
    # have. The spec has nothing to say about this, and it is the failure a
    # reader actually hits — an agent told to read `references/TOOLS.md` and
    # finding nothing there has no way to recover.
    for missing in _dangling_references(directory, body):
        report.errors.append(f"references a file that is not here: {missing}")

    return report


def _dangling_references(directory: Path, body: str) -> list[str]:
    """Paths under the conventional directories that the body names but that do not exist."""
    import re

    wanted: set[str] = set()
    for pattern in (
        r"\]\(([^)]+)\)",          # markdown links
        r"`((?:scripts|references|assets)/[^`]+)`",   # inline code paths
    ):
        for match in re.findall(pattern, body):
            path = str(match).split("#", 1)[0].strip()
            if path.startswith(discovery.CONVENTIONAL_DIRS):
                wanted.add(path)

    return sorted(path for path in wanted if not (directory / path).exists())


def main() -> int:
    terminal.utf8()
    args = parse_args()
    root = Path(args.root) if args.root else discovery.skills_root()

    directories = list(discovery.iter_skill_dirs(root))
    if args.skill:
        wanted = set(args.skill)
        directories = [entry for entry in directories if entry.name in wanted]
        unknown = wanted - {entry.name for entry in directories}
        if unknown:
            print(f"No such skill: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    if not directories:
        print(f"No skills found under {root}.", file=sys.stderr)
        return 2

    reports = [check(entry) for entry in directories]
    failed = [r for r in reports if not r.ok or (args.strict and r.warnings)]

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "skills": [
                        {"name": r.skill, "ok": r.ok, "errors": r.errors, "warnings": r.warnings}
                        for r in reports
                    ],
                    "ok": not failed,
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    for report in reports:
        mark = "ok  " if report.ok else "FAIL"
        print(f"{mark}  {report.skill}")
        for error in report.errors:
            print(f"        error: {error}")
        for warning in report.warnings:
            print(f"        warn : {warning}")

    print()
    print(f"{len(reports) - len(failed)}/{len(reports)} skills valid.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
