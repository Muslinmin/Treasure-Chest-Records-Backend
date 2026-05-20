# Architecture Review — v0.1.0 (discussed)
**Original review date:** 2026-05-20
**Revised after discussion:** 2026-05-20
**Reviewer:** Senior Developer
**Scope:** All code written to date (Phases 1–3 partial), plus architectural decisions agreed in review discussion

---

## 0. Revision Note — What Changed Since v0.1.0

This revision records the decisions made while walking through the v0.1.0 findings together. The substantive change is to the **ingestion trigger**, plus several finding corrections and additions.

### 0.1 Major decision — ingestion trigger: watchdog → manual sync

The system **no longer uses watchdog / event-driven ingestion.** It moves to a **manual sync** model: a `POST /ingest` endpoint that scans the inbox, ingests every pending CSV, and returns a per-file report.

**Why:**
- Imports happen on irregular days, and the user opens the app deliberately — there is no predictable batch window for which a scheduler would help, and no continuous-monitoring need that justifies a daemon thread.
- A single explicit action (tap *Sync*) both ingests and returns the result, giving direct feedback ("ingested march.csv: 47 rows; failed april.csv: bad header") — which a silent background ingest cannot.
- Simpler backend: no observer thread, no FastAPI lifespan juggling for the observer, and it is friendlier to the future Raspberry Pi host (sleeps instead of polling).
- **The file-stability problem disappears.** Watchdog fired on inode creation, before writing finished — that was the only reason `_wait_until_stable` existed. By the time the user taps Sync, the file has been stable for minutes. A one-shot "can I open and parse this" is sufficient; the polling loop is retired.

The trade-off accepted: a dropped file is not ingested until the user taps Sync. For a single-user app opened deliberately, this is fine — the data isn't going anywhere.

### 0.2 New ingestion architecture (the "seam")

The work is decoupled from the trigger across three layers, each with one responsibility:

```
POST /ingest          ← transport. Thin. Get session, call ingest_inbox, return the report.
ingest_inbox(db, path)← orchestration. Scan inbox, filter *.csv, loop, aggregate report.
process_file(fp, db)  ← unit of work. One file: parse, insert, recompute, commit, move.
```

- `process_file` and `ingest_inbox` live in **`app/ingest/pipeline.py`**.
- Both **receive an open session** — they never create or close one. The caller (the route via `Depends(get_db)`, or a test, or a future re-added watchdog adapter) owns the session lifecycle. This is the property that keeps the core trigger-agnostic / hot-swappable.
- The inbox **path is passed in as a function argument**, sourced from server-side config (`.env`). It is *never* supplied by the HTTP client — that would be a path-traversal hole. Passing it at the call site (rather than `os.getenv` deep inside) exists purely so tests can point the function at a temp directory.
- `insert_records` moves out of `handler.py` into **`app/db/queries.py`** (write logic belongs in the data layer).
- `handler.py` and `_wait_until_stable` are **retired**. If event-driven ingest is ever wanted again, it is a thin watchdog adapter that calls the same `process_file` — no change to the core.

### 0.3 Transaction scope — confirmed per-file

Ingestion uses **one transaction per file**, not one transaction for the whole batch.

- This is forced by the Phase 5.3 decision ("reject the whole *file*"). The file is the unit of atomicity, so a malformed file C must not roll back already-good files A and B.
- Mechanism: each `db.commit()` draws a durable line; a later `rollback()` can only discard the currently open (uncommitted) transaction. Committed files are behind closed lines and untouchable.
- The file-move is a side effect *outside* the DB transaction, so it must be sequenced immediately after that file's commit — only per-file commit keeps the database and filesystem consistent.
- `process_file` **catches its own exceptions and returns a result** instead of raising, so one bad file does not abort the loop.
- Implementation detail: **one session for the whole batch** (reused across files). `commit()` does not close the session; SQLAlchemy begins a fresh transaction on the next write. Do not open a new `SessionLocal()` per file.

### 0.4 Pull vs push — unchanged (5.1 retained)

Pull-only stands. "See the result immediately" is satisfied by the app **refetching `GET /summary` on load/focus** (option a) — no push channel. SSE/WebSocket (option b, live update while staring at the screen) remains deferred; it would reopen 5.1 and touch the ingest path, `main.py`, and the frontend at once.

### 0.5 Finding corrections vs v0.1.0

- **Finding #13 (rename `API-KEY` → `X-API-Key`) is withdrawn — it was wrong.** The `X-` prefix was deprecated by **RFC 6648 (June 2012)**; new headers should not use it. The real smell was the unconventional casing. Recommended fix instead: adopt the standard `Authorization: Bearer <key>` (FastAPI `HTTPBearer`), or at minimum `Api-Key` (Title-Case, no prefix).
- **Finding #1 (`_wait_until_stable` timeout) is now MOOT** — the function is retired with watchdog. Do not port the polling loop into `process_file`.
- **Watchdog lifespan shutdown** (would have been needed for clean `observer.stop()`/`join()`) is **MOOT** — no observer to stop.

---

## 1. System Overview

A personal finance backend that **ingests CSV bank exports on a manual sync trigger**, writes them into an encrypted SQLite database, precomputes monthly summaries, and exposes the data via a REST API. Single-user, single-tenant. Designed to run in Docker on a personal computer and later migrate to a Raspberry Pi.

### Current Build State

| Phase | Status | Notes |
|---|---|---|
| DB layer | ✅ Done | `models.py`, `session.py` working |
| `queries.py` | 🔄 Partial | `insert_records` done (with source_file duplicate guard); `get_transactions` not written |
| `pipeline.py` | ✅ Done | `process_file` + `ingest_inbox` — e2e tested, duplicate guard confirmed |
| `ingest.py` router | ✅ Done | Thin `POST /ingest`, e2e tested |
| `main.py` | ✅ Done | FastAPI instance, routers, logging config (FileHandler + formatter) |
| Auth | ✅ Done | `api_key.py` complete — header rename + hmac + startup guard still pending |
| Transactions route | 🔄 Skeleton | No query logic, no Pydantic model yet |
| Summary routes | ⬜ Not started | |
| Docker | ⬜ Not started | |

---

## 2. Target Project Structure (post-decision)

```
app/
├── __init__.py
├── db/
│   ├── models.py         ← Transaction + Summary ORM classes
│   ├── session.py        ← engine, SessionLocal, get_db()
│   └── queries.py        ← get_transactions() + insert_records() (NOT WRITTEN YET)
├── ingest/
│   ├── csv_parser.py     ← parse_csv()
│   └── pipeline.py       ← process_file(), ingest_inbox()  (NOT WRITTEN YET)
│   └── handler.py        ← RETIRED (watchdog adapter; remove or keep dormant)
├── summary/
│   └── aggregator.py     ← recompute_summary()
└── api/
    ├── auth/
    │   └── api_key.py    ← verify_api_key dependency
    └── routers/
        ├── transactions.py  ← GET /transactions skeleton
        ├── summary.py       ← GET /summary, /summary/monthly (NOT WRITTEN YET)
        └── ingest.py        ← POST /ingest (thin) (NOT WRITTEN YET)

test/
├── e2e_watcher.py        ← manual end-to-end test script (now exercises POST /ingest)
└── test_parser.py        ← pytest unit tests for csv_parser
└── fixtures/             ← committed sample CSVs (NOT CREATED YET)
```

---

## 3. Layer-by-Layer Review

### 3.1 `app/db/models.py`

**What's good:** integer cents; `UniqueConstraint("period","category")` on Summary; nullable `category`.

| Severity | Issue |
|---|---|
| Medium | Table casing inconsistent: `Summary` (capital) vs `transaction_records` (lowercase). Standardise to `"summary"`. **Trap:** `create_all` only creates *missing* tables — after the rename it will create `summary` *alongside* the stale `Summary`. Nuke the dev DB and recreate, or do the rename as a proper Alembic migration. |
| Low | Stale docstring: says `ref1 ← vendor name`; actual column is `vendor_name`. |
| Low | `updated_at` not refreshed on upsert (see 3.5). |

### 3.2 `app/db/session.py`

**What's good:** WAL pragma registered before `create_all`; `get_db()` `try/finally` closes the session; SQLCipher key via URL.

| Severity | Issue |
|---|---|
| Medium | `from urllib.parse import *` — wildcard import. Replace with `from urllib.parse import quote_plus`. |
| Medium *(new)* | Consider `sessionmaker(..., expire_on_commit=False)`. Default behaviour expires ORM attributes after `commit()`; harmless on the ingest path (we don't read objects post-commit), but on the **read** path a route that commits then returns an ORM object will trigger a re-fetch or error. Set it now so Phase 3 wiring doesn't surprise you. |
| Low | `create_all` is intentional dev-only; switch to Alembic before any schema change. (Also see the rename trap in 3.1.) |

### 3.3 `app/ingest/csv_parser.py`

**What's good:** skips blank-`Transaction Date` rows (trailing lines); debit = negative cents, credit = positive; `source_file` from filename.

| Severity | Issue |
|---|---|
| High | Skipping a malformed row with `continue` violates the 5.3 "reject the whole file" policy — it is silent partial ingestion. A *blank* line (no date) may be skipped; a *malformed* row must `raise`. Document the distinction in a comment. |
| Medium | `del row["Credit Amount"]` / `del row["Debit Amount"]` mutate the row in place and raise an unhelpful `KeyError` on the wrong format. Validate required columns **before** the row loop and raise a clear error. |
| Medium | Header detection via `line.startswith("Transaction Date")` breaks silently if the export has a preamble. Make header location explicit / validated. |
| Low | `records = list()` → `records = []`. |
| Low | `amount_cents = int()` is dead initialisation; always overwritten. |

### 3.4 Ingest pipeline — `app/ingest/pipeline.py` (replaces `handler.py`)

The transaction boundary, rollback-on-failure, and move-to-`failed/` logic from the old `on_created` are **sound and carry over** — they move into `process_file`. What changes is structure and trigger.

| Severity | Issue |
|---|---|
| High | **Duplicate ingestion protection.** Moving a file out of `inbox/` on success is the primary guard, but a fast double-tap of Sync could scan the same file twice before the move. Keep the `source_file` check before insert. (Content-hash is the bullet-proof upgrade if ever needed.) |
| Medium *(new)* | `filepath.rename()` is **not cross-device safe** — raises `OSError` if source/destination are on different filesystems. Same `data/` bind mount is fine today, but use `shutil.move()` as the safe habit. |
| Medium *(new)* | The move-to-`failed/` in the `except` block can **itself raise**, escaping uncaught and breaking the batch loop. Guard the failed-move so `process_file` always returns a result. |
| Low *(new)* | `insert_records` logs `"Inserted N records"` while records are only *added* to the session (not yet committed). In a finance app, use honest wording: `"Staged N records"`. |
| — | `_wait_until_stable` timeout finding — **MOOT** (function retired with watchdog). |

### 3.5 `app/summary/aggregator.py`

**What's good:** `coalesce(category,"Uncategorised")` in both SELECT and GROUP BY; `ON CONFLICT DO UPDATE` upsert.

| Severity | Issue |
|---|---|
| Low | `updated_at` not in the upsert `set_` dict — timestamp never reflects a recompute. Add `"updated_at": datetime.now()`. |
| Low → **verify** | `transaction_date.startswith(period)` — `transaction_date` is typed `DATE`, not `TEXT`, so calling `.startswith()` on it is suspect, not merely non-portable. Confirm summary row counts match raw transactions for the period; switch to a date-range bound (`>= first-of-month`, `< first-of-next-month`) regardless. |

### 3.6 `app/api/auth/api_key.py`

**What's good:** no detail message on 403; `auto_error=True`; router-level dependency.

| Severity | Issue |
|---|---|
| Medium *(was Low, corrected)* | Header `"API-KEY"`: **do not** rename to `X-API-Key` (RFC 6648 deprecated the `X-` prefix in 2012). Adopt standard `Authorization: Bearer <key>` via `HTTPBearer`, or use `Api-Key` (no prefix). |
| Medium *(new)* | `api_key != FAST_API_KEY` uses non-constant-time comparison (leaks length/prefix via timing). Use `hmac.compare_digest(api_key, FAST_API_KEY)`. Minor for a Tailscale-only app, but a one-line fix that CI linters will flag anyway. |
| Low | Missing `FAST_API_KEY` → `None` → every request 403s silently. Add an import-time guard that raises if the env var is unset (fail fast, not at request time). |

### 3.7 `app/api/routers/transactions.py`

**What's good:** optional params with sensible defaults; `Depends(get_db)`; router-level auth.

| Severity | Issue |
|---|---|
| High | Body is `pass` → empty 200. Wire `queries.get_transactions(...)`. |
| Medium | No Pydantic response model → serialising raw ORM objects fails. Define `TransactionResponse(BaseModel)` with `model_config = ConfigDict(from_attributes=True)` and set `response_model=`. |
| Medium *(new)* | cents → dollars conversion belongs in the **Pydantic layer** (a `@computed_field` reading `amount_cents` → `amount`), not in the route body and not as a property on the ORM model. |

### 3.8 `test/test_parser.py`

**What's good:** covers happy path, sign convention, empty-file exception; specific assertions.

| Severity | Issue |
|---|---|
| Medium | Hardcoded `r"C:\Users\lordm\..."` — un-runnable elsewhere/CI. Use `test/fixtures/` + `Path(__file__).parent`. |
| Medium | `test_exec.csv` may not exist in the repo. Commit the fixture or create it in test setup. |

---

## 4. Missing Pieces (Blocking for Phase 3 Completion)

| File | What it needs to do |
|---|---|
| `app/ingest/pipeline.py` | `process_file(filepath, db)` — one file, per-file commit + move, catch-and-return. `ingest_inbox(db, inbox_path)` — scan, filter `*.csv`, loop, aggregate report. Both **receive** the session. |
| `app/db/queries.py` | `get_transactions(db, limit, offset, date_from, date_to, category)` with conditional filters. Absorb `insert_records` from `handler.py`. |
| `app/api/routers/ingest.py` | Thin `POST /ingest`: `Depends(get_db)`, call `ingest_inbox(db, INBOX)`, return the report. Router-level auth. |
| `app/api/routers/summary.py` | `GET /summary?period=` and `GET /summary/monthly`. |
| `app/api/routers/transactions.py` | Wire `queries.get_transactions`; add Pydantic response model with cents→dollars computed field. |
| `app/main.py` | FastAPI instance, router registration, Uvicorn entrypoint. **No watchdog observer.** |

---

## 5. Priority Fix List

**Fix immediately (High):**
1. ~~`_wait_until_stable` timeout~~ — **MOOT** (watchdog retired).
2. Duplicate ingestion protection — `source_file` check before insert.
3. `csv_parser.py` — malformed rows raise (reject whole file); blank lines may skip.
4. `transactions.py` — wire `get_transactions` + Pydantic response model (with cents→dollars computed field).
5. Build the manual-sync seam: `process_file` + `ingest_inbox` in `pipeline.py`; move `insert_records` to `queries.py`.

**Fix before shipping (Medium):**
6. `from urllib.parse import *` → `from urllib.parse import quote_plus`.
7. Rename `"Summary"` → `"summary"` (mind the `create_all` trap — reset DB or use Alembic).
8. `test_parser.py` — portable fixtures under `test/fixtures/`.
9. Startup guard for missing `FAST_API_KEY`.
10. Auth header: adopt `Authorization: Bearer` (or `Api-Key`) — **not** `X-API-Key`.
11. Constant-time key comparison (`hmac.compare_digest`).
12. `shutil.move()` for cross-device-safe file moves; guard the failed-move.
13. `expire_on_commit=False` on the session maker.

**Polish (Low):**
14. Stale `ref1` comment in `models.py`.
15. `updated_at` refreshed on upsert in aggregator.
16. `records = list()` → `records = []`.
17. Honest log wording: `"Staged N records"` not `"Inserted"`.
18. Aggregator date filter: range bounds instead of `startswith` (and verify current behaviour).

---

## 6. Next Build Step (agreed)

Write, for review (you write first, I review):
1. `get_transactions(db, limit, offset, date_from, date_to, category)` in `queries.py`.
2. Wire it into `transactions.py` with a `TransactionResponse` Pydantic model (`from_attributes=True`, `@computed_field` for cents → dollars).
3. `GET /summary` and `GET /summary/monthly` in `summary.py`.

Fix in parallel (medium priority, before shipping):
- Auth header rename (`API-KEY` → `Authorization: Bearer`)
- `hmac.compare_digest` in `api_key.py`
- `FAST_API_KEY` startup guard
- `aggregator.py` date filter: replace `startswith(period)` with a proper date-range bound

---

## Appendix A — Re-adding Watchdog (if event-driven ingest is ever wanted)

Manual sync is the chosen trigger (see §0.1). This appendix records the recipe for re-adding event-driven ingest later, so the seam's payoff isn't lost.

**Principle:** you do not swap out the pipeline. `process_file` is the constant. You add a thin trigger adapter in front of it. Watchdog and `POST /ingest` are two doorbells ringing the same `process_file`.

### What changes vs what stays

| Piece | Re-adding watchdog |
|---|---|
| `process_file(fp, db)` | unchanged — not one line |
| `csv_parser.py`, `queries.py`, `aggregator.py`, DB layer | unchanged |
| `ingest_inbox(db, path)` | unchanged — keep for manual sync, or delete if going watchdog-only |
| `handler.py` (adapter) | add back — ~15 lines, calls `process_file` |
| `_wait_until_stable` | comes back — only inside the adapter, and use the bounded version (max attempts then raise), not the original infinite loop |
| `main.py` | gains a `lifespan` to start/stop the observer |

### The adapter

```python
# app/ingest/handler.py  (re-added watchdog trigger)
class CSVHandler(FileSystemEventHandler):
    def on_created(self, event):
        # 1. ignore directories and non-.csv          ← trigger-specific filtering
        # 2. _wait_until_stable(filepath)             ← stability concern lives HERE (bounded)
        # 3. db = SessionLocal()                      ← adapter sources its OWN session
        # 4. try:
        #        result = process_file(filepath, db)  ← THE SEAM, identical call the endpoint makes
        #        (log result.status)
        #    finally:
        #        db.close()                           ← adapter owns the session lifecycle
        ...
```

```python
# app/main.py  (gains lifespan)
@asynccontextmanager
async def lifespan(app):
    # observer = Observer(); observer.schedule(CSVHandler(), INBOX, recursive=False)
    # observer.start()
    # yield                                  ← app serves requests here
    # observer.stop(); observer.join(timeout=5)   ← clean shutdown so Docker stop doesn't hang
    ...
```

### Two subtleties

**Session sourcing differs — this is why `process_file` receives a session.** The endpoint gets its session from `Depends(get_db)` (FastAPI resolves dependencies during a request). The watchdog handler runs in a background thread with no request, so it opens `SessionLocal()` itself and closes it itself. `process_file` doesn't know which caller handed it the session. (This is the "two session styles, intentional" note made concrete.)

**The stability problem returns — but stays quarantined in the adapter.** Watchdog fires on inode creation, before the write finishes, so `_wait_until_stable` is needed again. It lives only in the adapter, before the `process_file` call, so the core never learns the file might be half-written.

**Scope differs:** watchdog maps one event → one file → one `process_file` call. The endpoint maps one request → scan → loop → many `process_file` calls. Both terminate at the same seam.

**Running both at once:** the seam supports any number of triggers, so manual sync and watchdog can coexist, both calling `process_file`. Cost: a narrow race — if watchdog and a sync scan pick up the same file before either moves it out of `inbox/`, both could process it. The `source_file` duplicate guard catches most orderings; a true concurrent double-read could slip through. Also, SQLite serialises writes, so two concurrent writers can briefly hit "database is locked" (the engine busy-timeout handles it). For these reasons most setups pick one trigger rather than running both.
