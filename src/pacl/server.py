# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Dylan Porter
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from pacl.config import Config
from pacl.substrate import Substrate


def build_app(
    *,
    config: Config,
    substrate: Substrate,
    intermediary: Any,
    pending: Any = None,
    lifespan: Any = None,
) -> FastAPI:
    """Build the FastAPI app.

    The agent-facing surface is the MCP server mounted at /mcp (see _load_app).
    This app itself only holds shared state and a health route; coordination
    egress is piggybacked on MCP tool responses, not exposed over HTTP.
    """
    if lifespan is not None:
        app = FastAPI(title="PACL", lifespan=lifespan)
    else:
        app = FastAPI(title="PACL")

    app.state.config = config
    app.state.substrate = substrate
    app.state.intermediary = intermediary
    app.state.pending = pending

    @app.get("/")
    def root() -> dict:
        return {"service": "pacl", "status": "ok"}

    return app


def _setup_tracing(config) -> None:
    """Configure Phoenix tracing if an API key is available.

    Safe to call without a key — returns silently so local dev works.
    """
    if not config.phoenix_api_key:
        return

    try:
        from phoenix.otel import register
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

        # Phoenix Cloud workspace-scoped endpoints (/s/<user>/) use self-hosted-style
        # Bearer auth, not the `api_key` header used by unscoped Phoenix Cloud.
        tracer_provider = register(
            project_name=config.phoenix_project,
            endpoint=f"{config.phoenix_collector_endpoint}/v1/traces",
            headers={"Authorization": f"Bearer {config.phoenix_api_key}"},
        )
        GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)
        GoogleGenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    except Exception as exc:  # pragma: no cover
        import logging
        logging.getLogger(__name__).warning("phoenix tracing setup skipped: %s", exc)


def _load_app() -> FastAPI:
    from contextlib import asynccontextmanager

    from pacl.agents.intermediary import Intermediary
    from pacl.config import load_config, make_substrate
    from pacl.pending import PendingQueue
    from pacl.mcp_server import build_mcp_server

    config = load_config()
    _setup_tracing(config)
    substrate = make_substrate(config)

    # The pending queue is shared with the MCP server so alerts the intermediary
    # enqueues are drained by each agent's tool calls (piggyback delivery).
    pending = PendingQueue()
    intermediary = Intermediary(substrate=substrate, config=config, pending=pending)

    mcp_server = build_mcp_server(
        substrate=substrate,
        intermediary_getter=lambda: intermediary,
        pending=pending,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await intermediary.start_loop()
        async with mcp_server.session_manager.run():
            yield
        await intermediary.stop_loop()

    app = build_app(
        config=config, substrate=substrate, intermediary=intermediary,
        pending=pending, lifespan=lifespan,
    )
    app.mount("/mcp", mcp_server.streamable_http_app())

    return app


app = _load_app()
