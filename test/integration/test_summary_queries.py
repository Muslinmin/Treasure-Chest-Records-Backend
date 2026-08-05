"""get_summary_by_period / get_summary_monthly, including ?rollup=true (§Planned:
Category Hierarchy).

Rollup is computed at read time by grouping existing leaf Summary rows by
COALESCE(parent_name, category) — never written as separate aggregate rows,
so these tests exercise the read path only; recompute_summary() itself is
covered by test_aggregator.py and is untouched by this feature.
"""

from datetime import datetime

from app.db.models import Summary
from app.db.queries import create_category, get_summary_by_period, get_summary_monthly


def add_summary_row(db, period, category, total_cents=-1000, tx_count=1) -> Summary:
    row = Summary(
        period=period,
        category=category,
        total_cents=total_cents,
        tx_count=tx_count,
        updated_at=datetime(2026, 5, 1),
    )
    db.add(row)
    return row


class TestGetSummaryByPeriodWithoutRollup:
    def test_default_returns_leaf_rows_unchanged(self, db):
        add_summary_row(db, "2026-05", "Groceries", total_cents=-500, tx_count=1)
        db.commit()

        rows = get_summary_by_period(db, "2026-05")
        assert len(rows) == 1
        assert isinstance(rows[0], Summary)
        assert rows[0].category == "Groceries"


class TestRollupGroupsByParent:
    def test_children_and_catch_all_roll_up_into_the_stem(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        add_summary_row(db, "2026-05", "Coffee", total_cents=-500, tx_count=2)
        add_summary_row(db, "2026-05", "Dining & Takeout (Other)", total_cents=-300, tx_count=1)
        add_summary_row(db, "2026-05", "Groceries", total_cents=-1000, tx_count=3)
        db.commit()

        rows = get_summary_by_period(db, "2026-05", rollup=True)
        by_category = {r["category"]: r for r in rows}

        assert by_category["Dining & Takeout"]["total_cents"] == -800
        assert by_category["Dining & Takeout"]["tx_count"] == 3
        assert by_category["Groceries"]["total_cents"] == -1000
        assert "Coffee" not in by_category
        assert "Dining & Takeout (Other)" not in by_category

    def test_a_top_level_category_with_no_children_rolls_up_to_itself(self, db):
        add_summary_row(db, "2026-05", "Groceries", total_cents=-200, tx_count=1)
        db.commit()

        rows = get_summary_by_period(db, "2026-05", rollup=True)
        assert len(rows) == 1
        assert rows[0]["category"] == "Groceries"
        assert rows[0]["total_cents"] == -200

    def test_a_category_name_with_no_matching_row_rolls_up_to_itself(self, db):
        """e.g. the aggregator's synthetic "Uncategorised" label, or a category
        since deleted — neither exists in the categories table."""
        add_summary_row(db, "2026-05", "Uncategorised", total_cents=-50, tx_count=1)
        db.commit()

        rows = get_summary_by_period(db, "2026-05", rollup=True)
        assert len(rows) == 1
        assert rows[0]["category"] == "Uncategorised"

    def test_empty_period_returns_empty_list(self, db):
        assert get_summary_by_period(db, "2026-05", rollup=True) == []


class TestGetSummaryMonthlyRollup:
    def test_rollup_is_scoped_per_period(self, db):
        create_category(db, "Coffee", carved_from=["Dining & Takeout"])
        db.commit()

        add_summary_row(db, "2026-04", "Coffee", total_cents=-100, tx_count=1)
        add_summary_row(db, "2026-04", "Dining & Takeout (Other)", total_cents=-200, tx_count=1)
        add_summary_row(db, "2026-05", "Coffee", total_cents=-400, tx_count=1)
        db.commit()

        rows = get_summary_monthly(db, "2026-04", "2026-05", rollup=True)
        by_period = {r["period"]: r for r in rows}
        assert by_period["2026-04"]["category"] == "Dining & Takeout"
        assert by_period["2026-04"]["total_cents"] == -300
        assert by_period["2026-05"]["category"] == "Dining & Takeout"
        assert by_period["2026-05"]["total_cents"] == -400
