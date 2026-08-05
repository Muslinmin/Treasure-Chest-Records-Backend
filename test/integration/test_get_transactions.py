"""queries.get_transactions — filters, including the category-matching bug fix.

Categories are stored Title Case (e.g. "Groceries"), as produced by rules.py
and the LLM categoriser. The category filter previously lower-cased the query
value and compared it against the stored value with a case-sensitive equality,
so it could never match anything — this is the regression test for that fix.
"""

from datetime import date

from app.db.models import Transaction
from app.db.queries import get_transactions


def add_transaction(db, day: int, category: str | None, amount_cents: int = -100) -> Transaction:
    txn = Transaction(
        transaction_date=date(2026, 5, day),
        amount_cents=amount_cents,
        description=f"ROW {day}",
        transaction_code="POS",
        vendor_name=f"ROW {day}",
        category=category,
        is_settled=True,
        is_category_manual=False,
        fingerprint=f"fp-{day}-{category}",
    )
    db.add(txn)
    return txn


def fetch(db, **overrides):
    params = {
        "limit": 50,
        "offset": 0,
        "date_from": None,
        "date_to": None,
        "category": None,
    }
    params.update(overrides)
    return get_transactions(db, **params)


class TestCategoryFilter:
    def test_matches_the_stored_title_case_category_exactly(self, db):
        add_transaction(db, day=10, category="Groceries")
        db.commit()

        results = fetch(db, category="Groceries")
        assert len(results) == 1

    def test_matches_regardless_of_query_casing(self, db):
        add_transaction(db, day=10, category="Groceries")
        db.commit()

        assert len(fetch(db, category="groceries")) == 1
        assert len(fetch(db, category="GROCERIES")) == 1
        assert len(fetch(db, category="GrOcErIeS")) == 1

    def test_matches_multi_word_categories_regardless_of_casing(self, db):
        add_transaction(db, day=10, category="Dining & Takeout")
        db.commit()

        assert len(fetch(db, category="dining & takeout")) == 1

    def test_a_non_matching_category_returns_nothing(self, db):
        add_transaction(db, day=10, category="Groceries")
        db.commit()

        assert fetch(db, category="Transport") == []

    def test_none_category_returns_every_row(self, db):
        add_transaction(db, day=10, category="Groceries")
        add_transaction(db, day=11, category="Transport")
        db.commit()

        assert len(fetch(db, category=None)) == 2

    def test_null_category_rows_are_excluded_by_a_category_filter(self, db):
        add_transaction(db, day=10, category=None)
        add_transaction(db, day=11, category="Groceries")
        db.commit()

        results = fetch(db, category="Groceries")
        assert len(results) == 1
        assert results[0].category == "Groceries"


class TestDateFilters:
    def test_date_from_is_inclusive(self, db):
        add_transaction(db, day=10, category="Groceries")
        add_transaction(db, day=11, category="Groceries")
        db.commit()

        results = fetch(db, date_from=date(2026, 5, 11))
        assert {r.transaction_date for r in results} == {date(2026, 5, 11)}

    def test_date_to_is_inclusive(self, db):
        add_transaction(db, day=10, category="Groceries")
        add_transaction(db, day=11, category="Groceries")
        db.commit()

        results = fetch(db, date_to=date(2026, 5, 10))
        assert {r.transaction_date for r in results} == {date(2026, 5, 10)}

    def test_date_range_and_category_combine(self, db):
        add_transaction(db, day=10, category="Groceries")
        add_transaction(db, day=15, category="Groceries")
        add_transaction(db, day=10, category="Transport")
        db.commit()

        results = fetch(db, date_from=date(2026, 5, 9), date_to=date(2026, 5, 12), category="groceries")
        assert len(results) == 1
        assert results[0].transaction_date == date(2026, 5, 10)


class TestPagination:
    def test_limit_caps_the_result_count(self, db):
        for day in range(10, 15):
            add_transaction(db, day=day, category="Groceries")
        db.commit()

        assert len(fetch(db, limit=2)) == 2

    def test_offset_skips_rows(self, db):
        for day in range(10, 13):
            add_transaction(db, day=day, category="Groceries")
        db.commit()

        all_rows = fetch(db, limit=50)
        offset_rows = fetch(db, limit=50, offset=1)
        assert offset_rows == all_rows[1:]
