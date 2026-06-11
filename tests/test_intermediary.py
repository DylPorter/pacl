from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pacl.agents.intermediary import Intermediary, EventRecord
from pacl.agents.tools import build_tools
from pacl.substrate import LocalSubstrate


@pytest.fixture
def substrate(tmp_path: Path) -> LocalSubstrate:
    return LocalSubstrate(root=tmp_path)


def _by_name(tools: list, name: str):
    return next(t for t in tools if t.__name__ == name)


def test_read_substrate_returns_content(substrate):
    substrate.write("agents/x.md", "# X\nhello")
    tools = build_tools(substrate=substrate, config=None)
    assert _by_name(tools, "read_substrate")("agents/x.md") == "# X\nhello"


def test_read_substrate_missing_returns_explicit_marker(substrate):
    tools = build_tools(substrate=substrate, config=None)
    assert _by_name(tools, "read_substrate")("agents/missing.md") == "(file does not exist)"


def test_write_substrate_persists(substrate):
    tools = build_tools(substrate=substrate, config=None)
    _by_name(tools, "write_substrate")("agents/new.md", "# new content")
    assert substrate.read("agents/new.md") == "# new content"


def test_list_substrate_returns_paths(substrate):
    substrate.write("agents/a.md", "A")
    substrate.write("agents/b.md", "B")
    tools = build_tools(substrate=substrate, config=None)
    listing = sorted(_by_name(tools, "list_substrate")("agents"))
    assert listing == ["agents/a.md", "agents/b.md"]


def test_push_notification_enqueues_to_queue(substrate):
    """push_notification appends to the notification queue (drained into pending after the run)."""
    queue: list = []
    tools = build_tools(substrate=substrate, config=None, notification_queue=queue)
    push = _by_name(tools, "push_notification")
    push("agent-1", "hello")
    assert queue == [{"agent_id": "agent-1", "message": "hello"}]


def test_append_substrate_grows_file(substrate):
    tools = build_tools(substrate=substrate, config=None)
    append = _by_name(tools, "append_substrate")
    append("events/today.md", "line 1\n")
    append("events/today.md", "line 2\n")
    assert substrate.read("events/today.md") == "line 1\nline 2\n"


@pytest.mark.asyncio
async def test_agent_crafted_notifications_land_in_pending(substrate):
    """Agent calls push_notification → enqueued → flushed to the per-agent pending queue.

    The agent decides the overlap is real and crafts custom messages; those must
    land in pending. There is no Python fallback — the agent's calls are the only path.
    """
    intermediary = Intermediary(substrate=substrate, config=None)
    intermediary._agent_scopes["agent-a"] = {
        "intent": "writing migration",
        "scope": {"db/migrate.py"},
        "updated_at": "2026-05-30T09:00:00",
    }

    async def _agent_notifies(prompt: str) -> str:
        intermediary._notification_queue.append({"agent_id": "agent-a", "message": "custom: agent-b is touching db/migrate.py"})
        intermediary._notification_queue.append({"agent_id": "agent-b", "message": "custom: agent-a is touching db/migrate.py"})
        return "done"

    intermediary._run_message = _agent_notifies
    intermediary.notify_event("update_intent", {
        "agent_id": "agent-b", "intent": "adding schema column",
        "domain": ["db/migrate.py"], "timestamp": "2026-05-30T09:05:00",
    })
    record = intermediary._event_queue.get_nowait()
    await intermediary._safe_run("", acting_id=record.acting_id, acting_intent=record.acting_intent)

    assert intermediary.pending.drain("agent-a")[0].startswith("custom:")
    assert intermediary.pending.drain("agent-b")[0].startswith("custom:")


@pytest.mark.asyncio
async def test_notify_event_puts_to_queue(substrate):
    """notify_event should enqueue the event, not immediately invoke the agent."""
    intermediary = Intermediary(substrate=substrate, config=None)

    runs: list[str] = []

    async def _mock_run(prompt: str) -> str:
        runs.append(prompt)
        return ""

    intermediary._run_message = _mock_run

    intermediary.notify_event("update_intent", {
        "agent_id": "a", "intent": "x", "domain": ["f.py"], "timestamp": "t",
    })

    assert intermediary._event_queue.qsize() == 1
    assert len(runs) == 0


@pytest.mark.asyncio
async def test_loop_drains_queue_and_runs_agent(substrate):
    """Background loop must drain pending events and invoke the agent once per batch."""
    intermediary = Intermediary(substrate=substrate, config=None, batch_window=0.05)

    prompts_received: list[str] = []

    async def _mock_run(prompt: str) -> str:
        prompts_received.append(prompt)
        return ""

    intermediary._run_message = _mock_run

    intermediary._event_queue.put_nowait(EventRecord(
        event_type="update_intent",
        payload={"agent_id": "a", "intent": "x", "domain": ["f.py"], "timestamp": "t"},
        acting_id="a", acting_intent="x",
    ))
    intermediary._event_queue.put_nowait(EventRecord(
        event_type="update_intent",
        payload={"agent_id": "b", "intent": "y", "domain": ["g.py"], "timestamp": "t"},
        acting_id="b", acting_intent="y",
    ))

    await intermediary.start_loop()
    await asyncio.sleep(0.2)
    await intermediary.stop_loop()

    assert len(prompts_received) == 1


@pytest.mark.asyncio
async def test_loop_stop_is_clean(substrate):
    """stop_loop must cancel the background task without raising."""
    intermediary = Intermediary(substrate=substrate, config=None)

    async def _mock_run(prompt: str) -> str:
        return ""

    intermediary._run_message = _mock_run
    await intermediary.start_loop()
    await intermediary.stop_loop()
    assert intermediary._loop_task is None or intermediary._loop_task.done()


def test_build_batch_prompt_includes_all_events(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)

    batch = [
        EventRecord("update_intent", {"agent_id": "a", "intent": "write auth", "domain": ["auth.py"], "timestamp": "T1"}, "a", "write auth"),
        EventRecord("report_activity", {"agent_id": "b", "summary": "finished login"}, "b", "(unknown)"),
    ]
    prompt = intermediary._build_batch_prompt(batch)

    assert "update_intent" in prompt
    assert "report_activity" in prompt
    assert "write auth" in prompt
    assert "finished login" in prompt


def test_build_batch_prompt_includes_working_memory(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    substrate.write("agents/intermediary-self.md", "# Memory\nLast run: noticed pattern X.")

    batch = [
        EventRecord("update_intent", {"agent_id": "a", "intent": "x", "domain": [], "timestamp": "T"}, "a", "x"),
    ]
    prompt = intermediary._build_batch_prompt(batch)

    assert "noticed pattern X" in prompt


def test_build_batch_prompt_includes_pending_evals_when_present(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    substrate.write(
        "agents/pending-evals.md",
        "- agent_id: agent-a | sent_at: 2026-05-30T12:00:00Z | message: Heads-up...",
    )

    batch = [
        EventRecord("update_intent", {"agent_id": "a", "intent": "x", "domain": [], "timestamp": "T"}, "a", "x"),
    ]
    prompt = intermediary._build_batch_prompt(batch)

    assert "[EVAL REQUIRED]" in prompt
    assert "agent-a" in prompt
    assert "annotate_span" in prompt


@pytest.mark.asyncio
async def test_flush_notifications_writes_pending_evals(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    intermediary._notification_queue.append({"agent_id": "x", "message": "test alert"})

    await intermediary._flush_notifications(acting_id="src", acting_intent="test")

    content = substrate.read("agents/pending-evals.md")
    assert content is not None
    assert "agent_id: x" in content


@pytest.mark.asyncio
async def test_flush_notifications_clears_pending_evals_when_none_sent(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    substrate.write("agents/pending-evals.md", "stale data from prior run")

    await intermediary._flush_notifications(acting_id="src", acting_intent="test")

    assert substrate.read("agents/pending-evals.md") is None


@pytest.mark.asyncio
async def test_run_with_retry_retries_on_503(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    calls = {"n": 0}

    async def flaky(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("503 UNAVAILABLE: model is overloaded")
        return "ok"

    intermediary._run_message = flaky
    result = await intermediary._run_with_retry("prompt", attempts=3, base_delay=0)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_run_with_retry_retries_on_transport_disconnect(substrate):
    """A transient transport disconnect (httpx.RemoteProtocolError) is retried."""
    import httpx

    inter = Intermediary(substrate=substrate, config=None)
    calls = {"n": 0}

    async def flaky(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return "ok"

    inter._run_message = flaky
    result = await inter._run_with_retry("p", attempts=3, base_delay=0)
    assert result == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_with_retry_does_not_retry_non_503(substrate):
    intermediary = Intermediary(substrate=substrate, config=None)
    calls = {"n": 0}

    async def boom(_prompt: str) -> str:
        calls["n"] += 1
        raise ValueError("bad input")

    intermediary._run_message = boom
    with pytest.raises(ValueError):
        await intermediary._run_with_retry("prompt", attempts=3, base_delay=0)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_flush_enqueues_alerts_to_pending_queue(substrate):
    """Piggyback egress: delivered alerts land in the recipient's pending queue."""
    inter = Intermediary(substrate=substrate, config=None)
    inter._notification_queue = [{"agent_id": "a1", "message": "coordinate on auth.py"}]
    await inter._flush_notifications(acting_id="a2", acting_intent="x")
    assert inter.pending.drain("a1") == ["coordinate on auth.py"]


@pytest.mark.asyncio
async def test_flush_notifications_logs_alert_emitted_event(substrate):
    """Delivering a notification logs an alert_emitted event to the events feed."""
    from datetime import date

    intermediary = Intermediary(substrate=substrate, config=None)
    intermediary._notification_queue.append({"agent_id": "x", "message": "heads up"})

    await intermediary._flush_notifications(acting_id="src", acting_intent="t")

    events = substrate.read(f"events/{date.today().isoformat()}.md") or ""
    assert "alert_emitted" in events
