"""Interactive-demo contexts.

The demo is a live sandbox: two agent terminals side by side, a PACL on/off toggle,
and a context selector. A context defines WHO the two agents are and the pre-existing
environment they reason over — so the same coordination story can be shown for a dev
team, a student group, or a blank slate. Everything is FICTIONAL (no client data).

There are no scripts. The user drives by typing (or clicking a quick-prompt). With
PACL ON, each agent is shown what its teammate is currently doing; with PACL OFF,
it's blind — that toggle is the whole demo.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDef:
    agent_id: str
    label: str
    role: str   # one-line persona/remit, injected into the agent's context


@dataclass(frozen=True)
class Context:
    key: str
    label: str
    blurb: str
    environment: str       # pre-existing context both agents share (markdown)
    agents: list           # exactly two AgentDef
    quick_prompts: list     # suggested messages the user can fire with a click


DEV = Context(
    key="dev",
    label="Dev Team",
    blurb="Two backend engineers on the same service.",
    environment="""\
**Acme Pay** is a payments SaaS, built by a small engineering team where each
developer works through their own AI coding agent. Live services: `checkout-service`
and `auth-service` (Python/FastAPI). Open tickets: PAY-101 — add rate-limiting to
checkout; PAY-102 — refactor the auth token flow. Deploy rule: one deploy to a
service at a time.""",
    agents=[
        AgentDef("dev-alice", "dev-alice", "Backend engineer at Acme Pay."),
        AgentDef("dev-bob", "dev-bob", "Backend engineer at Acme Pay."),
    ],
    quick_prompts=[
        "I'm about to refactor the checkout payment flow in checkout-service.",
        "I'll take PAY-101 — adding rate-limiting to the checkout endpoint.",
        "Deploying my checkout-service change to prod now.",
        "I'm picking up PAY-102, the auth token refactor.",
    ],
)

STUDENTS = Context(
    key="students",
    label="Study Group",
    blurb="Two classmates on a group presentation.",
    environment="""\
You're a student in **ENVS-101**, working with classmates on a 15-minute group
presentation on renewable energy, due Friday — and each of you is using your own AI
assistant to help pull it together. It needs to cover the current energy landscape,
the main challenges, and proposed solutions, and feel like one deck.""",
    agents=[
        AgentDef("maya", "maya", "A student on the project team."),
        AgentDef("leo", "leo", "A student on the project team."),
    ],
    quick_prompts=[
        "I'll start on the slides for the current energy landscape.",
        "I'll take the section on the main challenges.",
        "I'll compile everyone's slides into the final deck and submit it.",
        "I'm pulling together stats and sources for our solutions section.",
    ],
)

EMPTY = Context(
    key="empty",
    label="Blank",
    blurb="No seeded environment — start from scratch.",
    environment="",
    agents=[
        AgentDef("agent-1", "agent-1", "An agent working alongside a teammate."),
        AgentDef("agent-2", "agent-2", "An agent working alongside a teammate."),
    ],
    quick_prompts=[
        "I'm going to start working on the shared task.",
        "What is my teammate currently working on?",
    ],
)

CONTEXTS = {c.key: c for c in (DEV, STUDENTS, EMPTY)}


def get_context(key: str) -> Context:
    return CONTEXTS.get(key) or DEV


def list_contexts() -> list:
    return [
        {
            "key": c.key,
            "label": c.label,
            "blurb": c.blurb,
            "environment": c.environment,
            "agents": [{"agent_id": a.agent_id, "label": a.label, "role": a.role} for a in c.agents],
            "quick_prompts": c.quick_prompts,
        }
        for c in CONTEXTS.values()
    ]
