"""In-memory per-agent pending-alert queue — the egress side of piggyback delivery.

When the intermediary decides to alert an agent, that agent usually isn't mid-call,
so the alert waits here until the agent's next MCP tool call drains it (and it rides
back on that response). Alerts are transient and advisory, so they live in memory —
NOT in the markdown substrate, which is durable, LLM-readable knowledge.

Read-once (draining clears) + dedup of identical still-pending messages.

Single-process for now: enqueue (background loop) and drain (request handlers) share
one object on the same asyncio event loop, so the plain dict ops are atomic between
awaits. Cross-instance / durable delivery (Redis, Firestore, …) swaps in behind this
same interface — see the durable-storage item in the design's future work.
"""

from __future__ import annotations

from collections import defaultdict


class PendingQueue:
    def __init__(self) -> None:
        self._by_agent: dict[str, list[str]] = defaultdict(list)

    def enqueue(self, agent_id: str, message: str) -> None:
        """Buffer an alert for an agent, skipping an identical still-pending message."""
        queue = self._by_agent[agent_id]
        if message not in queue:
            queue.append(message)

    def drain(self, agent_id: str) -> list[str]:
        """Return and clear all pending alerts for an agent (read-once)."""
        return self._by_agent.pop(agent_id, [])

    def clear(self) -> None:
        """Drop every agent's pending alerts (used by the demo's reset)."""
        self._by_agent.clear()
