"""The Foodpanda delivery agent — the second agent in `order → deliver`.

This package is a whole agent, not a client for one. It has its own brain, its
own hands and its own brief: it receives a delivery request from the ordering
agent over HTTP, decides whether to take it, and then runs the job to the
customer's door leg by leg.

What is deliberately *not* here is any way to reach into the restaurant. This
agent knows only what the request carried — no cart, no wallet, no menu, no
database. That is the boundary `agent/delivery/contract.py` describes, and
keeping it means this package could be lifted out and run by a courier company
without a line changing.

It is served by `foodpanda_server.py` on 8103, separate from the ordering
agent on 8100 and from the in-house courier on 8102. Both of those keep
working exactly as they did; this is a third option the ordering side can be
pointed at with `DELIVERY_PROVIDER=mock_foodpanda`.
"""
