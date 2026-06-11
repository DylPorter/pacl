from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from pacl.agents.prompts import (
    SYSTEM_PROMPT,
    LOOP_PROMPT,
)
from pacl.events import append_event
from pacl.pending import PendingQueue

log = logging.getLogger(__name__)


@dataclass
class EventRecord:
    event_type: str
    payload: dict
    acting_id: str
    acting_intent: str


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
APP_NAME = "pacl"
SYSTEM_USER_ID = "intermediary"
# Seconds to collect additional events after the first arrives. Env-overridable so the
# live demo can shrink it (proactive alerts resolve fast enough to surface in-session).
BATCH_WINDOW = float(os.environ.get("PACL_BATCH_WINDOW", "2.0"))
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0  # base seconds for exponential backoff between model retries
INTENT_TTL_SECONDS = float(os.environ.get("PACL_INTENT_TTL", "3600"))  # 1h default


def _is_retryable(exc: Exception) -> bool:
    """True for transient model/transport errors worth a retry.

    Covers model-availability (503 / UNAVAILABLE) and transient transport blips
    (server disconnects, connection/read errors) — the latter observed live as
    httpx.RemoteProtocolError mid-run, which (with no fallback) would otherwise
    silently drop a coordination decision.
    """
    code = getattr(exc, "code", None)
    text = str(exc).upper()
    name = type(exc).__name__
    return (
        code == 503
        or "503" in text
        or "UNAVAILABLE" in text
        or "DISCONNECT" in text
        or name in {
            "RemoteProtocolError", "ConnectError", "ReadError",
            "ConnectTimeout", "ReadTimeout", "PoolTimeout",
        }
    )


def _classify_alert(recipient: str, acting_agent_ids: set[str]) -> tuple[str, float]:
    """Behavioral verdict for a past alert: did the recipient act in this batch?"""
    if recipient in acting_agent_ids:
        return ("acted_on", 1.0)
    return ("ignored", 0.0)


class Intermediary:
    """Wraps a Google ADK LlmAgent invoked per incoming event.

    The agent and runner are constructed lazily so the FastAPI app can start
    without a Gemini API key (tests / local dev without intermediary).
    """

    def __init__(
        self, *, substrate, config, batch_window: float = BATCH_WINDOW,
        model: str | None = None,
        pending: PendingQueue | None = None,
    ) -> None:
        self.substrate = substrate
        self.pending = pending or PendingQueue()
        self.config = config
        self.model = model or DEFAULT_MODEL
        self._agent: Any = None
        self._runner: Any = None
        self._session_service: Any = None
        # Serialize background runs so concurrent events don't stampede the
        # Gemini per-minute rate limit. Each run waits for the previous to
        # finish before starting; the SDK's built-in 429 retry handles the
        # per-call window from there.
        self._run_lock = asyncio.Lock()
        # In-memory map: agent_id -> {"intent": str, "scope": set[str], "updated_at": str}.
        # Source of truth for fast overlap detection. The agent's markdown file in
        # substrate is for the LlmAgent's own consumption; this dict is what the
        # pre-compute step reads. Avoids regex-fragility against agent-written
        # markdown formats that drift per prompt iteration.
        self._agent_scopes: dict[str, dict] = {}
        # Notification queue: push_notification tool appends here during an agent
        # run; _flush_notifications delivers them after the run completes. Cleared
        # before each run so stale entries never bleed across invocations.
        self._notification_queue: list[dict] = []
        self._event_queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        self._batch_window = batch_window
        self._loop_task: asyncio.Task | None = None

    async def start_loop(self) -> None:
        """Start the background event-processing loop. Called once at app startup."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.get_event_loop().create_task(self._loop_body())
        log.info("intermediary loop started")

    async def stop_loop(self) -> None:
        """Cancel the background loop. Called at app shutdown."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        log.info("intermediary loop stopped")

    def reset_state(self) -> None:
        """Clear all live coordination state so a fresh scenario starts clean — for the
        demo's context-switch / reset. Drops the in-memory overlap map, the pending alert
        queue, and the notification buffer. Does NOT touch the durable substrate (the
        route clears that), nor the running loop."""
        self._agent_scopes.clear()
        self._notification_queue.clear()
        self.pending.clear()

    def agent_scopes_snapshot(self) -> dict:
        """Read-only view of what PACL currently knows each agent is doing — for the
        demo's 'what PACL knows' panel. Maps agent_id -> {intent, scope, updated_at}."""
        return {
            aid: {
                "intent": d.get("intent", ""),
                "scope": sorted(d.get("scope", set()) or []),
                "updated_at": d.get("updated_at", ""),
            }
            for aid, d in self._agent_scopes.items()
        }

    def team_state_text(self, exclude_agent_id: str | None = None) -> str:
        """Authoritative, synchronous snapshot of who is doing what — read straight off the
        live overlap map (`_agent_scopes`), the SAME source of truth the coherence loop uses
        to detect overlaps. The `query` tool answers from this rather than having an LLM
        re-read free-text event logs, so a teammate's active intent is surfaced reliably and
        instantly (no per-query model call), and stale narration in the log can't mask it."""
        self._prune_stale_scopes()
        rows = [
            (aid, st) for aid, st in self._agent_scopes.items()
            if aid != exclude_agent_id and st.get("intent")
        ]
        if not rows:
            return "No other agents have any active work registered right now."
        lines = ["Current active work across the team (live, from PACL):"]
        for aid, st in rows:
            scope = sorted(st.get("scope", set()) or [])
            scope_txt = f" — touching: {', '.join(scope)}" if scope else ""
            lines.append(f"- {aid}: \"{st['intent']}\"{scope_txt} (since {st.get('updated_at', '')})")
        return "\n".join(lines)

    def recent_activity_text(self, max_chars: int = 2500) -> str:
        """Deterministic read of recent team activity from the durable events log (the
        substrate's append-only record). Lets history/audit questions ("what has alice
        worked on so far?") be answered, not just "what is everyone doing now?". No model
        call — it reads and returns the recent tail of the log, which is what makes PACL's
        substrate genuinely queryable as a knowledge base rather than only live state."""
        from datetime import date

        raw = self.substrate.read(f"events/{date.today().isoformat()}.md") or ""
        if not raw.strip():
            for p in sorted(self.substrate.list("events")):
                raw += (self.substrate.read(p) or "")
        raw = raw.strip()
        if not raw:
            return "No recorded team activity yet."
        if len(raw) > max_chars:
            raw = "…(earlier activity omitted)…\n\n" + raw[-max_chars:]
        return raw

    async def _loop_body(self) -> None:
        """Continuous loop: wait for first event, batch for _batch_window, run agent."""
        while True:
            first = await self._event_queue.get()
            batch: list[EventRecord] = [first]

            deadline = asyncio.get_event_loop().time() + self._batch_window
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    extra = await asyncio.wait_for(
                        self._event_queue.get(), timeout=remaining
                    )
                    batch.append(extra)
                except asyncio.TimeoutError:
                    break

            log.info("loop: processing batch of %d event(s)", len(batch))
            await self._eval_prior_alerts(batch)
            prompt = self._build_batch_prompt(batch)
            last = batch[-1]

            await self._safe_run(
                prompt,
                acting_id=last.acting_id,
                acting_intent=last.acting_intent,
            )

    def _build_batch_prompt(self, batch: list[EventRecord]) -> str:
        """Build the holistic prompt the agent receives for a batch of events."""
        memory = self.substrate.read("agents/intermediary-self.md") or "(no prior memory)"
        pending_evals = self.substrate.read("agents/pending-evals.md") or ""

        parts = [
            LOOP_PROMPT,
            "",
            "## Your working memory (from last run)",
            memory,
        ]

        self._prune_stale_scopes()
        if self._agent_scopes:
            parts.extend(["", "## Currently active intents (TTL-filtered)"])
            for aid, st in self._agent_scopes.items():
                parts.append(
                    f"- `{aid}`: \"{st['intent']}\" (domain {sorted(st['scope'])}, "
                    f"since {st['updated_at']})"
                )

        if pending_evals.strip():
            proj = getattr(self.config, "phoenix_project", "pacl-dev") or "pacl-dev"
            parts.extend([
                "",
                "## Alerts sent in the previous run (evaluate these)",
                pending_evals.strip(),
                "",
                "[EVAL REQUIRED] For each alert above:",
                f"1. Call `get-spans` (the Arize Phoenix MCP trace-read tool) with "
                f"`project_identifier='{proj}'` and a small `limit` to fetch your recent "
                f"spans, then find the one for that run.",
                "2. Check whether the notified agent posted a report_activity event "
                "in the current batch (see events below).",
                "3. Classify: acted_on (agent changed behavior), ignored (no follow-up), "
                "or unclear. Call annotate_span with the span's `context.span_id` (the hex "
                "OTel id, not the base64 GlobalID), label, and score "
                "(acted_on=1.0, unclear=0.5, ignored=0.0).",
                "4. Add a brief reflection on what this tells you to your working memory update.",
            ])

        parts.extend([
            "",
            f"## Pending events ({len(batch)} since last run)",
        ])

        for i, record in enumerate(batch, 1):
            parts.append(f"\n### Event {i}: {record.event_type} from `{record.acting_id}`")
            parts.append(f"Payload: {record.payload}")

        return "\n".join(parts)

    def _build(self) -> None:
        from google.adk import Runner
        from google.adk.agents import LlmAgent
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools.mcp_tool.mcp_toolset import (
            McpToolset,
            StdioConnectionParams,
            StdioServerParameters,
        )
        from google.genai.types import GenerateContentConfig

        from pacl.agents.tools import build_tools

        tools = build_tools(
            substrate=self.substrate,
            config=self.config,
            notification_queue=self._notification_queue,
        )

        # Arize Phoenix MCP server (stdio egress): here the intermediary becomes
        # an MCP *client* connecting OUT to a partner's server for its self-eval
        # trace reads (get-spans / get-trace / get-span-annotations). This is the
        # opposite role+transport from PACL's own MCP *server* (HTTP ingress that
        # the demo agents connect into), so the two never interfere.
        # Only wired when a Phoenix API key exists — config=None / keyless-deploy
        # paths (incl. the whole unit suite) keep an unchanged native toolset.
        arize_tools: list = []
        api_key = getattr(self.config, "phoenix_api_key", None)
        if api_key:
            endpoint = getattr(
                self.config, "phoenix_collector_endpoint", "https://app.phoenix.arize.com"
            )
            arize_tools = [
                McpToolset(
                    connection_params=StdioConnectionParams(
                        server_params=StdioServerParameters(
                            command="npx",
                            args=[
                                "-y",
                                "@arizeai/phoenix-mcp@latest",
                                "--baseUrl",
                                endpoint,
                                "--apiKey",
                                api_key,
                            ],
                        ),
                        # First spawn may npm-fetch the package; the 5s default is
                        # far too tight for an uncached container cold-start.
                        timeout=120.0,
                    )
                )
            ]

        self._agent = LlmAgent(
            name="pacl_intermediary",
            description="Reasons over team state to alert agents about overlapping work and route escalations.",
            model=self.model,
            instruction=SYSTEM_PROMPT,
            tools=[*tools, *arize_tools],
            generate_content_config=GenerateContentConfig(temperature=0.0),
        )
        self._session_service = InMemorySessionService()
        self._runner = Runner(
            app_name=APP_NAME,
            agent=self._agent,
            session_service=self._session_service,
            auto_create_session=True,
        )

    async def _run_message(self, text: str) -> str:
        from google.genai import types

        if self._runner is None:
            self._build()

        message = types.Content(role="user", parts=[types.Part(text=text)])
        # Fresh session per run: the agent's continuity comes from the markdown
        # substrate (intermediary-self.md), not from ADK session state. Reusing a
        # single session ID cross-contaminates batch runs and direct queries.
        session_id = f"run-{uuid.uuid4().hex}"
        accumulated: list[str] = []
        async for event in self._runner.run_async(
            user_id=SYSTEM_USER_ID,
            session_id=session_id,
            new_message=message,
        ):
            content = getattr(event, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", []) or []:
                if getattr(part, "text", None):
                    accumulated.append(part.text)
        return "".join(accumulated)

    async def _run_with_retry(
        self, prompt: str, *, attempts: int = RETRY_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY
    ) -> str:
        """Call the model, retrying transient 503/UNAVAILABLE errors with backoff.

        Agnostic mode has no Python fallback, so a single model 503 would otherwise
        be a silent failure. This keeps the run resilient to transient capacity blips.
        """
        for i in range(attempts):
            try:
                return await self._run_message(prompt)
            except Exception as exc:
                if i == attempts - 1 or not _is_retryable(exc):
                    raise
                log.warning(
                    "model call failed (retryable, attempt %d/%d): %s", i + 1, attempts, exc
                )
                await asyncio.sleep(base_delay * (2 ** i))
        raise RuntimeError("unreachable")

    async def _eval_prior_alerts(self, batch: list[EventRecord]) -> None:
        """Deterministic Python floor for the self-eval loop.

        Every cycle, grade the previous run's alerts (acted_on / ignored — did the
        recipient show up acting in this batch?) and write the verdict to Phoenix as
        an `agent_judge` annotation. Runs regardless of whether the LLM also does its
        [EVAL REQUIRED] pass, so the self-improvement annotations are GUARANTEED, not
        LLM-probabilistic. Same floor-under-the-agent pattern as the overlap fallback.
        """
        pending = self.substrate.read("agents/pending-evals.md") or ""
        recipients = []
        for line in pending.splitlines():
            if "agent_id:" in line:
                rid = line.split("agent_id:", 1)[1].split("|", 1)[0].strip()
                if rid:
                    recipients.append(rid)
        if not recipients:
            return
        cfg = self.config
        if cfg is None or not getattr(cfg, "phoenix_api_key", None):
            return
        acting = {r.acting_id for r in batch}
        try:
            from phoenix.client import Client

            client = Client(
                base_url=getattr(cfg, "phoenix_collector_endpoint", None) or None,
                api_key=getattr(cfg, "phoenix_api_key", None) or None,
            )
            proj = getattr(cfg, "phoenix_project", "pacl-dev")
            df = client.spans.get_spans_dataframe(project_name=proj, limit=10)
            if df is None or len(df) == 0:
                return
            span_id = str(df.index[0])
            for rid in recipients:
                label, score = _classify_alert(rid, acting)
                client.spans.add_span_annotation(
                    span_id=span_id,
                    annotation_name="agent_judge",
                    annotator_kind="CODE",
                    label=label,
                    score=score,
                    explanation=f"[auto] alert to {rid} -> {label} "
                    f"(recipient {'acted in' if label == 'acted_on' else 'absent from'} the next batch)",
                )
            log.info("python self-eval floor: annotated %d alert(s) on span %s", len(recipients), span_id)
        except Exception as exc:
            log.warning("python self-eval floor failed: %s", exc)

    def _prune_stale_scopes(self) -> None:
        """Drop intents older than the TTL so finished work stops causing overlaps."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        stale: list[str] = []
        for agent_id, state in self._agent_scopes.items():
            try:
                updated = datetime.fromisoformat(state["updated_at"])
                age = (now - updated).total_seconds()
            except (ValueError, KeyError, TypeError):
                continue  # unparseable / naive-vs-aware -> leave it
            if age > INTENT_TTL_SECONDS:
                stale.append(agent_id)
        for agent_id in stale:
            del self._agent_scopes[agent_id]

    async def _flush_notifications(
        self,
        acting_id: str,
        acting_intent: str,
    ) -> None:
        """Deliver queued notifications after the agent run completes.

        The agent calls push_notification during its run to enqueue messages.
        Once _run_message returns, this method publishes everything in the queue.
        If the agent enqueued nothing, nothing is delivered — the model decides.
        """
        from datetime import datetime, timezone

        published: list[dict] = []
        for entry in self._notification_queue:
            published.append(entry)
            log.info("notification queued (agent-crafted): %s", entry["agent_id"])

        # Piggyback queue (pure-MCP egress) + events-feed log.
        for e in published:
            self.pending.enqueue(e["agent_id"], e["message"])
            append_event(
                self.substrate, "alert_emitted",
                {"agent_id": e["agent_id"], "message": e["message"][:80]},
            )

        # Write pending-evals so the next run can score alert outcomes in Phoenix.
        if published:
            now = datetime.now(timezone.utc).isoformat()
            lines = ["---\nkind: pending-evals\n---\n\n# Pending Alert Evaluations\n"]
            for e in published:
                lines.append(
                    f"- agent_id: {e['agent_id']} | sent_at: {now} | "
                    f"message: {e['message'][:120]}"
                )
            self.substrate.write("agents/pending-evals.md", "\n".join(lines) + "\n")
            log.info("pending-evals.md written (%d alert(s))", len(published))
        else:
            # Clear any stale pending-evals from a prior run.
            self.substrate.delete("agents/pending-evals.md")

    def notify_event(self, event_type: str, payload: dict) -> None:
        """Enqueue an incoming event for batch processing by the background loop."""
        acting_id = payload.get("agent_id", "unknown")
        acting_intent = payload.get("intent", "(unknown)")

        if event_type == "update_intent":
            scope = set(payload.get("domain") or [])
            if acting_id and scope:
                self._agent_scopes[acting_id] = {
                    "intent": acting_intent,
                    "scope": scope,
                    "updated_at": payload.get("timestamp", "(unknown)"),
                }

        # Liveness: any activity from a known agent refreshes its freshness, so
        # TTL reflects "time since last activity", not "time since last intent".
        # (update_intent already set a fresh updated_at above.)
        if event_type != "update_intent" and acting_id in self._agent_scopes:
            self._agent_scopes[acting_id]["updated_at"] = payload.get(
                "timestamp", self._agent_scopes[acting_id]["updated_at"]
            )

        record = EventRecord(
            event_type=event_type,
            payload=payload,
            acting_id=acting_id,
            acting_intent=acting_intent,
        )
        try:
            self._event_queue.put_nowait(record)
        except asyncio.QueueFull:
            log.warning("event queue full, dropping event from %s", acting_id)

    async def _safe_run(
        self,
        prompt: str,
        *,
        acting_id: str = "unknown",
        acting_intent: str = "(unknown)",
    ) -> None:
        async with self._run_lock:
            self._notification_queue.clear()
            try:
                await self._run_with_retry(prompt)
            except Exception as exc:
                log.exception("intermediary run failed: %s", exc)
            finally:
                await self._flush_notifications(
                    acting_id=acting_id,
                    acting_intent=acting_intent,
                )

    async def answer_query(self, question: str) -> str:
        """Answer a direct natural-language question about team state (the `query` MCP tool)."""
        prompt = (
            f"A human is asking you the following question. Use your tools "
            f"(read_substrate, list_substrate, `get-spans` for Phoenix traces, etc.) "
            f"to gather context and answer in natural language.\n\n"
            f"You're the coordinator reasoning over the shared state the agents on "
            f"this system report into — not a blank slate. Other agents are "
            f"actively working, and some of what's being asked about may already "
            f"exist or already be underway. A vague or thin question is exactly when "
            f"to read the current state rather than assume nothing exists yet. "
            f"Ground yourself in what's actually there, then answer plainly: if "
            f"what's being asked for already exists or is already in progress, say "
            f"so and point to it; if it would duplicate or collide with what an "
            f"agent is mid-change on, surface that and flag who to coordinate "
            f"with.\n\n"
            f"Question: {question}"
        )
        try:
            return await self._run_message(prompt)
        except Exception as exc:
            return f"(query failed: {exc})"
