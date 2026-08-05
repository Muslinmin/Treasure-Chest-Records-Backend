"""Row identity: card masking and fingerprinting.

One mechanism applied uniformly to every row, regardless of transaction type —
no branching on ``transaction_code``, no special path for card rows.

    fingerprint = sha256( normalise(transaction_date)   |
                          normalise(amount_cents)       |
                          normalise(masked description) |
                          normalise(transaction_code)   )

Deliberately excluded from the hash:

``is_settled``      A pending row that settles between two exports is the *same*
                    transaction. Hashing it would make the settled copy look new
                    — the likeliest false negative.
``category`` /      Filled in after ingest. Hashing them would change a row's
``is_category_manual``  fingerprint the moment you categorise it.
``vendor_name``     Ref1/Ref2/Ref3 are stripped substrings of ``Description``;
                    they add no information the hash does not already have.

Pure functions only — no I/O, no database, no config.
"""

import hashlib
import re

# The bank's masked PAN, e.g. "5264-7110-2059-4505". Digits, x, X and * are all
# accepted because the mask style varies by statement section.
_CARD_PATTERN = re.compile(r"[\dxX*]{4}-[\dxX*]{4}-[\dxX*]{4}-[\dxX*]{4}")

_CARD_REPLACEMENT = "XXXX-XXXX-XXXX-XXXX"

# Separator between fingerprint fields. Without it, concatenation is ambiguous:
# ("ab", "c") and ("a", "bc") would hash identically.
_FIELD_SEPARATOR = "|"

_WHITESPACE = re.compile(r"\s+")


def mask_card(text: str | None) -> str | None:
    """Rewrite any card-number pattern to a constant mask.

    No key, no config, no reversal — the digits never reach the database at all,
    on top of the SQLCipher encryption already in place.

    Idempotent by construction: ``_CARD_REPLACEMENT`` itself matches
    ``_CARD_PATTERN`` and rewrites to itself, so a row re-parsed any number of
    times yields a byte-identical description and therefore a stable
    fingerprint.
    """
    if text is None:
        return None
    return _CARD_PATTERN.sub(_CARD_REPLACEMENT, text)


def normalise(value: object) -> str:
    """Strip, collapse internal whitespace, casefold.

    ``date`` values are rendered as ISO-8601 rather than via ``str()`` so the
    hash does not depend on the caller's date formatting.
    """
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return _WHITESPACE.sub(" ", str(value).strip()).casefold()


def compute_fingerprint(
    transaction_date: object,
    amount_cents: object,
    description: str | None,
    transaction_code: str | None,
) -> str:
    """sha256 over the fields that identify a transaction across re-exports.

    ``description`` is expected to be already masked — callers must mask before
    hashing so the stored value and the hashed value never diverge.
    """
    fields = (transaction_date, amount_cents, description, transaction_code)
    payload = _FIELD_SEPARATOR.join(normalise(field) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
