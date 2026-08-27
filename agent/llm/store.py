"""The one place that knows which provider and model everything runs on.

Four services run in four processes — the ordering agent on 8100, the A2A desk
on 8101, the courier on 8102, the Foodpanda dispatcher on 8103 — and a choice
made on one of them has to reach the other three. That rules out a module-level
variable, and a database would be a strange thing to add to a system whose
finished tasks live in a `deque`. So the selection is a small JSON file, and
every process reads it at the moment it builds a model client.

    {"provider": "llamacpp", "model": "qwen3-4b-instruct-2507",
     "updatedAt": "2026-08-25T…",
     "providers": {"llamacpp": {"baseUrl": "http://localhost:8080",
                                "temperature": 0.7, "maxTokens": 2000}}}

`providers` is the second half of the same idea: a provider with settings of its
own — where a local runtime listens, what temperature to ask it for — keeps them
here rather than in a file of its own, so one save is one atomic write and one
read answers both questions. It holds *overrides only*; a provider with no entry
runs on what .env and its adapter's defaults say, which is what every provider
did before this key existed. Nothing credential-shaped goes in it: API keys stay
in .env, where a file written by an HTTP endpoint cannot reach them.

The file is the *override*, not the whole configuration. When it is absent —
a fresh checkout, or an operator who has never opened the LLM screen — `active()`
answers with `AGENT_PROVIDER` and `AGENT_MODEL` from .env, which is exactly what
this system did before the file existed. So nothing changes until somebody
chooses, and once they have, the choice survives a restart.

Reads are cached against the file's mtime and size rather than repeated: a
model build is not a hot path, but a health poll from an open dashboard is, and
stat-ing is cheaper than parsing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.config import _default_model_for, canonical_provider, settings

#: Where the selection lives. Under `var/` next to the code rather than in the
#: user's home, because a checkout is the deployment here — and configurable so
#: two checkouts on one machine cannot fight over one file.
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "var" / "llm-config.json"


def path() -> Path:
    override = os.getenv("LLM_CONFIG_PATH", "").strip()
    return Path(override) if override else _DEFAULT_PATH


@dataclass(frozen=True)
class Selection:
    """Which brain every agent in this system is currently using."""

    provider: str
    model_id: str
    #: "central" when an operator chose it on the LLM screen, "environment"
    #: when it is still whatever .env says. The UI says which, because "why is
    #: it on this model" is the first question an operator asks.
    source: str = "environment"
    updated_at: str | None = None

    def to_view(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model_id,
            "source": self.source,
            "updatedAt": self.updated_at,
        }


def _from_environment() -> Selection:
    return Selection(provider=settings.provider, model_id=settings.model_id)


# The cache and its guard. `_stamp` is (mtime_ns, size) or None for "no file",
# and `_cached` is the whole parsed document rather than the selection alone —
# the per-provider settings live in the same file, and parsing it twice to
# answer two questions about it would be two reads where the file guarantees
# one.
_lock = threading.Lock()
_stamp: tuple[int, int] | None = None
_cached: dict | None = None


def _read_file() -> dict | None:
    """The saved document, or None if there isn't a usable one.

    Every failure — no file, bad JSON, a half-written file — reads as "no
    override". A corrupt state file must fall back to the environment rather
    than take four services down.
    """
    target = path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _document() -> dict:
    """The saved file as it stands, cached against its mtime and size."""
    target = path()
    try:
        info = target.stat()
        stamp: tuple[int, int] | None = (info.st_mtime_ns, info.st_size)
    except OSError:
        stamp = None

    with _lock:
        global _stamp, _cached
        if stamp == _stamp and _cached is not None:
            return _cached
        _cached = (_read_file() if stamp is not None else None) or {}
        _stamp = stamp
        return _cached


def active() -> Selection:
    """The selection every agent in every process should be running on."""
    raw = _document()

    # A blank provider or model reads as "no override" the same way a missing
    # file does — and it is a real state now rather than a corrupt one, reached
    # by configuring a provider's server URL before choosing that provider.
    provider = str(raw.get("provider") or "").strip().lower()
    model_id = str(raw.get("model") or "").strip()
    if not provider or not model_id:
        return _from_environment()

    return Selection(
        provider=provider,
        model_id=model_id,
        source="central",
        updated_at=raw.get("updatedAt"),
    )


def settings_for(provider: str) -> dict:
    """What has been saved for one provider. `{}` when nothing has.

    Overrides only, and deliberately not merged with a default here: an adapter
    knows what its own defaults are and this file does not, so merging is the
    adapter's job. A copy, because the dict behind it is the shared cache.
    """
    saved = _document().get("providers")
    if not isinstance(saved, dict):
        return {}
    values = saved.get(canonical_provider(provider))
    return dict(values) if isinstance(values, dict) else {}


def save(provider: str, model_id: str) -> Selection:
    """Write the selection and return what is now active. See `_write`."""
    provider = canonical_provider(provider)
    model_id = model_id.strip() or _default_model_for(provider)

    selection = Selection(
        provider=provider,
        model_id=model_id,
        source="central",
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # Read-modify-write rather than write: the per-provider settings live in
    # this same file, and choosing a provider must not throw away the server URL
    # somebody configured for it a minute earlier.
    document = dict(_document())
    document.update(
        {
            "provider": selection.provider,
            "model": selection.model_id,
            "updatedAt": selection.updated_at,
        }
    )
    _write(document)
    return selection


def save_settings(provider: str, values: dict) -> dict:
    """Merge `values` into one provider's settings and return the result.

    A merge rather than a replace, so a screen that only knows about the fields
    it drew cannot silently drop one somebody set from another build. A key
    whose value is None is a *removal* — that is how a field is put back to its
    .env default, which is not the same as pinning it to whatever that default
    happens to be today.
    """
    name = canonical_provider(provider)

    document = dict(_document())
    saved = document.get("providers")
    providers = dict(saved) if isinstance(saved, dict) else {}

    current = providers.get(name)
    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    if merged:
        providers[name] = merged
    else:
        providers.pop(name, None)

    document["providers"] = providers
    _write(document)
    return merged


def _write(document: dict) -> None:
    """Put `document` on disk atomically, and make this process see it at once.

    Atomically because the other three processes read this file without taking
    a lock: a reader that catches a half-written file would fall back to the
    environment for as long as it holds its cache, which is the one way a
    "one change applies everywhere" system could silently apply it in three
    places out of four.
    """
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(document, file, indent=2)
            file.write("\n")
        os.replace(temp_name, target)
    except BaseException:
        # Never leave the temporary file behind next to the real one.
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

    # Make this process see it immediately rather than on the next stat, so the
    # response to a save reports the state the save produced.
    with _lock:
        global _stamp, _cached
        try:
            info = target.stat()
            _stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            _stamp = None
        _cached = document
