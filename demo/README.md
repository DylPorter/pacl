# PACL two-tab demo

A no-login sandbox: two browser tabs are two real Gemini agents, each connected to
PACL's own MCP endpoint as an ordinary client. Work on the same thing in both tabs
and PACL coordinates them — overlap warnings and context handoffs surface in the
agents' own replies.

## Run

```bash
# from the repo root
PACL_MODE=agnostic uv run python -m uvicorn demo.app:app --port 8090
# open http://127.0.0.1:8090/demo
```

Pure PACL (no demo) is still `uvicorn pacl.server:app`. The demo only *adds* routes;
it never modifies `pacl/`. **`rm -rf demo/` restores pristine pure-MCP PACL.**

## The 2-tab reveal

1. Open `/demo` in two browser tabs. Pick a **different** agent in each (e.g. tab 1 =
   `dev-alice`, tab 2 = `dev-bob`). Identity is per-tab (`sessionStorage`) and sent to
   PACL as the `X-PACL-Agent` header.
2. **Tab 1 (dev-alice):** "I'm starting a refactor of the checkout payment flow in
   src/checkout.py." → the agent calls `update_intent`.
3. **Tab 2 (dev-bob):** "I'm about to optimize the checkout flow — I'll edit
   src/checkout.py." → `update_intent`. PACL's intermediary coordinates in the
   background (~5–15s).
4. **Tab 2 (dev-bob), follow-up:** "Before I start editing, is anyone else touching
   this?" → the agent calls `query`, drains the piggybacked alert, and replies with
   **⚡ PACL: dev-alice is also working on src/checkout.py — coordinate…**

The scenario buttons in the sidebar prefill these steps (overlap + handoff). The
coordination is delivered on the *next* tool call after the intermediary runs — the
follow-up turn is the reliable reveal.

## Env

| var | default | meaning |
|---|---|---|
| `PACL_MCP_URL` | `http://127.0.0.1:$PORT/mcp` | where the agents dial PACL |
| `GEMINI_MODEL` | `gemini-2.5-pro` | the tab agents' model (Pro reliably calls tools; Flash is too tool-shy) |
| `PACL_MODE` | `scaffolded` | intermediary mode; the demo sets `agnostic` |

## How it stays contained

Each tab agent reaches PACL **only** through the public `/mcp` surface (an
`MCPToolset` over Streamable HTTP), exactly like any standard MCP client would — no privileged
access to PACL internals. `demo/app.py` does `from pacl.server import app` then
`app.include_router(router)`; the wiring lives entirely in `demo/`.
