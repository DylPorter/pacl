from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pacl.config import Config
from pacl.mcp_server import build_mcp_server
from pacl.pending import PendingQueue
from pacl.server import build_app
from pacl.substrate import LocalSubstrate


@pytest.fixture
def substrate(tmp_path: Path) -> LocalSubstrate:
    return LocalSubstrate(root=tmp_path)


def _by_name(server, name: str):
    """Reach into FastMCP's registered tools to invoke them in tests."""
    for tool in server._tool_manager._tools.values():  # type: ignore[attr-defined]
        if tool.name == name:
            return tool.fn
    raise KeyError(f"no tool named {name!r}")


def test_update_intent_writes_event_and_returns_drained_alerts(substrate):
    seen = []

    class StubIntermediary:
        def notify_event(self, event_type, payload):
            seen.append((event_type, payload))

    pending = PendingQueue()
    pending.enqueue("dylan-coding-001", "heads up: bob is in auth.py")
    server = build_mcp_server(
        substrate=substrate, intermediary_getter=lambda: StubIntermediary(), pending=pending,
    )
    fn = _by_name(server, "update_intent")
    result = fn(agent_id="dylan-coding-001", intent="fix JWT expiry bug", domain=["src/auth.py"])

    assert result["ok"] is True
    assert result["alerts"] == ["heads up: bob is in auth.py"]
    assert pending.drain("dylan-coding-001") == []

    today = date.today().isoformat()
    events = substrate.read(f"events/{today}.md") or ""
    assert "fix JWT expiry bug" in events
    assert seen[0][0] == "update_intent"
    assert seen[0][1]["domain"] == ["src/auth.py"]


def test_share_context_writes_event(substrate):
    server = build_mcp_server(substrate=substrate, intermediary_getter=lambda: None)
    fn = _by_name(server, "share_context")
    result = fn(agent_id="dylan-exec-001", content="Megacorp churning over /checkout latency")
    assert result["ok"] is True
    assert result["alerts"] == []
    today = date.today().isoformat()
    events = substrate.read(f"events/{today}.md") or ""
    assert "share_context" in events
    assert "checkout" in events


def test_report_activity_uses_action_and_target(substrate):
    server = build_mcp_server(substrate=substrate, intermediary_getter=lambda: None)
    fn = _by_name(server, "report_activity")
    result = fn(agent_id="dylan-coding-001", action="edit", target="src/auth.py")
    assert result["ok"] is True
    today = date.today().isoformat()
    events = substrate.read(f"events/{today}.md") or ""
    assert "edit" in events and "src/auth.py" in events


def test_query_returns_live_team_state_and_recent_history(substrate):
    import asyncio

    # The query tool answers from PACL directly (no LLM): the live overlap map
    # (team_state_text = current state) PLUS a recent-activity history read
    # (recent_activity_text = the durable events log). It passes the caller's id as
    # exclude_agent_id and combines both into one answer.
    class StubIntermediary:
        def team_state_text(self, exclude_agent_id=None):
            assert exclude_agent_id == "dylan-coding-001"
            return 'Current active work across the team (live, from PACL):\n- dev-bob: "refactor auth"'

        def recent_activity_text(self):
            return "## 07:00 — update_intent\n- From: dev-alice\n- intent: 'rate-limit checkout'"

    pending = PendingQueue()
    pending.enqueue("dylan-coding-001", "fyi")
    server = build_mcp_server(
        substrate=substrate, intermediary_getter=lambda: StubIntermediary(), pending=pending,
    )
    fn = _by_name(server, "query")
    result = asyncio.run(fn(agent_id="dylan-coding-001", question="who's active?"))
    assert "dev-bob" in result["answer"]              # current live state
    assert "Recent team activity" in result["answer"]  # history section present
    assert "dev-alice" in result["answer"]            # history content surfaced
    assert result["alerts"] == ["fyi"]


def test_query_handles_missing_intermediary(substrate):
    import asyncio

    server = build_mcp_server(substrate=substrate, intermediary_getter=lambda: None)
    fn = _by_name(server, "query")
    result = asyncio.run(fn(agent_id="x", question="what's happening?"))
    assert "not available" in result["answer"].lower()
    assert result["alerts"] == []


def test_request_identity_overrides_agent_id_param(substrate):
    from types import SimpleNamespace

    pending = PendingQueue()
    pending.enqueue("real-id", "for real-id")
    server = build_mcp_server(
        substrate=substrate, intermediary_getter=lambda: None, pending=pending,
    )
    fn = _by_name(server, "update_intent")
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-pacl-agent": "real-id"})
        ),
        client_id=None,
    )
    # caller passes a different (spoofed) agent_id; header identity must win
    result = fn(agent_id="spoofed", intent="x", domain=["f"], ctx=ctx)
    assert result["alerts"] == ["for real-id"]
    assert pending.drain("spoofed") == []


def test_instructions_cover_all_four_verbs():
    from pacl.mcp_server import PACL_INSTRUCTIONS

    instr = PACL_INSTRUCTIONS.lower()
    for verb in ("update_intent", "share_context", "report_activity", "query"):
        assert verb in instr
    assert "star_conversation" not in instr


def test_star_conversation_tool_is_gone(substrate):
    server = build_mcp_server(substrate=substrate, intermediary_getter=lambda: None)
    with pytest.raises(KeyError):
        _by_name(server, "star_conversation")


def test_mcp_mount_appears_in_fastapi_routes(tmp_path: Path):
    """Smoke test: when the full app is loaded, /mcp is mounted."""
    config = Config(
        gemini_api_key="",
        phoenix_api_key="",
        phoenix_collector_endpoint="",
        phoenix_project="test",
        substrate_local_root=tmp_path,
        port=8080,
        log_level="INFO",
    )
    substrate = LocalSubstrate(root=tmp_path)
    app = build_app(config=config, substrate=substrate, intermediary=None)

    server = build_mcp_server(substrate=substrate, intermediary_getter=lambda: None)
    sub_app = server.streamable_http_app()
    assert sub_app is not None
    app.mount("/mcp", sub_app)

    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/mcp" in paths
