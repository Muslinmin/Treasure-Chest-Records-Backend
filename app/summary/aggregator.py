from sqlalchemy import delete, func, select
from app.db.models import Transaction, Summary
from sqlalchemy.dialects.sqlite import insert

from datetime import date


import logging


logger = logging.getLogger(__name__)


def recompute_summary(db, period: str) -> None:
    category_col = func.coalesce(Transaction.category, "Uncategorised").label("category")

    year, month = map(int, period.split("-"))
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year+1, 1, 1)
    else:
        end_date = date(year, month+1, 1)

    stmt = (
        select(
            category_col,
            func.sum(Transaction.amount_cents).label("total_cents"),
            func.count(Transaction.id).label("tx_count")
        )
        .where(Transaction.transaction_date >= start_date, Transaction.transaction_date < end_date)
        .group_by(category_col)
    )
    results = db.execute(stmt).all()

    # Recompute-from-scratch of the row set, not just the values: a category
    # that no longer has any rows in this period must not keep a stale
    # (period, category) row behind — otherwise GET /summary sums it alongside
    # the row the category was reassigned to.
    db.execute(delete(Summary).where(Summary.period == period))

    for row in results:
        upsert_stmt = insert(Summary).values(
            period=period,
            category=row.category,
            total_cents=row.total_cents,
            tx_count=row.tx_count
        ).on_conflict_do_update(
            index_elements=["period", "category"],
            set_={"total_cents": row.total_cents, "tx_count": row.tx_count}
        )
        db.execute(upsert_stmt)
    logger.info(f"Summary aggregated for period {period}")
