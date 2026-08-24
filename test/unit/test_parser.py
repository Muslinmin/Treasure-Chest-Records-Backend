import datetime

import pytest

from app.ingest.csv_parser import parse_csv
from app.ingest.identity import compute_fingerprint

from test.conftest import FIXTURES


@pytest.fixture
def records() -> list[dict]:
    return parse_csv(FIXTURES / "sample.csv")


class TestRowExtraction:
    def test_skips_the_preamble_and_reads_every_amount_bearing_row(self, records):
        """7 data rows in the fixture; the one with no amount is dropped."""
        assert len(records) == 6

    def test_parses_transaction_dates(self, records):
        assert records[0]["transaction_date"] == datetime.date(2026, 5, 20)
        assert records[4]["transaction_date"] == datetime.date(2026, 5, 18)

    def test_debits_are_negative_cents(self, records):
        assert records[0]["amount_cents"] == -1320
        assert records[1]["amount_cents"] == -340
        assert records[2]["amount_cents"] == -450

    def test_credits_are_positive_cents(self, records):
        assert records[4]["amount_cents"] == 45000

    def test_amount_columns_are_not_carried_through(self, records):
        for record in records:
            assert "Debit Amount" not in record
            assert "Credit Amount" not in record

    def test_settled_status_is_a_boolean(self, records):
        assert records[0]["is_settled"] is True
        assert records[5]["is_settled"] is False

    def test_vendor_name_comes_from_ref1(self, records):
        assert records[0]["vendor_name"] == "SUBWAY @ TEST MALL     SGP 19MAY"

    def test_category_starts_unset(self, records):
        assert records[0]["category"] is None
        assert records[0]["is_category_manual"] is False

    def test_rows_with_no_amount_are_skipped_not_fatal(self, records):
        assert not any("NO AMOUNT" in r["description"] for r in records)

    def test_raises_when_no_rows_survive(self):
        with pytest.raises(Exception, match="Records are empty"):
            parse_csv(FIXTURES / "empty.csv")

    def test_raises_a_clean_error_when_no_header_row_is_found(self):
        with pytest.raises(Exception, match="Headers are empty"):
            parse_csv(FIXTURES / "no_header.csv")


class TestCardMasking:
    def test_card_numbers_are_masked_in_the_description(self, records):
        assert records[0]["description"] == (
            "SUBWAY @ TEST MALL SGP 19MAY XXXX-XXXX-XXXX-XXXX 000000000000001"
        )

    def test_no_raw_card_digits_reach_any_field(self, records):
        for record in records:
            for value in record.values():
                assert "4111-1111-1111-1111" not in str(value)

    def test_ref2_and_ref3_are_discarded(self, records):
        for record in records:
            assert "Transaction Ref2" not in record
            assert "Transaction Ref3" not in record


class TestFingerprinting:
    def test_every_row_carries_a_fingerprint(self, records):
        for record in records:
            assert len(record["fingerprint"]) == 64

    def test_fingerprint_is_computed_over_the_masked_description(self, records):
        expected = compute_fingerprint(
            records[0]["transaction_date"],
            records[0]["amount_cents"],
            records[0]["description"],
            records[0]["transaction_code"],
        )
        assert records[0]["fingerprint"] == expected
        assert "XXXX-XXXX-XXXX-XXXX" in records[0]["description"]

    def test_reparsing_the_same_file_yields_identical_fingerprints(self, records):
        assert [r["fingerprint"] for r in parse_csv(FIXTURES / "sample.csv")] == [
            r["fingerprint"] for r in records
        ]

    def test_two_identical_rows_share_a_fingerprint(self, records):
        """The two coffees are indistinguishable in the export — by design."""
        assert records[2]["fingerprint"] == records[3]["fingerprint"]

    def test_distinct_transactions_have_distinct_fingerprints(self, records):
        distinct = {r["fingerprint"] for r in records}
        assert len(distinct) == 5  # 6 rows, one legitimate duplicate pair

    def test_settling_a_pending_row_does_not_change_its_fingerprint(self, records):
        """The likeliest false negative: a pending row that settles later."""
        settled = parse_csv(FIXTURES / "sample_settled.csv")
        pending_row = records[5]
        settled_row = settled[5]

        assert pending_row["is_settled"] is False
        assert settled_row["is_settled"] is True
        assert pending_row["fingerprint"] == settled_row["fingerprint"]


class TestSourceFileRemoved:
    def test_source_file_is_no_longer_emitted(self, records):
        for record in records:
            assert "source_file" not in record

    def test_the_same_rows_under_a_different_filename_fingerprint_identically(self, records):
        """Filenames stop mattering entirely."""
        renamed = parse_csv(FIXTURES / "sample_narrow.csv")
        assert renamed[0]["fingerprint"] == records[2]["fingerprint"]
