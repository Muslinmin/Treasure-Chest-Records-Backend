
import logging
logger = logging.getLogger(__name__)
from app.db.models import Transaction
from sqlalchemy.orm import Session


def insert_records(db: Session, records: list[dict]):
    for record in records:
        transaction = Transaction(**record)
        db.add(transaction)
    logger.info(f"Staged {len(records)} records")