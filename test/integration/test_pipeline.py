"""End-to-end ingest: uploaded CSV bytes → rows + summary → file archived.

The acceptance test for v1.1 lives here: a second ingest of the same file must
insert 0. Rewritten for v1.4 — the inbox/outbox/failed folder triplet is gone;
callers now hand ingest_uploads (filename, bytes) tuples directly, and every
uploaded file lands in one archive dir named
`{timestamp}_{stem}_{pass|failed}{ext}` (see the Planned: v1.4 design note in
.agent/architecture_and_progress.md).
"""

import pytest
from sqlalchemy import func, select

from app.db.models import Summary, Transaction
from app.ingest.pipeline import ingest_uploads, process_upload

from test.conftest import FIXTURES


@pytest.fixture
def upload():
    """Read a fixture file's bytes under an arbitrary upload name."""

    def _upload(fixture_name: str, as_name: str | None = None) -> tuple[str, bytes]:
        content = (FIXTURES / fixture_name).read_bytes()
        return (as_name or fixture_name, content)

    return _upload


def run(db, archive, uploads):
    return ingest_uploads(db, uploads, archive)


def row_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction))


def find_archived(archive, original_stem: str, outcome: str):
    return [p for p in archive.iterdir() if p.stem.endswith(f"_{original_stem}_{outcome}")]


class TestHappyPath:
    def test_ingests_every_amount_bearing_row(self, db, archive, upload):
        report = run(db, archive, [upload("sample.csv")])

        assert report == [{"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}]
        assert row_count(db) == 6

    def test_archives_the_file_with_a_pass_suffix(self, db, archive, upload):
        run(db, archive, [upload("sample.csv")])

        matches = find_archived(archive, "sample", "pass")
        assert len(matches) == 1
        assert matches[0].suffix == ".csv"

    def test_recomputes_the_summary_for_the_affected_period(self, db, archive, upload):
        run(db, archive, [upload("sample.csv")])

        rows = db.scalars(select(Summary)).all()
        assert {r.period for r in rows} == {"2026-05"}
        assert sum(r.tx_count for r in rows) == 6
        # -1320 -340 -450 -450 +45000 -999
        assert sum(r.total_cents for r in rows) == 41441

    def test_rejects_non_csv_files(self, db, archive):
        """v1.4 behavior change: a folder scan silently skipped non-.csv
        files; a direct-upload caller needs to know why theirs didn't go
        through, so it's reported as a failed entry instead."""
        report = run(db, archive, [("notes.txt", b"not a csv")])

        assert report == [
            {"file": "notes.txt", "status": "failed", "error": "Not a CSV file: notes.txt"}
        ]
        assert row_count(db) == 0
        assert len(find_archived(archive, "notes", "failed")) == 1


class TestIdempotency:
    def test_a_second_ingest_of_the_same_file_inserts_zero(self, db, archive, upload):
        """The v1.1 acceptance test."""
        run(db, archive, [upload("sample.csv")])

        second = run(db, archive, [upload("sample.csv")])

        assert second == [{"file": "sample.csv", "status": "ok", "inserted": 0, "skipped": 6}]
        assert row_count(db) == 6

    def test_the_filename_does_not_matter(self, db, archive, upload):
        """The v1.0 guard keyed on source_file; this is what it could not catch."""
        run(db, archive, [upload("sample.csv", "march.csv")])

        second = run(db, archive, [upload("sample.csv", "totally-different-name.csv")])

        assert second[0]["inserted"] == 0
        assert row_count(db) == 6

    def test_re_ingest_does_not_double_the_summary(self, db, archive, upload):
        run(db, archive, [upload("sample.csv")])
        run(db, archive, [upload("sample.csv")])

        rows = db.scalars(select(Summary)).all()
        assert sum(r.tx_count for r in rows) == 6
        assert sum(r.total_cents for r in rows) == 41441

    def test_a_settled_re_export_inserts_nothing(self, db, archive, upload):
        """sample_settled.csv is sample.csv with the pending row now settled."""
        run(db, archive, [upload("sample.csv")])

        second = run(db, archive, [upload("sample_settled.csv")])

        assert second[0]["inserted"] == 0
        assert row_count(db) == 6

    def test_a_narrow_re_export_erases_nothing(self, db, archive, upload):
        run(db, archive, [upload("sample.csv")])

        second = run(db, archive, [upload("sample_narrow.csv")])

        assert second[0] == {
            "file": "sample_narrow.csv",
            "status": "ok",
            "inserted": 0,
            "skipped": 1,
        }
        assert row_count(db) == 6


class TestFailureHandling:
    def test_an_unparseable_file_is_archived_as_failed(self, db, archive, upload):
        report = run(db, archive, [upload("empty.csv")])

        assert report[0]["status"] == "failed"
        assert "Records are empty" in report[0]["error"]
        assert len(find_archived(archive, "empty", "failed")) == 1
        assert row_count(db) == 0

    def test_a_headerless_file_gets_a_clean_error_not_a_leaked_exception(self, db, archive, upload):
        report = run(db, archive, [upload("no_header.csv")])

        assert report[0]["status"] == "failed"
        assert "Headers are empty" in report[0]["error"]
        assert "UnboundLocalError" not in report[0]["error"]
        assert len(find_archived(archive, "no_header", "failed")) == 1
        assert row_count(db) == 0

    def test_the_file_is_the_unit_of_atomicity(self, db, archive, upload):
        """A bad file must not roll back a good one already committed."""
        uploads = [upload("sample.csv", "a-good.csv"), upload("empty.csv", "b-bad.csv")]

        report = {entry["file"]: entry for entry in run(db, archive, uploads)}

        assert report["a-good.csv"]["status"] == "ok"
        assert report["b-bad.csv"]["status"] == "failed"
        assert row_count(db) == 6
        assert len(find_archived(archive, "a-good", "pass")) == 1
        assert len(find_archived(archive, "b-bad", "failed")) == 1


class TestReportShape:
    def test_process_upload_reports_inserted_and_skipped(self, db, archive, upload):
        file_name, content = upload("sample.csv")
        report = process_upload(file_name, content, db, archive)

        assert set(report) == {"file", "status", "inserted", "skipped"}
        assert "rows" not in report

    def test_no_uploads_reports_nothing(self, db, archive):
        assert run(db, archive, []) == []


class TestArchivingDisabled:
    """archive_path=None (e.g. a cloud deploy with no persistent volume for
    raw uploads) — ingest must still fully work, it just writes nothing to
    disk beyond a temp file that's cleaned up automatically."""

    def test_ingestion_succeeds_with_no_archive_path(self, db, upload):
        report = run(db, None, [upload("sample.csv")])

        assert report == [{"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}]
        assert row_count(db) == 6

    def test_a_failed_parse_still_reports_correctly(self, db, upload):
        report = run(db, None, [upload("empty.csv")])

        assert report[0]["status"] == "failed"
        assert "Records are empty" in report[0]["error"]
        assert row_count(db) == 0

    def test_rejects_non_csv_files_without_touching_disk(self, db):
        report = run(db, None, [("notes.txt", b"not a csv")])

        assert report == [
            {"file": "notes.txt", "status": "failed", "error": "Not a CSV file: notes.txt"}
        ]
        assert row_count(db) == 0
