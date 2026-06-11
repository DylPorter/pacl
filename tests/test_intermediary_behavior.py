from __future__ import annotations

import pytest

from pacl.agents.intermediary import Intermediary
from pacl.substrate import LocalSubstrate


def _inter(tmp_path):
    return Intermediary(substrate=LocalSubstrate(root=tmp_path), config=None)


def test_classify_alert_acted_vs_ignored():
    """The Python self-eval floor: a recipient who acted this batch -> acted_on, else ignored."""
    from pacl.agents.intermediary import _classify_alert

    assert _classify_alert("agent-a", {"agent-a", "agent-b"}) == ("acted_on", 1.0)
    assert _classify_alert("agent-c", {"agent-a", "agent-b"}) == ("ignored", 0.0)
    assert _classify_alert("agent-a", set()) == ("ignored", 0.0)


def test_no_overlap_precompute(tmp_path):
    """The intermediary never pre-computes overlap — the model must notice it itself."""
    inter = _inter(tmp_path)
    inter._agent_scopes["agent-a"] = {"intent": "x", "scope": {"src/auth.py"}, "updated_at": "t1"}
    inter.notify_event("update_intent", {
        "agent_id": "agent-b", "intent": "y", "domain": ["src/auth.py"], "timestamp": "t2",
    })
    record = inter._event_queue.get_nowait()
    assert not hasattr(record, "overlaps")


def test_uses_general_objective_prompt(tmp_path):
    from pacl.agents.intermediary import EventRecord

    inter = _inter(tmp_path)
    batch = [EventRecord("update_intent", {"agent_id": "a", "intent": "x", "domain": ["f.py"], "timestamp": "t"}, "a", "x")]
    prompt = inter._build_batch_prompt(batch)

    assert "[SYSTEM-DETECTED OVERLAP]" not in prompt
    assert "examples, not a checklist" in prompt


def test_intermediary_model_defaults_and_overrides(tmp_path):
    from pacl.agents.intermediary import Intermediary, DEFAULT_MODEL

    assert _inter(tmp_path).model == DEFAULT_MODEL
    overridden = Intermediary(
        substrate=LocalSubstrate(root=tmp_path), config=None, model="gemini-9.9-test",
    )
    assert overridden.model == "gemini-9.9-test"


@pytest.mark.asyncio
async def test_no_python_fallback_on_overlap(tmp_path):
    """There is no pre-compute + no fallback: a no-op LLM yields silence even for overlapping work."""
    from pacl.evals.harness import Event, Scenario, run_scenario

    scenario = Scenario(
        name="agnostic-overlap",
        description="two agents same file, no-op LLM",
        events=[
            Event("update_intent", {"agent_id": "agent-a", "intent": "refactor auth", "domain": ["src/auth.py"], "timestamp": "T1"}),
            Event("update_intent", {"agent_id": "agent-b", "intent": "add oauth", "domain": ["src/auth.py"], "timestamp": "T2"}),
        ],
        expect_no_alert=True,
    )

    async def noop(_prompt: str) -> str:
        return ""

    result = await run_scenario(
        scenario, substrate=LocalSubstrate(root=tmp_path), wait=0.5, run_message=noop,
    )

    assert result.passed, result.failures
    assert result.notifications["agent-a"] == []
    assert result.notifications["agent-b"] == []
