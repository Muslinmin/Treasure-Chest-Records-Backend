"""merchant_categories — §10.6 cache table, and the §10.7 FK it exercises for real."""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Category, MerchantCategory, Transaction
from app.db.queries import (
    apply_category_to_key,
    get_uncategorised,
    hard_delete_category,
    load_merchant_cache,
    upsert_merchant,
)


def add_transaction(db, category=None, is_category_manual=False, key_suffix="1") -> Transaction:
    txn = Transaction(
        transaction_date=date(2026, 5, 10),
        amount_cents=-450,
        description=f"ROW {key_suffix}",
        transaction_code="POS",
        vendor_name=f"ROW {key_suffix}",
        category=category,
        is_settled=True,
        is_category_manual=is_category_manual,
        fingerprint=f"fp-{key_suffix}",
    )
    db.add(txn)
    return txn


class TestGetUncategorised:
    def test_returns_only_null_category_non_manual_rows(self, db):
        add_transaction(db, category=None, is_category_manual=False, key_suffix="a")
        add_transaction(db, category="Groceries", is_category_manual=False, key_suffix="b")
        add_transaction(db, category=None, is_category_manual=True, key_suffix="c")
        db.commit()

        rows = get_uncategorised(db)
        assert len(rows) == 1
        assert rows[0].description == "ROW a"

    def test_empty_when_nothing_is_uncategorised(self, db):
        add_transaction(db, category="Groceries", key_suffix="a")
        db.commit()
        assert get_uncategorised(db) == []


class TestUpsertMerchant:
    def test_inserts_a_new_mapping(self, db):
        merchant = upsert_merchant(db, "grab*", "Transport", "rule")
        db.commit()
        assert merchant.merchant_key == "grab*"
        assert merchant.category == "Transport"
        assert merchant.source == "rule"
        assert merchant.hit_count == 0

    def test_updates_an_existing_mapping_in_place(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        db.commit()

        updated = upsert_merchant(db, "grab*", "Dining & Takeout", "manual")
        db.commit()

        assert updated.category == "Dining & Takeout"
        assert updated.source == "manual"
        assert db.scalar(select(func.count()).select_from(MerchantCategory)) == 1

    def test_rejects_a_category_that_does_not_exist(self, db):
        with pytest.raises(IntegrityError):
            upsert_merchant(db, "grab*", "Not A Real Category", "rule")
            db.commit()

    def test_source_is_restricted_to_the_enum(self, db):
        with pytest.raises(IntegrityError):
            upsert_merchant(db, "grab*", "Transport", "guess")
            db.commit()


class TestLoadMerchantCache:
    def test_loads_every_mapping_keyed_by_merchant_key(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        upsert_merchant(db, "mcdonalds", "Dining & Takeout", "llm")
        db.commit()

        cache = load_merchant_cache(db)
        assert set(cache) == {"grab*", "mcdonalds"}
        assert cache["grab*"].category == "Transport"

    def test_empty_cache_returns_an_empty_dict(self, db):
        assert load_merchant_cache(db) == {}


class TestApplyCategoryToKey:
    def test_categorises_every_listed_row_and_bumps_hit_count(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        t1 = add_transaction(db, key_suffix="a")
        t2 = add_transaction(db, key_suffix="b")
        db.commit()

        updated = apply_category_to_key(db, "grab*", [t1.id, t2.id], "Transport")
        db.commit()

        assert updated == 2
        assert t1.category == "Transport"
        assert t2.category == "Transport"
        cache = load_merchant_cache(db)
        assert cache["grab*"].hit_count == 2

    def test_never_overwrites_a_manually_categorised_row(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        manual = add_transaction(db, category="Shopping", is_category_manual=True, key_suffix="a")
        db.commit()

        updated = apply_category_to_key(db, "grab*", [manual.id], "Transport")
        db.commit()

        assert updated == 0
        assert manual.category == "Shopping"

    def test_empty_id_list_is_a_no_op(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        db.commit()
        assert apply_category_to_key(db, "grab*", [], "Transport") == 0


class TestForeignKeyEnforcement:
    def test_on_delete_restrict_blocks_deleting_a_referenced_category(self, db):
        upsert_merchant(db, "grab*", "Transport", "rule")
        db.commit()

        transport = db.scalars(select(Category).where(Category.name == "Transport")).one()
        db.delete(transport)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_hard_delete_category_reassigns_merchant_categories_too(self, db):
        """hard_delete_category must clear merchant_categories references
        first, or the FK's ON DELETE RESTRICT would block the delete."""
        upsert_merchant(db, "grab*", "Shopping", "rule")
        db.commit()

        hard_delete_category(db, "Shopping", reassign_to="Transport")
        db.commit()

        cache = load_merchant_cache(db)
        assert cache["grab*"].category == "Transport"
