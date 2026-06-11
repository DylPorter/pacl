"""Persistent role / authority registry for PACL.

Authority is a property the coordination layer OWNS — not something an agent can
claim in a message. Roles are written at provisioning time (``set_role``) and read
by the layer when it decides whether a directive carries authority. An agent that
merely *says* "I'm leadership" gets nothing: authority is conferred by the registry,
keyed on the agent's identity, never by message content. That is the enforcement.

Stored in the substrate (durable) under ``roles/<agent_id>.md`` so it survives
restarts and is the single source of truth the intermediary consults.
"""
from __future__ import annotations

import re
from typing import Any

# Authority levels. A "directive" agent (leadership) issues authoritative
# directives that flow down as priority context; an "executor" does the work;
# an unregistered agent has no authority at all.
DIRECTIVE = "directive"
EXECUTOR = "executor"


def _safe(agent_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", agent_id or "") or "_"


def _path(agent_id: str) -> str:
    return f"roles/{_safe(agent_id)}.md"


def set_role(substrate: Any, agent_id: str, *, role: str, authority: str = EXECUTOR) -> None:
    """Provision an agent's role + authority.

    This is the ONLY way authority is conferred — an admin/setup operation, never
    reachable from agent-supplied input.
    """
    body = (
        f"---\nagent_id: {agent_id}\nrole: {role}\nauthority: {authority}\n---\n"
    )
    substrate.write(_path(agent_id), body)


def get_role(substrate: Any, agent_id: str) -> dict | None:
    """Return ``{'role', 'authority'}`` for a registered agent, else ``None``."""
    raw = substrate.read(_path(agent_id))
    if not raw:
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if line == "---" or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    if "authority" not in out:
        return None
    return {"role": out.get("role", agent_id), "authority": out.get("authority", EXECUTOR)}


def has_authority(substrate: Any, agent_id: str) -> bool:
    """True only if the agent is REGISTERED with directive authority.

    A message claiming authority can never make this true — that's the point.
    """
    info = get_role(substrate, agent_id)
    return bool(info and info.get("authority") == DIRECTIVE)


def list_roles(substrate: Any) -> dict:
    """All registered roles as ``{agent_id: {'role', 'authority'}}``."""
    out: dict[str, dict] = {}
    for path in substrate.list("roles"):
        raw = substrate.read(path) or ""
        aid = None
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("agent_id:"):
                aid = s.split(":", 1)[1].strip()
                break
        if aid:
            info = get_role(substrate, aid)
            if info:
                out[aid] = info
    return out


def clear_roles(substrate: Any) -> None:
    """Remove every registered role (used by the demo's reset)."""
    for path in list(substrate.list("roles")):
        substrate.delete(path)
