"""The LLM configuration endpoints, as a factory every service mounts.

    GET   {prefix}/config      what every agent is running on
    PUT   {prefix}/config      change it, everywhere
    GET   {prefix}/providers   what can be chosen
    GET   {prefix}/models      what a provider can run
    GET   {prefix}/health      can the selection serve a run
    POST  {prefix}/test        ask the model a question and read the answer

Mounted on all four services rather than on one, the way `agent/console.py`
mounts its two endpoints: they are identical everywhere, the selection behind
them is a file all four processes read, and a screen that could only be reached
while the ordering agent happened to be up would be the wrong place to fix a
configuration problem.

Nothing here returns a key, and nothing here accepts one. Credentials stay in
.env, where they already are — this screen changes *which* provider is used, not
what it is authenticated with.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent.llm.providers import MissingApiKey, ProviderUnavailable
from agent.llm.service import llm


class SelectionIn(BaseModel):
    """A provider, and optionally a model. No model means the provider's default."""

    provider: str = Field(min_length=1, max_length=40)
    model: str | None = Field(default=None, max_length=200)


class TestIn(BaseModel):
    """What to test. Both optional — no body at all tests the active selection."""

    provider: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=200)


def _ok(data: dict) -> dict:
    """The envelope every endpoint in this repo answers in."""
    return {"success": True, "data": data}


def mount(app: Any, prefix: str = "/api/llm") -> None:
    """Register the LLM configuration endpoints on `app`."""

    @app.get(f"{prefix}/config")
    def read_config() -> dict:
        """What every agent in this system is currently running on."""
        return _ok({"active": llm.describe()})

    @app.put(f"{prefix}/config")
    def write_config(payload: SelectionIn) -> dict:
        """Point every agent at a different provider or model.

        Takes effect on the next agent built, in this process and in the other
        three: the selection is a file, and each service reads it when it
        builds a brain rather than holding one from startup.
        """
        try:
            llm.select(payload.provider, payload.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except MissingApiKey as exc:
            # 400 rather than 500: the request named a provider this deployment
            # cannot use, which is something the operator can fix on this screen.
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except OSError:
            raise HTTPException(
                status_code=500,
                detail="The selection could not be saved to disk. Check that the "
                "service can write to its var/ directory.",
            ) from None

        return _ok({"active": llm.describe()})

    @app.get(f"{prefix}/providers")
    def list_providers() -> dict:
        """Everything that can be selected, and whether each is usable here."""
        return _ok({"items": llm.providers(), "active": llm.describe()})

    @app.get(f"{prefix}/models")
    async def list_models(provider: str | None = None) -> dict:
        """What one provider can run. Defaults to the active one.

        Both dynamic providers reach the network for this — OpenRouter's
        catalogue, Ollama's installed tags — so it runs off the event loop.
        """
        name = provider or llm.active().provider
        try:
            adapter = llm.provider(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        try:
            items = await asyncio.to_thread(adapter.list_models)
        except ProviderUnavailable as exc:
            # 200 with the problem in it, not a 5xx: "the local runtime is not
            # running" is the single most common state this endpoint is asked
            # in, and it is something for the screen to render rather than an
            # error for it to handle.
            return _ok(
                {
                    "provider": adapter.name,
                    "displayName": adapter.display_name,
                    "dynamic": adapter.dynamic_models,
                    "items": [],
                    "problem": str(exc),
                }
            )

        return _ok(
            {
                "provider": adapter.name,
                "displayName": adapter.display_name,
                "dynamic": adapter.dynamic_models,
                "items": items,
                "problem": None,
            }
        )

    @app.get(f"{prefix}/health")
    async def provider_health(provider: str | None = None, model: str | None = None) -> dict:
        """Could this provider and model serve a run right now?

        Reaches the provider, so it runs off the event loop for the same reason
        the model list does.
        """
        return _ok(await asyncio.to_thread(llm.health, provider, model))

    @app.post(f"{prefix}/test")
    async def test_model(payload: TestIn | None = None) -> dict:
        """Ask the selected model a question and read the answer back.

        A real generation through the real client — the only check that proves
        the whole path. It never raises: a failure comes back as `ok: false`
        with a sentence, because a provider client's traceback can carry the
        request it was making.
        """
        provider = payload.provider if payload else None
        model = payload.model if payload else None
        return _ok(await asyncio.to_thread(llm.test, provider, model))
