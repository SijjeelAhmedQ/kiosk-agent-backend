"""Reading a claim out of an agent's sentence, and telling it from a denial.

Both agents get checked against their own session before their words go
anywhere — the merchant's reply against its basket, the buyer's report against
its wallet — and both checks need the same thing first: whether the sentence
that looks like a claim is actually denying one. "The order was paid" and "the
order was not paid" match the same pattern and mean opposite things.

Kept here rather than copied into both, because the two agents disagreeing about
what counts as a denial is a bug that only shows up in the case that matters:
a false report nobody caught.
"""

from __future__ import annotations

import re

#: A claim read inside a denial is not a claim. "I have not added it yet."
#
# Kept exactly as the merchant has always had it. Widening the set here would
# make the merchant's own check quietly more permissive — a claim it used to
# correct would start reading as a denial — and that check is the one thing
# standing between a described basket and a buyer paying for it.
DENIED = re.compile(
    r"\b(?:not|never|nothing|cannot|can't|isn't|haven't|empty)\b", re.IGNORECASE
)

#: Where one sentence ends, for reading a denial no wider than the clause it
#: sits in.
STOPS = ".!?\n"


def denied_near(text: str, match: re.Match) -> bool:
    """Is the thing that matched being *withheld* rather than asserted?

    Scoped to the sentence the match sits in, which matters most for the longer
    of the two messages this is asked about. A buyer's report is a paragraph —
    "I ordered one Big Mac; I could not pay for it" — and a denial test run over
    the whole of one finds a "not" in almost every report there is, which would
    suppress the check exactly when it is needed.
    """
    start = max(text.rfind(stop, 0, match.start()) for stop in STOPS) + 1
    ends = [pos for pos in (text.find(stop, match.end()) for stop in STOPS) if pos != -1]
    return bool(DENIED.search(text[start : min(ends, default=len(text))]))
