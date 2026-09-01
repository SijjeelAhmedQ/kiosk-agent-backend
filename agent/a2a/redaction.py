"""Taking the restaurant's plumbing back out of what the two agents say.

Product ids, line ids and category ids exist so the merchant can point the
restaurant at a row. They are not part of the order, neither agent has a tool
that takes one from a sentence, and a console transcript is read by people — so
an id in a message is at best noise and at worst something a customer ends up
looking at.

Both briefs say not to write one. This is the half that does not depend on a
model having read its brief, and it is shared because the leak is not one-sided:
the merchant writes an id it looked up, and the buyer writes back the id it was
just shown. Fixing only the first leaves the same string on screen with a
different speaker's name against it.

The real fix is upstream of both — an id that never crosses the wire cannot be
parroted back, which is why the estimate quote no longer carries one. This stays
as the backstop for the id a model got from somewhere else.
"""

from __future__ import annotations

import re

#: "(product id: p_burger_001)", "line id L4027c111", "the categoryId is c_3".
#: The label is what makes this safe to strip. `order` is deliberately not one
#: of them: a model that writes "order ID 495" means the number, and a rule that
#: took the digits out of that sentence would cost the customer the one
#: identifier in this conversation they are entitled to have. The order's real
#: id is a different string and is removed by name, through `notable`.
_ID_PHRASE = re.compile(
    r"""[(\[]?\s*
        \b(?:product|line|category|item)[\s_-]?ids?\b
        \s*(?:is|are|=|:)?\s*
        ['"`]?[A-Za-z0-9][\w./-]*['"`]?
        \s*[)\]]?""",
    re.IGNORECASE | re.VERBOSE,
)

#: A parenthetical, for dropping whole when an id is hiding inside one.
_BRACKETED = re.compile(r"[(\[][^()\[\]]{0,160}[)\]]")


def notable(ids: set[str]) -> set[str]:
    """The ids that can be struck by name without eating ordinary English.

    A restaurant is free to id a product `burger`, and a rule that removed every
    occurrence of an id would then quietly delete the word from a sentence. So a
    bare id is removed only when it carries a digit, an underscore or a hyphen,
    or is long enough to be a uuid. The labelled form above catches the rest,
    where the label is the proof that a word is being used as an id.
    """
    return {
        token
        for token in ids
        if len(token) >= 12 or (len(token) >= 6 and not token.isalpha())
    }


def strip_ids(said: str, ids: set[str] | None = None) -> str:
    """One agent's words with the restaurant's identifiers taken out.

    `ids` is what this conversation actually handled — the merchant knows them,
    the buyer does not and passes nothing, which leaves it the labelled form
    alone. That is the right split: the buyer only ever has an id because it was
    shown one, and being shown one is always labelled.
    """
    text = said or ""
    tokens = notable(ids or set())

    if tokens:
        text = _BRACKETED.sub(
            lambda m: "" if any(token in m.group(0) for token in tokens) else m.group(0), text
        )
    # A space rather than nothing, then collapsed below: cutting an id out of
    # the middle of a sentence must not weld the words either side of it
    # together.
    text = _ID_PHRASE.sub(" ", text)
    for token in sorted(tokens, key=len, reverse=True):
        text = text.replace(token, " ")

    text = re.sub(r"[(\[]\s*[)\]]", "", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
