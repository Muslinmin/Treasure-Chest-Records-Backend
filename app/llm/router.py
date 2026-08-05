"""litellm Router singleton — §10.8.

Constructed once and handed out by ``get_llm()``, mirroring ``session.py``'s
``get_db()``: a Router is genuinely stateful (per-model cooldown and failure
tracking), so building one per request would discard everything it has
learned about a provider being down and re-pay the discovery cost — a
timeout — on every call.

No fallback chain is configured, by design, for this phase (§10.13): a
provider failure raises loudly rather than silently falling back to a second
model. Revisit once a second provider is actually in play.

``LLM_MODEL`` is validated lazily, on first call, not at import time. Unlike
``FAST_API_KEY`` (a boot-time security invariant — the app must never run
unauthenticated), a missing or misconfigured model is a soft failure: §10.9
requires that a provider outage — including "never configured" — must not
change ``POST /ingest`` semantics. Raising at import would take the whole app
down before that guarantee could even apply.
"""

import os

from dotenv import load_dotenv
from litellm import Router

load_dotenv()

_MODEL_ALIAS = "categoriser"

_router: Router | None = None


def _build_router() -> Router:
    model = os.getenv("LLM_MODEL")
    if not model:
        raise RuntimeError("LLM_MODEL is not set")

    return Router(
        model_list=[
            {
                "model_name": _MODEL_ALIAS,
                "litellm_params": {"model": model},
            }
        ]
    )


def get_llm() -> Router:
    """The app-lifetime Router singleton. Raises if LLM_MODEL is unset."""
    global _router
    if _router is None:
        _router = _build_router()
    return _router
