# Treasure Chest Records — Backend API Contract

This is the interface contract for any frontend (Flutter, web, or otherwise) talking to this
backend. It documents exact request/response shapes as implemented today — not the aspirational
design. For internal architecture, data model rationale, and known gaps, see
[`.agent/architecture_and_progress.md`](.agent/architecture_and_progress.md).

## Base URL & Auth

Every endpoint on every router requires a Bearer token:

```
Authorization: Bearer <FAST_API_KEY>
```

Missing or wrong token → `403 Forbidden`, no body. There is no separate login flow — the token is
a single shared secret provisioned out of band (the server's `.env`).

## Conventions

- **Money** is always **integer cents**, signed — negative for debits/expenses, positive for
  credits/income (e.g. `-1320` = -$13.20, `45000` = $450.00). The server never converts to
  dollars; the frontend formats.
- **Dates**: `transaction_date`, `date_from`, `date_to` are ISO-8601 `YYYY-MM-DD`. `period` (on
  summaries) is `YYYY-MM`.
- **`category`** on a transaction can be `null` — categorisation runs automatically right after
  ingest, but a row can still be mid-pipeline or fall through to `"Unknown"`.
- **The category taxonomy is dynamic, not a hardcoded enum.** Always fetch it from
  `GET /categories` rather than hardcoding a category list in the frontend — categories can be
  added, renamed, or deactivated at runtime.
- All error responses use FastAPI's default shape: `{"detail": "<message>"}`.

---

## `POST /ingest`

Ingests every CSV currently sitting in the server's watched inbox folder, then automatically runs
categorisation on whatever became uncategorised as a result — one call does both.

**Request:** no body.

**Response `200`:**

```json
{
  "files": [
    {"file": "march.csv", "status": "ok", "inserted": 47, "skipped": 0},
    {"file": "bad.csv", "status": "failed", "error": "Records are empty ! []"}
  ],
  "categorised": {
    "rows": 47,
    "resolved_by_rules": 12,
    "resolved_unknown_no_key": 0,
    "resolved_by_cache": 20,
    "resolved_by_cluster": 5,
    "resolved_by_fuzzy": 2,
    "resolved_by_llm": 8,
    "llm_batches_attempted": 1,
    "llm_batches_failed": 0
  }
}
```

- `files` is empty (`[]`) if the inbox had nothing to ingest.
- `categorised` can be `{}` if the categorisation step itself failed unexpectedly (network/provider
  outage, misconfiguration) — **this never affects `files`**: ingest results are committed and
  files moved to the outbox regardless of whether categorisation succeeds.
- Money values inside newly-inserted rows aren't returned here — call `GET /transactions` /
  `GET /summary` afterward to read the actual data.

> **Breaking change note:** this endpoint used to return a bare JSON array (just what `files` is
> now). It now returns an object. Any existing client parsing `POST /ingest`'s response as a list
> needs to be updated to read `response.files` instead.

---

## `GET /transactions`

| Query param | Type | Default | Notes |
|---|---|---|---|
| `date_from` | `YYYY-MM-DD` | none | inclusive |
| `date_to` | `YYYY-MM-DD` | none | inclusive |
| `category` | string | none | case-insensitive exact match |
| `retrieve_limit` | int | `50` | |
| `offset` | int | `0` | |

**Response `200`:** array of

```json
{
  "id": 123,
  "transaction_date": "2026-05-20",
  "amount_cents": -1320,
  "description": "SUBWAY @ TEST MALL SGP 19MAY XXXX-XXXX-XXXX-XXXX",
  "transaction_code": "UMC-S",
  "vendor_name": "SUBWAY @ TEST MALL     SGP 19MAY",
  "category": "Dining & Takeout",
  "is_settled": true,
  "is_category_manual": false
}
```

---

## `GET /summary`

| Query param | Type | Default |
|---|---|---|
| `period` | `YYYY-MM` | current month |

**Response `200`:** array of

```json
{
  "period": "2026-05",
  "category": "Dining & Takeout",
  "total_cents": -4520,
  "tx_count": 3,
  "updated_at": "2026-05-22T14:03:11"
}
```

One row per category present in that period — categories with zero transactions in the period
don't appear. Sum `total_cents` across the array yourself if you need a period total.

## `GET /summary/monthly`

No query params — always the trailing 12 months from today. Same row shape as `GET /summary`, one
row per `(period, category)` combination across the whole window.

---

## `POST /categorise`

Standalone re-run of the same categorisation pipeline `POST /ingest` triggers automatically.
Useful for backfilling rows that were never categorised (e.g. after a provider outage) without
re-ingesting anything.

**Request:** no body. **Response `200`:** the same stats object shown under `categorised` above.

---

## `GET /categories`

| Query param | Type | Default |
|---|---|---|
| `include_inactive` | bool | `false` |

**Response `200`:** array of

```json
{"name": "Dining & Takeout", "is_system": false, "is_active": true, "created_at": "2026-08-05T00:00:00"}
```

`is_system: true` categories (`Unknown`, `Transfer In`, `Transfer Out`, `Interest`, `Income`) are
reserved and can never be deleted or deactivated. Use this endpoint to populate any category
picker — don't hardcode the list.

## `POST /categories`

**Request body:**

```json
{"name": "Coffee", "carved_from": ["Dining & Takeout"]}
```

- `carved_from` is optional (defaults to `[]`).
- `[]` (or omitted) → plain new category, no other effect.
- Non-empty → **validated only.** The server checks every listed parent category exists and is
  active, but currently does **not** retroactively re-derive or reassign any transactions already
  filed under those parents. Functionally identical to `carved_from: []` today — treat it as
  future-reserved, not working, behaviour.

**Response `201`/`200`:** the created `CategoryResponse` (same shape as `GET /categories`).

**Errors:** `400 {"detail": "..."}` if the name already exists, or any `carved_from` entry isn't a
real, active category.

## `DELETE /categories/{name}`

| Query param | Type | Default |
|---|---|---|
| `reassign_to` | string | none |

- **No `reassign_to`** → soft delete: sets `is_active = false`. Existing transactions keep the
  label. Response: `{"name": "...", "status": "deactivated"}`.
- **With `reassign_to`** → hard delete: bulk-reassigns every transaction (and cached merchant
  mapping) from `name` to `reassign_to`, deletes `name` from the taxonomy, and recomputes every
  affected month's summary. Response:
  ```json
  {"name": "Old Category", "status": "deleted", "reassigned_to": "New Category", "recomputed_periods": ["2026-05", "2026-06"]}
  ```

**Errors:** `400 {"detail": "..."}` — the category doesn't exist, is a system category (never
deletable), or `reassign_to` isn't a real/active category.

---

## Known Gaps Relevant to a Frontend

See [`.agent/architecture_and_progress.md`](.agent/architecture_and_progress.md) for the full list.
The ones that actually affect client behavior:

- `POST /categories`'s `carved_from` doesn't do what its name implies yet (validation only).
- `POST /ingest`'s response shape changed to an object (see the breaking-change note above).
- LLM-driven categorisation may take a noticeable pause on the *first* `POST /ingest` after a large
  backfill (many new merchants in one batch) — subsequent calls are fast (steady-state is usually
  0–2 new merchants per import).
