"""Live smoke test for app.llm.categoriser.Categoriser against a real provider.

Not part of the pytest suite on purpose — the whole test suite is designed to
run with no network and no spend (see .agent/architecture_and_progress.md).
This script is the deliberate exception: it makes one real LLM call using
whatever LLM_MODEL / provider API key are already sitting in .env (loaded
automatically by app.llm.router — this script never reads .env itself, and
never prints the API key).

Usage, from the repo root:
    .venv/bin/python scripts/live_llm_smoke_test.py

Fixtures below are realistic Singapore bank-export merchant keys — the same
shape normalise.py/cluster.py would hand the LLM in production — covering
most of the 13 starting categories, so a human can eyeball whether the
categorisation is sane before wiring this up for real.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.queries import SYSTEM_CATEGORIES, STARTING_CATEGORIES  # noqa: E402
from app.llm.categoriser import Categoriser  # noqa: E402
from app.llm.router import get_llm  # noqa: E402

FIXTURE_KEYS = [
    "ntuc fairprice",
    "cold storage",
    "mcdonalds",
    "starbucks",
    "gongcha",
    "grab*",
    "simplygo pte. ltd.",
    "shopee",
    "daiso japan - spc",
    "netflix.com",
    "spotify",
    "flyscoot",
    "agoda",
    "guardian pharmacy",
    "watsons",
    "singtel",
    "sp services",
    "popular bookstore",
    "giving.sg",
    "bank service charge",
]

TAXONOMY = SYSTEM_CATEGORIES + STARTING_CATEGORIES


def main() -> int:
    try:
        router = get_llm()
    except RuntimeError as e:
        print(f"Cannot run live test: {e}")
        print("Set LLM_MODEL (and the matching provider API key) in .env, then re-run.")
        return 1

    model = router.model_list[0]["litellm_params"]["model"]
    print(f"Model under test: {model}")
    print(f"Taxonomy ({len(TAXONOMY)}): {', '.join(TAXONOMY)}")
    print(f"Fixture keys: {len(FIXTURE_KEYS)}\n")

    results = Categoriser().categorise(FIXTURE_KEYS, TAXONOMY)

    width = max(len(k) for k in FIXTURE_KEYS)
    unknown = []
    missing = []
    for key in FIXTURE_KEYS:
        category = results.get(key)
        if category is None:
            missing.append(key)
            print(f"  {key.ljust(width)}  -> (no answer returned)")
        else:
            if category == "Unknown":
                unknown.append(key)
            print(f"  {key.ljust(width)}  -> {category}")

    print(f"\n{len(results)}/{len(FIXTURE_KEYS)} keys answered.")
    if unknown:
        print(f"Stored as Unknown (low confidence or off-taxonomy): {unknown}")
    if missing:
        print(f"No answer at all for: {missing}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
