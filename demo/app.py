"""Demo entrypoint: pure PACL app + the demo router, nothing more.

    uv run uvicorn demo.app:app --port 8090

Pure PACL (no demo) is still ``uvicorn pacl.server:app``. This module only ADDS
routes onto the existing app; it never modifies ``pacl/``. ``rm -rf demo/``
restores pristine pure-MCP PACL.
"""
from __future__ import annotations

from pacl.server import app
from demo.routes import router

app.include_router(router)
