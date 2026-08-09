import os
from pathlib import Path
from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key
from app.ingest.pipeline import ingest_and_categorise
from app.llm.categoriser import Categoriser

router = APIRouter(dependencies=[Depends(verify_api_key)])

# ARCHIVE is optional: unset it (e.g. in a cloud deploy with no persistent
# volume for raw uploads) and ingest_and_categorise skips writing uploads to
# disk entirely, parsing straight from the request body. Set it (e.g. for a
# local test run) to keep a pass/failed-suffixed copy of every upload.
_archive_env = os.getenv("ARCHIVE")
ARCHIVE_PATH = Path(_archive_env) if _archive_env else None

# v1.4 — POST /ingest takes CSVs directly as multipart uploads instead of
# scanning a server-side inbox folder. Response shape unchanged from §10.9:
# {"files": [...], "categorised": {...}}.
@router.post("/ingest")
async def ingest(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    uploads = [(f.filename, await f.read()) for f in files]
    return ingest_and_categorise(db, uploads, ARCHIVE_PATH, Categoriser())
