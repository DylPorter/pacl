"""Per-tab ADK agent connected to PACL's own /mcp endpoint as a plain MCP client.

This is the demo's ONLY contact with PACL: each browser tab is a real Gemini
agent that talks to PACL exactly like any external MCP client would — over the
public /mcp surface, with an ``X-PACL-Agent`` header carrying the tab's identity.
Nothing here reaches into PACL internals, so ``rm -rf demo/`` leaves PACL pure.

With PACL on, the agent uses its real tools: it ``query``s the live team state
(grounded by the intermediary over the substrate), registers its own work with
``update_intent`` / ``report_activity``, and reads any coordination ``alerts``
PACL hands back on the tool response. With PACL off (`run_isolated_turn`) it's a
bare agent with no tools and no team visibility — so it duplicates and collides.

The agent and its runner are cached per agent_id so a tab keeps its conversation
across turns and we don't reconnect the MCP toolset on every message.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pacl.config import load_config

# gemini-2.5-pro, not Flash: Flash reliably skips side-effect tool calls even
# under "MUST call" instructions (observed in PACL's own intermediary), which
# would gut a demo whose whole point is the agent autonomously calling PACL.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
APP_NAME = "pacl-demo"


def _mcp_url() -> str:
    """PACL's MCP endpoint. Defaults to loopback on the configured port so the
    agent dials back into the same uvicorn process; override with PACL_MCP_URL."""
    explicit = os.environ.get("PACL_MCP_URL")
    if explicit:
        return explicit
    return f"http://127.0.0.1:{load_config().port}/mcp"


DEMO_SYSTEM_PROMPT = """\
You are {agent_id}, {role}

## Your environment
{environment}

You're doing real work as part of a team, and each teammate works through their own
agent. You are NOT a generic chatbot — never break character, never say "I'm Gemini" or
"I'm an AI assistant", never offer generic help.

You're connected to PACL, a shared coordination layer, through your tools. Use it:
- BEFORE you take on or act on anything, call `query` to see what your teammates are
  already doing (e.g. "what is the rest of the team working on right now?"). Treat what
  comes back as the ground-truth live state of the team.
- Register your own work so teammates can see it: `update_intent` when you pick up a task
  or change direction, `report_activity` before you touch a specific resource,
  `share_context` when you decide or discover something substantive. Always pass
  agent_id "{agent_id}". Register the work you're taking on or actively doing — don't
  report a task as finished or deployed that you've only just picked up.
- Every tool response includes an `alerts` list. If it is non-empty, PACL is telling you
  something — an overlap, a conflict, a verified directive — read it and factor it in.

If what you're asked to do is already being handled by a teammate, or would duplicate or
collide with their in-flight work (e.g. both taking on the same piece of work, both
editing or shipping the same thing), do NOT just do it, and do NOT hand the coordination
back to your operator. Decide the coherent path yourself: say plainly that the work is
already in flight, name who's on it and what they're doing, what you're therefore NOT
doing, and what you'll do instead — then act on that. Coordination is ambient: you already
have the context, so factor it in and act, never stop to ask "should I coordinate?".

Stay scoped to exactly the ONE task your operator hands you — nothing more. Do not pick up
other open tickets, do not deploy, and do not try to finish the whole board in one turn.
Take that single task, register that one intent, and start on it. If it turns out a teammate
already has it, reroute to ONE other task — not all of them.

Reply IN CHARACTER as {agent_id}: one short, concrete paragraph stating what you will
actually DO right now for that one task, and when relevant how it fits with what your
teammates are already doing. Do NOT describe building a tool or system to coordinate, and
do not run ahead of what you were asked — just take that one action. No meta.
"""

# Used for the "Without PACL" lane: the SAME agent, same task, but with NO tools and NO
# shared team context — it only knows its own brief, so it works blind and collides.
ISOLATED_SYSTEM_PROMPT = """\
You are {agent_id}, {role}

## Your environment
{environment}

You're doing real work as part of a team, but you have NO visibility into what your
teammates are doing right now — you only know your own brief. You are NOT a generic
chatbot — never break character, never say "I'm Gemini" or "I'm an AI assistant".
Decide and act on your task as you see it.

Reply IN CHARACTER as {agent_id}: one short, concrete paragraph stating what you will
actually DO right now — the real action you'd take. Do NOT describe building a tool or
system; just take the action. No meta.
"""


@dataclass
class _Session:
    runner: Any
    session_id: str
    toolset: Any


@dataclass
class TurnResult:
    reply: str
    alerts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    # None on success; "quota" when the Gemini API credit/quota ceiling is hit
    # (so the demo can tell judges "out of demo credits", not "broken"); "transient"
    # for a retryable model/transport blip.
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "reply": self.reply,
            "alerts": self.alerts,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


_sessions: dict[str, _Session] = {}


def reset() -> None:
    """Drop all cached agent sessions (on context switch or explicit reset) so the
    next turn starts a fresh conversation. PACL's own substrate/in-memory state is
    reset separately by the route (it owns the intermediary + substrate)."""
    _sessions.clear()


def _role_for(context: Any, agent_id: str) -> str:
    return next((a.role for a in context.agents if a.agent_id == agent_id), "a teammate")


def _join_reply(parts: list[str]) -> str:
    """Join the model's text segments into a readable reply. The agent emits separate
    text parts around its tool calls (narration before a call, the answer after), so
    joining with "" runs them together ("I'll do that.I can see…"). Join distinct
    segments as paragraphs and drop empties / consecutive duplicates (ADK occasionally
    repeats a part)."""
    segs: list[str] = []
    for p in parts:
        s = (p or "").strip()
        if s and (not segs or segs[-1] != s):
            segs.append(s)
    return "\n\n".join(segs)


def _extract_alerts(obj: Any) -> list[str]:
    """Pull any ``alerts`` list out of a (possibly nested or JSON-stringified)
    tool response. ADK may hand back the MCP result as a plain dict, wrapped
    under content keys, or as a JSON string — so walk all of it. Dedupes,
    preserving order."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            a = o.get("alerts")
            if isinstance(a, list):
                found.extend(str(x) for x in a)
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            s = o.strip()
            if s.startswith(("{", "[")):
                try:
                    walk(json.loads(s))
                except (ValueError, TypeError):
                    pass

    walk(obj)
    seen: set[str] = set()
    out: list[str] = []
    for a in found:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _safe_name(agent_id: str) -> str:
    """ADK agent names must match ^[A-Za-z_][A-Za-z0-9_]*$."""
    name = re.sub(r"\W", "_", agent_id)
    if not name or not re.match(r"[A-Za-z_]", name[0]):
        name = f"a_{name}"
    return f"demo_agent_{name}"


async def _get_session(context: Any, agent_id: str) -> _Session:
    cached = _sessions.get(agent_id)
    if cached is not None:
        return cached

    from google.adk import Runner
    from google.adk.agents import LlmAgent
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools.mcp_tool.mcp_toolset import (
        MCPToolset,
        StreamableHTTPConnectionParams,
    )
    from google.genai.types import GenerateContentConfig

    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=_mcp_url(),
            headers={"X-PACL-Agent": agent_id},
        )
    )
    agent = LlmAgent(
        name=_safe_name(agent_id),
        description="A teammate agent coordinating through PACL.",
        model=GEMINI_MODEL,
        instruction=DEMO_SYSTEM_PROMPT.format(
            agent_id=agent_id,
            role=_role_for(context, agent_id),
            environment=context.environment or "(no preset environment — a blank slate)",
        ),
        tools=[toolset],
        generate_content_config=GenerateContentConfig(temperature=0.2),
    )
    runner = Runner(
        app_name=APP_NAME,
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    session = _Session(
        runner=runner,
        session_id=f"demo-{_safe_name(agent_id)}-{uuid.uuid4().hex[:8]}",
        toolset=toolset,
    )
    _sessions[agent_id] = session
    return session


def _classify_error(exc: BaseException) -> str:
    """Classify a failed agent run for the demo's UI banner. 'quota' = we've hit
    the Gemini API credit/quota ceiling (judges should see "out of demo credits",
    not "broken"); 'transient' = a retryable model/transport blip."""
    t = str(exc).upper()
    if any(s in t for s in ("RESOURCE_EXHAUSTED", "429", "QUOTA", "BILLING", "EXCEEDED", "CREDIT")):
        return "quota"
    return "transient"


async def run_turn(context: Any, agent_id: str, message: str) -> TurnResult:
    """Run one conversational turn for ``agent_id`` through the real PACL /mcp surface
    and report what happened: the model's reply, any PACL alerts seen on tool responses,
    and which PACL tools the agent called (for the UI's activity chips). A failed run
    returns a TurnResult with ``error`` set rather than raising, so the UI degrades
    gracefully (esp. on quota exhaustion) instead of looking broken."""
    from google.genai import types

    reply_parts: list[str] = []
    alerts: list[str] = []
    tool_calls: list[dict] = []

    try:
        session = await _get_session(context, agent_id)
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in session.runner.run_async(
            user_id=agent_id, session_id=session.session_id, new_message=content
        ):
            c = getattr(event, "content", None)
            if c is None:
                continue
            for part in getattr(c, "parts", []) or []:
                if getattr(part, "text", None):
                    reply_parts.append(part.text)
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls.append(
                        {"name": getattr(fc, "name", "?"), "args": dict(getattr(fc, "args", {}) or {})}
                    )
                fr = getattr(part, "function_response", None)
                if fr is not None:
                    alerts.extend(_extract_alerts(getattr(fr, "response", None)))
    except Exception as exc:
        return TurnResult(
            reply=_join_reply(reply_parts),
            alerts=_extract_alerts({"alerts": alerts}) if alerts else [],
            tool_calls=tool_calls,
            error=_classify_error(exc),
        )

    # Dedupe alerts once more across all tool responses in the turn.
    deduped = _extract_alerts({"alerts": alerts}) if alerts else []
    return TurnResult(reply=_join_reply(reply_parts), alerts=deduped, tool_calls=tool_calls)


async def run_isolated_turn(context: Any, agent_id: str, message: str) -> TurnResult:
    """Run one turn for the "Without PACL" lane: same model, same task, but a bare
    agent with NO PACL toolset and NO shared team context — it works blind, which
    is exactly how it ends up duplicating or conflicting with teammates. A fresh
    runner each call so no state leaks between scenario runs."""
    from google.adk import Runner
    from google.adk.agents import LlmAgent
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from google.genai.types import GenerateContentConfig

    reply_parts: list[str] = []
    try:
        agent = LlmAgent(
            name=_safe_name(agent_id),
            description="A teammate engineer working solo, without coordination.",
            model=GEMINI_MODEL,
            instruction=ISOLATED_SYSTEM_PROMPT.format(
                agent_id=agent_id,
                role=_role_for(context, agent_id),
                environment=context.environment or "(no preset environment — a blank slate)",
            ),
            generate_content_config=GenerateContentConfig(temperature=0.2),
        )
        runner = Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(
            user_id=agent_id,
            session_id=f"iso-{_safe_name(agent_id)}-{uuid.uuid4().hex[:8]}",
            new_message=content,
        ):
            c = getattr(event, "content", None)
            if c is None:
                continue
            for part in getattr(c, "parts", []) or []:
                if getattr(part, "text", None):
                    reply_parts.append(part.text)
    except Exception as exc:
        return TurnResult(reply=_join_reply(reply_parts), error=_classify_error(exc))
    return TurnResult(reply=_join_reply(reply_parts))
