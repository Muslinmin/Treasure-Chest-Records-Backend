# Treasure Chest Records — Architecture & Progress

**Last updated:** 2026-08-09

**Status:** v1.1 (ingestion, dedupe, summaries) is shipped and running in Docker against the real
export (669 rows), on the pre-v1.4 inbox/outbox/failed model. v1.2 (category assignment), v1.3
(category hierarchy — parent/child categories, `carved_from` re-derivation, rollup totals), and v1.4
(direct CSV upload, single archive folder) are fully implemented and tested (209 tests) but not yet
deployed — see Known Gaps.

A single-user, single-tenant backend for personal transaction records. The frontend uploads
bank-exported CSVs directly to `POST /ingest`, and the service parses, stores (encrypted at rest),
categorises, and pre-aggregates the data for a remote frontend to read.

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
| Error/perf monitoring | sentry-sdk (no-op until `SENTRY_DSN` set) | 2.66.1 |
| Parsing | Python stdlib `csv` (no pandas) | — |
| Packaging | Docker + Docker Compose, `python:3.12-slim` | — |
| Dev | pytest 9.0.3, ruff 0.15.13 | — |

---

## Repository Layout

```
Treasure-Chest-Records-Backend/
├── app/
│   ├── main.py                     FastAPI instance, logging (console + best-effort file), Sentry
│   │                                init, request-timing middleware, router registration
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
│   │   └── pipeline.py             ingest_uploads() / process_upload() / ingest_and_categorise()
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
│   ├── conftest.py                 in-memory `db` + tmp `archive` fixtures, FIXTURES path
│   ├── fixtures/*.csv              synthetic, card-masked
│   ├── unit/                       pure-function tests (identity, normalise, rules, cluster, LLM)
│   └── integration/                DB-backed tests (pipeline, aggregator, categories, categorise)
├── data/                           HOST STATE — bind-mounted, git-ignored
│   └── archive/  db/
├── logs/                           bind-mounted to /logs in container
├── Dockerfile  docker-compose.yaml  requirements.txt  .env
```

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
dedupes by fingerprint, and aggregates (file archiving is now v1.4's direct-upload model, see
below — the deployed version still runs the pre-v1.4 inbox/outbox/failed scan). `GET /transactions`
and `GET /summary[/monthly]` serve off the pre-aggregated `Summary` table. Bearer-token auth is a
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

**v1.4 — direct CSV upload.** `POST /ingest` takes CSVs as `multipart/form-data` instead of scanning
a server-side watched folder — the frontend sends files directly. The inbox/outbox/failed
three-folder model is retired in favour of one `ARCHIVE` folder; outcome is encoded in the filename
rather than which directory a file lives in: every uploaded file is archived as
`{timestamp}_{original-stem}_{pass|failed}{original-ext}` (e.g.
`20260809-153012-482913_march_pass.csv`). The timestamp prefix exists because re-uploading a file
under the same original name is now a normal action (unlike the old one-time folder drop), so a
bare rename would silently overwrite the previous archive entry. A non-`.csv` upload is archived
with the `_failed` suffix and reported as a normal `"failed"` entry in the response (`error: "Not a
CSV file: <name>"`) rather than silently skipped, since a direct-upload caller has no other way to
learn why a file didn't go through. `app/ingest/pipeline.py`'s `process_upload`/`ingest_uploads`
replace `process_file`/`ingest_inbox`; `csv_parser.parse_csv` is unchanged (still `Path`-based —
uploaded bytes are staged to a real path before parsing). The response shape
(`{"files": [...], "categorised": {...}}`) and file-level atomicity guarantee (one bad file in a
batch never blocks or rolls back others already committed) are unchanged from v1.2. The
already-dead `app/ingest/handler.py` (watchdog adapter) and `test/e2e_watcher.py` were deleted,
since both referenced the now-removed `INBOX`/`OUTBOX`/`FAILED_BOX` env vars. `archive_path` is
`Path | None` throughout the pipeline — every upload stages through a `tempfile.TemporaryDirectory`
first (so `parse_csv` always has a real path to read, regardless of whether archiving is on), and
only gets `shutil.move`d into `archive_path` afterward if one was configured; with `archive_path`
`None` (cloud deploy, no persistent volume for raw uploads) nothing survives past the temp dir's
own cleanup. See Direct CSV Upload (v1.4) below.

**Observability.** `app/main.py` wires in `sentry-sdk` (no-op until `SENTRY_DSN` is set —
FastAPI/Starlette request tracing and error capture are auto-instrumented once it is), a console
log handler that's always active plus a best-effort file handler for `/logs/app.log` (no longer
crashes if `/logs` doesn't exist — it just logs a warning and continues console-only), and a
`RequestTimingMiddleware` that logs every request's method/path/status/duration regardless of
whether Sentry is configured. `app/ingest/pipeline.py`'s `ingest_and_categorise` additionally logs
the ingest-phase and categorise-phase durations separately, since those are the two parts of
`POST /ingest` a caller is most likely to want broken down (the LLM call inside categorise is the
one that can vary widely). See Observability below.

**Tests:** 212 passing (`pytest test -q`) — pure-function unit tests for identity/normalise/
rules/cluster/LLM-router/categoriser, and DB-backed integration tests for ingest, aggregation,
categories, merchant cache, category hierarchy (`test/integration/test_hierarchy.py`), summary
rollup (`test/integration/test_summary_queries.py`), the full categorise pipeline (against a
stub/dict-backed fake categoriser — no network, no spend), and archive-disabled ingest
(`archive_path=None`).

---

## API Surface

| Endpoint | Notes |
|---|---|
| `POST /ingest` | `multipart/form-data` CSV upload(s); ingests then auto-categorises. Returns `{"files": [...], "categorised": {...}}` |
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
 ├── ARCHIVE            → OPTIONAL. Server-side path where every uploaded CSV is archived
 │                         (pass/failed suffix), never client-supplied. Unset it entirely (no
 │                         empty-string trick needed) to skip archiving — e.g. a cloud deploy with
 │                         no persistent volume for raw uploads. Ingest works identically either
 │                         way; only the on-disk audit trail is affected.
 ├── LLM_MODEL          → litellm model string (e.g. "openai/gpt-4o-mini"). Currently UNSET —
 │                         validated lazily on first use, not at import, so its absence doesn't
 │                         stop the app booting. Provider API keys follow litellm's own env var
 │                         convention (OPENAI_API_KEY etc.), not a var here.
 ├── SENTRY_DSN         → OPTIONAL. Unset (the current default) → sentry_sdk.init(dsn=None) is a
 │                         documented no-op, app behaves exactly as if the SDK weren't installed.
 │                         Set it once you create a sentry.io project to get error capture +
 │                         performance tracing for free, no other code changes needed.
 ├── ENVIRONMENT        → tagged onto every Sentry event/transaction. Defaults to "development" if
 │                         unset; set to e.g. "production" in the cloud deploy's env.
```

---

## Deployment

Docker Compose on the host (PC today, Raspberry Pi planned). `./data` (archive/db) and `./logs` are
bind-mounted host state; everything else rebuilds from source. Host migration = copy `data/` +
compose file + `.env`, then `docker compose up -d` — no code changes required. Deploying v1.4 onto
the currently-running host additionally requires merging `data/{inbox,outbox,failed}/` into
`data/archive/` and swapping `INBOX`/`OUTBOX`/`FAILED_BOX` for `ARCHIVE` in `.env` — filesystem/config
only, no DB migration.

Running tests (host venv, no Docker/SQLCipher needed — `test/conftest.py` uses plain SQLite):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt sqlalchemy==2.0.49
.venv/bin/python -m pytest test -q          # 212 passed
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

## Direct CSV Upload (v1.4)

Motivated by wanting the frontend to send CSV files straight to the server over the wire, instead
of the old model where a human drops files into a server-side watched folder before calling
`POST /ingest`. This retires the inbox/outbox/failed three-folder model in favour of one archive
folder, with outcome (pass/failed) encoded in the filename rather than which directory a file sits
in.

**Config.** `INBOX` / `OUTBOX` / `FAILED_BOX` (3 env vars, 3 dirs) collapsed to a single `ARCHIVE`
env var / `data/archive/` dir. Filesystem-layout change only — no DB schema involved, so deploying
it onto the currently-running host is copying/merging `data/{inbox,outbox,failed}/` into
`data/archive/` and updating `.env`, not a migration. `ARCHIVE` is itself optional (see
Configuration above) — cloud deploys with no persistent volume for raw uploads can simply omit it,
since the DB (which does need a persistent volume regardless) is the only durable state that
matters; the archive folder was always just a debugging/audit convenience, never load-bearing for
correctness.

**Filename convention.** Every uploaded file, whether it parses successfully or not, is archived as:

```
{YYYYMMDD-HHMMSS-ffffff}_{original-stem}_{pass|failed}{original-ext}
```

e.g. `march.csv` → `20260809-153012-482913_march_pass.csv`, or `..._march_failed.csv` on error.
The timestamp prefix is load-bearing, not cosmetic: unlike the old model (a file is dropped into
the inbox once, then moved), direct upload makes re-uploading a file under the same original name
a normal, expected action (e.g. re-uploading `march.csv` after fixing a bad row) — without a
disambiguating prefix, the second archive write would silently overwrite the first inside a single
shared folder. The pass/failed token stays immediately before the extension. The `"file"` field in
the API response is still the caller's original filename, not the archived one — the timestamp is
server-side bookkeeping only.

**Endpoint contract.** `POST /ingest` moved from no request body to `multipart/form-data`, one or
more files under a `files` field (`list[UploadFile]`). This is a **breaking request-shape change**
on top of the still-undeployed v1.2 response-shape breaking change (§ Known Gaps) — both need to
ship to the frontend together. The response shape itself
(`{"files": [...], "categorised": {...}}`) is unchanged; only how ingestion is triggered changed.

**Invalid/non-CSV uploads.** The old `ingest_inbox` directory scan silently skipped any file in the
inbox that wasn't `*.csv` — there was no caller waiting on a response, so nothing needed telling. A
direct-upload caller does, so a non-`.csv` filename is instead archived with the `_failed` suffix
and reported as a normal entry in `files` with an explanatory `error`
(`"Not a CSV file: <name>"`) — a deliberate behavior change from the old silent-skip.

**Pipeline** (`app/ingest/pipeline.py`):
- `process_upload(file_name, content, db, archive_path: Path | None)` (replaces `process_file`)
  writes the uploaded bytes to a staging path inside a fresh `tempfile.TemporaryDirectory()` first
  — always, regardless of whether archiving is configured — then parses that path
  (`csv_parser.parse_csv` is untouched — it already takes a `Path`). Only if `archive_path` is not
  `None` does it then `shutil.move` (not `Path.rename` — the temp dir and `archive_path` are very
  likely different filesystems, and a bare rename raises `EXDEV` across devices) the staged file
  into its final `_pass`/`_failed` name; with `archive_path=None` the temp dir's own cleanup is the
  only thing that happens to it. File-level atomicity is preserved exactly as before: one bad file
  in a batch doesn't roll back or block others already committed.
- `ingest_uploads(db, uploads: list[tuple[str, bytes]], archive_path: Path | None)` (replaces the
  `ingest_inbox` directory scan) iterates the files the caller actually sent in the request.
- `ingest_and_categorise` keeps its overall shape (ingest step commits per file, then categorise
  runs in its own try/except so a provider outage never affects ingest) — it just calls
  `ingest_uploads` instead of `ingest_inbox` and takes one path param instead of three.

**Router** (`app/api/routers/ingest.py`): reads a single `ARCHIVE` env var, `Path(...)` if set else
`None` (empty string counts as unset too — `if _archive_env else None`); `ingest` is now `async`
and `await`s `file.read()` on each `UploadFile` before handing `(filename, bytes)` pairs to
`ingest_and_categorise`. Requires `python-multipart` (added to `requirements.txt` — FastAPI/
Starlette need it to parse `UploadFile`/`File`).

**Dead code removed.** `app/ingest/handler.py` (already-unused watchdog adapter, never wired into
`requirements.txt`) and `test/e2e_watcher.py` both referenced `INBOX`/`OUTBOX`/`FAILED_BOX`, which
no longer exist. Both were deleted rather than updated, since they were already superseded by the
manual-sync model before this change.

**Test fixtures.** `test/conftest.py`'s `boxes` fixture (inbox/outbox/failed triplet) became a
single `archive` dir fixture. `test/integration/test_pipeline.py`, `test_ingest_and_categorise.py`,
and `test_categorise.py` — which used to copy fixtures into `boxes["inbox"]` and call
`ingest_inbox`/`process_file` — now build `(filename, bytes)` uploads read from `test/fixtures/`
and call `ingest_uploads`/`process_upload`/`ingest_and_categorise` directly against the single
`archive` dir. `test_pipeline.py`'s `TestArchivingDisabled` class covers `archive_path=None`
specifically (successful ingest, a parse failure, and a non-CSV rejection — all with nothing
written to disk beyond the temp file that gets auto-cleaned). Router-level tests
(`multipart/form-data` through the actual FastAPI endpoint) weren't added — matching the existing
convention that routers stay thin wrappers tested only through the business-logic functions they
call.

**Not yet decided:** upload size limits and content-type/MIME validation beyond the filename
extension check aren't specified — see Known Gaps.

---

## Observability

Motivated by wanting to know how long `POST /ingest` actually takes (it runs synchronously — parse,
insert, aggregate, then a possible LLM round-trip for categorisation — before the frontend gets a
response) and, more generally, to have real signal on a deployed instance instead of guessing from
Docker logs, ahead of ever wanting more than one user.

**Sentry (`app/main.py`).** `sentry_sdk.init(dsn=os.getenv("SENTRY_DSN") or None, environment=...,
traces_sample_rate=1.0)` runs unconditionally at import — with no DSN this is a documented no-op
(confirmed: the SDK disables itself cleanly, no crash, no network calls), so it's always safe to
leave in place regardless of whether a Sentry project exists yet. Once `SENTRY_DSN` is set,
FastAPI/Starlette request tracing and unhandled-exception capture are auto-instrumented — no
integration list to maintain by hand. `traces_sample_rate=1.0` (100% of requests traced) is
deliberately not throttled: at single-user volume the cost is negligible, and full visibility is
worth more than sampling. **This will need revisiting before a multi-user future** — see Known
Gaps.

**Logging (`app/main.py`).** The `"app"` logger (parent of every module's `logging.getLogger(__name__)`,
e.g. `app.ingest.pipeline`) now always gets a console `StreamHandler` — most cloud platforms only
capture stdout/stderr for their log aggregator, and a container's local filesystem is typically
ephemeral anyway, so console output must not depend on anything else being configured correctly.
The `/logs/app.log` file handler is now best-effort: wrapped in `try/except OSError`, so a missing
`/logs` mount logs one warning and continues console-only instead of crashing the app at import —
this used to be a real Known Gap (running uvicorn outside the container failed unless `/logs`
existed) and is now fixed as a side effect of this work.

**Request timing (`app/main.py`).** `RequestTimingMiddleware` (a `BaseHTTPMiddleware`) wraps every
request and logs `{method} {path} -> {status} in {duration_ms}ms` at INFO level, regardless of
whether Sentry is configured — this is the plain-logging half of the profiling ask, Sentry's own
performance tracing is the other half once a DSN is set.

**Phase timing (`app/ingest/pipeline.py`).** `ingest_and_categorise` separately times and logs the
ingest phase (`ingest_uploads` — parse/insert/aggregate for every uploaded file) and the categorise
phase (rules → cache → cluster → fuzzy → LLM), since those two have very different latency profiles
— ingest is local CPU/DB work, categorise can make network calls to an LLM provider. Breaking them
out in the log line answers "which part was slow" without needing to reach for Sentry's trace view.

---

## Known Gaps

- **No Alembic baseline.** Schema is created via `Base.metadata.create_all()` at import — fine for
  new tables (how `categories`/`merchant_categories` were added) and, so far, for
  `categories.parent_id` too (v1.3 shipped before v1.2 ever deployed, so the real DB still has no
  `categories` table to `ALTER`). The *next* change to a column on a table that has actually
  reached a deployed DB will need real migrations, not a rebuild-and-re-ingest.
- **`LLM_MODEL` is unset.** Categorisation's LLM layer will fail loudly (logged, ingest unaffected)
  until it's configured.
- **`POST /ingest`'s request and response shapes both changed** — request went from no body to
  `multipart/form-data` file upload(s) (v1.4); response went from a bare list to
  `{"files": [...], "categorised": {...}}` (v1.2). Both are breaking changes; need frontend
  coordination before deploy.
- **v1.4 upload size limits and content-type/MIME validation are unspecified** — only the filename
  extension (`*.csv`) is checked before staging a file to disk; no max size is enforced anywhere in
  the stack (FastAPI, Starlette, or the app).
- **Neither v1.2, v1.3, nor v1.4 has ever run against the real (private) export, a real LLM
  provider, or a real HTTP client sending multipart uploads** — only against committed synthetic
  fixtures and stub/fake categorisers, calling the pipeline functions directly.
- **LLM call sends only the merchant key text**, not also transaction_code/amount sign — a
  simplification, since by the time a key reaches the LLM layer, rules.py has already resolved
  everything where that context would matter.
- **PayLah top-ups (`ITR`, ~34% of the export) are categorised as `Transfer Out`**, not reconciled
  against actual spend — that needs a second, OCR-based ingestion pipeline and is out of scope.
- **`SENTRY_DSN` is unset.** Error/performance monitoring code is in place and confirmed safe to
  leave running with no DSN (documented no-op), but nothing is actually being reported until a
  sentry.io project is created and its DSN added to `.env`.
- **`traces_sample_rate=1.0` (100% tracing) is a single-user-scale choice.** Fine at current volume;
  would need to drop to a fraction (or move to dynamic sampling) before a multi-user future, both
  for Sentry event-volume cost and to avoid tracing overhead scaling with concurrent request count.
- **No CI wiring.** Tests run locally via the host venv above; `requirements.txt` (prod deps) is
  only installed inside the Docker image.
