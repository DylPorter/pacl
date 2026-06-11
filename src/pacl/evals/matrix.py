"""Run the {model} x scenario matrix, K reps each, report pass rates.

Turns single live runs into a results table: where does each model *reliably*
produce the right coordination outcome? The `llm-acted` column counts passes
where the agent itself did the work — there is no Python fallback anymore, so
passes == llm-acted by construction.

Usage:
    python -m pacl.evals.matrix [K]   # K reps per cell, default 3
"""
from __future__ import annotations

import asyncio
import sys

from pacl.config import load_config
from pacl.evals.harness import LIVE_SCENARIOS, run_scenario

# gemini-3.5-flash excluded for now: capacity-blocked (503s) would pollute the
# table with availability artifacts rather than capability. Add it back when stable.
MODELS = ["gemini-2.5-pro", "gemini-3.1-pro-preview"]


async def main() -> int:
    config = load_config()
    if not config.gemini_api_key:
        print("GEMINI_API_KEY is not set (.env) — cannot run live matrix.")
        return 2

    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cells: list[tuple[str, str, int, int]] = []  # model, scenario, passes, llm_acted

    for model in MODELS:
        for scenario in LIVE_SCENARIOS:
            passes = 0
            llm_acted = 0
            for i in range(k):
                r = await run_scenario(
                    scenario, config=config, model=model, wait=180.0
                )
                if r.passed:
                    passes += 1
                    llm_acted += 1
                flag = "PASS" if r.passed else "FAIL"
                print(f"  {model} | {scenario.name} | rep {i + 1}/{k}: {flag}", flush=True)
            cells.append((model, scenario.name, passes, llm_acted))

    print(f"\n=== matrix pass rates (K={k}) ===")
    print(f"{'model':26} {'scenario':18} {'pass':>6}  {'llm-acted':>9}")
    for model, name, passes, llm_acted in cells:
        print(f"{model:26} {name:18} {passes:>3}/{k}  {llm_acted:>6}/{k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
