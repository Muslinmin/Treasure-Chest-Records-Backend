"""recompute_summary — DELETE-before-insert, §4.2.

Recompute-from-scratch must be true of the row set, not just the values:
a category that no longer has any rows in a period must not leave a stale
(period, category) row behind for GET /summary to sum alongside the new one.
"""

from datetime import date

from sqlalchemy import func, select

from app.db.models import Summary, Transaction
from app.summary.aggregator import recompute_summary


def add_transaction(db, category: str | None, amount_cents: int = -450, day: int = 10) -> Transaction:
    txn = Transaction(
        transaction_date=date(2026, 5, day),
        amount_cents=amount_cents,
        description="TEST ROW",
        transaction_code="ITR",
        vendor_name="TEST ROW",
        category=category,
        is_settled=True,
        is_category_manual=False,
        fingerprint=f"fp-{category}-{day}-{amount_cents}",
    )
    db.add(txn)
    return txn


class TestRecomputeFromScratch:
    def test_a_category_with_no_remaining_rows_is_not_orphaned(self, db):
        txn = add_transaction(db, category="Food")
        db.commit()
        recompute_summary(db, "2026-05")
        db.commit()

        assert {r.category for r in db.scalars(select(Summary)).all()} == {"Food"}

        txn.category = "Groceries"
        db.commit()
        recompute_summary(db, "2026-05")
        db.commit()

        rows = db.scalars(select(Summary)).all()
        assert {r.category for r in rows} == {"Groceries"}

    def test_recompute_is_idempotent_on_repeated_calls(self, db):
        add_transaction(db, category="Food")
        db.commit()

        recompute_summary(db, "2026-05")
        db.commit()
        recompute_summary(db, "2026-05")
        db.commit()

        rows = db.scalars(select(Summary)).all()
        assert len(rows) == 1
        assert rows[0].tx_count == 1

    def test_totals_still_match_after_reassignment(self, db):
        add_transaction(db, category="Food", amount_cents=-450, day=10)
        add_transaction(db, category="Food", amount_cents=-900, day=11)
        db.commit()
        recompute_summary(db, "2026-05")
        db.commit()

        first = db.scalars(select(Transaction).where(Transaction.category == "Food")).first()
        first.category = "Transport"
        db.commit()
        recompute_summary(db, "2026-05")
        db.commit()

        rows = {r.category: r for r in db.scalars(select(Summary)).all()}
        assert set(rows) == {"Food", "Transport"}
        assert rows["Food"].tx_count == 1
        assert rows["Transport"].tx_count == 1

    def test_other_periods_are_untouched(self, db):
        add_transaction(db, category="Food", day=10)
        db.add(
            Transaction(
                transaction_date=date(2026, 6, 1),
                amount_cents=-100,
                description="JUNE ROW",
                transaction_code="ITR",
                vendor_name="JUNE ROW",
                category="Food",
                is_settled=True,
                is_category_manual=False,
                fingerprint="fp-june",
            )
        )
        db.commit()
        recompute_summary(db, "2026-05")
        recompute_summary(db, "2026-06")
        db.commit()

        assert db.scalar(select(func.count()).select_from(Summary)) == 2

        recompute_summary(db, "2026-05")
        db.commit()

        periods = {r.period for r in db.scalars(select(Summary)).all()}
        assert periods == {"2026-05", "2026-06"}
