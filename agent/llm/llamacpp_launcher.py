"""Start `llama-server` when the errand needs it and nothing is answering.

llama.cpp is the one provider in this system whose *process* is the operator's
responsibility. A hosted provider is always up and Ollama runs as a service that
Windows starts on login; `llama-server` is a program somebody typed, in a window
somebody can close — and closing it turns the LLM Configuration screen's
llama.cpp card into "server is not reachable at http://localhost:8080" until
that somebody remembers scripts/llama-server.ps1.

So: if llama.cpp is the selected provider, and its port answers, nothing here
does anything. If it does not answer, this starts the same script by the same
settings — one GGUF, one port, `.env`'s `LLAMACPP_*` block — and waits for the
model to finish loading.

Nothing here replaces the script or duplicates what it decides. Which GGUF, how
much context, how many layers on the GPU: still `scripts/llama-server.ps1`
reading `.env`, exactly as when it is run by hand. This only supplies the hand.

It is deliberately narrow:

* only when the selection is llama.cpp — no other provider has a process to
  start, and Ollama's daemon is not this module's business;
* only when the port is silent — a server somebody started with `-Model
  qwen3-8b` is left alone rather than joined by a second one on the same port;
* never fatal — a failure to start is reported and the run continues to the
  provider's own unreachable message, which is the sentence that already
  explains this.

`LLAMACPP_AUTOSTART=false` in .env turns it off for a deployment that starts the
runtime some other way (a service, a container, another machine).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from agent.config import canonical_provider, settings
from agent.llm import store
from agent.llm.providers import LlamaCppProvider, ProviderUnavailable

__all__ = ["ensure", "reachable", "selected"]

#: The repo root — this file is agent/llm/llamacpp_launcher.py.
_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "llama-server.ps1"
_LOG_DIR = _ROOT / "var"

#: How often to ask a starting server whether it has finished loading. The 4B
#: takes twenty-odd seconds off a warm disk and the 30B rather longer, so this
#: is a poll rather than a sleep-and-hope.
_POLL_SECONDS = 2.0

#: Windows process-creation flags: no console window, and its own process
#: group. The group is what makes the server outlive the run that started it —
#: the next errand finds it already up, which is the whole point — and keeps
#: Ctrl+C on the agent from taking the model down with it.
#:
#: Not DETACHED_PROCESS, which looks like the right flag and is not: a
#: PowerShell with no console at all loses everything `Write-Host` prints, and
#: the start script says what it is loading before it loads it. The banner went
#: nowhere and so did the reason when something failed. CREATE_NO_WINDOW gives
#: the child a console it does not show, which keeps both.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _quiet(message: str) -> None:
    """The default reporter: say nothing."""


def selected() -> bool:
    """Is llama.cpp what this system is currently running on?

    Read through `store.active()` rather than `settings.provider`, because the
    LLM Configuration screen is the newer authority of the two and .env is only
    what it falls back to.
    """
    return canonical_provider(store.active().provider) == "llamacpp"


def reachable(provider: LlamaCppProvider | None = None) -> bool:
    """Is something already serving on llama.cpp's configured port?

    Asked of the adapter rather than of a hardcoded URL, so a server URL saved
    on the LLM screen is the one probed — the same address the agent will use a
    moment later.
    """
    adapter = provider or LlamaCppProvider()
    try:
        adapter.list_models()
        return True
    except ProviderUnavailable:
        return False


def ensure(
    log: Callable[[str], None] = _quiet,
    *,
    timeout: float | None = None,
) -> str:
    """Make sure llama.cpp is up, if llama.cpp is what we are running on.

    Returns a short status word — `disabled`, `not-selected`, `running`,
    `started`, or `failed` — for a caller that wants to say something about it.
    Never raises: every way this can fail ends in the provider's own
    "not reachable" message a few lines later, and that sentence is clearer
    than anything this could throw.
    """
    if not settings.llamacpp_autostart:
        return "disabled"
    if not selected():
        return "not-selected"

    adapter = LlamaCppProvider()
    if reachable(adapter):
        return "running"

    if os.name != "nt":
        log(
            f"llama.cpp is not answering at {adapter.host}, and this machine is not "
            "Windows. Start it with `llama-server -m model.gguf --host 127.0.0.1 "
            "--port 8080 --jinja`."
        )
        return "failed"

    if not _SCRIPT.exists():
        log(f"llama.cpp is not answering at {adapter.host}, and {_SCRIPT} is missing.")
        return "failed"

    log(f"llama.cpp is not answering at {adapter.host} - starting llama-server...")
    try:
        _spawn()
    except OSError as exc:
        log(f"could not start llama-server: {exc}")
        return "failed"

    return "started" if _await_load(adapter, log, timeout) else "failed"


def _spawn() -> None:
    """Launch the start script in a hidden window, with its output in var/.

    The same two log files the script's own operators read, appended rather
    than truncated: a server that dies ten minutes in leaves the reason there,
    and the next run must not erase it on the way past.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_SCRIPT),
    ]
    with open(_LOG_DIR / "llama-server.out.log", "a", encoding="utf-8") as out, open(
        _LOG_DIR / "llama-server.err.log", "a", encoding="utf-8"
    ) as err:
        subprocess.Popen(
            command,
            cwd=str(_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        )


def _await_load(
    adapter: LlamaCppProvider,
    log: Callable[[str], None],
    timeout: float | None,
) -> bool:
    """Wait for the GGUF to finish loading, or say why we gave up.

    Loading a multi-gigabyte file onto a card is the slow half of starting this
    runtime, and until it is done the port either refuses or answers 503 — so
    "started" means "answering with a model", not "process created".
    """
    limit = settings.llamacpp_autostart_timeout if timeout is None else timeout
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        if reachable(adapter):
            log(f"llama-server is up at {adapter.host}.")
            return True

    log(
        f"llama-server did not answer at {adapter.host} within {limit:.0f}s. "
        f"See {_LOG_DIR / 'llama-server.err.log'}."
    )
    return False
