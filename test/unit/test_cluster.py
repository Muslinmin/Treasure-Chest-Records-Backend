"""§10.3 — prefix clustering, including the google* non-collapse guard case."""

from app.categorise.cluster import cluster_keys


class TestCollapsesReferenceShapedSuffixes:
    def test_grab_suffixes_collapse_to_one_key(self):
        keys = [
            "grab* a-98tuiknwx8qtav",
            "grab* b-12nvjkslq9wpav",
            "grab* c-77mzxplt2ekfav",
        ]
        mapping = cluster_keys(keys)
        assert len(set(mapping.values())) == 1
        assert mapping["grab* a-98tuiknwx8qtav"] == "grab*"

    def test_mcdonalds_numeric_suffixes_collapse(self):
        keys = ["mcdonalds 930060", "mcdonalds 040521", "mcdonalds 771102"]
        mapping = cluster_keys(keys)
        assert len(set(mapping.values())) == 1
        assert mapping["mcdonalds 930060"] == "mcdonalds"

    def test_spotify_alphanumeric_suffixes_collapse(self):
        keys = ["spotify p41e4e80eb", "spotify q9c3a01ff2"]
        mapping = cluster_keys(keys)
        assert mapping["spotify p41e4e80eb"] == mapping["spotify q9c3a01ff2"]


class TestDoesNotCollapseWordShapedSuffixes:
    def test_google_star_products_are_not_collapsed(self):
        """The whole point of the guard: same prefix, but the remainder is a
        real product name, not an opaque reference — must stay distinct."""
        keys = ["google*google one", "google*chatgpt", "google*tinder dating"]
        mapping = cluster_keys(keys)
        assert mapping["google*google one"] == "google*google one"
        assert mapping["google*chatgpt"] == "google*chatgpt"
        assert mapping["google*tinder dating"] == "google*tinder dating"
        assert len(set(mapping.values())) == 3

    def test_unrelated_merchants_with_no_shared_prefix_are_untouched(self):
        keys = ["daiso japan - spc", "simplygo pte. ltd.", "acme payroll pte ltd"]
        mapping = cluster_keys(keys)
        assert all(mapping[k] == k for k in keys)

    def test_a_short_coincidental_prefix_does_not_trigger_clustering(self):
        keys = ["ikea", "ice cream parlour"]
        mapping = cluster_keys(keys)
        assert mapping["ikea"] == "ikea"
        assert mapping["ice cream parlour"] == "ice cream parlour"


class TestSingletons:
    def test_a_singleton_key_maps_to_itself(self):
        assert cluster_keys(["top-up to paylah! :"]) == {"top-up to paylah! :": "top-up to paylah! :"}

    def test_an_empty_corpus_returns_an_empty_mapping(self):
        assert cluster_keys([]) == {}


class TestMixedCorpus:
    def test_clustering_and_non_clustering_groups_coexist(self):
        keys = [
            "grab* a-98tuiknwx8qtav",
            "grab* b-12nvjkslq9wpav",
            "google*google one",
            "google*chatgpt",
            "daiso japan - spc",
        ]
        mapping = cluster_keys(keys)
        assert mapping["grab* a-98tuiknwx8qtav"] == mapping["grab* b-12nvjkslq9wpav"] == "grab*"
        assert mapping["google*google one"] == "google*google one"
        assert mapping["google*chatgpt"] == "google*chatgpt"
        assert mapping["daiso japan - spc"] == "daiso japan - spc"

    def test_duplicate_keys_in_the_input_are_handled_once(self):
        keys = ["mcdonalds 930060", "mcdonalds 930060", "mcdonalds 040521"]
        mapping = cluster_keys(keys)
        assert set(mapping) == {"mcdonalds 930060", "mcdonalds 040521"}
