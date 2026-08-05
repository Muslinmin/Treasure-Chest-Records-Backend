"""Category assignment orchestration — §10.1, build order step 8.

Runs the five-layer resolution over every row where ``category IS NULL AND
is_category_manual = 0``:

    0  rules.decide(...)                    deterministic, no key needed  §10.4
       normalise.merchant_key(...) → None → "Unknown"                    §10.2
    1  exact match on merchant_categories                                §10.6
    2  prefix-cluster match                                              §10.3
    3  fuzzy match >= threshold (rapidfuzz)                              §10.13
    4  collect into the miss set → batched to the Categoriser            §10.8

``categorise()`` receives an open ``Session`` and a ``Categoriser`` and owns
neither, preserving the trigger seam described in §3 — the same contract as
``pipeline.process_file``. Unlike ``process_file`` it commits more than once:
layers 0-3 first, then once per LLM batch, so a provider failure partway
through the miss set does not discard resolutions already made. This is what
makes "what returned is written, the rest stays NULL, and the next run
retries only those" (§10.8) true in practice, not just in the description.

The ``Categoriser`` interface — "merchant keys → categories" — is what this
module depends on, not litellm or a Router (§10.8): that is what makes this
checkpoint (build step 8) runnable with a dict-backed stub, no network, no
spend, before any LLM code exists.
"""

import logging
from typing import Protocol

from sqlalchemy.orm import Session

from app.categorise.cluster import cluster_keys
from app.categorise.normalise import merchant_key
from app.categorise.rules import decide
from app.db.queries import (
    apply_category_to_key,
    get_uncategorised,
    list_categories,
    load_merchant_cache,
    upsert_merchant,
)
from app.summary.aggregator import recompute_summary

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover - exercised only if rapidfuzz is missing
    fuzz = None
    process = None

logger = logging.getLogger(__name__)

BATCH_SIZE = 40
FUZZY_SCORE_CUTOFF = 85


class Categoriser(Protocol):
    def categorise(self, keys: list[str], taxonomy: list[str]) -> dict[str, str]: ...


def _period_of(row) -> str:
    return row.transaction_date.strftime("%Y-%m")


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _apply_and_track(db, key, rows, category, stat_name, stats, affected_periods) -> None:
    applied = apply_category_to_key(db, key, [row.id for row in rows], category)
    stats[stat_name] += applied
    if applied:
        affected_periods.update(_period_of(row) for row in rows)


def categorise(db: Session, categoriser: Categoriser) -> dict:
    """Resolve every uncategorised row. Returns per-layer counts for the caller."""
    rows = get_uncategorised(db)
    stats = {
        "rows": len(rows),
        "resolved_by_rules": 0,
        "resolved_unknown_no_key": 0,
        "resolved_by_cache": 0,
        "resolved_by_cluster": 0,
        "resolved_by_fuzzy": 0,
        "resolved_by_llm": 0,
        "llm_batches_attempted": 0,
        "llm_batches_failed": 0,
    }
    if not rows:
        return stats

    affected_periods: set[str] = set()
    cache = load_merchant_cache(db)
    by_key: dict[str, list] = {}

    # Layer 0 — deterministic rules, and the no-key -> Unknown fallback. Both
    # write the transaction row directly; neither touches merchant_categories,
    # since the resolution isn't merchant-key-based.
    for row in rows:
        category = decide(row.transaction_code, row.amount_cents, row.vendor_name, None)
        if category is not None:
            row.category = category
            affected_periods.add(_period_of(row))
            stats["resolved_by_rules"] += 1
            continue

        key = merchant_key(row.transaction_code, row.vendor_name)
        if key is None:
            row.category = "Unknown"
            affected_periods.add(_period_of(row))
            stats["resolved_unknown_no_key"] += 1
            continue

        by_key.setdefault(key, []).append(row)

    # Cluster this run's new keys together with the existing cache, so a
    # brand-new row can match an already-cached cluster (layer 2) even though
    # its own exact key has never been seen before.
    canonical = cluster_keys(list(cache) + list(by_key))
    clustered_cache: dict[str, str] = {}
    for cached_key, entry in cache.items():
        clustered_cache.setdefault(canonical.get(cached_key, cached_key), entry.category)

    fuzzy_choices = list(cache) if (process is not None and cache) else []
    miss_keys: list[str] = []

    for key, key_rows in by_key.items():
        if key in cache:
            _apply_and_track(
                db, key, key_rows, cache[key].category, "resolved_by_cache", stats, affected_periods
            )
            continue

        key_canonical = canonical.get(key, key)
        if key_canonical in clustered_cache:
            _apply_and_track(
                db, key, key_rows, clustered_cache[key_canonical], "resolved_by_cluster",
                stats, affected_periods,
            )
            continue

        if fuzzy_choices:
            match = process.extractOne(
                key, fuzzy_choices, scorer=fuzz.WRatio, score_cutoff=FUZZY_SCORE_CUTOFF
            )
            if match is not None:
                matched_key = match[0]
                _apply_and_track(
                    db, key, key_rows, cache[matched_key].category, "resolved_by_fuzzy",
                    stats, affected_periods,
                )
                continue

        miss_keys.append(key)

    for period in affected_periods:
        recompute_summary(db, period)
    db.commit()

    if not miss_keys:
        return stats

    taxonomy = [c.name for c in list_categories(db)]

    for batch in _chunked(miss_keys, BATCH_SIZE):
        stats["llm_batches_attempted"] += 1
        try:
            answers = categoriser.categorise(batch, taxonomy)
        except Exception:
            logger.exception("Categoriser batch failed; stopping — next run retries the rest")
            stats["llm_batches_failed"] += 1
            break

        batch_periods: set[str] = set()
        for key, category in answers.items():
            if key not in by_key or category not in taxonomy:
                continue
            upsert_merchant(db, key, category, source="llm")
            applied = apply_category_to_key(db, key, [row.id for row in by_key[key]], category)
            if applied:
                stats["resolved_by_llm"] += applied
                batch_periods.update(_period_of(row) for row in by_key[key])

        for period in batch_periods:
            recompute_summary(db, period)
        db.commit()

    return stats
