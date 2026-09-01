# Agent Skills

A standardised skill layer over the Friends Kitchen agents, following the
[Agent Skills specification](https://agentskills.io/specification).

It **wraps** the existing system. It does not replace any part of it, and it
does not change how any part of it behaves.

```
Existing agent  ──▶  existing tools  ──▶  existing services  ──▶  Friends Kitchen API
                                              ▲
                                              │  HTTP, the same calls the console makes
                                        Agent Skill layer
                                        SKILL.md + scripts/
```

Nothing here is imported by `agent/` or by the four service modules, and
nothing here imports them. Delete `skills/` and the application still starts,
still runs an errand, and still answers on every route it answered on before —
the agents simply have no skills, which `agent/skills/equip.py` treats as the
ordinary case. That is the contract this layer is written to.

The one direction that does cross is deliberate and read-only: `agent/skills/`
reads this directory *as data* — the frontmatter for a catalogue, the body when
an agent opens a skill, and `scripts/` when it runs one, in a subprocess. It
never imports `_shared/`, and `_shared/` never imports `agent/`.

## What is here

Two kinds of skill live here, and the difference is who reads them.

**Runtime skills**, scoped by `metadata.agents` to the agents that run errands.
These are loaded into an agent's own prompt and called mid-run:

| Skill | Covers | For |
| --- | --- | --- |
| `order-math` | Cart subtotals, coupon coverage, what is left of the cash limit | ordering |
| `errand-report` | The closing summary an errand ends with | ordering |

Only the ordering agent is equipped today. Both would suit the A2A buyer as
well — it carries a wallet and reports back the same way — and giving it them
is one `equip()` call in `agent/a2a/buyer_agent.py` plus its name in each
skill's `metadata.agents`. It has not been done, so the scope says what is
true rather than what is intended.

**Operator skills**, scoped to `operator` — the person or host agent running
the floor, not the agents themselves. An errand agent is deliberately not given
these: `friends-kitchen-ordering` describes how to *start* an errand, which is
not something an errand should be doing.

| Skill | Covers |
| --- | --- |
| `friends-kitchen-ordering` | The errand agent on 8100 — browse, cart, coupon, place, pay, hand over |
| `friends-kitchen-a2a-negotiation` | Buyer and merchant on 8101 — the agent-to-agent order |
| `friends-kitchen-delivery-dispatch` | The couriers on 8102 and 8103 — job states, gates, tracking |
| `friends-kitchen-llm-configuration` | The one model selection all four services read |
| `friends-kitchen-operations` | Which services are up, how to start them, their logs |

Each is one directory with a `SKILL.md`, and `scripts/`, `references/` and
`assets/` where they earn their place.

A skill that names no agents at all goes to every one of them, which is the
right default for something like a reporting format. Scoping matters because a
skill an errand cannot use is a step the errand can waste.

## The rule the scripts follow

**A skill script never reimplements business logic.** It calls the endpoint or
the entry point that already does the work:

```
request → skill → existing endpoint / existing function → existing result
```

So `place_order.py` posts to `POST /api/agent/runs`, exactly as the control
panel does, and the agent that runs is the one `agent/friends_kitchen_agent.py`
builds. The cash ceiling is still enforced in `authorize_payment`; the delivery
lifecycle is still enforced by the transition table in `agent/foodpanda/jobs.py`.
A skill that went round either of those would be going round the only things
that make this system safe.

The scripts use **the standard library only** — `urllib`, not `httpx`. A
spec-compliant agent host will not necessarily be inside this repository's
virtual environment, and a deterministic script that first needs a package
installed is not deterministic.

## Using them

Discovery is the host's job: an agent runtime that implements Agent Skills reads
the `SKILL.md` files here, holds each `name` and `description` from startup, and
loads the body of one only when a task calls for it.

This repository ships that runtime — `agent/skills/`. One call gives an agent
the four tools and the paragraph naming what it may reach for:

```python
from agent.skills import equip

tools, prompt = equip(tools, prompt, agent="ordering")
```

The agent then calls `list_skills`, `open_skill`, `read_skill_file` and
`run_skill_script`, which are the spec's three disclosure layers plus the one
thing a layer cannot do. Those calls stream to the control panel like any other
step, so a run's trace names the skills it used. `AGENT_SKILLS_PATH` adds skill
trees ahead of this one; `AGENT_SKILL_TIMEOUT` bounds a script.

To read the same catalogue by hand:

```bash
# The catalogue — name and description only, the startup view
python skills/_shared/list_skills.py

# One skill in full, with an index of its scripts, references and assets
python skills/_shared/list_skills.py --show friends-kitchen-ordering

# Check every skill against the specification
python skills/_shared/validate_skills.py
python skills/_shared/validate_skills.py --json --strict
```

`_shared/` is not a skill — the leading underscore says so, and it has no
`SKILL.md`, so a spec-compliant discoverer skips it either way. It holds the
frontmatter reader, the spec rules, the discovery helpers and the small HTTP
client the scripts share.

## Adding one

1. `skills/<name>/SKILL.md`, where `<name>` is lowercase letters, digits and
   single hyphens — and matches the directory exactly.
2. Frontmatter with `name` and `description` at minimum. Write the description
   to say **what it does and when to use it**: it is all a host has to go on
   when deciding whether to open the skill.
3. Keep the body under 500 lines. Move detail into `references/`; the whole body
   is loaded the moment the skill activates, and the reference files are not.
4. Reference other files by a path relative to the skill root
   (`references/REFERENCE.md`), one level deep.
5. `python skills/_shared/validate_skills.py <name>`.

The rules the validator applies are written out in `_shared/fkskills/spec.py`
from the specification, so this repository can answer the question offline. The
reference implementation, [`skills-ref`](https://github.com/agentskills/agentskills),
is worth running too when it is installed:

```bash
skills-ref validate ./skills/friends-kitchen-ordering
```
