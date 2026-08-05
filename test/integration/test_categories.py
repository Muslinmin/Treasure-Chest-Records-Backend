"""categories table — seeding, CRUD, and the §10.5 delete/reassign contract."""

from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import Category, MerchantCategory, Transaction
from app.db.queries import (
    SYSTEM_CATEGORIES,
    STARTING_CATEGORIES,
    create_category,
    get_category,
    get_children,
    hard_delete_category,
    has_children,
    list_categories,
    list_categories_with_parent_names,
    list_leaf_categories,
    seed_categories,
    soft_delete_category,
)


class TestSeeding:
    def test_seed_creates_every_system_and_starting_category(self, db):
        """conftest.py's db fixture already seeds — this asserts what landed."""
        names = {c.name for c in db.scalars(select(Category)).all()}
        assert names == set(SYSTEM_CATEGORIES) | set(STARTING_CATEGORIES)

    def test_system_categories_are_flagged_is_system(self, db):
        for name in SYSTEM_CATEGORIES:
            assert get_category(db, name).is_system is True

    def test_starting_categories_are_not_system(self, db):
        for name in STARTING_CATEGORIES:
            assert get_category(db, name).is_system is False

    def test_seeding_twice_does_not_duplicate(self, db):
        seed_categories(db)
        db.commit()
        names = [c.name for c in db.scalars(select(Category)).all()]
        assert len(names) == len(set(names))


class TestCreateCategory:
    def test_pure_addition_with_no_parents(self, db):
        result = create_category(db, "Coffee", carved_from=[])
        db.commit()
        category = result["category"]
        assert category.name == "Coffee"
        assert category.is_system is False
        assert category.is_active is True
        assert category.parent_id is None
        assert result["catch_all"] is None
        assert result["catch_all_created"] is False
        assert result["affected_periods"] == set()

    def test_duplicate_name_is_rejected(self, db):
        with pytest.raises(ValueError):
            create_category(db, "Groceries", carved_from=[])

    def test_carved_from_validates_parents_exist_and_are_active(self, db):
        with pytest.raises(ValueError):
            create_category(db, "Coffee", carved_from=["Nonexistent Category"])

    def test_carved_from_rejects_more_than_one_parent(self, db):
        """§Planned: Category Hierarchy explicitly rules out multi-parent (DAG)
        categories — a leaf can have at most one parent."""
        with pytest.raises(ValueError):
            create_category(db, "Coffee", carved_from=["Dining & Takeout", "Unknown"])

    def test_carved_from_single_valid_parent_succeeds(self, db):
        result = create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        category = result["category"]
        parent = get_category(db, "Dining & Takeout")
        assert category.name == "Coffee"
        assert category.parent_id == parent.id

    def test_carved_from_rejects_inactive_parent(self, db):
        soft_delete_category(db, "Housing")
        db.commit()
        with pytest.raises(ValueError):
            create_category(db, "Rent Splitting", carved_from=["Housing"])

    def test_carved_from_rejects_a_parent_that_already_has_a_parent(self, db):
        """Caps the tree at two levels: a leaf-with-a-parent is atomic and
        cannot itself be carved from."""
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        with pytest.raises(ValueError):
            create_category(db, "Espresso", carved_from=["Coffee"])

    def test_first_carve_promotes_parent_and_creates_catch_all(self, db):
        result = create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        assert result["catch_all"] == "Dining & Takeout (Other)"
        assert result["catch_all_created"] is True
        catch_all = get_category(db, "Dining & Takeout (Other)")
        assert catch_all is not None
        parent = get_category(db, "Dining & Takeout")
        assert catch_all.parent_id == parent.id
        assert has_children(db, parent) is True
        assert {c.name for c in get_children(db, "Dining & Takeout")} == {"Coffee", "Dining & Takeout (Other)"}

    def test_first_carve_migrates_existing_rows_to_the_catch_all(self, db):
        db.add(
            Transaction(
                transaction_date=date(2026, 5, 10),
                amount_cents=-500,
                description="ROW",
                transaction_code="POS",
                vendor_name="ROW",
                category="Dining & Takeout",
                is_settled=True,
                is_category_manual=False,
                fingerprint="fp-dining-1",
            )
        )
        db.add(MerchantCategory(merchant_key="mcdonalds", category="Dining & Takeout", source="llm"))
        db.commit()

        result = create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        assert result["affected_periods"] == {"2026-05"}
        row = db.scalars(select(Transaction)).one()
        assert row.category == "Dining & Takeout (Other)"
        merchant = db.scalars(select(MerchantCategory)).one()
        assert merchant.category == "Dining & Takeout (Other)"

    def test_second_carve_under_same_parent_reuses_the_catch_all(self, db):
        first = create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        second = create_category(db, "Fast Food", carved_from=["Dining & Takeout"])
        db.commit()

        assert first["catch_all_created"] is True
        assert second["catch_all_created"] is False
        assert second["catch_all"] == "Dining & Takeout (Other)"
        assert second["affected_periods"] == set()
        assert {c.name for c in get_children(db, "Dining & Takeout")} == {
            "Coffee", "Fast Food", "Dining & Takeout (Other)"
        }


class TestLeafAndParentListings:
    def test_stem_is_excluded_from_leaf_categories(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        leaf_names = {c.name for c in list_leaf_categories(db)}
        assert "Dining & Takeout" not in leaf_names
        assert "Coffee" in leaf_names
        assert "Dining & Takeout (Other)" in leaf_names

    def test_list_categories_with_parent_names_reports_parent(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        rows = {r["name"]: r["parent_name"] for r in list_categories_with_parent_names(db)}
        assert rows["Coffee"] == "Dining & Takeout"
        assert rows["Dining & Takeout (Other)"] == "Dining & Takeout"
        assert rows["Dining & Takeout"] is None


class TestSoftDelete:
    def test_deactivates_without_removing_the_row(self, db):
        soft_delete_category(db, "Shopping")
        db.commit()

        category = get_category(db, "Shopping")
        assert category is not None
        assert category.is_active is False

    def test_deactivated_category_is_excluded_from_default_listing(self, db):
        soft_delete_category(db, "Shopping")
        db.commit()

        assert "Shopping" not in {c.name for c in list_categories(db)}
        assert "Shopping" in {c.name for c in list_categories(db, include_inactive=True)}

    def test_rows_already_carrying_it_keep_their_label(self, db):
        db.add(
            Transaction(
                transaction_date=date(2026, 5, 10),
                amount_cents=-100,
                description="ROW",
                transaction_code="POS",
                vendor_name="ROW",
                category="Shopping",
                is_settled=True,
                is_category_manual=False,
                fingerprint="fp-shopping-1",
            )
        )
        db.commit()

        soft_delete_category(db, "Shopping")
        db.commit()

        row = db.scalars(select(Transaction)).one()
        assert row.category == "Shopping"

    def test_system_categories_cannot_be_soft_deleted(self, db):
        with pytest.raises(ValueError):
            soft_delete_category(db, "Unknown")

    def test_nonexistent_category_raises(self, db):
        with pytest.raises(ValueError):
            soft_delete_category(db, "Not A Real Category")

    def test_a_stem_cannot_be_soft_deleted(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        with pytest.raises(ValueError):
            soft_delete_category(db, "Dining & Takeout")


class TestHardDelete:
    def test_system_categories_cannot_be_hard_deleted(self, db):
        with pytest.raises(ValueError):
            hard_delete_category(db, "Interest", reassign_to="Income")

    def test_reassign_target_must_be_active(self, db):
        with pytest.raises(ValueError):
            hard_delete_category(db, "Shopping", reassign_to="Not A Real Category")

    def test_bulk_reassigns_every_row_and_removes_the_category(self, db):
        for i in range(3):
            db.add(
                Transaction(
                    transaction_date=date(2026, 5, 10 + i),
                    amount_cents=-100 * (i + 1),
                    description=f"ROW {i}",
                    transaction_code="POS",
                    vendor_name=f"ROW {i}",
                    category="Shopping",
                    is_settled=True,
                    is_category_manual=False,
                    fingerprint=f"fp-shopping-{i}",
                )
            )
        db.commit()

        affected_periods = hard_delete_category(db, "Shopping", reassign_to="Groceries")
        db.commit()

        assert affected_periods == {"2026-05"}
        assert get_category(db, "Shopping") is None
        rows = db.scalars(select(Transaction)).all()
        assert all(r.category == "Groceries" for r in rows)

    def test_no_affected_rows_returns_an_empty_period_set(self, db):
        affected_periods = hard_delete_category(db, "Shopping", reassign_to="Groceries")
        db.commit()

        assert affected_periods == set()
        assert get_category(db, "Shopping") is None

    def test_a_stem_cannot_be_hard_deleted(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        with pytest.raises(ValueError):
            hard_delete_category(db, "Dining & Takeout", reassign_to="Groceries")

    def test_reassign_target_cannot_be_a_stem(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()
        with pytest.raises(ValueError):
            hard_delete_category(db, "Shopping", reassign_to="Dining & Takeout")
