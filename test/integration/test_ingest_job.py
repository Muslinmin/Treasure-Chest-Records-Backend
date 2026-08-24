"""End-to-end ingest job flow (v1.5) — POST /ingest's async contract.

POST /ingest now returns as soon as the (fast, DB-only) upload phase is
done, and runs categorisation — which depends on an external LLM provider
with unbounded latency — as a background job instead of holding the HTTP
response open for it (see the "propose to me this feature" design
discussion). The router (app/api/routers/ingest.py) is a thin wrapper around
ingest_uploads + queries.create_ingest_job + run_categorisation_job +
queries.get_ingest_job, matching the rest of the suite's convention of
testing business logic rather than routes directly — fastapi isn't even
installed in this dev venv, only inside the Docker image the app runs in.
"""

import json
from datetime import date, datetime, timedelta

import pytest

from app.db.queries import (
    create_ingest_job,
    get_ingest_job,
    get_uncategorised,
    insert_records,
    reconcile_orphaned_jobs,
)
from app.ingest.identity import compute_fingerprint
from app.ingest.pipeline import ingest_uploads, run_categorisation_job

from test.conftest import FIXTURES
from test.integration.test_categorise import DictCategoriser, MISS_ANSWERS


@pytest.fixture
def upload():
    def _upload(fixture_name: str, as_name: str | None = None) -> tuple[str, bytes]:
        content = (FIXTURES / fixture_name).read_bytes()
        return (as_name or fixture_name, content)

    return _upload


class _KeepAliveSession:
    """run_categorisation_job() closes the session it opened once its work
    is done — correct in production (it opened its own, throwaway one), but
    in a test the "session" is the fixture the test still needs afterward.
    Proxies everything except close(), which no-ops.
    """

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def make_row_record(
    vendor_name: str,
    transaction_code: str = "UMC-S",
    amount_cents: int = -500,
    transaction_date: date = date(2026, 6, 1),
) -> dict:
    """A debit row that rules.decide() can't resolve, so it needs a merchant
    key looked up — unlike sample.csv's ITR/ICT rows, which rules resolve
    outright and so are useless for exercising the miss path.
    """
    record = {
        "transaction_date": transaction_date,
        "amount_cents": amount_cents,
        "description": vendor_name,
        "transaction_code": transaction_code,
        "vendor_name": vendor_name,
        "category": None,
        "is_settled": True,
        "is_category_manual": False,
    }
    record["fingerprint"] = compute_fingerprint(
        transaction_date, amount_cents, vendor_name, transaction_code
    )
    return record


class TestFullJobFlow:
    def test_upload_to_completed_job_round_trip(self, db, archive, upload):
        collect_ids: list[int] = []
        file_results = ingest_uploads(db, [upload("sample.csv")], archive, collect_ids=collect_ids)
        assert file_results == [{"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}]
        assert len(collect_ids) == 6

        job = create_ingest_job(db)
        db.commit()
        assert job.status == "pending"

        run_categorisation_job(
            lambda: _KeepAliveSession(db), job.id, collect_ids, DictCategoriser(MISS_ANSWERS)
        )

        fetched = get_ingest_job(db, job.id)
        assert fetched.status == "completed"
        assert fetched.error is None
        assert get_uncategorised(db) == []

        result = json.loads(fetched.result)
        assert result["resolved_by_rules"] == 3
        assert result["resolved_by_llm"] == 3


class TestJobIsScopedToItsOwnRows:
    def test_one_jobs_categorisation_does_not_touch_another_jobs_rows(self, db, archive):
        """The whole point of collect_ids: two ingests that both leave rows
        uncategorised must not step on each other's job results."""
        ids_a: list[int] = []
        insert_records(db, [make_row_record("MERCHANT ALPHA")], collect_ids=ids_a)
        db.commit()

        ids_b: list[int] = []
        insert_records(db, [make_row_record("MERCHANT BETA")], collect_ids=ids_b)
        db.commit()

        job_a = create_ingest_job(db)
        db.commit()
        run_categorisation_job(
            lambda: _KeepAliveSession(db),
            job_a.id,
            ids_a,
            DictCategoriser({"merchant alpha": "Shopping"}),
        )

        remaining = get_uncategorised(db)
        assert [row.id for row in remaining] == ids_b
        assert remaining[0].vendor_name == "MERCHANT BETA"

        assert get_ingest_job(db, job_a.id).status == "completed"


class TestFailureIsRecordedNotSwallowed:
    def test_an_exception_outside_the_batch_loop_marks_the_job_failed(self, db, monkeypatch):
        """service.categorise() itself can raise (e.g. get_uncategorised
        failing) — run_categorisation_job's own try/except is the backstop,
        independent of service.py's per-batch handling."""
        import app.ingest.pipeline as pipeline_module

        def _boom(db, categoriser, transaction_ids=None):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(pipeline_module, "categorise", _boom)

        job = create_ingest_job(db)
        db.commit()

        run_categorisation_job(lambda: _KeepAliveSession(db), job.id, [], DictCategoriser())

        fetched = get_ingest_job(db, job.id)
        assert fetched.status == "failed"
        assert fetched.error == "unexpected failure during categorisation"

    def test_a_provider_outage_still_completes_the_job_not_fails_it(self, db, archive, upload):
        """service.categorise() already catches per-batch LLM failures and
        reports them in its stats — that isn't the job itself failing. Only
        an exception escaping categorise() entirely should mark the job
        "failed"."""
        collect_ids: list[int] = []
        ingest_uploads(db, [upload("sample.csv")], archive, collect_ids=collect_ids)

        class _AlwaysFails:
            def categorise(self, keys, taxonomy):
                raise RuntimeError("provider outage")

        job = create_ingest_job(db)
        db.commit()
        run_categorisation_job(lambda: _KeepAliveSession(db), job.id, collect_ids, _AlwaysFails())

        fetched = get_ingest_job(db, job.id)
        assert fetched.status == "completed"
        assert json.loads(fetched.result)["llm_batches_failed"] == 1


class TestStaleRunningJobIsReconciledOnPoll:
    def test_a_running_job_with_no_recent_update_is_marked_failed(self, db):
        job = create_ingest_job(db)
        job.status = "running"
        job.updated_at = datetime.now() - timedelta(minutes=30)
        db.commit()

        fetched = get_ingest_job(db, job.id, stale_after=timedelta(minutes=10))

        assert fetched.status == "failed"
        assert fetched.error == "stale: no progress detected"

    def test_a_recently_updated_running_job_is_left_alone(self, db):
        job = create_ingest_job(db)
        job.status = "running"
        db.commit()

        fetched = get_ingest_job(db, job.id, stale_after=timedelta(minutes=10))

        assert fetched.status == "running"

    def test_a_stale_pending_job_is_left_alone(self, db):
        """Only "running" is treated as possibly-hung; "pending" just means
        the background task hasn't started yet, which reconcile_orphaned_jobs
        (startup) rather than staleness (poll-time) is responsible for."""
        job = create_ingest_job(db)
        job.updated_at = datetime.now() - timedelta(minutes=30)
        db.commit()

        fetched = get_ingest_job(db, job.id, stale_after=timedelta(minutes=10))

        assert fetched.status == "pending"

    def test_unknown_job_id_returns_none(self, db):
        assert get_ingest_job(db, "does-not-exist") is None


class TestOrphanedJobsReconciledOnStartup:
    def test_pending_and_running_jobs_are_marked_failed(self, db):
        pending = create_ingest_job(db)
        running = create_ingest_job(db)
        running.status = "running"
        db.commit()

        count = reconcile_orphaned_jobs(db)
        db.commit()

        assert count == 2
        assert get_ingest_job(db, pending.id).status == "failed"
        assert get_ingest_job(db, running.id).status == "failed"
        assert get_ingest_job(db, pending.id).error == "interrupted by server restart"

    def test_completed_and_failed_jobs_are_left_alone(self, db):
        completed = create_ingest_job(db)
        completed.status = "completed"
        failed = create_ingest_job(db)
        failed.status = "failed"
        db.commit()

        count = reconcile_orphaned_jobs(db)

        assert count == 0
        assert get_ingest_job(db, completed.id).status == "completed"
        assert get_ingest_job(db, failed.id).status == "failed"
