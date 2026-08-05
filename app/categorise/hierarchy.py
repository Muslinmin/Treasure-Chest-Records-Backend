"""Category hierarchy — carved_from re-derivation (§Planned: Category Hierarchy, v1.3).

Carving a new leaf out of an existing top-level category promotes that
category to a stem and auto-generates a ``"<Stem> (Other)"`` catch-all
sibling, migrating everything previously filed directly under the stem onto
it (``queries.create_category``, DB-only, no LLM). This module does the half
of the design that needs the LLM: re-deriving which of the catch-all's
merchants actually belong under the new leaf instead.

Cost stays bounded by unique merchant count, never transaction count, by
scanning ``merchant_categories`` rather than ``transaction_records`` — the
same reasoning ``categorise.service``'s cache-first layers already rely on.

Every non-``manual`` candidate goes to the LLM, batched ~40 keys at a time to
the same ``Categoriser`` interface service.py depends on, choosing between
exactly the new leaf and the catch-all it came from. A binary choice, not the
full taxonomy: the question here is narrower than general categorisation,
and the real ``LLMCategoriser`` builds its answer enum from whatever
taxonomy it's given, so this works unmodified.

There is deliberately no cheaper deterministic pre-filter (e.g. matching the
merchant key against words in the new category's name) — a merchant literally
named "May's Coffee" may sell pastries and lunch, not coffee. A merchant's own
name is not reliable evidence of what a specific purchase there was for,
so every reassignment goes through the same judgment call, never a string
match against the category label.

``manual``-sourced merchant_categories rows are never touched — the same
protection that keeps a manually-categorised transaction safe from every
categorisation rebuild (service.py) applies here too (§10.5).
"""

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.categorise.normalise import merchant_key
from app.db.models import MerchantCategory, Transaction
from app.db.queries import apply_category_to_key, create_category, upsert_merchant
from app.summary.aggregator import recompute_summary

logger = logging.getLogger(__name__)

BATCH_SIZE = 40


class Categoriser(Protocol):
    def categorise(self, keys: list[str], taxonomy: list[str]) -> dict[str, str]: ...


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _transactions_by_merchant_key(db: Session, category: str) -> dict[str, list[Transaction]]:
    rows = db.scalars(select(Transaction).where(Transaction.category == category)).all()
    grouped: dict[str, list[Transaction]] = {}
    for row in rows:
        key = merchant_key(row.transaction_code, row.vendor_name)
        if key is not None:
            grouped.setdefault(key, []).append(row)
    return grouped


def rederive_category(
    db: Session, categoriser: Categoriser, new_category: str, catch_all: str
) -> tuple[dict, set[str]]:
    """Reassign merchants under ``catch_all`` that belong under ``new_category``.

    Returns ``(stats, affected_periods)`` — the latter for the caller to
    recompute summaries over, mirroring ``hard_delete_category``'s contract.
    """
    stats = {
        "candidates": 0,
        "resolved_by_llm": 0,
        "llm_batches_attempted": 0,
        "llm_batches_failed": 0,
    }
    affected_periods: set[str] = set()

    merchants = db.scalars(
        select(MerchantCategory).where(
            MerchantCategory.category == catch_all,
            MerchantCategory.source != "manual",
        )
    ).all()
    stats["candidates"] = len(merchants)
    if not merchants:
        return stats, affected_periods

    tx_by_key = _transactions_by_merchant_key(db, catch_all)

    def _reassign(key: str, category: str, source: str) -> int:
        upsert_merchant(db, key, category, source=source)
        rows = tx_by_key.get(key, [])
        if not rows:
            return 0
        applied = apply_category_to_key(db, key, [r.id for r in rows], category)
        if applied:
            affected_periods.update(r.transaction_date.strftime("%Y-%m") for r in rows)
        return applied

    miss_keys = [m.merchant_key for m in merchants]

    for batch in _chunked(miss_keys, BATCH_SIZE):
        stats["llm_batches_attempted"] += 1
        try:
            answers = categoriser.categorise(batch, [new_category, catch_all])
        except Exception:
            logger.exception("Re-derivation batch failed; stopping — next run retries the rest")
            stats["llm_batches_failed"] += 1
            break

        for key in batch:
            if answers.get(key) == new_category:
                stats["resolved_by_llm"] += _reassign(key, new_category, "llm")
            # else: stays under catch_all — already correctly filed there.

    return stats, affected_periods


def carve_category(db: Session, categoriser: Categoriser, name: str, carved_from: list[str]) -> dict:
    """Create ``name`` (optionally carved from a parent) and re-derive if needed.

    Owns the whole request: creation, catch-all migration, LLM-backed
    re-derivation, summary recompute, and the commit — matching the
    trigger-seam contract ``categorise.service.categorise`` and
    ``queries.hard_delete_category`` already follow (caller supplies an open
    session and, here, a categoriser; this function commits).
    """
    result = create_category(db, name, carved_from)
    category = result["category"]
    catch_all = result["catch_all"]
    affected_periods = set(result["affected_periods"])

    rederivation_stats = None
    if catch_all is not None:
        rederivation_stats, extra_periods = rederive_category(db, categoriser, name, catch_all)
        affected_periods |= extra_periods

    for period in affected_periods:
        recompute_summary(db, period)
    db.commit()
    db.refresh(category)

    return {
        "category": category,
        "catch_all_created": catch_all if result["catch_all_created"] else None,
        "rederivation": rederivation_stats,
        "recomputed_periods": sorted(affected_periods),
    }
