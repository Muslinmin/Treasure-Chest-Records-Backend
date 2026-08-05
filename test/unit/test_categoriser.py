"""app.llm.categoriser.Categoriser — the concrete LLM-backed implementation.

No network: get_llm() is monkeypatched to a fake Router whose completion()
returns a canned structured-output payload, so these exercise the response
parsing and the §10.13 confidence floor without ever calling out.
"""

import json

import app.llm.categoriser as categoriser_module
from app.llm.categoriser import CONFIDENCE_FLOOR, Categoriser

TAXONOMY = ["Transport", "Dining & Takeout", "Unknown"]


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeRouter:
    def __init__(self, answers: list[dict]):
        self._payload = json.dumps({"answers": answers})
        self.calls: list[dict] = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._payload)


def _install_fake_router(monkeypatch, answers: list[dict]) -> _FakeRouter:
    fake = _FakeRouter(answers)
    monkeypatch.setattr(categoriser_module, "get_llm", lambda: fake)
    return fake


class TestHappyPath:
    def test_returns_a_high_confidence_answer_as_is(self, monkeypatch):
        _install_fake_router(
            monkeypatch, [{"key": "grab*", "category": "Transport", "confidence": 0.95}]
        )
        result = Categoriser().categorise(["grab*"], TAXONOMY)
        assert result == {"grab*": "Transport"}

    def test_sends_the_taxonomy_constrained_schema(self, monkeypatch):
        fake = _install_fake_router(
            monkeypatch, [{"key": "grab*", "category": "Transport", "confidence": 0.9}]
        )
        Categoriser().categorise(["grab*"], TAXONOMY)

        schema = fake.calls[0]["response_format"]["json_schema"]["schema"]
        category_enum = schema["properties"]["answers"]["items"]["properties"]["category"]["enum"]
        assert category_enum == TAXONOMY


class TestConfidenceFloor:
    def test_below_floor_is_stored_as_unknown_not_the_guessed_category(self, monkeypatch):
        _install_fake_router(
            monkeypatch,
            [{"key": "mysterious inc", "category": "Dining & Takeout", "confidence": 0.4}],
        )
        result = Categoriser().categorise(["mysterious inc"], TAXONOMY)
        assert result == {"mysterious inc": "Unknown"}

    def test_exactly_at_the_floor_is_accepted(self, monkeypatch):
        _install_fake_router(
            monkeypatch,
            [{"key": "grab*", "category": "Transport", "confidence": CONFIDENCE_FLOOR}],
        )
        result = Categoriser().categorise(["grab*"], TAXONOMY)
        assert result == {"grab*": "Transport"}


class TestMalformedOrOffListAnswers:
    def test_a_category_outside_the_taxonomy_is_stored_as_unknown(self, monkeypatch):
        """Defence in depth — the schema enum should prevent this, but a
        provider that ignores structured output shouldn't crash the batch."""
        _install_fake_router(
            monkeypatch, [{"key": "grab*", "category": "Made Up Category", "confidence": 0.99}]
        )
        result = Categoriser().categorise(["grab*"], TAXONOMY)
        assert result == {"grab*": "Unknown"}

    def test_an_answer_for_a_key_that_was_never_asked_about_is_ignored(self, monkeypatch):
        _install_fake_router(
            monkeypatch, [{"key": "not in the batch", "category": "Transport", "confidence": 0.9}]
        )
        result = Categoriser().categorise(["grab*"], TAXONOMY)
        assert result == {}

    def test_a_duplicate_answer_for_the_same_key_keeps_the_first(self, monkeypatch):
        _install_fake_router(
            monkeypatch,
            [
                {"key": "grab*", "category": "Transport", "confidence": 0.9},
                {"key": "grab*", "category": "Dining & Takeout", "confidence": 0.9},
            ],
        )
        result = Categoriser().categorise(["grab*"], TAXONOMY)
        assert result == {"grab*": "Transport"}


class TestEmptyBatch:
    def test_an_empty_key_list_never_calls_the_router(self, monkeypatch):
        fake = _install_fake_router(monkeypatch, [])
        result = Categoriser().categorise([], TAXONOMY)
        assert result == {}
        assert fake.calls == []
