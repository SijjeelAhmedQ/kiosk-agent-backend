"""Reading the YAML frontmatter off a `SKILL.md`.

PyYAML is used when it is importable and a deliberately small parser is used
when it is not. That fallback is the point: adding a dependency to
`requirements.txt` to read a metadata block would be a change to the
application's install, and this layer is supposed to cost the application
nothing. The subset understood by the fallback is exactly the subset the
Agent Skills specification defines — scalars, plus a one-level string map for
`metadata` — so a skill that parses one way parses the same the other way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

DELIMITER = "---"


class FrontmatterError(ValueError):
    """The file has no usable frontmatter block."""


def split(text: str) -> tuple[str, str]:
    """`(frontmatter_text, body)` for one SKILL.md.

    Raises:
        FrontmatterError: when the file does not open with a `---` fence or the
            fence is never closed. Both are the same mistake from the author's
            point of view — there is no metadata to read — so they are one
            exception with a sentence that says which.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    if start >= len(lines) or lines[start].strip() != DELIMITER:
        raise FrontmatterError(
            "SKILL.md must begin with a YAML frontmatter block fenced by `---`."
        )

    for index in range(start + 1, len(lines)):
        if lines[index].strip() == DELIMITER:
            return "\n".join(lines[start + 1 : index]), "\n".join(lines[index + 1 :])

    raise FrontmatterError("The frontmatter block opened with `---` but never closed.")


def parse(text: str) -> dict[str, Any]:
    """The frontmatter of one SKILL.md as a mapping."""
    block, _ = split(text)
    return _load(block)


def read(path: Path) -> tuple[dict[str, Any], str]:
    """`(frontmatter, body)` for a SKILL.md on disk."""
    text = Path(path).read_text(encoding="utf-8")
    block, body = split(text)
    return _load(block), body


def _load(block: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _minimal_load(block)

    data = yaml.safe_load(block) or {}
    if not isinstance(data, dict):
        raise FrontmatterError("The frontmatter must be a mapping of fields to values.")
    return data


def _minimal_load(block: str) -> dict[str, Any]:
    """`key: value`, plus one level of indented map — and nothing else.

    Everything the specification allows in frontmatter fits in that: five scalar
    fields and `metadata`, which is a map from strings to strings. Anything
    richer is not valid frontmatter anyway, so refusing to guess at it here
    turns a silently mis-read skill into a message that names the line.
    """
    data: dict[str, Any] = {}
    current: dict[str, str] | None = None

    for number, raw in enumerate(block.split("\n"), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indented = raw[:1].isspace()
        line = raw.strip()
        if ":" not in line:
            raise FrontmatterError(f"Line {number} of the frontmatter is not `key: value`.")

        key, _, value = line.partition(":")
        key, value = key.strip(), _unquote(value.strip())

        if indented:
            if current is None:
                raise FrontmatterError(f"Line {number} is indented under no field.")
            current[key] = value
            continue

        if value == "":
            current = {}
            data[key] = current
        else:
            current = None
            data[key] = value

    return data


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
