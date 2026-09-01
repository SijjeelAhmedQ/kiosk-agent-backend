---
name: friends-kitchen-llm-configuration
description: Read and change the model every Friends Kitchen agent runs on — the shared provider and model selection, per-provider settings for local runtimes, credential health, and a real end-to-end test generation. Use when asked which model an agent is using, to switch provider or model, to point at a local runtime such as Ollama, llama.cpp, LM Studio, Jan or vLLM, to diagnose a missing API key or an unreachable model, or when working with agent/llm/ and the /api/llm endpoints.
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+ and any one of the four services running (ports 8100, 8101, 8102, 8103) — the LLM endpoints are mounted identically on all of them. Reaching a cloud provider needs outbound network access.
metadata:
  author: friends-kitchen
  agents: "operator"
  version: "1.0"
  scope: "all four services"
  mounted-at: "/api/llm"
allowed-tools: Bash(python:*) Read
---

# Which brain the agents run on

One selection, read by all four services. Change it once and the ordering
agent, both A2A agents and the Foodpanda dispatcher follow on the next agent
each of them builds.

This skill reads and writes that selection through the endpoints
`agent/llm/api.py` already publishes. It adds no provider, no adapter and no
new place for the answer to live.

## Why it reaches every service at once

The selection is a **file** in `var/`, not a variable held from startup. Each
service reads it when it builds a model client, which is what makes a switch
made on one screen reach a service that is already running — no restart, and no
service can drift onto a different brain without saying so.

The endpoints are mounted identically on all four processes for the same
reason: a screen that could only be reached while one particular service
happened to be up would be the wrong place to fix a configuration problem.
`--service` picks which one to ask; the answer is the same.

## Reading it

```
python skills/friends-kitchen-llm-configuration/scripts/show_llm.py
python .../show_llm.py --providers
python .../show_llm.py --models ollama
python .../show_llm.py --health
```

`--health` asks the provider about itself: is there a key, is the runtime
answering, does it list the model. `--test` goes further and **actually runs a
generation** through exactly the client an agent would get — the only check that
proves the whole path, and the one that catches a key that is present but
rejected, or a model that is listed but not servable.

## Changing it

```
python .../select_llm.py anthropic
python .../select_llm.py ollama --model qwen2.5:14b
python .../select_llm.py llamacpp --set base_url=http://localhost:8080/v1 --set ctx=16384
```

`PUT /api/llm/config` for the provider and model; `PUT /api/llm/settings` for a
provider's own knobs. Settings are a **merge, not a replace** — only the fields
sent are touched, and a field sent as `null` goes back to its `.env` default.

Run `--set` with no provider argument to see which fields the selected provider
declares. The screen draws itself from that declaration rather than from a
hard-coded form, and so does this script: a provider with a server URL and a
temperature gets two fields, a cloud vendor with nothing to configure gets none.

**Prefer this over editing `.env`.** A running service will not pick up an edit
to `.env`; it will pick up a selection change immediately. `.env` is the
fallback for when nothing has been chosen.

## Reading a health report

```json
{"provider": "ollama", "displayName": "…", "kind": "local", "model": "…",
 "ok": false,
 "checks": [{"label": "Runtime answering", "ok": false, "detail": "…"}],
 "problem": "…"}
```

`problem` is one sentence written for a person, and for a local runtime it names
the fix in that runtime's own words — `ollama pull` is not advice a llama.cpp
operator can act on. `checks` is the same answer broken into lines.

`kind` is `local` or `cloud`, and it changes what a failure means. A local
runtime has no credential to get wrong, so almost everything that fails there is
the machine rather than the configuration.

## Two traps

* **A model id belongs to one vendor.** Asking a local Ollama for
  `claude-opus-5` gets an opaque 404. `--models <provider>` lists what that
  provider can actually run; for Ollama and OpenRouter it is fetched live.
* **`pinned: true` on a health payload means that agent is not following the
  central selection.** An `A2A_BUYER_*`, `A2A_MERCHANT_*` or `MOCK_FOODPANDA_*`
  variable is deciding its brain instead, because nobody has chosen centrally.
  A side running on something other than the shared selection is never silent
  about it — see `references/PROVIDERS.md`.

## A2A runs two agents on one budget

The commonest failure in a negotiation is a rate limit rather than a bug: the
buyer and the merchant share a key by default, and a per-model budget is
consumed twice as fast. The fix is `A2A_BUYER_PROVIDER` and
`A2A_MERCHANT_PROVIDER` pointing at different providers — or waiting it out.

## Files

* `scripts/show_llm.py` — the active selection, providers, models, health, test.
* `scripts/select_llm.py` — switch provider or model, or set a provider's fields.
* `references/PROVIDERS.md` — the endpoints, the payloads, and where a selection
  actually lives.
