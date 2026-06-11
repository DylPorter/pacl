from __future__ import annotations

import pytest

from pacl.config import Config
from pacl.substrate import LocalSubstrate


@pytest.mark.asyncio
async def test_intermediary_loop_starts_and_stops_with_app(tmp_path):
    """App lifespan starts/stops the intermediary loop cleanly; root health route responds."""
    from contextlib import asynccontextmanager

    from httpx import AsyncClient, ASGITransport

    from pacl.agents.intermediary import Intermediary
    from pacl.server import build_app

    substrate = LocalSubstrate(root=tmp_path)
    config = Config(
        gemini_api_key="",
        phoenix_api_key="",
        phoenix_collector_endpoint="",
        phoenix_project="",
        substrate_local_root=tmp_path,
        port=8090,
        log_level="WARNING",
    )
    intermediary = Intermediary(substrate=substrate, config=config)

    @asynccontextmanager
    async def lifespan(app):
        await intermediary.start_loop()
        yield
        await intermediary.stop_loop()

    app = build_app(config=config, substrate=substrate, intermediary=intermediary, lifespan=lifespan)

    async with lifespan(app):
        assert intermediary._loop_task is not None
        assert not intermediary._loop_task.done()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/")
            assert r.status_code == 200

    assert intermediary._loop_task is None or intermediary._loop_task.done()
