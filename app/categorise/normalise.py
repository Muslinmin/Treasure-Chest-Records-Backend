"""Merchant key extraction — §10.2.

Pure functions, no I/O — same contract as ``app/ingest/identity.py``. The
bank's export format varies by ``transaction_code``, and the noise within each
format sits at fixed positions, so almost all of the work here is stripping a
known suffix or prefix rather than anything fuzzy. Fuzzy matching (cluster.py,
§10.3) exists precisely for what positional stripping cannot resolve — a
per-transaction reference with no fixed shape.

| Code       | Ref1 shape                                      | Stripped                        |
|------------|--------------------------------------------------|----------------------------------|
| UMC-S      | ``DAISO JAPAN - SPC      SI SGP 17MAY``          | trailing DDMMM, then city + CTY |
| ITR        | ``TOP-UP TO PAYLAH! :``                          | nothing — already constant      |
| ICT        | ``PayNow Transfer 7653398`` (+ Ref2 ``To: name``)| resolved by rules; no key       |
| POS / AWL  | ``20594505,SIMPLYGO PTE. LTD.``                  | leading ``<acct>,``             |
| IDT        | ``REVDDT26050615102062676``                      | no key possible — opaque        |
| IBG / INT  | clean org name / empty                           | —                                |
"""

import re

from app.ingest.identity import normalise as _normalise

# Trailing "DDMMM", e.g. " 17MAY" — the export's transaction date suffix.
_TRAILING_DATE = re.compile(r"\s+\d{2}[A-Z]{3}$")

# Trailing "<city> <CTY>", e.g. " SI SGP" — a 2-4 letter city code followed by
# a 3-letter country code, both uppercase.
_TRAILING_CITY_COUNTRY = re.compile(r"\s+[A-Z]{2,4}\s+[A-Z]{3}$")

# Leading "<account number>," on POS/AWL rows, e.g. "20594505,".
_LEADING_ACCOUNT = re.compile(r"^\d+,\s*")

# Codes whose Ref1 carries no usable merchant signal: ICT is resolved by
# rules.py from Ref2 (a counterparty name, deliberately never turned into a
# key — see §10.4's privacy note); IDT references are opaque bank-internal
# tokens with no stable merchant name to extract.
_NO_KEY_CODES = frozenset({"ICT", "IDT"})


def merchant_key(transaction_code: str | None, ref1: str | None) -> str | None:
    """Extract a normalised merchant key from a row's code and Ref1.

    Returns ``None`` when no usable key exists — ``transaction_code`` is one
    of ``_NO_KEY_CODES``, ``ref1`` is missing, or stripping leaves nothing.
    """
    if transaction_code in _NO_KEY_CODES:
        return None
    if not ref1:
        return None

    raw = ref1
    if transaction_code == "UMC-S":
        raw = _TRAILING_DATE.sub("", raw)
        raw = _TRAILING_CITY_COUNTRY.sub("", raw)
    elif transaction_code in ("POS", "AWL"):
        raw = _LEADING_ACCOUNT.sub("", raw)
    # ITR, IBG, INT and anything else: no positional noise to strip.

    key = _normalise(raw)
    return key or None
