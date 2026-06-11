"""Clear all substrate state, all registered agents, and (optionally) Phoenix project data.

Usage:
    python scripts/wipe.py [--phoenix]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pacl.config import load_config, make_substrate


def wipe_substrate(substrate) -> int:
    count = 0
    for prefix in ("agents", "events", "tickets"):
        for path in list(substrate.list(prefix)):
            substrate.delete(path)
            count += 1
    return count


def wipe_phoenix(config) -> bool:
    try:
        import phoenix as px  # noqa: F401

        print(
            "NOTE: Phoenix does not expose project-wipe via SDK. "
            "Either rotate PHOENIX_PROJECT in .env to a fresh name, "
            "or manually clear the project in Phoenix Cloud UI."
        )
        return True
    except Exception as exc:
        print(f"phoenix wipe skipped: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phoenix", action="store_true", help="note Phoenix project reset guidance")
    args = parser.parse_args()

    config = load_config()
    substrate = make_substrate(config)

    n = wipe_substrate(substrate)
    print(f"wiped {n} substrate files")

    if args.phoenix:
        wipe_phoenix(config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
