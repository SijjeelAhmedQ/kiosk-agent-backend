"""Filling in the closing summary, so two runs of one errand read the same.

The wording is not here. It is in `assets/report.template.md`, which this reads
at run time — the template is the thing a person edits, and a script that
carried its own copy of the sentences would let the two drift apart.

Reads the run's figures as JSON on stdin, writes JSON on stdout with the
finished `report` in it. Standard library only, no network.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: Where the wording lives, relative to the skill root. The runtime runs a
#: script with the skill root as its working directory, and sets SKILL_DIR
#: besides — either way this resolves without the script knowing where it is
#: installed.
TEMPLATE = "assets/report.template.md"

#: A sentence frame in the template: four spaces, a name, then the sentence.
_FRAME = re.compile(r"^ {4}(?P<name>[A-Za-z]+)\s{2,}(?P<sentence>\S.*)$")


def _skill_root() -> Path:
    return Path(os.getenv("SKILL_DIR") or Path(__file__).resolve().parents[1])


def frames(template: str) -> dict[str, str]:
    """The named sentence frames, read off the template."""
    found = {}
    for line in template.splitlines():
        match = _FRAME.match(line)
        if match:
            found[match["name"]] = match["sentence"].strip()
    if not found:
        raise ValueError(f"{TEMPLATE} has no sentence frames in it.")
    return found


def _joined(value: object) -> str:
    """A list of items as English: `a`, `a and b`, `a, b and c`."""
    if isinstance(value, str):
        return value.strip()
    items = [str(item).strip() for item in (value or []) if str(item).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _has(value: object) -> bool:
    """Is there anything here worth putting in a sentence?

    A figure of zero is the case this exists for: `Rs 0` of coupon cover is not
    news, and a sentence saying so is worse than the silence it replaces.
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    digits = [ch for ch in text if ch.isdigit()]
    if not digits:
        # Not a figure at all — a word, a name. Keep it.
        return True
    return any(digit != "0" for digit in digits)


def render(run: dict, sentences: dict[str, str]) -> list[str]:
    """The report, one finished sentence per entry."""
    said: list[str] = []

    ordered = _joined(run.get("ordered"))
    if not ordered:
        raise ValueError("`ordered` is required — an errand report has to say what was bought.")

    order_number = str(run.get("orderNumber") or "").strip()
    if order_number:
        said.append(sentences["orderedOn"].format(ordered=ordered, orderNumber=order_number))
    else:
        said.append(sentences["ordered"].format(ordered=ordered))

    if _has(run.get("couponCovered")):
        said.append(sentences["coupon"].format(couponCovered=run["couponCovered"]))

    if _has(run.get("cashSpent")):
        if _has(run.get("cashRemaining")) and _has(run.get("cashLimit")):
            said.append(
                sentences["chargedRemaining"].format(
                    cashSpent=run["cashSpent"],
                    cashRemaining=run["cashRemaining"],
                    cashLimit=run["cashLimit"],
                )
            )
        else:
            said.append(sentences["charged"].format(cashSpent=run["cashSpent"]))

    if str(run.get("deliveredTo") or "").strip():
        said.append(sentences["delivery"].format(deliveredTo=str(run["deliveredTo"]).strip()))

    if run.get("failed") is True:
        said.append(sentences["failed"])

    exceptions = [str(item).strip() for item in (run.get("exceptions") or []) if str(item).strip()]
    for item in exceptions:
        said.append(sentences["exception"].format(exception=item))

    if not exceptions and run.get("failed") is not True:
        said.append(sentences["clean"])

    return said


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(
            "errand-report: pipe the run's figures in as JSON — see SKILL.md.",
            file=sys.stderr,
        )
        return 2
    try:
        run = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"errand-report: that is not JSON — {exc}", file=sys.stderr)
        return 2
    if not isinstance(run, dict):
        print("errand-report: expected a JSON object of the run's figures.", file=sys.stderr)
        return 2

    try:
        template = (_skill_root() / TEMPLATE).read_text(encoding="utf-8")
        said = render(run, frames(template))
    except (OSError, KeyError, ValueError) as exc:
        print(f"errand-report: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {"report": " ".join(said), "sentences": said},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
