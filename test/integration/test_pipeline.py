"""End-to-end ingest: CSV on disk → rows + summary → file moved.

The acceptance test for v1.1 lives here: a second ingest of the same file must
insert 0.
"""

import shutil

import pytest
from sqlalchemy import func, select

from app.db.models import Summary, Transaction
from app.ingest.pipeline import ingest_inbox, process_file

from test.conftest import FIXTURES


@pytest.fixture
def drop(boxes):
    """Copy a fixture into the inbox under an arbitrary name."""

    def _drop(fixture_name: str, as_name: str | None = None):
        target = boxes["inbox"] / (as_name or fixture_name)
        shutil.copy(FIXTURES / fixture_name, target)
        return target

    return _drop


def run(db, boxes):
    return ingest_inbox(db, boxes["inbox"], boxes["outbox"], boxes["failed"])


def row_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction))


class TestHappyPath:
    def test_ingests_every_amount_bearing_row(self, db, boxes, drop):
        drop("sample.csv")
        report = run(db, boxes)

        assert report == [{"file": "sample.csv", "status": "ok", "inserted": 6, "skipped": 0}]
        assert row_count(db) == 6

    def test_moves_the_file_to_the_outbox(self, db, boxes, drop):
        drop("sample.csv")
        run(db, boxes)

        assert (boxes["outbox"] / "sample.csv").exists()
        assert not (boxes["inbox"] / "sample.csv").exists()

    def test_recomputes_the_summary_for_the_affected_period(self, db, boxes, drop):
        drop("sample.csv")
        run(db, boxes)

        rows = db.scalars(select(Summary)).all()
        assert {r.period for r in rows} == {"2026-05"}
        assert sum(r.tx_count for r in rows) == 6
        # -1320 -340 -450 -450 +45000 -999
        assert sum(r.total_cents for r in rows) == 41441

    def test_ignores_non_csv_files(self, db, boxes):
        (boxes["inbox"] / "notes.txt").write_text("not a csv")
        assert run(db, boxes) == []
        assert row_count(db) == 0


class TestIdempotency:
    def test_a_second_ingest_of_the_same_file_inserts_zero(self, db, boxes, drop):
        """The v1.1 acceptance test."""
        drop("sample.csv")
        run(db, boxes)

        drop("sample.csv")
        second = run(db, boxes)

        assert second == [{"file": "sample.csv", "status": "ok", "inserted": 0, "skipped": 6}]
        assert row_count(db) == 6

    def test_the_filename_does_not_matter(self, db, boxes, drop):
        """The v1.0 guard keyed on source_file; this is what it could not catch."""
        drop("sample.csv", "march.csv")
        run(db, boxes)

        drop("sample.csv", "totally-different-name.csv")
        second = run(db, boxes)

        assert second[0]["inserted"] == 0
        assert row_count(db) == 6

    def test_re_ingest_does_not_double_the_summary(self, db, boxes, drop):
        drop("sample.csv")
        run(db, boxes)
        drop("sample.csv")
        run(db, boxes)

        rows = db.scalars(select(Summary)).all()
        assert sum(r.tx_count for r in rows) == 6
        assert sum(r.total_cents for r in rows) == 41441

    def test_a_settled_re_export_inserts_nothing(self, db, boxes, drop):
        """sample_settled.csv is sample.csv with the pending row now settled."""
        drop("sample.csv")
        run(db, boxes)

        drop("sample_settled.csv")
        second = run(db, boxes)

        assert second[0]["inserted"] == 0
        assert row_count(db) == 6

    def test_a_narrow_re_export_erases_nothing(self, db, boxes, drop):
        drop("sample.csv")
        run(db, boxes)

        drop("sample_narrow.csv")
        second = run(db, boxes)

        assert second[0] == {
            "file": "sample_narrow.csv",
            "status": "ok",
            "inserted": 0,
            "skipped": 1,
        }
        assert row_count(db) == 6


class TestFailureHandling:
    def test_an_unparseable_file_is_moved_to_failed(self, db, boxes, drop):
        drop("empty.csv")
        report = run(db, boxes)

        assert report[0]["status"] == "failed"
        assert "Records are empty" in report[0]["error"]
        assert (boxes["failed"] / "empty.csv").exists()
        assert row_count(db) == 0

    def test_the_file_is_the_unit_of_atomicity(self, db, boxes, drop):
        """A bad file must not roll back a good one already committed."""
        drop("sample.csv", "a-good.csv")
        drop("empty.csv", "b-bad.csv")

        report = {entry["file"]: entry for entry in run(db, boxes)}

        assert report["a-good.csv"]["status"] == "ok"
        assert report["b-bad.csv"]["status"] == "failed"
        assert row_count(db) == 6
        assert (boxes["outbox"] / "a-good.csv").exists()
        assert (boxes["failed"] / "b-bad.csv").exists()


class TestReportShape:
    def test_process_file_reports_inserted_and_skipped(self, db, boxes, drop):
        path = drop("sample.csv")
        report = process_file(path, db, boxes["outbox"], boxes["failed"])

        assert set(report) == {"file", "status", "inserted", "skipped"}
        assert "rows" not in report

    def test_an_empty_inbox_reports_nothing(self, db, boxes):
        assert run(db, boxes) == []
