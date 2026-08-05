"""ingest_and_categorise — §10.9's POST /ingest contract.

The router (app/api/routers/ingest.py) is a thin wrapper around this
function, matching the rest of the codebase's convention of keeping business
logic testable outside FastAPI (routers aren't tested directly anywhere in
this suite).
"""

import shutil

import pytest

from app.db.queries import get_uncategorised, load_merchant_cache
from app.ingest.pipeline import ingest_and_categorise

from test.conftest import FIXTURES
from test.integration.test_categorise import DictCategoriser, MISS_ANSWERS


@pytest.fixture
def drop(boxes):
    def _drop(fixture_name: str, as_name: str | None = None):
        target = boxes["inbox"] / (as_name or fixture_name)
        shutil.copy(FIXTURES / fixture_name, target)
        return target

    return _drop


def run(db, boxes, categoriser):
    return ingest_and_categorise(db, boxes["inbox"], boxes["outbox"], boxes["failed"], categoriser)


class TestContractShape:
    def test_returns_files_and_categorised_keys(self, db, boxes, drop):
        drop("sample.csv")
        result = run(db, boxes, DictCategoriser())

        assert set(result) == {"files", "categorised"}
        assert result["files"] == [
            {"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}
        ]


class TestCategorisationRunsAfterIngest:
    def test_rows_are_categorised_in_the_same_call(self, db, boxes, drop):
        drop("sample.csv")
        result = run(db, boxes, DictCategoriser(MISS_ANSWERS))

        assert result["categorised"]["resolved_by_rules"] == 3
        assert result["categorised"]["resolved_by_llm"] == 3
        assert get_uncategorised(db) == []

    def test_an_empty_inbox_has_nothing_to_categorise(self, db, boxes):
        result = ingest_and_categorise(
            db, boxes["inbox"], boxes["outbox"], boxes["failed"], DictCategoriser()
        )
        assert result["files"] == []
        assert result["categorised"]["rows"] == 0


class TestCategorisationFailureDoesNotFailIngest:
    def test_a_categoriser_that_always_raises_still_lets_ingest_succeed(self, db, boxes, drop):
        class _AlwaysFails:
            def categorise(self, keys, taxonomy):
                raise RuntimeError("provider outage")

        drop("sample.csv")
        result = run(db, boxes, _AlwaysFails())

        assert result["files"] == [
            {"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}
        ]
        # Layer-0 rule resolutions still happened; only the LLM batch failed.
        assert result["categorised"]["llm_batches_failed"] == 1
        assert (boxes["outbox"] / "sample.csv").exists()

    def test_a_categoriser_that_raises_outside_the_batch_loop_still_lets_ingest_succeed(
        self, db, boxes, drop, monkeypatch
    ):
        """service.categorise() itself can raise (e.g. get_uncategorised
        failing) — ingest_and_categorise's own try/except is the backstop,
        independent of service.py's per-batch handling."""
        import app.ingest.pipeline as pipeline_module

        def _boom(db, categoriser):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(pipeline_module, "categorise", _boom)

        drop("sample.csv")
        result = run(db, boxes, DictCategoriser())

        assert result["files"] == [
            {"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}
        ]
        assert result["categorised"] == {}
        assert (boxes["outbox"] / "sample.csv").exists()


class TestMerchantCacheIsPopulated:
    def test_llm_resolved_keys_are_cached_for_the_next_ingest(self, db, boxes, drop):
        drop("sample.csv")
        run(db, boxes, DictCategoriser(MISS_ANSWERS))

        cache = load_merchant_cache(db)
        assert cache["subway @ test"].category == "Dining & Takeout"
        assert cache["subway @ test"].source == "llm"
