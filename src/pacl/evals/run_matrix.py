"""Parallel, resumable, checkpointed matrix runner for the eval suite.

Runs {models} x {scenarios} x K reps, bounded by a concurrency semaphore,
appending each run's FULL outcome to a JSONL checkpoint as it finishes.
Re-running skips cells already in the checkpoint, so a crash (or a quota stall)
loses nothing — just relaunch.

Each record is self-contained (events, ground_truth, notifications, tickets,
binary verdict) so the downstream blinded judge reads only the JSONL, never the
scenario module — and can be anonymized (model stripped) before grading.

Usage:
    EVAL_K=50 EVAL_CONCURRENCY=5 \
    EVAL_MODELS=gemini-2.5-pro,gemini-3.1-pro-preview \
    python -m pacl.evals.run_matrix

Env knobs (all optional):
    EVAL_K            reps per cell                (default 5)
    EVAL_CONCURRENCY  max concurrent live runs     (default 5)
    EVAL_MODELS       comma-separated model ids    (default pro,3.1-preview)
    EVAL_OUT          checkpoint path              (default ./.eval_runs/runs.jsonl)
    EVAL_DEADLINE_S   hard wall-clock cap, seconds (default 28800 = 8h)
    EVAL_WAIT         per-run safety cap, seconds  (default 180)
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
import uuid
from pathlib import Path

import pacl.agents.intermediary as _inter
from pacl.config import load_config
from pacl.evals.harness import LIVE_SCENARIOS, run_scenario

# Eval-only resilience: the intermediary's _is_retryable doesn't cover quota
# 429s / RESOURCE_EXHAUSTED. Under sustained matrix load those dominate; without
# backoff a rate-limited run dies EMPTY — and with no fallback there is nothing
# to mask it, which silently corrupts the results (an empty run fails every
# "expect alert" check and trivially passes every "expect silence" one). Patch
# retryability at runtime so the existing backoff loop also retries quota errors.
# Core source is untouched; rm of this file restores stock behavior.
_orig_is_retryable = _inter._is_retryable


def _is_retryable_with_quota(exc) -> bool:
    t = str(exc).upper()
    return _orig_is_retryable(exc) or ("429" in t or "RESOURCE_EXHAUSTED" in t or "QUOTA" in t)


_inter._is_retryable = _is_retryable_with_quota


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _cell_key(model: str, scenario: str, rep: int) -> str:
    return f"{model}|{scenario}|{rep}"


def _load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                done.add(_cell_key(r["model"], r["scenario"], r["rep"]))
            except (ValueError, KeyError):
                continue
    return done


async def main() -> int:
    base = load_config()
    if not base.gemini_api_key:
        print("GEMINI_API_KEY not set (.env) — cannot run live matrix.")
        return 2
    # Strip Phoenix so the overnight run has no external-tracing dependency to
    # flake on; the agent's optional phoenix tools just no-op.
    config = dataclasses.replace(base, phoenix_api_key="")

    k = int(_env("EVAL_K", "5"))
    concurrency = int(_env("EVAL_CONCURRENCY", "5"))
    models = [m.strip() for m in _env("EVAL_MODELS", "gemini-2.5-pro,gemini-3.1-pro-preview").split(",") if m.strip()]
    out = Path(_env("EVAL_OUT", "./.eval_runs/runs.jsonl"))
    deadline_s = float(_env("EVAL_DEADLINE_S", "28800"))
    wait = float(_env("EVAL_WAIT", "180"))
    out.parent.mkdir(parents=True, exist_ok=True)

    done = _load_done(out)
    # Rep-outer ordering (then scenario, then model) so that if quota/latency
    # drifts during the run it spreads across models/scenarios symmetrically
    # rather than front-loading one model on fresh quota and starving another.
    cells = [
        (model, scenario, rep)
        for rep in range(k)
        for scenario in LIVE_SCENARIOS
        for model in models
    ]
    pending = [c for c in cells if _cell_key(c[0], c[1].name, c[2]) not in done]

    total = len(cells)
    print(
        f"matrix: {len(models)} models x {len(LIVE_SCENARIOS)} scenarios x K={k} "
        f"= {total} runs | {len(done)} already done | {len(pending)} to run | "
        f"concurrency={concurrency}",
        flush=True,
    )

    sem = asyncio.Semaphore(concurrency)
    file_lock = asyncio.Lock()
    start = time.monotonic()
    counter = {"n": 0}

    async def one(model: str, scenario, rep: int) -> None:
        async with sem:
            if time.monotonic() - start > deadline_s:
                return
            rec: dict = {
                "run_id": uuid.uuid4().hex,
                "model": model, "scenario": scenario.name, "rep": rep,
                "description": scenario.description,
                "ground_truth": scenario.ground_truth,
                "events": [{"event_type": e.event_type, "payload": e.payload} for e in scenario.events],
                "expect_alert_to": scenario.expect_alert_to,
                "expect_no_alert": scenario.expect_no_alert,
                "expect_ticket": scenario.expect_ticket,
            }
            try:
                r = await run_scenario(
                    scenario, config=config, model=model, wait=wait,
                    freshen_timestamps=True,
                )
                rec.update({
                    "passed": r.passed,
                    "llm_acted": bool(r.passed),
                    "failures": r.failures,
                    "notifications": r.notifications,
                    "tickets": r.tickets,
                    "error": None,
                })
            except Exception as exc:  # one bad run must not kill the batch
                rec.update({
                    "passed": False, "llm_acted": False,
                    "failures": [f"run error: {type(exc).__name__}: {exc}"],
                    "notifications": {}, "tickets": [], "error": str(exc),
                })
            async with file_lock:
                with out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                counter["n"] += 1
                n = counter["n"]
            flag = "PASS" if rec["passed"] else "FAIL"
            print(f"  [{n}/{len(pending)}] {model} | {scenario.name} r{rep}: {flag}", flush=True)

    tasks = [asyncio.create_task(one(m, sc, rp)) for (m, sc, rp) in pending]
    if tasks:
        await asyncio.gather(*tasks)

    _report(out, models, k)
    return 0


def _report(out: Path, models: list[str], k: int) -> None:
    records = []
    with out.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # cell -> {passes, llm_acted, n}
    from collections import defaultdict
    cell = defaultdict(lambda: {"pass": 0, "llm": 0, "n": 0})
    for r in records:
        key = (r["model"], r["scenario"])
        c = cell[key]
        c["n"] += 1
        if r.get("passed"):
            c["pass"] += 1
            if r.get("llm_acted"):
                c["llm"] += 1

    scenarios = [s.name for s in LIVE_SCENARIOS]
    print("\n=== matrix pass rates (binary / deterministic) ===")
    print(f"{'model':26} {'scenario':32} {'pass':>8}  {'llm-acted':>9}")
    for model in models:
        for name in scenarios:
            c = cell[(model, name)]
            if c["n"] == 0:
                continue
            print(f"{model:26} {name:32} {c['pass']:>3}/{c['n']:<4} {c['llm']:>4}/{c['n']:<4}")
    print(f"\ntotal records: {len(records)}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
