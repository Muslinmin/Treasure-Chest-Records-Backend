from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from pathlib import Path
import time
import logging
from app.ingest.csv_parser import parse_csv 

import os

from dotenv import load_dotenv

from app.db.session import SessionLocal

from app.db.queries import insert_records
from app.summary import aggregator

logger = logging.getLogger(__name__)


load_dotenv()

INBOX_PATH = os.getenv("INBOX")
OUTBOX_PATH = os.getenv("OUTBOX")
FAILED_PATH = os.getenv("FAILED_BOX")


class CSVHandler(FileSystemEventHandler):

    def on_created(self, event):
        # 1. ignore if it's a directory, not a file
        # 2. ignore if it's not a .csv file
        # 3. run the stability check
        # 4. call parse_csv
        # 5. insert records into the database
        # 6. move file to processed/ on success, failed/ on any exception
        if event.is_directory:
            return
        filepath = Path(event.src_path)
        if filepath.suffix.lower() != ".csv":
            return
        db = None
        try:
            logger.info("Waiting for folder stability....")
            _wait_until_stable(filepath)
            records = parse_csv(filepath)
            logger.info("CSV parsed.....")
            db = SessionLocal()
            logger.info("Session Created.....")
            periods = {r["transaction_date"].strftime("%Y-%m") for r in records}
            logger.info("Periods parsed.....")
            insert_records(db, records)
            logger.info("Records inserted.....")
            for p in periods:
                aggregator.recompute_summary(db, p)
            db.commit()
            filepath.rename(Path(OUTBOX_PATH) / filepath.name)
            logger.info(f"Successfully ingested {filepath.name}")
        except Exception as e:
            if db:
                db.rollback()
            logger.error(f"Failed to ingest {filepath.name}: {e}")
            filepath.rename(Path(FAILED_PATH) / filepath.name)
        finally:
            if db:
                db.close()

def _wait_until_stable(filepath: Path):
    previous_size = -1
    previous_mtime = -1
    while True:
        current_size = filepath.stat().st_size
        current_mtime = filepath.stat().st_mtime
        if current_size == previous_size and current_mtime == previous_mtime:
            break
        previous_size = current_size
        previous_mtime = current_mtime
        time.sleep(0.5)


