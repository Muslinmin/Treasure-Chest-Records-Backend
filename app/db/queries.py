
import logging
logger = logging.getLogger(__name__)
from app.db.models import Transaction
from sqlalchemy.orm import Session
from sqlalchemy import select


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