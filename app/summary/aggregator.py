"""
1. Query transactions WHERE record_date starts with period (e.g. "2025-04")
2. Group the results by category
3. For each category group:
   - sum the amount_cents
   - count the rows
4. Upsert each group into summary table

"""


from sqlalchemy import func, select
from app.db.models import Transaction, Summary
from sqlalchemy.dialects.sqlite import insert

from datetime import datetime, date





def recompute_summary(db, period: str) -> None:
    category_col = func.coalesce(Transaction.category, "Uncategorised").label("category")
    
    date_obj = datetime.strptime(period, "%Y-%m")

    month = date_obj.month
    year = date_obj.year

    stmt = (
        select(
            category_col,
            func.sum(Transaction.amount_cents).label("total_cents"),
            func.count(Transaction.id).label("tx_count")
        )
        .where(Transaction.transaction_date >= date(year, month, 1), Transaction.transaction_date < date(year, month+1, 1))
        .group_by(category_col)
    )
    results = db.execute(stmt).all()



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
