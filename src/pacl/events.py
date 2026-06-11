"""Events log helper. Shared between HTTP and MCP entry points.

Lives in its own module to avoid a circular import between server.py
(which builds the FastAPI app and mounts the MCP sub-app) and
mcp_server.py (which needs to append events from MCP tool calls).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from pacl.substrate import Substrate


def append_event(substrate: Substrate, event_type: str, payload: dict) -> None:
    """Append an event to the daily events log."""
    today = date.today().isoformat()
    path = f"events/{today}.md"
    existing = substrate.read(path)
    if existing is None:
        substrate.write(path, f"# Events — {today}\n\n")

    ts = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
    agent_id = payload.get("agent_id", "unknown")
    body_pieces = [f"- {k}: {v!r}" for k, v in payload.items() if k != "timestamp"]
    body = "\n".join(body_pieces)
    substrate.append(
        path,
        f"## {ts} — {event_type}\n- From: {agent_id}\n{body}\n\n",
    )
