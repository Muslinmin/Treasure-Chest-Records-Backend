
import logging
import shutil
from pathlib import Path
from sqlalchemy.orm import Session

from app.ingest.csv_parser import parse_csv
from app.db.queries import insert_records
from app.summary.aggregator import recompute_summary

logger = logging.getLogger(__name__)



def process_file(filepath: Path, db: Session, processed_path: Path, failed_path: Path) -> dict:
    file_name = filepath.name
    try:
        records = parse_csv(filepath)
        logger.info("CSV parsed.....")
        periods = {r["transaction_date"].strftime("%Y-%m") for r in records}
        logger.info("Periods retrieved.....")
        insert_records(db, records)
        logger.info("Records inserted.....")
        for p in periods:
            recompute_summary(db, p)
        db.commit()
        logger.info(f"Successfully ingested {filepath.name}")
        shutil.move(filepath, processed_path / file_name)
        logger.info(f"Moved {filepath.name} to {processed_path}")
        return {"file": file_name, "status": "ok", "rows": len(records)}
    except Exception as e:
        db.rollback()
        logger.info(f"Failed to ingest {filepath.name} : {e}")
        try:
            shutil.move(filepath, failed_path / file_name)
            logger.info(f"Moved {filepath.name} to {failed_path}")
        except Exception as move_err:
            logger.error(f"Failed to move file {filepath}: {move_err}")
        return {"file": file_name, "status": "failed", "error": str(e)}


def ingest_inbox(db: Session, inbox_path: Path, processed_path: Path, failed_path: Path) -> list[dict]:
    logger.debug("is this line called?")
    logger.debug(f"paths: {inbox_path} \n {processed_path} \n {failed_path} \n")
    result = []
    for item in inbox_path.iterdir():
        if item.is_file():
            logger.debug(f"Found {item.name}")
            if item.suffix == ".csv":
                result_dict = process_file(item, db, processed_path, failed_path)
                result.append(result_dict)
    logger.warning(f"No valid files found in {inbox_path}")
    return result
