
import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

import sentry_sdk
from sqlalchemy.orm import Session

from app.ingest.csv_parser import parse_csv
from app.db.queries import insert_records
from app.summary.aggregator import recompute_summary
from app.categorise.service import Categoriser, categorise

logger = logging.getLogger(__name__)


def _archived_name(file_name: str, outcome: str) -> str:
    """{timestamp}_{original-stem}_{pass|failed}{original-ext} — see
    "Direct CSV Upload (v1.4)" in .agent/architecture_and_progress.md. The
    timestamp prefix exists because direct upload makes re-uploading a file
    under the same original name a normal action, unlike the old one-time
    inbox drop, so a bare rename would silently overwrite the prior archive
    entry.
    """
    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{timestamp}_{stem}_{outcome}{suffix}"


def process_upload(file_name: str, content: bytes, db: Session, archive_path: Path | None) -> dict:
    """archive_path=None skips writing anything to disk beyond the temp file
    parse_csv needs to read from — nothing persists past this call. Pass a
    real directory to keep a pass/failed-suffixed audit copy of every upload.
    """
    if Path(file_name).suffix.lower() != ".csv":
        error = f"Not a CSV file: {file_name}"
        if archive_path is not None:
            (archive_path / _archived_name(file_name, "failed")).write_bytes(content)
        logger.info(f"Rejected {file_name}: {error}")
        return {"file": file_name, "status": "failed", "error": error}

    with tempfile.TemporaryDirectory() as tmp_dir:
        staged = Path(tmp_dir) / file_name
        staged.write_bytes(content)
        try:
            records = parse_csv(staged)
            logger.info("CSV parsed.....")
            periods = {r["transaction_date"].strftime("%Y-%m") for r in records}
            logger.info("Periods retrieved.....")
            counts = insert_records(db, records)
            logger.info("Records inserted.....")
            for p in periods:
                recompute_summary(db, p)
            db.commit()
            logger.info(
                f"Successfully ingested {file_name}: "
                f"{counts['inserted']} inserted, {counts['skipped']} skipped"
            )
            if archive_path is not None:
                # shutil.move (not Path.rename): tmp_dir and archive_path are
                # very likely different filesystems/mounts, and a bare rename
                # raises EXDEV across devices.
                shutil.move(staged, archive_path / _archived_name(file_name, "pass"))
            return {
                "file": file_name,
                "status": "ok",
                "inserted": counts["inserted"],
                "skipped": counts["skipped"],
            }
        except Exception as e:
            db.rollback()
            logger.info(f"Failed to ingest {file_name} : {e}")
            if archive_path is not None:
                try:
                    shutil.move(staged, archive_path / _archived_name(file_name, "failed"))
                except Exception as move_err:
                    logger.error(f"Failed to archive file {staged}: {move_err}")
            return {"file": file_name, "status": "failed", "error": str(e)}


def ingest_uploads(
    db: Session, uploads: list[tuple[str, bytes]], archive_path: Path | None
) -> list[dict]:
    result = []
    for file_name, content in uploads:
        result.append(process_upload(file_name, content, db, archive_path))
    if not result:
        logger.warning("No files were uploaded")
    return result


def ingest_and_categorise(
    db: Session,
    uploads: list[tuple[str, bytes]],
    archive_path: Path | None,
    categoriser: Categoriser,
) -> dict:
    """POST /ingest's full contract: ingest uploaded files, then categorise.

    Categorisation runs in its own transaction, after ingest_uploads has
    already committed per file — a provider outage (or any other failure in
    the categorise step) must not change ingest semantics. Files already
    committed and archived are unaffected either way; the exception is
    logged, not raised.
    """
    ingest_start = time.perf_counter()
    with sentry_sdk.start_span(op="ingest.uploads", name="ingest_uploads") as span:
        span.set_data("file_count", len(uploads))
        files = ingest_uploads(db, uploads, archive_path)
    ingest_ms = (time.perf_counter() - ingest_start) * 1000
    logger.info(f"ingest_uploads: {len(uploads)} file(s) in {ingest_ms:.1f}ms")

    categorised = {}
    categorise_start = time.perf_counter()
    try:
        with sentry_sdk.start_span(op="categorise.run", name="categorise"):
            categorised = categorise(db, categoriser)
    except Exception:
        logger.exception("Categorisation failed after ingest; ingest itself is unaffected")
    categorise_ms = (time.perf_counter() - categorise_start) * 1000
    logger.info(f"categorise: {categorise_ms:.1f}ms")

    return {"files": files, "categorised": categorised}
