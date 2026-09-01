"""The Agent Skills specification, as checks.

https://agentskills.io/specification

Written out here rather than taken on trust so that "the skills follow the
spec" is something this repository can answer for itself, on a machine with no
network and no `skills-ref` install. `validate_skills.py` is the CLI over it.

The rules, verbatim from the specification:

* `name` — required. 1-64 characters, lowercase `a-z`, `0-9` and `-` only. May
  not start or end with a hyphen, may not contain `--`, and must match the
  parent directory name.
* `description` — required. 1-1024 characters, non-empty.
* `license` — optional. A license name, or the name of a bundled license file.
* `compatibility` — optional. 1-500 characters.
* `metadata` — optional. A map from string keys to string values.
* `allowed-tools` — optional. A space-separated string of pre-approved tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Fields the specification defines. Anything else is reported as a warning
#: rather than an error: the spec does not forbid extra keys, but an unexpected
#: one is far more often a typo for a real field than a deliberate extension.
KNOWN_FIELDS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")

REQUIRED_FIELDS = ("name", "description")

NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500

#: Not a rule, a recommendation — the spec asks for a body under 500 lines and
#: under about 5000 tokens, because the whole of it is loaded the moment the
#: skill activates. Reported as a warning so a long skill is visible without
#: being a failure.
BODY_MAX_LINES = 500


@dataclass
class Report:
    """What validation found for one skill."""

    skill: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(
    frontmatter: dict[str, Any],
    directory_name: str | None = None,
    body: str | None = None,
) -> Report:
    """Check one skill's frontmatter (and optionally its body) against the spec."""
    report = Report(skill=directory_name or str(frontmatter.get("name", "<unnamed>")))

    for required in REQUIRED_FIELDS:
        if required not in frontmatter:
            report.errors.append(f"`{required}` is required and is missing.")

    for key in frontmatter:
        if key not in KNOWN_FIELDS:
            report.warnings.append(
                f"`{key}` is not a field the specification defines; clients may ignore it."
            )

    _check_name(frontmatter.get("name"), directory_name, report)
    _check_description(frontmatter.get("description"), report)
    _check_compatibility(frontmatter.get("compatibility"), report)
    _check_metadata(frontmatter.get("metadata"), report)
    _check_allowed_tools(frontmatter.get("allowed-tools"), report)
    _check_license(frontmatter.get("license"), report)

    if body is not None:
        lines = len(body.strip().split("\n"))
        if lines > BODY_MAX_LINES:
            report.warnings.append(
                f"The body is {lines} lines; the specification recommends keeping "
                f"SKILL.md under {BODY_MAX_LINES} and moving detail into references/."
            )

    return report


def _check_name(value: Any, directory_name: str | None, report: Report) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        report.errors.append("`name` must be a string.")
        return
    if not 1 <= len(value) <= NAME_MAX:
        report.errors.append(f"`name` must be 1-{NAME_MAX} characters; this one is {len(value)}.")
    if value.startswith("-") or value.endswith("-"):
        report.errors.append("`name` may not start or end with a hyphen.")
    if "--" in value:
        report.errors.append("`name` may not contain consecutive hyphens.")
    if not NAME_PATTERN.match(value):
        report.errors.append(
            "`name` may contain only lowercase letters, digits and single hyphens."
        )
    if directory_name is not None and value != directory_name:
        report.errors.append(
            f"`name` is {value!r} but the directory is {directory_name!r}; they must match."
        )


def _check_description(value: Any, report: Report) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        report.errors.append("`description` must be a string.")
        return
    if not value.strip():
        report.errors.append("`description` must not be empty.")
    if len(value) > DESCRIPTION_MAX:
        report.errors.append(
            f"`description` must be at most {DESCRIPTION_MAX} characters; this one is {len(value)}."
        )


def _check_compatibility(value: Any, report: Report) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        report.errors.append("`compatibility` must be a string.")
        return
    if not 1 <= len(value) <= COMPATIBILITY_MAX:
        report.errors.append(
            f"`compatibility` must be 1-{COMPATIBILITY_MAX} characters; this one is {len(value)}."
        )


def _check_metadata(value: Any, report: Report) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        report.errors.append("`metadata` must be a map from string keys to string values.")
        return
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            report.errors.append(
                f"`metadata.{key}` must be a string value; the specification allows "
                "only strings here, so quote numbers and versions."
            )


def _check_allowed_tools(value: Any, report: Report) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        report.errors.append(
            "`allowed-tools` must be a single space-separated string, not a list."
        )


def _check_license(value: Any, report: Report) -> None:
    if value is not None and not isinstance(value, str):
        report.errors.append("`license` must be a string.")
