# Treasure Chest Records — Architecture & Progress

**Last updated:** 2026-08-05

**Status:** v1.1 (ingestion, dedupe, summaries) is shipped and running in Docker against the real
export (669 rows). v1.2 (category assignment) is fully implemented and tested (171 tests) but not
yet deployed — see Known Gaps.

A single-user, single-tenant backend for personal transaction records. The user drops bank-exported
CSVs into a watched folder, triggers a manual sync, and the service parses, stores (encrypted at
rest), categorises, and pre-aggregates the data for a remote frontend to read.

---

## Tech Stack

| Layer | Choice | Version |
|---|---|---|
| HTTP framework | FastAPI | 0.136.1 |
| ASGI server | Uvicorn (standard) | 0.47.0 |
| ORM | SQLAlchemy (2.x style) | 2.0.49 |
| Database | SQLite + SQLCipher (`sqlite+pysqlcipher://`) | `sqlcipher3-binary` |
| Validation / DTOs | Pydantic | 2.13.4 |
| Config | python-dotenv | 1.2.2 |
| Migrations | Alembic (dependency present, **not wired**) | 1.18.4 |
| Fuzzy matching | rapidfuzz | 3.14.5 |
| LLM | litellm | 1.95.0 |
| Parsing | Python stdlib `csv` (no pandas) | — |
| Packaging | Docker + Docker Compose, `python:3.12-slim` | — |
| Dev | pytest 9.0.3, ruff 0.15.13 | — |

---

## Repository Layout

```
Treasure-Chest-Records-Backend/
├── app/
│   ├── main.py                     FastAPI instance, logging, router registration
│   ├── api/
│   │   ├── auth/api_key.py         Bearer-token check (hmac.compare_digest)
│   │   └── routers/
│   │       ├── ingest.py           POST /ingest
│   │       ├── transactions.py     GET  /transactions
│   │       ├── summary.py          GET  /summary, GET /summary/monthly
│   │       └── categories.py       POST /categorise, POST/DELETE/GET /categories
│   ├── db/
│   │   ├── models.py               Transaction, Summary, Category, MerchantCategory
│   │   ├── queries.py              inserts, reads, category/merchant-cache CRUD
│   │   └── session.py              engine, SQLCipher URL, WAL + FK pragmas, get_db()
│   ├── ingest/
│   │   ├── csv_parser.py           Bank CSV → list[dict]
│   │   ├── identity.py             mask_card / normalise / compute_fingerprint
│   │   ├── pipeline.py             ingest_inbox() / process_file() / ingest_and_categorise()
│   │   └── handler.py              legacy watchdog adapter — retired, unused
│   ├── categorise/
│   │   ├── normalise.py            transaction_code + Ref1 → merchant key (pure)
│   │   ├── cluster.py              prefix clustering over the merchant-key corpus (pure)
│   │   ├── rules.py                deterministic category decisions (pure)
│   │   └── service.py              orchestrates layers 0-4, writes back, recomputes summaries
│   ├── llm/
│   │   ├── router.py               litellm Router singleton, lazy LLM_MODEL validation
│   │   └── categoriser.py          LLM-backed Categoriser (JSON-schema constrained)
│   └── summary/aggregator.py       recompute_summary() — delete+upsert per period
├── test/
│   ├── conftest.py                 in-memory `db` + tmp `boxes` fixtures, FIXTURES path
│   ├── fixtures/*.csv              synthetic, card-masked
│   ├── unit/                       pure-function tests (identity, normalise, rules, cluster, LLM)
│   └── integration/                DB-backed tests (pipeline, aggregator, categories, categorise)
├── data/                           HOST STATE — bind-mounted, git-ignored
│   └── inbox/  outbox/  failed/  db/
├── logs/                           bind-mounted to /logs in container
├── Dockerfile  docker-compose.yaml  requirements.txt  .env
```

`app/ingest/handler.py` is dead code (watchdog-based, superseded by manual sync); it imports
`watchdog`, which isn't in `requirements.txt`, so it never runs in the container.

---

## Data Model

```
transaction_records
  id, transaction_date, amount_cents (signed, debits negative), description (card-masked),
  transaction_code, vendor_name, category (nullable), is_settled, is_category_manual,
  fingerprint (indexed, NOT unique — row identity for dedupe, see below)

Summary
  id, period ("YYYY-MM"), category, total_cents, tx_count, updated_at
  UNIQUE(period, category)

categories
  id, name (unique, natural key), is_system, is_active, created_at
  Seeded: 5 system rows (Unknown, Transfer In, Transfer Out, Interest, Income) + 13 starting
  categories (Groceries, Dining & Takeout, Transport, Shopping, Subscriptions & Digital Services,
  Travel, Health & Wellness, Bills & Utilities, Housing, Personal Care, Education, Gifts &
  Donations, Fees & Charges)

merchant_categories
  id, merchant_key (unique), category (FK → categories.name, ON DELETE RESTRICT ON UPDATE CASCADE),
  source ('rule' | 'llm' | 'manual'), hit_count, created_at, updated_at
```

**Row identity (dedupe):** `fingerprint = sha256(transaction_date | amount_cents | masked
description | transaction_code)`. Deliberately excludes `is_settled`, `category`,
`is_category_manual`, and `vendor_name`. Indexed but **not unique** — dedupe is count
reconciliation (DB converges to `max(db_count, file_count)` per fingerprint), not existence-based,
because two identical same-day purchases are legitimate and share a fingerprint. Card numbers are
regex-masked to `XXXX-XXXX-XXXX-XXXX` before storage and before hashing, so they never reach the
database.

**Summary aggregation:** recomputed from scratch per affected period on every ingest
(`DELETE FROM Summary WHERE period = ?` then re-insert), so a category reassignment can't leave an
orphaned `(period, category)` row behind.

---

## What's Done

**v1.0 / v1.1 — ingestion, dedupe, summaries.** Deployed in Docker, real export re-ingested (669
rows; re-ingesting the same or a differently-named file inserts 0). `POST /ingest` parses,
dedupes by fingerprint, aggregates, and moves files to outbox/failed. `GET /transactions` and
`GET /summary[/monthly]` serve off the pre-aggregated `Summary` table. Bearer-token auth is a
router-level dependency on every router. DB is SQLCipher-encrypted at rest.

**v1.2 — category assignment.** Five-layer resolution per uncategorised row: deterministic rules
(`app/categorise/rules.py`) → exact merchant-cache match → prefix-cluster match
(`app/categorise/cluster.py`) → fuzzy match (rapidfuzz, threshold 85) → LLM fallback, batched ~40
keys at a time, JSON-schema-constrained to the active category taxonomy, with a 60% confidence
floor below which an answer is stored as `Unknown` rather than guessed. No LLM fallback chain is
configured (a provider failure raises and is logged, not silently retried on a second model).
`POST /ingest` now runs categorisation automatically after ingest, in its own transaction — a
categoriser failure is caught and logged, never fails the ingest itself. `POST /categorise` runs
the same pipeline standalone (for backfills/recovery). `POST`/`DELETE`/`GET /categories` manage the
mutable taxonomy — `carved_from` on create validates parent categories but does not yet re-derive
merchants already filed under them (see Gaps). `PRAGMA foreign_keys=ON` is enforced on every
connection (both `session.py` and the test engine).

**Tests:** 171 passing (`pytest test -q`) — pure-function unit tests for identity/normalise/
rules/cluster/LLM-router/categoriser, and DB-backed integration tests for ingest, aggregation,
categories, merchant cache, and the full categorise pipeline (against a stub/dict-backed fake
categoriser — no network, no spend).

---

## API Surface

| Endpoint | Notes |
|---|---|
| `POST /ingest` | Ingests inbox CSVs, then auto-categorises. Returns `{"files": [...], "categorised": {...}}` |
| `GET /transactions` | Filters: date range, category, limit/offset |
| `GET /summary` | Defaults to current month |
| `GET /summary/monthly` | Trailing 12 months |
| `POST /categorise` | Standalone categorisation run / backfill |
| `POST /categories` | Add a category; `carved_from` scopes intended re-derivation (not yet wired) |
| `DELETE /categories/{name}` | Soft delete by default; `?reassign_to=` for hard delete + bulk reassignment |
| `GET /categories` | Taxonomy listing |

All routers require `Authorization: Bearer <FAST_API_KEY>`.

---

## Configuration

```
.env (git-ignored, chmod 600, never baked into the image)
 ├── FAST_API_KEY       → Bearer token, hmac.compare_digest. Missing → hard RuntimeError at boot.
 ├── PASS_PHRASE        → SQLCipher key
 ├── DATABASE_FILEPATH  → data/db/database.db
 ├── INBOX / OUTBOX / FAILED_BOX → server-side paths, never client-supplied
 ├── LLM_MODEL          → litellm model string (e.g. "openai/gpt-4o-mini"). Currently UNSET —
 │                         validated lazily on first use, not at import, so its absence doesn't
 │                         stop the app booting. Provider API keys follow litellm's own env var
 │                         convention (OPENAI_API_KEY etc.), not a var here.
```

---

## Deployment

Docker Compose on the host (PC today, Raspberry Pi planned). `./data` (inbox/outbox/failed/db) and
`./logs` are bind-mounted host state; everything else rebuilds from source. Host migration = copy
`data/` + compose file + `.env`, then `docker compose up -d` — no code changes required.

Running tests (host venv, no Docker/SQLCipher needed — `test/conftest.py` uses plain SQLite):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt sqlalchemy==2.0.49
.venv/bin/python -m pytest test -q          # 171 passed
```

---

## Known Gaps

- **No Alembic baseline.** Schema is created via `Base.metadata.create_all()` at import — fine for
  new tables (how `categories`/`merchant_categories` were added), but the next change to an
  *existing* table's columns needs real migrations, not a rebuild-and-re-ingest.
- **`LLM_MODEL` is unset.** Categorisation's LLM layer will fail loudly (logged, ingest unaffected)
  until it's configured.
- **`POST /ingest` response shape changed** — from a bare list to
  `{"files": [...], "categorised": {...}}`. Breaking change; needs frontend coordination before
  deploy.
- **v1.2 has never run against the real (private) export or a real LLM provider** — only against
  the committed synthetic fixture and stub/fake categorisers.
- **`carved_from` on `POST /categories` doesn't re-derive existing rows.** It validates the parent
  categories exist and creates the new category, but merchants already filed under the parents are
  not retroactively reassigned.
- **LLM call sends only the merchant key text**, not also transaction_code/amount sign — a
  simplification, since by the time a key reaches the LLM layer, rules.py has already resolved
  everything where that context would matter.
- **PayLah top-ups (`ITR`, ~34% of the export) are categorised as `Transfer Out`**, not reconciled
  against actual spend — that needs a second, OCR-based ingestion pipeline and is out of scope.
- **`main.py` hardcodes `/logs/app.log`** — running uvicorn outside the container fails unless
  `/logs` exists.
- **No CI wiring.** Tests run locally via the host venv above; `requirements.txt` (prod deps) is
  only installed inside the Docker image.
