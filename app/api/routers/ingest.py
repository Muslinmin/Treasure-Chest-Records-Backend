import os
from pathlib import Path
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key
from app.ingest.pipeline import ingest_inbox

router = APIRouter(dependencies=[Depends(verify_api_key)])

INBOX_PATH = Path(os.getenv("INBOX"))
PROCESSED_PATH = Path(os.getenv("OUTBOX"))
FAILED_PATH = Path(os.getenv("FAILED_BOX"))

@router.post("/ingest")
def ingest(db: Session = Depends(get_db)):
    result = ingest_inbox(db, INBOX_PATH, PROCESSED_PATH, FAILED_PATH)
    return result
