"""§10.2 — one test per transaction_code, plus the shared edge cases."""

from app.categorise.normalise import merchant_key


class TestPerTransactionCode:
    def test_umc_s_strips_trailing_date_then_city_and_country(self):
        assert (
            merchant_key("UMC-S", "DAISO JAPAN - SPC      SI SGP 17MAY")
            == "daiso japan - spc"
        )

    def test_itr_is_already_constant(self):
        assert merchant_key("ITR", "TOP-UP TO PAYLAH! :") == "top-up to paylah! :"

    def test_ict_emits_no_key(self):
        """Resolved by rules.py from Ref2 instead — never turned into a key."""
        assert merchant_key("ICT", "PayNow Transfer 7653398") is None

    def test_pos_strips_leading_account_number(self):
        assert merchant_key("POS", "20594505,SIMPLYGO PTE. LTD.") == "simplygo pte. ltd."

    def test_awl_strips_leading_account_number(self):
        assert merchant_key("AWL", "10023344,GRABFOOD SG") == "grabfood sg"

    def test_idt_emits_no_key(self):
        """Opaque bank-internal reference — no stable merchant name to extract."""
        assert merchant_key("IDT", "REVDDT26050615102062676") is None

    def test_ibg_passes_through_a_clean_org_name(self):
        assert merchant_key("IBG", "ACME PAYROLL PTE LTD") == "acme payroll pte ltd"

    def test_int_with_empty_ref1_emits_no_key(self):
        assert merchant_key("INT", "") is None


class TestSharedEdgeCases:
    def test_none_ref1_emits_no_key(self):
        assert merchant_key("UMC-S", None) is None

    def test_umc_s_without_a_matching_suffix_is_left_unstripped(self):
        """Grab's ref1 doesn't follow the date/city/country shape — cluster.py
        (§10.3) handles what positional stripping can't."""
        assert merchant_key("UMC-S", "GRAB* A-98TUIKNWX8QTAV") == "grab* a-98tuiknwx8qtav"

    def test_collapsing_internal_whitespace_still_matches_the_trailing_suffix(self):
        assert (
            merchant_key("UMC-S", "MCDONALDS   930060   SI SGP 02JUN")
            == "mcdonalds 930060"
        )

    def test_unknown_transaction_code_passes_through_unchanged(self):
        assert merchant_key("XYZ", "Some Org Name") == "some org name"
