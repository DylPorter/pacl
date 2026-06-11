from __future__ import annotations

import pytest

from pacl.agents.intermediary import Intermediary
from pacl.evals.harness import Event, Scenario, run_scenario
from pacl.substrate import LocalSubstrate


@pytest.mark.asyncio
async def test_no_overlap_scenario_stays_silent(tmp_path):
    scenario = Scenario(
        name="no-overlap",
        description="two agents declare intent on different files",
        events=[
            Event("update_intent", {
                "agent_id": "agent-a", "intent": "fix billing rounding",
                "domain": ["src/billing.py"], "timestamp": "T1",
            }),
            Event("update_intent", {
                "agent_id": "agent-b", "intent": "tweak dashboard layout",
                "domain": ["src/ui/dashboard.tsx"], "timestamp": "T2",
            }),
        ],
        expect_no_alert=True,
    )

    async def noop(_prompt: str) -> str:
        return ""

    result = await run_scenario(
        scenario, substrate=LocalSubstrate(root=tmp_path),
        batch_window=0.05, wait=0.5, run_message=noop,
    )

    assert result.passed, result.failures
    assert result.notifications["agent-a"] == []
    assert result.notifications["agent-b"] == []


@pytest.mark.asyncio
async def test_unmet_expectation_fails_the_scenario(tmp_path):
    scenario = Scenario(
        name="should-fail",
        description="expects an alert the no-op LLM will never send",
        events=[
            Event("update_intent", {
                "agent_id": "lonely-agent", "intent": "do a thing",
                "domain": ["src/solo.py"], "timestamp": "T1",
            }),
        ],
        expect_alert_to=["lonely-agent"],
    )

    async def noop(_prompt: str) -> str:
        return ""

    result = await run_scenario(
        scenario, substrate=LocalSubstrate(root=tmp_path),
        batch_window=0.05, wait=0.5, run_message=noop,
    )

    assert not result.passed
    assert any("lonely-agent" in f for f in result.failures)


@pytest.mark.asyncio
async def test_escalation_scenario_detects_ticket(tmp_path):
    sub = LocalSubstrate(root=tmp_path)
    scenario = Scenario(
        name="escalation",
        description="shared exec complaint should yield a ticket",
        events=[
            Event("share_context", {
                "agent_id": "dylan-exec",
                "content": "Customer megacorp_42 hitting timeouts on /checkout, threatening to churn.",
                "timestamp": "T1",
            }),
        ],
        expect_ticket=True,
    )

    async def writes_ticket(_prompt: str) -> str:
        sub.write("tickets/t-2026-001.md", "# Escalation\ncustomer megacorp_42 /checkout latency")
        return "wrote ticket"

    result = await run_scenario(
        scenario, substrate=sub, batch_window=0.05, wait=0.5, run_message=writes_ticket,
    )

    assert result.passed, result.failures
    assert result.tickets


@pytest.mark.asyncio
async def test_piggyback_delivers_after_run(tmp_path):
    """A queued alert lands in the recipient's pending queue (piggyback)."""
    substrate = LocalSubstrate(root=tmp_path)
    inter = Intermediary(substrate=substrate, config=None)
    inter._notification_queue = [{"agent_id": "a1", "message": "coordinate on auth.py"}]
    await inter._flush_notifications(acting_id="a2", acting_intent="refactor auth")
    assert inter.pending.drain("a1") == ["coordinate on auth.py"]
