"""Fingerprint count reconciliation — the §4.3 contract."""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.db.models import Transaction
from app.db.queries import insert_records
from app.ingest.identity import compute_fingerprint


def make_record(
    description: str = "COFFEE STALL 12",
    amount_cents: int = -450,
    transaction_date: date = date(2026, 5, 19),
    transaction_code: str = "ITR",
    **overrides,
) -> dict:
    record = {
        "transaction_date": transaction_date,
        "amount_cents": amount_cents,
        "description": description,
        "transaction_code": transaction_code,
        "vendor_name": description,
        "category": None,
        "is_settled": True,
        "is_category_manual": False,
    }
    record["fingerprint"] = compute_fingerprint(
        transaction_date, amount_cents, description, transaction_code
    )
    record.update(overrides)
    return record


def row_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction))


def count_for(db, fingerprint: str) -> int:
    return db.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.fingerprint == fingerprint
        )
    )


class TestReconciliationTable:
    """The four cases tabulated in §4.3."""

    @pytest.mark.parametrize(
        "db_count, file_count, expected_inserted, expected_skipped",
        [
            (1, 1, 0, 1),  # re-import of the same file
            (2, 2, 0, 2),  # both coffees already stored
            (0, 2, 2, 0),  # both real coffees survive
            (2, 3, 1, 2),  # third was still pending in the earlier export
        ],
        ids=["1/1", "2/2", "0/2", "2/3"],
    )
    def test_converges_to_max_of_db_and_file(
        self, db, db_count, file_count, expected_inserted, expected_skipped
    ):
        if db_count:
            insert_records(db, [make_record() for _ in range(db_count)])
            db.commit()

        result = insert_records(db, [make_record() for _ in range(file_count)])
        db.commit()

        assert result == {"inserted": expected_inserted, "skipped": expected_skipped}
        assert row_count(db) == max(db_count, file_count)


class TestIdempotency:
    def test_second_ingest_of_the_same_rows_inserts_zero(self, db):
        records = [make_record("COFFEE"), make_record("TEA"), make_record("CAKE")]

        first = insert_records(db, records)
        db.commit()
        second = insert_records(db, records)
        db.commit()

        assert first["inserted"] == 3
        assert second == {"inserted": 0, "skipped": 3}
        assert row_count(db) == 3

    def test_repeated_ingests_never_drift(self, db):
        records = [make_record("COFFEE"), make_record("COFFEE"), make_record("TEA")]
        for _ in range(5):
            insert_records(db, records)
            db.commit()
        assert row_count(db) == 3


class TestPartialOverlap:
    def test_only_the_new_rows_are_inserted(self, db):
        insert_records(db, [make_record("COFFEE"), make_record("TEA")])
        db.commit()

        result = insert_records(
            db, [make_record("COFFEE"), make_record("TEA"), make_record("CAKE")]
        )
        db.commit()

        assert result == {"inserted": 1, "skipped": 2}
        assert row_count(db) == 3


class TestDeliberateNonBehaviours:
    def test_no_within_file_dedupe(self, db):
        """Two identical rows in one file are two real transactions."""
        result = insert_records(db, [make_record(), make_record()])
        db.commit()

        assert result == {"inserted": 2, "skipped": 0}
        assert row_count(db) == 2

    def test_a_narrow_re_export_never_deletes(self, db):
        """DB holds 3, file holds 1 — nothing inserted, nothing removed."""
        insert_records(db, [make_record() for _ in range(3)])
        db.commit()

        result = insert_records(db, [make_record()])
        db.commit()

        assert result == {"inserted": 0, "skipped": 1}
        assert row_count(db) == 3

    def test_a_duplicate_file_is_not_an_error(self, db):
        """Filenames stop mattering; re-ingest is a success with inserted 0."""
        insert_records(db, [make_record()])
        db.commit()
        assert insert_records(db, [make_record()])["inserted"] == 0


class TestFieldsExcludedFromIdentity:
    def test_a_pending_row_that_settles_is_not_reinserted(self, db):
        insert_records(db, [make_record(is_settled=False)])
        db.commit()

        result = insert_records(db, [make_record(is_settled=True)])
        db.commit()

        assert result == {"inserted": 0, "skipped": 1}
        assert row_count(db) == 1

    def test_categorising_a_row_does_not_make_it_reinsertable(self, db):
        record = make_record()
        insert_records(db, [record])
        db.commit()

        stored = db.scalars(select(Transaction)).one()
        stored.category = "food"
        stored.is_category_manual = True
        db.commit()

        assert insert_records(db, [record]) == {"inserted": 0, "skipped": 1}
        assert row_count(db) == 1

    def test_vendor_name_is_not_part_of_identity(self, db):
        """Ref1 is a stripped substring of the description — it adds nothing."""
        insert_records(db, [make_record(vendor_name="COFFEE STALL 12")])
        db.commit()

        result = insert_records(db, [make_record(vendor_name="COFFEE  STALL   12")])
        db.commit()

        assert result == {"inserted": 0, "skipped": 1}
        assert row_count(db) == 1


class TestDistinctRowsCoexist:
    @pytest.mark.parametrize(
        "differing",
        [
            {"transaction_date": date(2026, 5, 20)},
            {"amount_cents": -451},
            {"description": "TEA STALL 12"},
            {"transaction_code": "ICT"},
        ],
        ids=["date", "amount", "description", "code"],
    )
    def test_a_change_in_any_hashed_field_is_a_new_row(self, db, differing):
        insert_records(db, [make_record()])
        db.commit()

        result = insert_records(db, [make_record(**differing)])
        db.commit()

        assert result == {"inserted": 1, "skipped": 0}
        assert row_count(db) == 2

    def test_unrelated_fingerprints_do_not_interfere(self, db):
        insert_records(db, [make_record("COFFEE"), make_record("COFFEE")])
        db.commit()

        result = insert_records(db, [make_record("COFFEE"), make_record("TEA")])
        db.commit()

        assert result == {"inserted": 1, "skipped": 1}
        assert count_for(db, make_record("COFFEE")["fingerprint"]) == 2
        assert count_for(db, make_record("TEA")["fingerprint"]) == 1


class TestStorage:
    def test_fingerprint_is_persisted_on_the_row(self, db):
        record = make_record()
        insert_records(db, [record])
        db.commit()

        stored = db.scalars(select(Transaction)).one()
        assert stored.fingerprint == record["fingerprint"]

    def test_the_fingerprint_column_is_not_unique(self, db):
        """A UNIQUE constraint would silently discard the second coffee."""
        insert_records(db, [make_record(), make_record()])
        db.commit()  # must not raise IntegrityError
        assert row_count(db) == 2
