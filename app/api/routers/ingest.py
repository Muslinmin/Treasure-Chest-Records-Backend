import json
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db, SessionLocal
from app.db import queries
from app.api.auth.api_key import verify_api_key
from app.ingest.pipeline import ingest_uploads, run_categorisation_job
from app.llm.categoriser import Categoriser

router = APIRouter(dependencies=[Depends(verify_api_key)])

# ARCHIVE is optional: unset it (e.g. in a cloud deploy with no persistent
# volume for raw uploads) and ingest_uploads skips writing uploads to disk
# entirely, parsing straight from the request body. Set it (e.g. for a local
# test run) to keep a pass/failed-suffixed copy of every upload.
_archive_env = os.getenv("ARCHIVE")
ARCHIVE_PATH = Path(_archive_env) if _archive_env else None


# v1.5 — POST /ingest returns as soon as the (fast, DB-only) upload phase is
# done, and runs categorisation — which depends on an external LLM provider
# with unbounded latency — as a background job instead of holding the
# request open for it. 202 body: {"files": [...], "job_id": ..., "status_url":
# ...}; poll GET /ingest/jobs/{job_id} for the outcome.
@router.post("/ingest", status_code=202)
async def ingest(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    uploads = [(f.filename, await f.read()) for f in files]

    collect_ids: list[int] = []
    file_results = ingest_uploads(db, uploads, ARCHIVE_PATH, collect_ids=collect_ids)

    job = queries.create_ingest_job(db)
    db.commit()

    background_tasks.add_task(
        run_categorisation_job, SessionLocal, job.id, collect_ids, Categoriser()
    )

    status_url = f"/ingest/jobs/{job.id}"
    return JSONResponse(
        status_code=202,
        headers={"Location": status_url},
        content={"files": file_results, "job_id": job.id, "status_url": status_url},
    )


@router.get("/ingest/jobs/{job_id}")
def get_ingest_job(job_id: str, db: Session = Depends(get_db)):
    job = queries.get_ingest_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.commit()

    body = {
        "job_id": job.id,
        "status": job.status,
        "result": json.loads(job.result) if job.result else None,
        "error": job.error,
    }
    headers = {"Retry-After": "3"} if job.status in ("pending", "running") else None
    return JSONResponse(content=body, headers=headers)
