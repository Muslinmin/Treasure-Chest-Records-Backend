
from app.db.models import Transaction, Summary
from sqlalchemy.orm import Session
from sqlalchemy import select

from datetime import date
import logging


logger = logging.getLogger(__name__)


def insert_records(db: Session, records: list[dict]):
    source_file = records[0]["source_file"]
    stmt = select(Transaction).filter(Transaction.source_file == source_file)
    existing = db.execute(stmt).first()
    if not existing:
        for record in records:
            transaction = Transaction(**record)
            db.add(transaction)
        logger.info(f"Staged {len(records)} records")
    else:
        raise ValueError(f"{source_file} has already been ingested")


def get_transactions(
    db: Session,
    limit: int,
    offset: int,
    date_from: date | None,
    date_to: date | None,
    category: str | None,
) -> list[Transaction]:
    stmt = select(Transaction)
    
    if date_from is not None:
        stmt= stmt.where(Transaction.transaction_date >= date_from)
    
    if date_to is not None:
        stmt= stmt.where(Transaction.transaction_date <= date_to)
    
    if category is not None:
        category_lower = category.lower()
        stmt = stmt.where(Transaction.category == category_lower)

    stmt = stmt.limit(limit).offset(offset)
    records = db.scalars(stmt).all()
    logger.info("Transactions retrieved!")
    return records


def get_summary_by_period(db: Session, period: str) -> list[Summary]:
    stmt = select(Summary)
    stmt = stmt.where(Summary.period == period)
    records = db.scalars(stmt).all()
    logger.info(f"Summary for {period} retrieved!")
    return records


def get_summary_monthly(db: Session, start_period: str, end_period: str) -> list[Summary]:
    stmt = select(Summary)
    
    stmt= stmt.where(Summary.period >= start_period, Summary.period <= end_period)
    

    records = db.scalars(stmt).all()
    logger.info(f"Summary for period range {start_period} and {end_period} retrieved!")

    return records