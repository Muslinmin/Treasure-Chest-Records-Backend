"""§10.4 — one test per row of the rules table, plus the deferral cases."""

from app.categorise.rules import decide


class TestRulesTable:
    def test_int_is_always_interest(self):
        assert decide("INT", 500, "", "") == "Interest"
        assert decide("INT", -500, "", "") == "Interest"

    def test_itr_is_always_transfer_out(self):
        assert decide("ITR", -1000, "TOP-UP TO PAYLAH! :", None) == "Transfer Out"

    def test_idt_is_always_unknown(self):
        assert decide("IDT", -500, "REVDDT26050615102062676", None) == "Unknown"
        assert decide("IDT", 500, "REVDDT26050615102062676", None) == "Unknown"

    def test_credit_ict_from_a_person_is_transfer_in(self):
        assert decide("ICT", 5000, "PayNow Transfer 7653398", "From: Jane Tan") == "Transfer In"

    def test_credit_ict_from_an_entity_is_income(self):
        assert decide("ICT", 500000, "PayNow Transfer 1122334", "From: Acme Pte Ltd") == "Income"

    def test_credit_with_no_key_is_unknown(self):
        """No transaction-code branch produces a key, so it can't be resolved
        by the merchant-key pipeline either — Unknown rather than a guess."""
        assert decide("XYZ", 1000, None, None) == "Unknown"


class TestDeferredToKeyPipeline:
    def test_credit_umc_s_refund_defers_to_the_merchant_key_pipeline(self):
        """A card refund — same merchant key as the original debit, so it
        should converge on that merchant's already-cached category rather
        than being special-cased here."""
        assert decide("UMC-S", 1320, "DAISO JAPAN - SPC SI SGP 17MAY", None) is None

    def test_credit_pos_refund_defers_to_the_merchant_key_pipeline(self):
        assert decide("POS", 2000, "20594505,SIMPLYGO PTE. LTD.", None) is None

    def test_ordinary_debit_rows_are_not_touched_by_rules(self):
        assert decide("UMC-S", -1320, "DAISO JAPAN - SPC SI SGP 17MAY", None) is None
        assert decide("POS", -2000, "20594505,SIMPLYGO PTE. LTD.", None) is None

    def test_debit_ict_defers_rather_than_resolving(self):
        """Outgoing PayNow transfers aren't in the rules table at all — they
        fall through to the merchant-key pipeline, which resolves them to
        Unknown via the no-key path (ICT never emits a key)."""
        assert decide("ICT", -5000, "PayNow Transfer 7653398", "To: Jane Tan") is None


class TestIctCounterpartyClassification:
    def test_missing_ref2_defaults_to_person(self):
        assert decide("ICT", 5000, "PayNow Transfer 7653398", None) == "Transfer In"

    def test_entity_markers_are_case_insensitive(self):
        assert decide("ICT", 5000, "PayNow Transfer 1122334", "from: acme pte ltd") == "Income"

    def test_a_plain_personal_name_is_not_misclassified_as_an_entity(self):
        assert decide("ICT", 5000, "PayNow Transfer 9988776", "From: John Lim") == "Transfer In"
