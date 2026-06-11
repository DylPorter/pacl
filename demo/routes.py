"""Demo HTTP surface — a live two-agent sandbox over the REAL PACL engine.

Each turn drives a per-tab ADK agent that talks to PACL through its own /mcp
surface (see demo/agent.py): with PACL on it queries live team state, registers
its own work, and reads coordination alerts PACL pushes back; with PACL off it's
a bare, blind agent. The intermediary + substrate are PACL's real ones, shared
in-process (app.state) — the demo never fakes coordination.

    GET  /demo            -> the two-terminal chat UI
    GET  /demo/contexts   -> selectable contexts (Dev / Study Group / Blank)
    POST /demo/context    -> switch context (resets the sandbox + PACL state)
    POST /demo/chat       -> one live agent turn ({context_key, agent_id, message, pacl})
    POST /demo/reset      -> clear the sandbox + PACL state
    GET  /demo/shared     -> what PACL currently knows each agent is doing
    GET  /demo/alerts     -> drain any proactive alerts PACL has pushed for an agent
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from demo import agent
from demo.contexts import get_context, list_contexts

router = APIRouter()
_STATIC = Path(__file__).parent / "static"


class ContextRequest(BaseModel):
    key: str = Field(max_length=64)


class ChatRequest(BaseModel):
    context_key: str = Field(max_length=64)
    agent_id: str = Field(max_length=64)
    message: str = Field(max_length=4000)
    pacl: bool = True


def _reset_pacl(request: Request) -> None:
    """Reset PACL's live state for a fresh scenario: clear the intermediary's in-memory
    overlap map + pending alerts, and wipe the transient substrate (events, tickets, and
    per-agent state), preserving only the intermediary's own seed/memory doc."""
    agent.reset()
    intermediary = getattr(request.app.state, "intermediary", None)
    if intermediary is not None and hasattr(intermediary, "reset_state"):
        intermediary.reset_state()
    substrate = getattr(request.app.state, "substrate", None)
    if substrate is None:
        return
    for prefix in ("events", "tickets"):
        for path in list(substrate.list(prefix)):
            substrate.delete(path)
    for path in list(substrate.list("agents")):
        if path.rsplit("/", 1)[-1] != "intermediary-self.md":
            substrate.delete(path)


@router.get("/demo")
def demo_index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@router.get("/demo/contexts")
def demo_contexts() -> list:
    return list_contexts()


@router.post("/demo/context")
def demo_set_context(req: ContextRequest, request: Request) -> dict:
    _reset_pacl(request)
    ctx = get_context(req.key)
    return {
        "key": ctx.key,
        "label": ctx.label,
        "blurb": ctx.blurb,
        "environment": ctx.environment,
        "agents": [{"agent_id": a.agent_id, "label": a.label, "role": a.role} for a in ctx.agents],
        "quick_prompts": ctx.quick_prompts,
    }


@router.post("/demo/chat")
async def demo_chat(req: ChatRequest, request: Request) -> dict:
    ctx = get_context(req.context_key)
    agent_id = req.agent_id.strip() or "agent-1"
    if req.pacl:
        result = await agent.run_turn(ctx, agent_id, req.message)
    else:
        result = await agent.run_isolated_turn(ctx, agent_id, req.message)
    return result.as_dict()


@router.post("/demo/reset")
def demo_reset(request: Request) -> dict:
    _reset_pacl(request)
    return {"ok": True}


@router.get("/demo/shared")
def demo_shared(request: Request) -> dict:
    """What PACL currently knows each agent is doing (its live overlap-detection state)."""
    intermediary = getattr(request.app.state, "intermediary", None)
    if intermediary is None or not hasattr(intermediary, "agent_scopes_snapshot"):
        return {}
    return intermediary.agent_scopes_snapshot()


@router.get("/demo/alerts")
def demo_alerts(request: Request, agent_id: str = "") -> dict:
    """Drain any proactive coordination alerts PACL has pushed for this agent since the
    last poll — lets the UI surface async pushes that land after a turn completes."""
    pending = getattr(request.app.state, "pending", None)
    aid = agent_id.strip()
    if pending is None or not aid:
        return {"alerts": []}
    return {"alerts": pending.drain(aid)}
