from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    gemini_api_key: str
    phoenix_api_key: str
    phoenix_collector_endpoint: str
    phoenix_project: str
    substrate_local_root: Path
    port: int
    log_level: str


def load_config() -> Config:
    load_dotenv()
    return Config(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        phoenix_api_key=os.environ.get("PHOENIX_API_KEY", ""),
        phoenix_collector_endpoint=os.environ.get(
            "PHOENIX_COLLECTOR_ENDPOINT", "https://app.phoenix.arize.com"
        ),
        phoenix_project=os.environ.get("PHOENIX_PROJECT", "pacl-dev"),
        substrate_local_root=Path(os.environ.get("SUBSTRATE_LOCAL_ROOT", "./substrate")),
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )


def make_substrate(config: Config):
    from pacl.substrate import LocalSubstrate

    return LocalSubstrate(root=config.substrate_local_root)
