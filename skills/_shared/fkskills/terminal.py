"""Making the console print what the services actually said.

Every agent here writes em dashes and every amount is `Rs 1,093`, and a Windows
console defaults to cp1252 — without this a report comes out as mojibake, and
the reader cannot tell a rendering fault from a broken run.

The same fix `run.py` applies for the same reason, in a function rather than at
import: a module should not reconfigure a caller's streams as a side effect of
being imported.
"""

from __future__ import annotations

import sys


def utf8() -> None:
    """Put stdout and stderr into UTF-8, replacing what cannot be encoded.

    Never raises. A stream that has been redirected to something without
    `reconfigure` is a stream that is already being captured by something able
    to decode it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
