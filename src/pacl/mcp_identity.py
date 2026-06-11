"""Resolve the calling agent's identity from the MCP request, not (primarily) a
model-supplied tool param.

Resolution order (first hit wins):
1. `X-PACL-Agent` header — the client sets it in its MCP config. Most reliable
   for the June-12 demo and not model-spoofable.
2. MCP `client_id` (Context property, derived from request `_meta`).
3. `_meta` clientInfo name — forward path for the 2026-07-28 RC (stateless;
   client info travels in `_meta` per request).

If none resolve, returns DEFAULT_AGENT_ID; callers fall back to the explicit
`agent_id` tool param so the demo always has a working identity. Post-2026-07-28
this becomes the OAuth principal and the param fallback is dropped.
"""

from __future__ import annotations

from typing import Any

DEFAULT_AGENT_ID = "anonymous-agent"


def resolve_agent_id(ctx: Any) -> str:
    """Best-effort agent id from the request context. Defensive: any failure
    falls through to the next source, ending at DEFAULT_AGENT_ID."""
    if ctx is None:
        return DEFAULT_AGENT_ID
    # 1. Explicit header set by the client's MCP config.
    try:
        headers = ctx.request_context.request.headers
        agent = headers.get("x-pacl-agent")
        if agent:
            return str(agent)
    except Exception:
        pass
    # 2. MCP client_id.
    try:
        cid = ctx.client_id
        if cid:
            return str(cid)
    except Exception:
        pass
    # 3. 2026-07-28 RC: clientInfo name carried in request _meta.
    try:
        meta = ctx.request_context.meta
        name = getattr(meta, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    return DEFAULT_AGENT_ID
