"""Agent-to-agent ordering: a buyer agent and the restaurant's own agent, talking.

This package is deliberately self-contained. The existing errand flow — `run.py`,
`server.py` on 8100, `agent/tools/` — is not imported *into* here in any way that
could change its behaviour, and nothing here is imported *by* it. The two flows
share the restaurant's HTTP client and a couple of plain classes, and nothing
else: no module-level cart, no module-level wallet, no shared order.

Run it on its own port so that is true of the process as well:

    .venv\\Scripts\\python -m uvicorn a2a_server:app --port 8101 --reload
"""
