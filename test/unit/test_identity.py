from datetime import date

import pytest

from app.ingest.identity import compute_fingerprint, mask_card, normalise


class TestMaskCard:
    def test_masks_a_card_number_embedded_in_a_description(self):
        masked = mask_card("SUBWAY @ TEST MALL SGP 19MAY 4111-1111-1111-1111 000000000000001")
        assert masked == "SUBWAY @ TEST MALL SGP 19MAY XXXX-XXXX-XXXX-XXXX 000000000000001"

    def test_is_idempotent(self):
        """Re-parsing a stored row must not change it, or the fingerprint drifts."""
        once = mask_card("PAID WITH 4111-1111-1111-1111")
        twice = mask_card(once)
        assert once == twice == "PAID WITH XXXX-XXXX-XXXX-XXXX"

    def test_masks_x_and_star_mask_styles(self):
        assert mask_card("xxxx-xxxx-xxxx-4505") == "XXXX-XXXX-XXXX-XXXX"
        assert mask_card("****-****-****-4505") == "XXXX-XXXX-XXXX-XXXX"

    def test_masks_every_occurrence_in_one_string(self):
        assert mask_card("4111-1111-1111-1111 and 5264-7110-2059-4505") == (
            "XXXX-XXXX-XXXX-XXXX and XXXX-XXXX-XXXX-XXXX"
        )

    def test_leaves_non_card_text_untouched(self):
        assert mask_card("Incoming PayNow Ref 0000001 From: JANE DOE") == (
            "Incoming PayNow Ref 0000001 From: JANE DOE"
        )

    def test_does_not_mask_the_long_reference_token(self):
        """Ref3 is what makes card descriptions unique — it must survive."""
        assert "000000000000001" in mask_card("SGP 4111-1111-1111-1111 000000000000001")

    def test_passes_none_through(self):
        assert mask_card(None) is None


class TestNormalise:
    def test_strips_and_casefolds(self):
        assert normalise("  Coffee Stall  ") == "coffee stall"

    def test_collapses_internal_whitespace(self):
        assert normalise("SUBWAY @ TEST MALL     SGP 19MAY") == "subway @ test mall sgp 19may"

    def test_renders_dates_as_iso(self):
        assert normalise(date(2026, 5, 20)) == "2026-05-20"

    def test_renders_integers(self):
        assert normalise(-1320) == "-1320"

    def test_none_becomes_empty_string(self):
        assert normalise(None) == ""


class TestComputeFingerprint:
    def test_is_a_sha256_hex_digest(self):
        fingerprint = compute_fingerprint(date(2026, 5, 20), -1320, "COFFEE", "ITR")
        assert len(fingerprint) == 64
        assert set(fingerprint) <= set("0123456789abcdef")

    def test_is_stable_across_calls(self):
        args = (date(2026, 5, 20), -1320, "COFFEE STALL 12", "ITR")
        assert compute_fingerprint(*args) == compute_fingerprint(*args)

    def test_ignores_whitespace_and_case_differences(self):
        assert compute_fingerprint(
            date(2026, 5, 20), -1320, "COFFEE   STALL 12", "ITR"
        ) == compute_fingerprint(date(2026, 5, 20), -1320, " coffee stall 12 ", "itr")

    @pytest.mark.parametrize(
        "changed",
        [
            (date(2026, 5, 21), -1320, "COFFEE", "ITR"),
            (date(2026, 5, 20), -1321, "COFFEE", "ITR"),
            (date(2026, 5, 20), -1320, "TEA", "ITR"),
            (date(2026, 5, 20), -1320, "COFFEE", "ICT"),
        ],
        ids=["date", "amount", "description", "code"],
    )
    def test_every_hashed_field_changes_the_hash(self, changed):
        baseline = compute_fingerprint(date(2026, 5, 20), -1320, "COFFEE", "ITR")
        assert compute_fingerprint(*changed) != baseline

    def test_field_boundaries_are_unambiguous(self):
        """Without a separator, ("ab","c") and ("a","bc") would collide."""
        assert compute_fingerprint(date(2026, 5, 20), -1320, "AB", "C") != compute_fingerprint(
            date(2026, 5, 20), -1320, "A", "BC"
        )

    def test_a_masked_and_unmasked_description_hash_differently(self):
        """Guards the ordering rule: mask first, then hash."""
        raw = "SGP 4111-1111-1111-1111 000000000000001"
        assert compute_fingerprint(date(2026, 5, 20), -1320, raw, "UMC-S") != compute_fingerprint(
            date(2026, 5, 20), -1320, mask_card(raw), "UMC-S"
        )
