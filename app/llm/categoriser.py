"""LLM-backed Categoriser — §10.8.

Implements the "merchant keys -> categories" interface
``app.categorise.service`` depends on (its ``Categoriser`` Protocol). Batching
into ~40-key groups is the caller's job (service.py); this class makes one
structured-output call per batch it's given.

The category is constrained to an enum built from the active taxonomy, so an
off-list answer is structurally impossible and "Unknown" is always reachable.
Each answer carries a confidence; anything below ``CONFIDENCE_FLOOR`` (§10.13)
is stored as "Unknown" rather than a guess. No fallback chain — a provider
failure raises out of ``categorise()`` and is caught by the caller
(service.py's batch loop), which is what makes the failure loud rather than a
silently wrong answer.

Simplification versus §10.8's fuller call shape: only the merchant key text is
sent, not also its transaction_code and amount sign. By the time a key
reaches this layer, every code-driven rule in §10.4 has already resolved
(layer 0 runs first), so what remains is overwhelmingly ordinary card/wallet
debits — sign carries little disambiguating signal for those, and the merchant
key text is what actually identifies the category.
"""

import json
import logging

import sentry_sdk

from app.llm.router import get_llm

logger = logging.getLogger(__name__)

CONFIDENCE_FLOOR = 0.60

_SYSTEM_PROMPT = (
    "You classify anonymised bank-transaction merchant keys into a fixed "
    "list of personal-budget categories. Each key is a normalised merchant "
    "name extracted from a transaction description — never a person's name. "
    "Answer using only the given category list, and give a confidence from "
    "0 to 1 for every answer."
)


def _build_response_schema(taxonomy: list[str]) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "merchant_categories",
            "schema": {
                "type": "object",
                "properties": {
                    "answers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "category": {"type": "string", "enum": taxonomy},
                                "confidence": {"type": "number"},
                            },
                            "required": ["key", "category", "confidence"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["answers"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def _build_user_prompt(keys: list[str], taxonomy: list[str]) -> str:
    categories = "\n".join(f"- {name}" for name in taxonomy)
    merchant_keys = "\n".join(f"- {key}" for key in keys)
    return (
        f"Categories:\n{categories}\n\n"
        f"Merchant keys to classify:\n{merchant_keys}"
    )


class Categoriser:
    """Concrete implementation of ``app.categorise.service.Categoriser``."""

    def categorise(self, keys: list[str], taxonomy: list[str]) -> dict[str, str]:
        if not keys:
            return {}

        router = get_llm()
        with sentry_sdk.start_span(op="ai.completion", name="litellm.completion") as span:
            span.set_data("batch_size", len(keys))
            response = router.completion(
                model="categoriser",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(keys, taxonomy)},
                ],
                response_format=_build_response_schema(taxonomy),
            )

        payload = json.loads(response.choices[0].message.content)
        results: dict[str, str] = {}
        for answer in payload.get("answers", []):
            key = answer.get("key")
            if key not in keys or key in results:
                continue
            category = answer.get("category")
            confidence = answer.get("confidence", 0)
            if category not in taxonomy or confidence < CONFIDENCE_FLOOR:
                results[key] = "Unknown"
            else:
                results[key] = category

        return results
