# Treasure Chest Records — Architecture & Progress

**Last updated:** 2026-08-05

**Status:** v1.1 (ingestion, dedupe, summaries) is shipped and running in Docker against the real
export (669 rows). v1.2 (category assignment) and v1.3 (category hierarchy — parent/child
categories, `carved_from` re-derivation, rollup totals) are fully implemented and tested (208
tests) but not yet deployed — see Known Gaps.

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
│   │   ├── service.py              orchestrates layers 0-4, writes back, recomputes summaries
│   │   └── hierarchy.py            carve_category / rederive_category — v1.3 carved_from re-derivation
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
  id, name (unique, natural key), is_system, is_active,
  parent_id (nullable, self-referential FK -> categories.id, ON DELETE RESTRICT), created_at
  Seeded: 5 system rows (Unknown, Transfer In, Transfer Out, Interest, Income) + 13 starting
  categories (Groceries, Dining & Takeout, Transport, Shopping, Subscriptions & Digital Services,
  Travel, Health & Wellness, Bills & Utilities, Housing, Personal Care, Education, Gifts &
  Donations, Fees & Charges) — all seeded top-level (parent_id NULL)

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

**v1.3 — category hierarchy.** `categories.parent_id` (self-referential, nullable, `ON DELETE
RESTRICT`) caps the tree at two levels: a category is a leaf until the first thing is carved from
it, at which point it's permanently a stem (pure grouping label, holds zero transactions directly)
and gains a `"<Stem> (Other)"` catch-all leaf sibling that absorbs everything it held directly.
`POST /categories`'s `carved_from` is now fully wired (`app/categorise/hierarchy.py`):
`queries.create_category` does the DB-only migration (promote parent, create/reuse the catch-all,
bulk-move existing transactions/merchant-cache rows onto it), then `rederive_category` re-derives
which of the catch-all's merchants actually belong under the new leaf — every non-`manual`
candidate is batched to the same `Categoriser` interface `service.py` depends on, choosing between
exactly the new leaf and the catch-all (never the full taxonomy). There is deliberately no cheaper
deterministic pre-filter (e.g. matching the merchant name against the new category's own name): a
merchant literally named "May's Coffee" may sell pastries, not coffee, so a merchant's own name is
never treated as evidence of what a given purchase there was for — every reassignment goes through
the LLM. `manual`-sourced merchant-cache rows and `is_category_manual` transactions are never
touched by re-derivation, same protection `service.py` already gives them. Stems are excluded from
the taxonomy `service.py` offers the rules/LLM layers (`list_leaf_categories`) and from
`DELETE /categories`'s `reassign_to` target — a stem must never hold a transaction directly, or
`?rollup=true` would double-count it against its own children. `GET /summary`/`GET /summary/monthly`
gain `?rollup=true`, grouping existing leaf `Summary` rows by parent at read time (never stored) —
cheap, since `Summary` only ever has a handful of rows per period. `GET /categories` gains
`parent_name`.

**Tests:** 209 passing (`pytest test -q`) — pure-function unit tests for identity/normalise/
rules/cluster/LLM-router/categoriser, and DB-backed integration tests for ingest, aggregation,
categories, merchant cache, category hierarchy (`test/integration/test_hierarchy.py`), summary
rollup (`test/integration/test_summary_queries.py`), and the full categorise pipeline (against a
stub/dict-backed fake categoriser — no network, no spend).

---

## API Surface

| Endpoint | Notes |
|---|---|
| `POST /ingest` | Ingests inbox CSVs, then auto-categorises. Returns `{"files": [...], "categorised": {...}}` |
| `GET /transactions` | Filters: date range, category, limit/offset |
| `GET /summary` | Defaults to current month; `?rollup=true` groups by parent category |
| `GET /summary/monthly` | Trailing 12 months; `?rollup=true` as above |
| `POST /categorise` | Standalone categorisation run / backfill |
| `POST /categories` | Add a category; `carved_from` (single parent) carves it out and re-derives affected merchants |
| `DELETE /categories/{name}` | Soft delete by default; `?reassign_to=` for hard delete + bulk reassignment; rejects stems on either side |
| `GET /categories` | Taxonomy listing, with `parent_name` |

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
.venv/bin/python -m pytest test -q          # 209 passed
```

---

## Category Hierarchy (v1.3)

Motivated by wanting to break broad categories (e.g. "Dining & Takeout") into finer subcategories
(e.g. "Coffee") without re-running the LLM over the full transaction history. Note LLM cost is
already bounded by unique merchant count, not row count, because of the `merchant_categories`
cache (layers 0-3 resolve before the LLM is ever called — see What's Done above) — this design
extends that same merchant-scoped indirection to retroactive re-derivation rather than introducing
a new cost surface.

**Model — two levels, atomic stems, no recursion:**
- A category is either a **leaf** (holds transactions directly, has zero children) or a **stem**
  (a pure grouping label with ≥1 children, holds zero transactions directly). A category is a leaf
  until the first time something is carved out of it, at which point it's permanently promoted to
  a stem — it can never later become a child of something else.
- **Tree, not DAG.** Each category has at most one parent (`categories.parent_id`, self-referential
  nullable FK, `ON DELETE RESTRICT`). Multi-parent categories are out of scope — they'd turn
  rollup into a DISTINCT-and-double-count problem for no clear benefit at this scale.
- **`carved_from` may only reference a currently top-level category** (`parent_id IS NULL`).
  Carving from an existing leaf-with-a-parent is rejected — leaves are atomic, they don't get
  their own children. This caps the hierarchy at exactly 2 levels by construction, so rollup is
  always a single join, never a recursive walk.
- **Catch-all leaf.** The first carve under a given stem auto-generates a sibling leaf,
  `"<Stem> (Other)"`, that absorbs whatever remains unclassified under the stem — it is *not* the
  stem's own name reused as a leaf, since reusing the same string for both roles reads as
  double-counting once rollup sums them. Every subsequent carve under the same stem pulls matching
  merchants out of that one catch-all leaf, never out of an already-resolved sibling.

**Re-derivation — merchant-scoped, not row-scoped** (`app/categorise/hierarchy.py`):
1. `queries.create_category` does the DB-only half: validates `carved_from`, creates the new leaf,
   and — on the first carve under a given parent — creates the `"<Stem> (Other)"` catch-all and
   bulk-migrates every `transaction_records`/`merchant_categories` row currently filed under the
   parent's own name onto it. This part touches every non-`manual` *and* `manual` row alike — it's
   a mechanical rename of which bucket-name a stem's former direct holdings live under, not a
   re-categorisation decision, so it doesn't need the manual-override protection the next step does.
2. `hierarchy.rederive_category` then re-derives which of the catch-all's merchants actually belong
   under the new leaf, scanning `merchant_categories` (bounded by unique merchant count, never
   transaction count) and skipping `source = 'manual'` rows entirely — those are a real
   categorisation decision a user made, and must survive every rebuild including this one, same
   protection `service.categorise()` gives manually-edited transactions.
   Every candidate is batched ~40 keys at a time to the same `Categoriser` interface `service.py`
   depends on, but with a **binary** taxonomy — just the new leaf and the catch-all it came from,
   not the full category list, since the question here ("does this merchant belong under Coffee, or
   stay in Dining & Takeout (Other)?") is narrower than general categorisation. `LLMCategoriser`'s
   answer-enum is built from whatever taxonomy it's handed, so this works against the same
   implementation service.py uses, unmodified. There is deliberately no cheaper deterministic
   pre-filter (e.g. matching the merchant key against words in the new category's own name) — a
   merchant literally named "May's Coffee" may sell pastries and lunch, not coffee, so the
   merchant's own name is never treated as reliable evidence of what a specific purchase there was
   for. Every reassignment goes through the same judgment call.
3. Each reassignment does `upsert_merchant` (moves the cache entry to the new category) +
   `apply_category_to_key` (bulk-updates the currently-filed transaction rows, still refusing
   `is_category_manual` rows) — the same two primitives `service.categorise()` and
   `hard_delete_category` already use, not new write paths.
4. `carve_category` recomputes `Summary` for every period touched by either the migration or the
   re-derivation, then commits — it owns the whole request end to end, same trigger-seam contract
   `service.categorise()` and `hard_delete_category` follow (caller supplies an open session and a
   categoriser; this function commits).

**Rollup — computed at read time, never stored** (`queries._rollup_summary_rows`):
`Summary` is never written with parent-level aggregate rows — writing both leaf and rollup rows
into the same table would make "sum every row for a period" double-count. Instead,
`GET /summary` / `GET /summary/monthly` take an optional `?rollup=true` that groups the leaf rows
already returned by `COALESCE(parent_name, category)` and sums `total_cents`/`tx_count` in Python.
Cheap because `Summary` only ever has a handful of rows per period regardless of transaction count
— arithmetic over ~10-30 rows, not a transaction scan. A category name with no matching row in
`categories` (e.g. the aggregator's synthetic `"Uncategorised"`, or a since-hard-deleted category)
rolls up to itself rather than erroring.

**Taxonomy exclusion.** `queries.list_leaf_categories` excludes stems and is what `service.py`'s
`taxonomy` list is now built from — a stem must never be an answer the rules/LLM layer can give a
fresh transaction, or a `?rollup=true` sum would double-count it against its own children.
`hard_delete_category`'s `reassign_to` and `soft_delete_category`/`hard_delete_category`'s target
both reject a category that `has_children` (a stem), for the same reason.

**Explicitly out of scope:** multi-parent (DAG) categories; recursive/multi-level stems (a leaf
can never itself be carved). Both ruled out for the same reason — added query/write complexity
(recursive CTEs, DISTINCT-based double-count avoidance) without a clear benefit at single-user
personal-finance scale.

---

## Known Gaps

- **No Alembic baseline.** Schema is created via `Base.metadata.create_all()` at import — fine for
  new tables (how `categories`/`merchant_categories` were added) and, so far, for
  `categories.parent_id` too (v1.3 shipped before v1.2 ever deployed, so the real DB still has no
  `categories` table to `ALTER`). The *next* change to a column on a table that has actually
  reached a deployed DB will need real migrations, not a rebuild-and-re-ingest.
- **`LLM_MODEL` is unset.** Categorisation's LLM layer will fail loudly (logged, ingest unaffected)
  until it's configured.
- **`POST /ingest` response shape changed** — from a bare list to
  `{"files": [...], "categorised": {...}}`. Breaking change; needs frontend coordination before
  deploy.
- **Neither v1.2 nor v1.3 has ever run against the real (private) export or a real LLM provider** —
  only against committed synthetic fixtures and stub/fake categorisers.
- **LLM call sends only the merchant key text**, not also transaction_code/amount sign — a
  simplification, since by the time a key reaches the LLM layer, rules.py has already resolved
  everything where that context would matter.
- **PayLah top-ups (`ITR`, ~34% of the export) are categorised as `Transfer Out`**, not reconciled
  against actual spend — that needs a second, OCR-based ingestion pipeline and is out of scope.
- **`main.py` hardcodes `/logs/app.log`** — running uvicorn outside the container fails unless
  `/logs` exists.
- **No CI wiring.** Tests run locally via the host venv above; `requirements.txt` (prod deps) is
  only installed inside the Docker image.
