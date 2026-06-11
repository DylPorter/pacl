from __future__ import annotations

from typing import Any

from pacl.substrate import Substrate


def build_tools(
    *,
    substrate: Substrate,
    config: Any,
    notification_queue: list | None = None,
) -> list:
    """Return the list of callables the intermediary agent has access to.

    ADK 2.0 LlmAgent accepts plain Python callables as tools; their docstrings
    serve as the tool descriptions for the model.

    notification_queue: if provided, push_notification enqueues rather than
    publishing immediately. The caller flushes the queue after the agent run
    so delivery is guaranteed regardless of whether the agent chose to call the
    tool. Pass None to get the original fire-immediately behaviour (used in
    unit tests that don't need the queue pattern).
    """

    def read_substrate(path: str) -> str:
        """Read a markdown file from the shared substrate by path (e.g., 'agents/coder-1.md').

        Returns the file content as a string, or '(file does not exist)' if missing.
        """
        content = substrate.read(path)
        return content if content is not None else "(file does not exist)"

    def write_substrate(path: str, content: str) -> str:
        """Write or overwrite a markdown file in the substrate at the given path.

        Use for creating tickets, updating agent state files, or writing the intermediary self-note.
        """
        substrate.write(path, content)
        return f"wrote {path}"

    def append_substrate(path: str, content: str) -> str:
        """Append content to a markdown file in the substrate.

        Use primarily for the daily events log (events/YYYY-MM-DD.md).
        """
        substrate.append(path, content)
        return f"appended to {path}"

    def list_substrate(prefix: str) -> list:
        """List markdown files under a substrate prefix.

        Examples: list_substrate('agents'), list_substrate('tickets'), list_substrate('events').
        """
        return list(substrate.list(prefix))

    def push_notification(agent_id: str, message: str) -> str:
        """Push a natural-language notification to a specific agent via the comm layer.

        The message should be one paragraph: WHO is affected, WHAT they are
        doing, WHEN they started, and a concrete coordination suggestion.

        Calling this tool records your decision. Delivery is guaranteed by the
        comm layer after your run completes — you do not need to verify it.
        """
        if notification_queue is not None:
            notification_queue.append({"agent_id": agent_id, "message": message})
        return f"queued notification to {agent_id}"

    # NOTE: trace READS are no longer a native SDK tool. The intermediary reads
    # spans through the Arize Phoenix MCP server (get-spans / get-trace), wired as
    # an McpToolset in Intermediary._build(). Only the annotation WRITE stays on
    # the SDK below, because Phoenix MCP exposes no span-annotation-write tool.

    def annotate_span(span_id: str, label: str, score: float, explanation: str) -> str:
        """Write an annotation to a Phoenix span via the direct Python SDK.

        Use for recording eval results from the LLM-as-Judge pipeline.
        Phoenix MCP does not expose annotation-write tools, so this uses the
        SDK directly. Cleanly separates the read-side (MCP) from the write-side (SDK).
        """
        try:
            from phoenix.client import Client

            client = Client(
                base_url=getattr(config, "phoenix_collector_endpoint", None) or None,
                api_key=getattr(config, "phoenix_api_key", None) or None,
            )
            client.spans.add_span_annotation(
                span_id=span_id,
                annotation_name="agent_judge",
                annotator_kind="LLM",
                label=label,
                score=score,
                explanation=explanation,
            )
            return f"annotated {span_id} = {label} ({score})"
        except Exception as exc:
            return f"(phoenix annotate failed: {exc})"

    return [
        read_substrate,
        write_substrate,
        append_substrate,
        list_substrate,
        push_notification,
        annotate_span,
    ]
