"""Deterministic category decisions — §10.4.

Pure functions, no I/O. Run before any key extraction or cache lookup (§10.1
layer 0), because they are free, exact, and cover the largest single blocks of
the export — 288 of 669 real rows (43%) never reach the LLM because of this
module alone.

| Condition                              | Category                      |
|-----------------------------------------|--------------------------------|
| ``INT``                                 | ``Interest``                  |
| credit + ``ICT`` incoming from a person | ``Transfer In``               |
| credit + ``ICT`` incoming from an entity| ``Income``                    |
| credit + no key                         | ``Unknown``                   |
| ``ITR`` (PayLah top-up)                 | ``Transfer Out``              |
| ``IDT``                                 | ``Unknown`` — opaque reference|

Everything else — including a credit ``UMC-S``/``POS`` row, i.e. a card
refund — returns ``None``: unresolved by this layer, deferred to the
merchant-key pipeline (§10.1 layers 1-4), which for a refund converges on the
same merchant's already-cached category without any special-casing here.

Privacy note (§7, §10.4): ``ICT`` counterparty names never leave this module —
they are classified person-vs-entity here, by rule, and are never turned into
a key or sent to an LLM.
"""

from app.categorise.normalise import merchant_key

_ENTITY_MARKERS = (
    "PTE",
    "LTD",
    "LLC",
    "LLP",
    "INC",
    "CORP",
    "PL",
    "COMPANY",
    "ENTERPRISE",
    "ENTERPRISES",
    "HOLDINGS",
    "GROUP",
    "SERVICES",
    "PTY",
)


def _is_entity(counterparty: str) -> bool:
    words = counterparty.upper().replace(".", "").split()
    return any(word in _ENTITY_MARKERS for word in words)


def _counterparty_name(ref2: str | None) -> str | None:
    if not ref2:
        return None
    name = ref2.strip()
    for prefix in ("To:", "From:"):
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):].strip()
            break
    return name or None


def decide(
    transaction_code: str | None,
    amount_cents: int,
    ref1: str | None,
    ref2: str | None,
) -> str | None:
    """Return a deterministic category, or ``None`` if unresolved by this layer."""
    is_credit = amount_cents > 0

    if transaction_code == "INT":
        return "Interest"

    if transaction_code == "ITR":
        return "Transfer Out"

    if transaction_code == "IDT":
        return "Unknown"

    if transaction_code == "ICT" and is_credit:
        counterparty = _counterparty_name(ref2)
        if counterparty and _is_entity(counterparty):
            return "Income"
        return "Transfer In"

    if is_credit and merchant_key(transaction_code, ref1) is None:
        return "Unknown"

    return None
