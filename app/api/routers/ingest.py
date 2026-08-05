import os
from pathlib import Path
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key
from app.ingest.pipeline import ingest_and_categorise
from app.llm.categoriser import Categoriser

router = APIRouter(dependencies=[Depends(verify_api_key)])

INBOX_PATH = Path(os.getenv("INBOX"))
PROCESSED_PATH = Path(os.getenv("OUTBOX"))
FAILED_PATH = Path(os.getenv("FAILED_BOX"))

# §10.9 — POST /ingest now returns {"files": [...], "categorised": {...}}
# rather than a bare list. Breaking change for the frontend; see §10.9's note
# on response-model drift before deploying this.
@router.post("/ingest")
def ingest(db: Session = Depends(get_db)):
    return ingest_and_categorise(db, INBOX_PATH, PROCESSED_PATH, FAILED_PATH, Categoriser())
