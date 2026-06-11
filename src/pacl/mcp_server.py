"""MCP server — the single agent-facing surface for PACL.

Mounts as a sub-app at /mcp on the FastAPI service. Any MCP-compatible agent
(any client speaking the MCP wire protocol) discovers the tools
via standard `tools/list` and connects with one line of config:

    {
      "mcpServers": {
        "pacl": { "url": "https://<host>/mcp", "headers": {"X-PACL-Agent": "<id>"} }
      }
    }

Four broad verbs (update_intent / share_context / report_activity / query).
Receiving needs no tool: every response carries the caller's pending
coordination alerts, drained from the per-agent queue (piggyback delivery).

Identity is resolved from the request (header / client_id / _meta) when
available, falling back to the explicit `agent_id` param for the demo. See
pacl.mcp_identity. Post-2026-07-28 this becomes the OAuth principal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from pacl.events import append_event
from pacl.mcp_identity import resolve_agent_id, DEFAULT_AGENT_ID
from pacl.pending import PendingQueue


PACL_INSTRUCTIONS = """\
PACL (Proactive Agent Coordination Layer) keeps a team of independent agents
coherent. Share what you're doing, and PACL hands back — on the response to any
call — coordination you need: overlapping work, escalations, and context other
agents produced that you'd otherwise be missing.

Call these tools LIBERALLY — on pretty much every meaningful step. Over-sharing
is cheap and safe; PACL stays silent unless something genuinely needs you.

- New goal or change of direction (even rough) -> `update_intent`.
- Anything substantive discussed, decided, or discovered -> `share_context`.
- About to act on a file/doc/resource -> `report_activity`.
- Want to know what the rest of the team is doing -> `query`.

Every response includes an `alerts` list. If it's non-empty, those are messages
PACL has for you — read them and act before continuing. You don't manage
sessions, signatures, or delivery; the connection handles that.
"""


def build_mcp_server(
    *, substrate, intermediary_getter, pending: PendingQueue | None = None
) -> FastMCP:
    """Build the PACL MCP server.

    `intermediary_getter` is a callable returning the current intermediary
    instance (or None). Passing a callable rather than the instance lets the
    MCP server tolerate intermediary construction happening after server
    setup (which is the normal lifecycle).

    `pending` is the shared PendingQueue the intermediary enqueues alerts into;
    every tool response drains the caller's alerts from it. Must be the SAME
    object the intermediary holds (wired in _load_app).
    """
    pending = pending or PendingQueue()
    server = FastMCP(
        name="pacl",
        instructions=PACL_INSTRUCTIONS,
        streamable_http_path="/",  # mount at root so the FastAPI mount path is the full prefix
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    def _emit(event_type: str, payload: dict) -> dict:
        """Append the event, notify the intermediary, and drain the caller's alerts."""
        append_event(substrate, event_type, payload)
        intermediary = intermediary_getter()
        if intermediary is not None:
            intermediary.notify_event(event_type, payload)
        return {"ok": True, "alerts": pending.drain(payload["agent_id"])}

    def _aid(agent_id_param: str, ctx) -> str:
        """Prefer request-bound identity; fall back to the explicit param."""
        resolved = resolve_agent_id(ctx)
        return resolved if resolved != DEFAULT_AGENT_ID else agent_id_param

    @server.tool()
    def update_intent(
        agent_id: str, intent: str, domain: list[str], ctx: Context | None = None
    ) -> dict:
        """Tell PACL what you (your human) are now trying to do. Call this whenever
        the user states a new goal, picks up a task, or changes direction — even a
        rough one. PACL uses it to spot overlapping work across agents and to brief
        you with relevant context other agents already produced.

        Args:
            agent_id: your agent's id.
            intent: natural-language description of the goal ("fix the checkout
                latency bug", "draft the Q3 board deck").
            domain: freeform tags for what this touches — files, modules, topics,
                or initiatives (["src/auth.py"], ["checkout", "billing"]). A hint,
                not a strict key.

        Returns: {"ok": True, "alerts": [...]} — alerts are coordination messages
        PACL has for you; act on them.
        """
        return _emit("update_intent", {
            "agent_id": _aid(agent_id, ctx), "intent": intent, "domain": domain,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @server.tool()
    def share_context(agent_id: str, content: str, ctx: Context | None = None) -> dict:
        """Hand PACL a chunk of substance other agents might need — a conversation,
        a decision, a finding, anything that would otherwise die at the handoff.
        Call it liberally whenever something meaningful is discussed or decided.

        Args:
            agent_id: your agent's id.
            content: the raw text to share.

        Returns: {"ok": True, "alerts": [...]}.
        """
        return _emit("share_context", {
            "agent_id": _aid(agent_id, ctx), "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @server.tool()
    def report_activity(
        agent_id: str, action: str, target: str, ctx: Context | None = None
    ) -> dict:
        """Tell PACL you're acting on a resource right now. Call before you act so
        PACL can catch real-time collisions (two agents on the same thing).

        Args:
            agent_id: your agent's id.
            action: any verb — "edit", "read", "update", "draft", "delete".
            target: the resource — a file, document, table, topic.

        Returns: {"ok": True, "alerts": [...]}.
        """
        return _emit("report_activity", {
            "agent_id": _aid(agent_id, ctx), "action": action, "target": target,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @server.tool()
    async def query(agent_id: str, question: str, ctx: Context | None = None) -> dict:
        """Ask PACL about the team — both what everyone is doing NOW and what they've done.
        The answer has two parts, both read straight from PACL (no per-query model call):
          1. live coordination state — each other agent's current active intent + what it's
             touching, from the authoritative overlap map PACL uses to detect collisions
             (reliable and instant);
          2. recent team activity (history) — the recent tail of the durable events log,
             so questions like "what has alice worked on so far?" are answerable too.

        Args:
            agent_id: your agent's id.
            question: free-form natural language. The answer carries the live state +
                recent history; reason over it to answer the specific question asked.

        Returns: {"answer": str, "alerts": [...]}.
        """
        aid = _aid(agent_id, ctx)
        intermediary = intermediary_getter()
        if intermediary is not None:
            answer = (
                intermediary.team_state_text(exclude_agent_id=aid)
                + "\n\n## Recent team activity (history, from the durable log)\n"
                + intermediary.recent_activity_text()
            )
        else:
            answer = "(intermediary not available — check server logs)"
        return {"answer": answer, "alerts": pending.drain(aid)}

    return server
