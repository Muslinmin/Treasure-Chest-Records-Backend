"""§10.8 — the Router singleton, and the lazy-validation contract from §10.13.

No network calls: constructing a litellm Router only parses model_list, it
never contacts a provider.
"""

import pytest
from litellm import Router

import app.llm.router as router_module
from app.llm.router import get_llm


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    """Every test starts with no cached Router, regardless of import order."""
    monkeypatch.setattr(router_module, "_router", None)


class TestLazyValidation:
    def test_raises_when_llm_model_is_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL", raising=False)
        with pytest.raises(RuntimeError, match="LLM_MODEL"):
            get_llm()

    def test_succeeds_once_llm_model_is_set(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o-mini")
        assert isinstance(get_llm(), Router)


class TestSingleton:
    def test_repeated_calls_return_the_same_instance(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o-mini")
        first = get_llm()
        second = get_llm()
        assert first is second

    def test_unsetting_the_env_var_after_first_build_does_not_matter(self, monkeypatch):
        """Confirms the singleton doesn't re-validate on every call — only
        the first call ever touches os.environ."""
        monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o-mini")
        first = get_llm()

        monkeypatch.delenv("LLM_MODEL", raising=False)
        second = get_llm()

        assert first is second
