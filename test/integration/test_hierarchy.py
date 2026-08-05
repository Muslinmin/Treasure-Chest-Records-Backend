"""app/categorise/hierarchy.py — carved_from re-derivation (§Planned: Category Hierarchy).

Mirrors test_categorise.py's checkpoint style: a dict-backed fake Categoriser
(imported from there, no network, no spend) drives the binary
new-leaf-vs-catch-all disambiguation ``rederive_category`` sends to the LLM
for every non-manual candidate — there is no cheaper deterministic layer,
since a merchant's own name is not reliable evidence of what a given
purchase there was for (see TestCarveNeverGuessesFromTheMerchantName).
"""

from datetime import date

import pytest
from sqlalchemy import select

from app.categorise.hierarchy import carve_category, rederive_category
from app.db.models import Summary, Transaction
from app.db.queries import get_children, load_merchant_cache, upsert_merchant

from test.integration.test_categorise import DictCategoriser


def add_transaction(db, category, vendor_name, key_suffix, is_category_manual=False) -> Transaction:
    txn = Transaction(
        transaction_date=date(2026, 5, 10),
        amount_cents=-450,
        description=f"ROW {key_suffix}",
        transaction_code="POS",
        vendor_name=vendor_name,
        category=category,
        is_settled=True,
        is_category_manual=is_category_manual,
        fingerprint=f"fp-{key_suffix}",
    )
    db.add(txn)
    return txn


class TestCarvePureAddition:
    def test_no_parent_means_no_rederivation_and_no_llm_calls(self, db):
        stub = DictCategoriser()
        result = carve_category(db, stub, "Meal Prep", carved_from=[])

        assert result["category"].name == "Meal Prep"
        assert result["catch_all_created"] is None
        assert result["rederivation"] is None
        assert result["recomputed_periods"] == []
        assert stub.calls == 0


class TestCarveNeverGuessesFromTheMerchantName:
    def test_a_name_that_looks_obvious_still_goes_through_the_llm(self, db):
        """A merchant literally named "May's Coffee" may sell pastries and
        lunch, not coffee — the merchant's own name is not evidence of what a
        specific purchase there was for. There is no free string-match layer:
        every non-manual candidate is batched to the LLM, and its answer
        decides the outcome, never a match against the category label."""
        add_transaction(db, "Dining & Takeout", "MAY'S COFFEE", "a")
        upsert_merchant(db, "may's coffee", "Dining & Takeout", source="llm")
        db.commit()

        stub = DictCategoriser({"may's coffee": "Dining & Takeout (Other)"})
        result = carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert result["catch_all_created"] == "Dining & Takeout (Other)"
        assert stub.calls == 1
        assert stub.seen_batches == [["may's coffee"]]
        assert result["rederivation"]["resolved_by_llm"] == 0

        row = db.scalars(select(Transaction)).one()
        assert row.category == "Dining & Takeout (Other)"
        cache = load_merchant_cache(db)
        assert cache["may's coffee"].category == "Dining & Takeout (Other)"

    def test_the_llm_can_still_confirm_an_obvious_looking_match(self, db):
        add_transaction(db, "Dining & Takeout", "STARBUCKS COFFEE", "a")
        upsert_merchant(db, "starbucks coffee", "Dining & Takeout", source="llm")
        db.commit()

        stub = DictCategoriser({"starbucks coffee": "Coffee"})
        result = carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert stub.calls == 1
        assert result["rederivation"]["resolved_by_llm"] == 1
        assert result["recomputed_periods"] == ["2026-05"]

        row = db.scalars(select(Transaction)).one()
        assert row.category == "Coffee"
        cache = load_merchant_cache(db)
        assert cache["starbucks coffee"].category == "Coffee"
        assert cache["starbucks coffee"].source == "llm"

        summary_categories = {r.category for r in db.scalars(select(Summary)).all()}
        assert summary_categories == {"Coffee"}


class TestCarveWithLlmDisambiguation:
    def test_ambiguous_merchants_are_batched_to_the_categoriser(self, db):
        add_transaction(db, "Dining & Takeout", "KOPITIAM", "a")
        add_transaction(db, "Dining & Takeout", "MCDONALDS", "b")
        upsert_merchant(db, "kopitiam", "Dining & Takeout", source="llm")
        upsert_merchant(db, "mcdonalds", "Dining & Takeout", source="llm")
        db.commit()

        stub = DictCategoriser({"kopitiam": "Coffee", "mcdonalds": "Dining & Takeout (Other)"})
        result = carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert stub.calls == 1
        assert set(stub.seen_batches[0]) == {"kopitiam", "mcdonalds"}
        assert result["rederivation"]["resolved_by_llm"] == 1

        rows = {r.description: r.category for r in db.scalars(select(Transaction)).all()}
        assert rows["ROW a"] == "Coffee"
        assert rows["ROW b"] == "Dining & Takeout (Other)"

        cache = load_merchant_cache(db)
        assert cache["kopitiam"].category == "Coffee"
        assert cache["mcdonalds"].category == "Dining & Takeout (Other)"

    def test_the_llm_taxonomy_is_the_binary_choice_not_the_full_list(self, db):
        upsert_merchant(db, "kopitiam", "Dining & Takeout", source="llm")
        db.commit()

        stub = DictCategoriser({"kopitiam": "Coffee"})
        carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert stub.seen_batches[0] == ["kopitiam"]


class TestManualOverridesSurviveRederivation:
    def test_a_manually_sourced_merchant_is_never_reassigned(self, db):
        add_transaction(db, "Dining & Takeout", "STARBUCKS COFFEE", "a")
        upsert_merchant(db, "starbucks coffee", "Dining & Takeout", source="manual")
        db.commit()

        stub = DictCategoriser()
        result = carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert result["rederivation"]["candidates"] == 0
        assert stub.calls == 0

        row = db.scalars(select(Transaction)).one()
        assert row.category == "Dining & Takeout (Other)"
        cache = load_merchant_cache(db)
        assert cache["starbucks coffee"].category == "Dining & Takeout (Other)"
        assert cache["starbucks coffee"].source == "manual"

    def test_a_manually_categorised_transaction_row_is_never_overwritten(self, db):
        add_transaction(db, "Dining & Takeout", "STARBUCKS COFFEE", "a", is_category_manual=True)
        upsert_merchant(db, "starbucks coffee", "Dining & Takeout", source="llm")
        db.commit()

        carve_category(db, DictCategoriser(), "Coffee", carved_from=["Dining & Takeout"])

        row = db.scalars(select(Transaction)).one()
        assert row.category == "Dining & Takeout (Other)"
        assert row.is_category_manual is True


class TestSecondCarveReusesCatchAll:
    def test_re_derivation_runs_again_scoped_to_the_existing_catch_all(self, db):
        add_transaction(db, "Dining & Takeout", "STARBUCKS COFFEE", "a")
        add_transaction(db, "Dining & Takeout", "KFC", "b")
        upsert_merchant(db, "starbucks coffee", "Dining & Takeout", source="llm")
        upsert_merchant(db, "kfc", "Dining & Takeout", source="llm")
        db.commit()

        first_stub = DictCategoriser({"starbucks coffee": "Coffee", "kfc": "Dining & Takeout (Other)"})
        carve_category(db, first_stub, "Coffee", carved_from=["Dining & Takeout"])
        second = carve_category(
            db, DictCategoriser({"kfc": "Fast Food"}), "Fast Food", carved_from=["Dining & Takeout"]
        )

        assert second["catch_all_created"] is None
        assert second["rederivation"]["resolved_by_llm"] == 1
        rows = {r.description: r.category for r in db.scalars(select(Transaction)).all()}
        assert rows["ROW a"] == "Coffee"
        assert rows["ROW b"] == "Fast Food"
        assert {c.name for c in get_children(db, "Dining & Takeout")} == {
            "Coffee", "Fast Food", "Dining & Takeout (Other)"
        }


class TestCarveRejectsInvalidRequests:
    def test_carving_from_an_already_carved_leaf_is_rejected(self, db):
        carve_category(db, DictCategoriser(), "Coffee", carved_from=["Dining & Takeout"])
        with pytest.raises(ValueError):
            carve_category(db, DictCategoriser(), "Espresso", carved_from=["Coffee"])


class TestRederiveCategoryNoCandidates:
    def test_empty_catch_all_makes_no_llm_calls(self, db):
        stub = DictCategoriser()
        stats, periods = rederive_category(db, stub, "Coffee", "Dining & Takeout (Other)")
        assert stats["candidates"] == 0
        assert periods == set()
        assert stub.calls == 0


class TestRederivePartialBatchFailure:
    def test_a_failing_batch_leaves_earlier_progress_committed(self, db, monkeypatch):
        import app.categorise.hierarchy as hierarchy

        monkeypatch.setattr(hierarchy, "BATCH_SIZE", 1)
        add_transaction(db, "Dining & Takeout", "KOPITIAM", "a")
        add_transaction(db, "Dining & Takeout", "SUBWAY", "b")
        upsert_merchant(db, "kopitiam", "Dining & Takeout", source="llm")
        upsert_merchant(db, "subway", "Dining & Takeout", source="llm")
        db.commit()

        stub = DictCategoriser({"kopitiam": "Coffee", "subway": "Coffee"}, fail_on_batch=2)
        result = carve_category(db, stub, "Coffee", carved_from=["Dining & Takeout"])

        assert result["rederivation"]["llm_batches_attempted"] == 2
        assert result["rederivation"]["llm_batches_failed"] == 1
        assert result["rederivation"]["resolved_by_llm"] == 1

        cache = load_merchant_cache(db)
        # One of the two batches succeeded before the simulated failure — which
        # one is order-dependent (dict iteration order), so assert on the
        # invariant that matters: exactly one merchant moved, one didn't.
        moved = [k for k in ("kopitiam", "subway") if cache[k].category == "Coffee"]
        stayed = [k for k in ("kopitiam", "subway") if cache[k].category == "Dining & Takeout (Other)"]
        assert len(moved) == 1
        assert len(stayed) == 1
