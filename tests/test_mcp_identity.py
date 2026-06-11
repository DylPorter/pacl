from __future__ import annotations

from types import SimpleNamespace

from pacl.mcp_identity import resolve_agent_id, DEFAULT_AGENT_ID


def test_resolves_from_header():
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-pacl-agent": "dylan-coding"})
        ),
        client_id=None,
    )
    assert resolve_agent_id(ctx) == "dylan-coding"


def test_falls_back_to_default_when_none():
    assert resolve_agent_id(None) == DEFAULT_AGENT_ID


def test_falls_back_to_default_when_empty_ctx():
    assert resolve_agent_id(SimpleNamespace()) == DEFAULT_AGENT_ID


def test_resolves_from_client_id_when_no_header():
    ctx = SimpleNamespace(request_context=SimpleNamespace(), client_id="agent-x")
    assert resolve_agent_id(ctx) == "agent-x"
