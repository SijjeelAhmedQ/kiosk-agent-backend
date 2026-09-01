# The shared LLM selection

`agent/llm/` is the definition — `service.py` holds the logic, `store.py` the
file, `providers.py` the adapters, `api.py` the endpoints. This describes them.

## Where the answer lives

A file under `var/`, written by `agent/llm/store.py`. Not an environment
variable held from startup, and that is the whole mechanism:

* Each service reads it **when it builds a model client**, not at import. So a
  switch reaches a process that is already running.
* All four services read the same file. So one change moves the ordering agent,
  both A2A agents and the Foodpanda dispatcher together.
* `.env` is the fallback for when nothing has been chosen. `source` on the
  active selection says which is in force: `central` for the file, otherwise the
  environment.

A key is never in that file. Credentials stay in `.env`, out of reach of an
endpoint that writes to disk; `keyEnv` on a provider names the variable to put
one in.

## The endpoints

Mounted at `/api/llm` on **all four services**, identically. Which one you ask
decides nothing but which process answers.

| Route | Does |
| --- | --- |
| `GET /api/llm/config` | The active selection: `{active: {provider, model, source, updatedAt, displayName, kind, ready, problem}}` |
| `PUT /api/llm/config` | Select. Body `{provider, model}`; `model` null takes the provider's default |
| `GET /api/llm/providers` | `{items: [...], active: {...}}` — everything selectable, and whether each is usable here |
| `GET /api/llm/models?provider=` | What one provider can run. Live for Ollama and OpenRouter |
| `GET /api/llm/settings?provider=` | `{provider, displayName, fields: [...], values: {...}}` |
| `PUT /api/llm/settings` | Body `{provider, values}`. **A merge** — a field sent as null returns to its .env default |
| `GET /api/llm/health?provider=&model=` | Could this serve a run right now? |
| `POST /api/llm/test` | Body `{provider, model}` or `{}`. **Actually generates.** Never raises |

`PUT /config` answers **400**, not 500, for a provider this deployment cannot
use: the request named something the operator can fix on this screen.

`GET /models` answers **200 with `problem` set** when a local runtime is not
running, rather than a 5xx. That is the commonest state it is asked in, and it
is something for a screen to render rather than an error to handle.

## A provider, described

```json
{
  "name": "ollama", "displayName": "Ollama", "kind": "local",
  "blurb": "…", "featured": true, "dynamicModels": true,
  "requiresKey": false, "keyEnv": null,
  "configured": true, "problem": null,
  "defaultModel": "…", "startHint": "…",
  "settings": [ ], "settingValues": { }
}
```

`kind` is `local` or `cloud`, and it changes what a failure means: a local
runtime has no credential to get wrong, so almost everything that fails there is
the machine rather than the configuration.

`settings` is the **shape** of the provider's configuration section and
`settingValues` is what is in force. The LLM screen draws itself from that
declaration — a provider with a server URL and a temperature gets two fields, a
provider with neither gets no section at all — rather than branching on a
provider's name. `select_llm.py --fields` reads the same declaration.

A field:

```json
{"key": "base_url", "label": "Server URL", "kind": "url", "default": "…",
 "help": "…", "advanced": false, "min": null, "max": null, "step": null,
 "number": "float"}
```

`kind` is `url`, `number` or `text`. `advanced: true` folds a knob away on the
screen — the ones that have a right answer almost always.

## Health, and test

Health asks the provider about **itself**: is there a key, is the runtime
answering, does it list the model.

```json
{"provider": "…", "displayName": "…", "kind": "local", "model": "…",
 "ok": false,
 "checks": [{"label": "…", "ok": false, "detail": "…"}],
 "problem": "…"}
```

Test asks the **model** a question and reads the answer back, through exactly
the client an agent would get — so a key that is present but rejected, and a
model that is listed but not servable, fail here rather than on the next errand.
It runs three checks in order and stops at the first failure: provider known,
provider configured, provider answering, then a real one-word generation. On
success it adds `reply`.

It never raises. A failure comes back as `ok: false` with a **sentence**,
because a provider client's traceback can carry the request it was making —
headers included.

## Provider spellings

Several providers answer to more than one name, and every spelling reaches one
adapter *and* one default model. `agent/config.canonical_provider` is the table:

| Canonical | Also written |
| --- | --- |
| `ollama` | `local`, `local-llm` |
| `llamacpp` | `llama.cpp`, `llama-cpp`, `llama_cpp`, `llamaserver`, `llama-server` |
| `lmstudio` | `lm-studio`, `lm_studio`, `lmstudio.ai` |
| `janai` | `jan`, `jan.ai`, `jan-ai`, `jan_ai` |
| `gpt4all` | `gpt-4-all`, `gpt-4all` |
| `vllm` | `vllm-openai` |

The table is in one place because two copies of it was the original bug: a
spelling that reached the adapter but missed the default model asked a local
Ollama for a cloud model id and got an opaque 404.

## When an agent is not following the selection

Three agents can be pinned to something else, and none of them is silent about
it — each health endpoint reports `pinned`.

| Agent | Pinned by | Reported at |
| --- | --- | --- |
| A2A buyer | `A2A_BUYER_PROVIDER`, `A2A_BUYER_MODEL` | `GET /api/a2a/health` → `buyer.pinned` |
| A2A merchant | `A2A_MERCHANT_PROVIDER`, `A2A_MERCHANT_MODEL` | `GET /api/a2a/health` → `merchant.pinned` |
| Foodpanda dispatcher | `MOCK_FOODPANDA_*` | `GET /api/foodpanda/health` → `dispatcher.pinned` |

`pinned` is true **only when nobody has chosen centrally**. Once the selection
file says `central`, it wins and the pins go quiet — which is why splitting the
two A2A sides across providers means unsetting the central choice, not adding
to it.

Splitting them is the fix for the commonest A2A failure: two agents on one key
consume a per-model budget twice as fast, and the run dies with a rate-limit
sentence rather than a bug.

## llama.cpp starts itself

It is the only provider whose runtime is a window somebody can close rather than
a hosted endpoint or a service that starts on login. When it is the selected
provider and its port is silent, `agent/llm/llamacpp_launcher.py` starts
`scripts/llama-server.ps1` on the same `.env` settings and waits for the GGUF to
load.

Never fatal: if it cannot be started, building the model fails a moment later
with the provider's own message, which names the address and the command. Every
other provider, and a llama-server already up, costs one local request.
