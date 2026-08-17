"""Handing a confirmed order to whoever delivers it.

Kept as its own package, separate from `agent/tools/`, because delivery is a
different service with a different failure mode: the restaurant refusing a
coupon and Foodpanda not answering the phone are unrelated problems, and an
order that is bought but undelivered is a state the ordering side has no
opinion about.

    contract.py       the shapes and the interface — the whole boundary
    courier_agent.py  Friends Kitchen's own delivery agent (the default)
    foodpanda.py      Foodpanda's courier API
    registry.py       which one this deployment uses
"""
