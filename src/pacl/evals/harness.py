"""Behavioral eval harness for the PACL intermediary.

Drives the real comm-layer pipeline end-to-end (intent/activity/escalation
observations -> background loop -> live Gemini reasoning -> notifications +
substrate writes) and asserts on observable outcomes. Unlike the unit tests,
which stub the LLM, this harness is meant to run against a live model so we can
verify the *prompt-driven policy* actually fires — overlap detection, escalation
routing — not just that the plumbing is wired.

It doubles as the empirical backbone for writing PACL up: each Scenario is a
fixture with a known-correct outcome, so a run produces a pass/fail report plus
the actual alert text for quality inspection.
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pacl.agents.intermediary import Intermediary
from pacl.substrate import LocalSubstrate


@dataclass
class Event:
    """A single observation posted to the comm layer."""
    event_type: str  # "update_intent" | "report_activity" | "share_context"
    payload: dict


@dataclass
class Scenario:
    name: str
    description: str
    events: list[Event]
    expect_alert_to: list[str] = field(default_factory=list)
    expect_no_alert: bool = False
    expect_ticket: bool = False
    # Plain-language statement of the correct outcome, for the blinded LLM judge.
    # Never reveals which mode produced a result — only what *should* happen.
    ground_truth: str = ""


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    notifications: dict[str, list[str]]
    tickets: list[str]
    failures: list[str]
    used_fallback: bool


async def run_scenario(
    scenario: Scenario,
    *,
    config=None,
    substrate: LocalSubstrate | None = None,
    batch_window: float = 0.05,
    wait: float = 8.0,
    run_message=None,
    model: str | None = None,
    freshen_timestamps: bool = False,
) -> ScenarioResult:
    """Run one scenario end-to-end and evaluate its expectations.

    Pass ``run_message`` to stub the LLM call (deterministic unit testing);
    omit it to run against live Gemini via the real intermediary. Coordination
    is captured from each agent's pending queue — the piggyback egress.
    """
    own_tmp = None
    if substrate is None:
        own_tmp = tempfile.TemporaryDirectory()
        substrate = LocalSubstrate(root=Path(own_tmp.name))

    intermediary = Intermediary(
        substrate=substrate, config=config, batch_window=batch_window, model=model
    )
    if run_message is not None:
        intermediary._run_message = run_message

    # Live fixtures hard-code historical ISO timestamps, but the intent TTL is
    # measured against the real wall clock — so a 2-day-old fixture is pruned as
    # stale before it can surface as an active intent in the prompt context.
    # Freshening to "now" (order preserved) keeps active-intents live and is also
    # the realistic case (agents declaring intent seconds apart). Off by default so
    # the stubbed-LLM unit tests (which use unparseable timestamps) are untouched.
    events = scenario.events
    if freshen_timestamps:
        import copy
        from datetime import datetime, timedelta, timezone

        base = datetime.now(timezone.utc)
        n = len(scenario.events)
        events = []
        for i, ev in enumerate(scenario.events):
            payload = copy.deepcopy(ev.payload)
            if "timestamp" in payload:
                payload["timestamp"] = (base - timedelta(seconds=(n - 1 - i) * 2)).isoformat()
            events.append(Event(ev.event_type, payload))

    agent_ids: list[str] = []
    for aid in [e.payload.get("agent_id") for e in events] + scenario.expect_alert_to:
        if aid and aid not in agent_ids:
            agent_ids.append(aid)

    for ev in events:
        intermediary.notify_event(ev.event_type, ev.payload)

    # Drain everything queued into one batch and AWAIT the run to completion.
    # We deliberately do NOT use the background loop + a fixed sleep: a slow live
    # LLM run would get cancelled at the sleep deadline and the Python fallback
    # would fire, masking whether the model itself acted. wait is a safety cap.
    batch = []
    while not intermediary._event_queue.empty():
        batch.append(intermediary._event_queue.get_nowait())
    if batch:
        prompt = intermediary._build_batch_prompt(batch)
        last = batch[-1]
        try:
            await asyncio.wait_for(
                intermediary._safe_run(
                    prompt,
                    acting_id=last.acting_id,
                    acting_intent=last.acting_intent,
                ),
                timeout=wait,
            )
        except asyncio.TimeoutError:
            pass

    # Coordination is delivered via the per-agent pending queue (piggyback).
    notifications = {aid: intermediary.pending.drain(aid) for aid in agent_ids}
    tickets = list(substrate.list("tickets"))
    # No Python fallback exists anymore — every alert is agent-crafted by
    # construction. The field is retained for backward-compat with the matrix
    # reporting but is always False now.
    used_fallback = False

    failures: list[str] = []
    for aid in scenario.expect_alert_to:
        if not notifications.get(aid):
            failures.append(f"expected alert to {aid!r}, got none")

    if scenario.expect_no_alert:
        fired = {aid: msgs for aid, msgs in notifications.items() if msgs}
        if fired:
            failures.append(f"expected silence, but alerts fired: {fired}")

    if scenario.expect_ticket and not tickets:
        failures.append("expected a ticket to be written, found none")

    if own_tmp is not None:
        own_tmp.cleanup()

    return ScenarioResult(
        name=scenario.name,
        passed=not failures,
        notifications=notifications,
        tickets=tickets,
        failures=failures,
        used_fallback=used_fallback,
    )


# --------------------------------------------------------------------------
# Live scenarios — the actual eval suite, run against real Gemini via __main__.
# These are NOT unit tests; they verify the prompt-driven policy fires.
# --------------------------------------------------------------------------

LIVE_SCENARIOS: list[Scenario] = [
    Scenario(
        name="overlap-software",
        description="Two agents independently take on overlapping work (here: the same code path).",
        events=[
            Event("update_intent", {
                "agent_id": "coder-a", "intent": "fix the JWT expiry bug",
                "domain": ["src/auth.py"], "timestamp": "2026-06-02T10:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "coder-b", "intent": "patch token-expiry handling",
                "domain": ["src/auth.py"], "timestamp": "2026-06-02T10:01:30Z",
            }),
        ],
        expect_alert_to=["coder-a", "coder-b"],
    ),
    Scenario(
        name="overlap-non-software",
        description="The same overlap behavior in a non-software domain — two agents drafting the same deliverable.",
        events=[
            Event("update_intent", {
                "agent_id": "analyst-a",
                "intent": "write the competitive-landscape section of the Q3 board deck",
                "domain": ["Q3 board deck", "competitive landscape"], "timestamp": "2026-06-02T11:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "analyst-b",
                "intent": "draft the competitor comparison for the Q3 board deck",
                "domain": ["Q3 board deck", "competitive landscape"], "timestamp": "2026-06-02T11:00:40Z",
            }),
        ],
        expect_alert_to=["analyst-a", "analyst-b"],
    ),
    Scenario(
        name="no-overlap-silent",
        description="Two agents on unrelated work — the intermediary must stay silent.",
        events=[
            Event("update_intent", {
                "agent_id": "coder-a", "intent": "add a Stripe webhook handler",
                "domain": ["src/payments/stripe.py"], "timestamp": "2026-06-02T10:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "coder-b", "intent": "redesign the sidebar nav",
                "domain": ["src/ui/Sidebar.tsx"], "timestamp": "2026-06-02T10:01:00Z",
            }),
        ],
        expect_no_alert=True,
    ),
    Scenario(
        name="escalation-ticket",
        description="A flagged customer complaint should become a structured ticket for whoever can act on it.",
        events=[
            Event("share_context", {
                "agent_id": "exec-a",
                "content": (
                    "Just off a call with a customer's CTO — they're getting timeouts on "
                    "/checkout, p95 latency spiked to ~3s yesterday. They'll churn if it's "
                    "not fixed by Friday. Need someone to dig into the traces."
                ),
                "timestamp": "2026-06-02T10:05:00Z",
            }),
        ],
        expect_ticket=True,
        ground_truth=(
            "A customer-impacting escalation (CTO call, churn risk, latency spike) "
            "that needs a structured, durable record for whoever can act. Correct "
            "outcome: write a ticket capturing the issue and urgency."
        ),
    ),
    # --- Discriminating scenarios (added 2026-06-04) -----------------------
    # Designed to separate naive tag-intersection from genuine semantic
    # coordination. The matched-tag fixtures above are a layup for any
    # tag-overlap heuristic; these are not.
    Scenario(
        name="overlap-semantic-mismatched-tags",
        description=(
            "Two agents on the SAME real work, tagged with DISJOINT domain terms. "
            "The overlap is visible only by reasoning about intent meaning, never by "
            "tag intersection — so a tag-overlap heuristic is structurally blind."
        ),
        events=[
            Event("update_intent", {
                "agent_id": "coder-a", "intent": "refactor the checkout payment flow",
                "domain": ["checkout"], "timestamp": "2026-06-02T09:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "coder-b",
                "intent": "optimize how customer payments are processed at checkout",
                "domain": ["src/payment_flow.py"], "timestamp": "2026-06-02T09:01:10Z",
            }),
        ],
        expect_alert_to=["coder-a", "coder-b"],
        ground_truth=(
            "A genuine overlap: both agents are working on the checkout payment flow. "
            "Their domain tags ('checkout' vs 'src/payment_flow.py') do NOT intersect, "
            "so the conflict is only catchable by understanding intent. Correct "
            "outcome: alert both agents to coordinate."
        ),
    ),
    Scenario(
        name="false-overlap-precision",
        description=(
            "Two agents share a coarse domain tag ('billing') but do genuinely "
            "non-conflicting work on different artifacts. Tag intersection is "
            "non-empty, but there is no real collision — alerting would be noise."
        ),
        events=[
            Event("update_intent", {
                "agent_id": "coder-a", "intent": "write the user-facing FAQ for the billing page",
                "domain": ["billing"], "timestamp": "2026-06-02T09:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "coder-b",
                "intent": "rotate the API credentials for the billing service provider",
                "domain": ["billing"], "timestamp": "2026-06-02T09:01:10Z",
            }),
        ],
        expect_no_alert=True,
        ground_truth=(
            "NOT a real conflict: one writes end-user docs, the other rotates API "
            "credentials — same coarse tag 'billing', different artifacts, no collision. "
            "Correct outcome: stay silent. Any conflict/coordination alert here is a "
            "false positive."
        ),
    ),
    Scenario(
        name="handoff-context",
        description=(
            "An exec shares a decision carrying a hard constraint; a dev then "
            "independently takes on exactly that work. The dev should be briefed with "
            "the exec's context. No overlap signal or shared tag exists — purely semantic."
        ),
        events=[
            Event("share_context", {
                "agent_id": "exec-a",
                "content": (
                    "Decision from the leadership sync: we're building a customer-facing "
                    "status page. The dev team owns it, and it MUST read incident data "
                    "from the existing `incidents` table rather than standing up a new store."
                ),
                "timestamp": "2026-06-02T09:00:00Z",
            }),
            Event("update_intent", {
                "agent_id": "dev-b", "intent": "build the customer-facing status page",
                "domain": ["status page"], "timestamp": "2026-06-02T09:02:00Z",
            }),
        ],
        expect_alert_to=["dev-b"],
        ground_truth=(
            "A handoff: the exec's decision contains a hard constraint (read from the "
            "existing `incidents` table) that the dev who just picked up the status page "
            "needs before building. No Python overlap signal exists for this. Correct "
            "outcome: brief dev-b with that context/constraint."
        ),
    ),
]


def _format_report(results: list[ScenarioResult], *, model: str = "?") -> str:
    lines = ["", f"=== PACL eval harness (live · model={model}) ===", ""]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"[{status}] {r.name}")
        for aid, msgs in r.notifications.items():
            for m in msgs:
                lines.append(f"    -> {aid}: {m[:160]}")
        if r.tickets:
            lines.append(f"    tickets: {r.tickets}")
        for f in r.failures:
            lines.append(f"    ! {f}")
        lines.append("")
    passed = sum(1 for r in results if r.passed)
    lines.append(f"{passed}/{len(results)} scenarios passed")
    return "\n".join(lines)


async def main() -> int:
    from pacl.agents.intermediary import DEFAULT_MODEL
    from pacl.config import load_config

    config = load_config()
    if not config.gemini_api_key:
        print("GEMINI_API_KEY is not set (.env) — cannot run live eval.")
        return 2

    # Model is env-driven so the experiment is just:
    #   GEMINI_MODEL={...} python -m pacl.evals.harness
    results: list[ScenarioResult] = []
    for scenario in LIVE_SCENARIOS:
        print(f"running scenario: {scenario.name} (model={DEFAULT_MODEL}) ...")
        # Run is awaited to completion; wait is a generous safety cap so a slow
        # multi-turn Gemini run isn't cut off.
        result = await run_scenario(scenario, config=config, wait=180.0)
        results.append(result)

    print(_format_report(results, model=DEFAULT_MODEL))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
