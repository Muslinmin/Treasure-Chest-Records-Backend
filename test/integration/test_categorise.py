"""service.categorise() — build order step 8, and the §10.12 test spec.

The real check-in point from §10.10 step 8 is running the five-layer pipeline
end-to-end against real transaction data with a stub categoriser — no
network, no spend — to see the true miss count before any LLM code exists.
The actual 669-row bank export is private data that never enters this repo
(§7); ``sample.csv`` is the committed synthetic stand-in, card-masked and
already exercised by test_pipeline.py, so it plays that role here too.

Ingested, sample.csv yields six rows:
    SUBWAY (UMC-S debit)          -> no rule; key "subway @ test"
    KOPITIAM (UMC-S debit)        -> no rule; key "kopitiam test outlet"
    COFFEE STALL 12 (ITR debit)   -> rules.decide -> "Transfer Out"
    COFFEE STALL 12 (ITR debit)   -> rules.decide -> "Transfer Out"
    JANE DOE incoming (ICT credit)-> rules.decide -> "Transfer In"
    PENDING TEST SHOP (UMC-S debit, not settled) -> no rule; key "pending test shop"

So layer 0 resolves 3 of 6 rows, and the empty-cache run puts the other 3
distinct merchant keys into one LLM batch.
"""

import pytest
from sqlalchemy import select

from app.categorise import service
from app.categorise.service import categorise
from app.db.models import Summary
from app.db.queries import get_uncategorised, load_merchant_cache
from app.ingest.pipeline import ingest_uploads

from test.conftest import FIXTURES


class DictCategoriser:
    """The §10.12 dict-backed fake — no network, no spend."""

    def __init__(self, answers: dict[str, str] | None = None, fail_on_batch: int | None = None):
        self.answers = answers or {}
        self.fail_on_batch = fail_on_batch
        self.calls = 0
        self.seen_batches: list[list[str]] = []

    def categorise(self, keys: list[str], taxonomy: list[str]) -> dict[str, str]:
        self.calls += 1
        self.seen_batches.append(list(keys))
        if self.fail_on_batch == self.calls:
            raise RuntimeError("simulated provider outage")
        return {key: self.answers[key] for key in keys if key in self.answers}


@pytest.fixture
def ingested(db, archive):
    """sample.csv, already ingested — six rows, none categorised yet."""
    content = (FIXTURES / "sample.csv").read_bytes()
    ingest_uploads(db, [("sample.csv", content)], archive)
    return db


MISS_ANSWERS = {
    "subway @ test": "Dining & Takeout",
    "kopitiam test outlet": "Dining & Takeout",
    "pending test shop": "Shopping",
}


class TestCheckpointMissCount:
    def test_reports_the_true_miss_count_with_a_silent_stub(self, ingested):
        """The step 8 checkpoint: with no answers at all, layers 0-3 still
        resolve what they can, and the rest is reported honestly as missed —
        not guessed at."""
        stub = DictCategoriser()
        stats = categorise(ingested, stub)

        assert stats["rows"] == 6
        assert stats["resolved_by_rules"] == 3
        assert stats["resolved_unknown_no_key"] == 0
        assert stats["resolved_by_llm"] == 0
        assert stats["llm_batches_attempted"] == 1
        assert stub.calls == 1
        assert stub.seen_batches == [
            ["subway @ test", "kopitiam test outlet", "pending test shop"]
        ]

        remaining = get_uncategorised(ingested)
        assert len(remaining) == 3
        assert {r.category for r in remaining} == {None}


class TestEndToEndResolution:
    def test_every_row_is_resolved_and_the_cache_is_populated(self, ingested):
        stub = DictCategoriser(MISS_ANSWERS)
        stats = categorise(ingested, stub)

        assert stats["resolved_by_rules"] == 3
        assert stats["resolved_by_llm"] == 3
        assert get_uncategorised(ingested) == []

        cache = load_merchant_cache(ingested)
        assert cache["subway @ test"].category == "Dining & Takeout"
        assert cache["subway @ test"].source == "llm"
        assert cache["pending test shop"].category == "Shopping"

    def test_summary_reflects_every_resolved_row_with_no_orphans(self, ingested):
        categorise(ingested, DictCategoriser(MISS_ANSWERS))

        rows = ingested.scalars(select(Summary)).all()
        categories = {r.category for r in rows}
        assert categories == {"Dining & Takeout", "Transfer Out", "Transfer In", "Shopping"}
        assert sum(r.tx_count for r in rows) == 6


class TestIdempotency:
    def test_a_second_run_makes_zero_llm_calls(self, ingested):
        stub = DictCategoriser(MISS_ANSWERS)
        categorise(ingested, stub)
        assert stub.calls == 1

        second_stats = categorise(ingested, stub)
        assert stub.calls == 1
        assert second_stats["rows"] == 0


class TestManualOverrideSurvival:
    def test_a_manually_categorised_row_is_never_touched(self, ingested):
        row = get_uncategorised(ingested)[0]
        row.category = "Personal Care"
        row.is_category_manual = True
        ingested.commit()

        categorise(ingested, DictCategoriser(MISS_ANSWERS))

        ingested.refresh(row)
        assert row.category == "Personal Care"
        assert row.is_category_manual is True


class TestPartialBatchFailure:
    def test_a_failing_batch_leaves_earlier_batches_committed(self, ingested, monkeypatch):
        """A categoriser raising mid-run must not discard resolutions from
        batches that already succeeded — §10.8's "next run retries only
        those" promise."""
        monkeypatch.setattr(service, "BATCH_SIZE", 1)
        stub = DictCategoriser({"subway @ test": "Dining & Takeout"}, fail_on_batch=2)

        stats = categorise(ingested, stub)

        assert stats["llm_batches_attempted"] == 2
        assert stats["llm_batches_failed"] == 1
        assert stats["resolved_by_llm"] == 1

        cache = load_merchant_cache(ingested)
        assert "subway @ test" in cache
        assert "kopitiam test outlet" not in cache

        remaining = get_uncategorised(ingested)
        assert len(remaining) == 2


class TestNoUncategorisedRows:
    def test_an_empty_inbox_makes_no_llm_calls(self, db):
        stub = DictCategoriser()
        stats = categorise(db, stub)
        assert stats["rows"] == 0
        assert stub.calls == 0
