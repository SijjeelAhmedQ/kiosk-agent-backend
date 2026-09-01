---
name: friends-kitchen-operations
description: Bring up, check and read the logs of the four Friends Kitchen agent services — the ordering agent on 8100, the A2A ordering desk on 8101, the in-house courier on 8102 and the Foodpanda dispatcher on 8103 — plus the restaurant API they all buy from. Use when asked whether the system is running, which port a service is on, how to start one, why a console shows a service as offline, or to read a service's live log (server.py, a2a_server.py, delivery_server.py, foodpanda_server.py, var/).
license: Proprietary. See the repository README.
compatibility: Requires Python 3.10+. Reads the health and console endpoints of whichever services are running; reports the rest as down rather than failing. Talks to localhost only.
metadata:
  author: friends-kitchen
  agents: "operator"
  version: "1.0"
  scope: "four services plus the restaurant API"
  ports: "8100, 8101, 8102, 8103"
allowed-tools: Bash(python:*) Read
---

# Running the floor

Five processes make up the demonstrable system, and the ports are not
interchangeable — the consoles, the agent cards and the delivery handover all
name them.

| What | Port | Start it with |
| --- | --- | --- |
| Friends Kitchen REST API | 8000 | in `../friends-kitchen-backend`: `.venv\Scripts\python -m uvicorn app.main:app --port 8000` |
| Ordering agent | 8100 | `.venv\Scripts\python -m uvicorn server:app --port 8100` |
| A2A ordering desk | 8101 | `.venv\Scripts\python -m uvicorn a2a_server:app --port 8101` |
| In-house courier | 8102 | `.venv\Scripts\python -m uvicorn delivery_server:app --port 8102` |
| Foodpanda dispatcher | 8103 | `.venv\Scripts\python -m uvicorn foodpanda_server:app --port 8103` |

All from the repository root, and `--reload` while developing. The control
panel is a separate repository (`../friends-kitchen-agent-frontend`, Vite on
5174); Friends Kitchen's own site is on 5173, which is why the agent's panel is
not.

**Nothing here starts a service.** Starting a process is the operator's, and a
skill that did it silently would be a skill that started four of them on the
wrong ports. What this does is say what is up, what is not, and what the exact
command is.

## What is running

```
python skills/friends-kitchen-operations/scripts/service_status.py
python .../service_status.py --json
```

One line per service: up or down, what it is running on, and — when it is down —
the command that starts it. It also reports the restaurant API, because every
agent here ultimately buys from it and a floor with all four agents up and no
restaurant behind them fails at the first tool call.

Exit code 1 if anything an errand needs is missing, so it can gate a script.

## Which ones do you actually need

Only 8100 and the restaurant API for a counter order. Add a courier — 8102 or
8103, whichever `DELIVERY_PROVIDER` names — for a delivery. 8101 only for
agent-to-agent ordering. A console for a service that is not running shows it
offline; that is the console working, not a fault.

## Reading a service's log

```
python .../tail_console.py --service ordering
python .../tail_console.py --service foodpanda --follow
```

Every service publishes its own log on `{prefix}/console` for the scrollback
and `{prefix}/console/events` to follow it live — the same stream the
operations dashboard renders. This is what to read when a run failed and the
trace stopped before saying why.

There are also plain files under `var/` (`agent-8100.out.log`,
`a2a_server.err.log`, and so on) for whatever was written before the logging
handler was installed, or after a process died.

## The two things a health payload is asked

* **Is the service up?** Whether `{prefix}/health` answers at all.
* **Can it do its job?** `hasApiKey` / `ready` on the payload. A service can be
  perfectly up and unable to build an agent, and those are different problems
  with different fixes. See `references/SERVICES.md`.

## Tracing across the hops

`FK_OTEL` turns on OpenTelemetry in every service. Off, it costs nothing; on,
`traceparent` goes out on httpx and is read back in by FastAPI, so an errand
that touches the ordering agent, a courier and the restaurant is **one trace**
rather than four unrelated ones. Each health payload reports `telemetry` —
whether an exporter is configured, never what is in the headers, because
`OTEL_EXPORTER_OTLP_HEADERS` usually carries a token.

## Files

* `scripts/service_status.py` — the whole floor, up or down, with start commands.
* `scripts/tail_console.py` — one service's log, backlog or live.
* `references/SERVICES.md` — each service, its routes, and what its health means.
