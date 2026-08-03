# Treasure Chest Records — Architecture & Implementation Progress

**Last updated:** 2026-08-03
**Status:** v1.0 — end-to-end pipeline working, running in Docker.
**In flight:** v1.1 — row-level identity via fingerprinting (§4.3), replacing the `source_file`
dedupe guard. Sections tagged **[v1.1]** describe the agreed target design, *not* what runs today;
the only piece in the tree so far is the `fingerprint` column on the model.

A single-user, single-tenant backend for personal transaction records. The user drops bank-exported
CSVs into a watched folder, triggers a manual sync, and the service parses, stores (encrypted at
rest), and pre-aggregates the data for a remote frontend to read.

---

## 1. Tech Stack (as pinned in `requirements.txt`)

| Layer | Choice | Version |
|---|---|---|
| HTTP framework | FastAPI | 0.136.1 |
| ASGI server | Uvicorn (standard) | 0.47.0 |
| ORM | SQLAlchemy (2.x style) | 2.0.49 |
| Database | SQLite + SQLCipher (`sqlite+pysqlcipher://`) | `sqlcipher3-binary` |
| Validation / DTOs | Pydantic | 2.13.4 |
| Config | python-dotenv | 1.2.2 |
| Migrations | Alembic (dependency present, **not yet wired**) | 1.18.4 |
| Parsing | Python stdlib `csv` (no pandas) | — |
| Packaging | Docker + Docker Compose, `python:3.12-slim` | — |
| Dev | pytest 9.0.3, ruff 0.15.13 | — |

---

## 2. Repository Layout

```
Treasure-Chest-Records-Backend/
├── app/
│   ├── main.py                     FastAPI instance, logging setup, router registration
│   ├── api/
│   │   ├── auth/api_key.py         Bearer-token check (hmac.compare_digest)
│   │   └── routers/
│   │       ├── ingest.py           POST /ingest
│   │       ├── transactions.py     GET  /transactions
│   │       └── summary.py          GET  /summary, GET /summary/monthly
│   ├── db/
│   │   ├── models.py               Transaction + Summary ORM models
│   │   ├── queries.py              insert_records, get_transactions, get_summary_*
│   │   └── session.py              engine, SQLCipher URL, WAL pragma, get_db()
│   ├── ingest/
│   │   ├── csv_parser.py           Bank CSV → list[dict]
│   │   ├── identity.py             [v1.1] mask_card / normalise / compute_fingerprint
│   │   ├── pipeline.py             ingest_inbox() / process_file()  ← active path
│   │   └── handler.py              legacy watchdog adapter (retired, see §9)
│   └── summary/aggregator.py       recompute_summary() upsert per period
├── test/
│   ├── test_parser.py              parser unit tests
│   └── e2e_watcher.py              throwaway watchdog driver script
├── data/                           HOST STATE — bind-mounted, git-ignored
│   ├── inbox/  outbox/  failed/  db/
├── logs/                           bind-mounted to /logs in container
├── Dockerfile  docker-compose.yaml  requirements.txt  .env
```

---

## 3. System Architecture

```
                                   ┌──────────────────────────────┐
                                   │   Frontend (Flutter / web)   │
                                   │   pull-only, no push channel │
                                   └───────────────┬──────────────┘
                                                   │ HTTPS + Bearer API key
                                                   ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │                        FastAPI app  (app/main.py)                          │
  │                                                                            │
  │   ┌──────────────────────────────────────────────────────────────────┐     │
  │   │  AUTH GATE — verify_api_key()   app/api/auth/api_key.py           │     │
  │   │  HTTPBearer → hmac.compare_digest(token, FAST_API_KEY) → 403      │     │
  │   │  Registered as a router-level dependency on ALL three routers.    │     │
  │   └───────────────────────────────┬──────────────────────────────────┘     │
  │                                   ▼                                        │
  │   ┌───────────────┐   ┌───────────────────┐   ┌──────────────────────┐      │
  │   │  ingest.py    │   │ transactions.py   │   │     summary.py       │      │
  │   │  POST /ingest │   │ GET /transactions │   │ GET /summary         │      │
  │   │               │   │                   │   │ GET /summary/monthly │      │
  │   └───────┬───────┘   └─────────┬─────────┘   └──────────┬───────────┘      │
  └───────────┼─────────────────────┼────────────────────────┼──────────────────┘
              │ WRITE path          │ READ path              │ READ path
              ▼                     │                        │
  ┌───────────────────────┐         │                        │
  │  app/ingest/          │         │                        │
  │    pipeline.py        │         │                        │
  │   ingest_inbox()      │         │                        │
  │   process_file()      │         │                        │
  └───────┬───────┬───────┘         │                        │
          │       │                 │                        │
          ▼       ▼                 │                        │
  ┌────────────┐ ┌──────────────────┴────┐                   │
  │csv_parser  │ │  app/summary/         │◀──────────────────┘
  │ parse_csv()│ │    aggregator.py      │
  └────────────┘ │  recompute_summary()  │
                 └───────────┬───────────┘
                             ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │                       DATA LAYER  (app/db/)                                │
  │   queries.py  ── SELECT/INSERT statements, no session ownership            │
  │   session.py  ── engine + SessionLocal + get_db() FastAPI dependency        │
  │   models.py   ── Transaction, Summary (DeclarativeBase)                    │
  └───────────────────────────────┬────────────────────────────────────────────┘
                                  ▼
                    ┌──────────────────────────────┐
                    │  SQLite + SQLCipher (AES-256)│
                    │  data/db/database.db, WAL on │
                    └──────────────────────────────┘
```

**Key architectural property — the trigger seam.** `process_file()` and `ingest_inbox()` *receive*
an open `Session`; they never open or close one. Session lifecycle is owned by the caller — the
route via `Depends(get_db)`, a test, or any future adapter. Likewise the inbox/outbox/failed paths
are passed in as arguments (sourced server-side from `.env`, **never** from the HTTP client, which
would be a path-traversal hole). This is what makes the ingestion trigger hot-swappable without
touching the core.

---

## 4. Write Flow — `POST /ingest`

```
   User drops march.csv into data/inbox/          (host filesystem, bind-mounted)
                    │
                    ▼
   Frontend taps "Sync" ──▶ POST /ingest  (Bearer token)
                    │
                    ▼
   ┌────────────────────────────────────────────────────────────┐
   │ routers/ingest.py                                          │
   │   db = Depends(get_db)          ← one session for the batch│
   │   ingest_inbox(db, INBOX, OUTBOX, FAILED_BOX)              │
   └───────────────────────────┬────────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │ pipeline.ingest_inbox()                                    │
   │   for item in inbox.iterdir():                             │
   │       if item.is_file() and item.suffix == ".csv":         │
   │           report.append(process_file(...))                 │
   │   return report          ← list[dict], one entry per file  │
   └───────────────────────────┬────────────────────────────────┘
                               ▼  per file
   ┌────────────────────────────────────────────────────────────┐
   │ pipeline.process_file()                                    │
   │                                                            │
   │   1. parse_csv(filepath) ────────────┐                     │
   │        skip preamble to header row   │ raises on           │
   │        row → dict, cents, sign, flags│ empty result        │
   │   2. periods = {YYYY-MM of each row} │                     │
   │   3. insert_records(db, records)     │ inserts only what   │
   │        fingerprint reconcile (§4.3)  │ the DB lacks        │
   │   4. recompute_summary(db, p) ∀ p    │                     │
   │   5. db.commit()   ◀── durable line drawn HERE             │
   │   6. shutil.move(file → outbox/)                           │
   │      return {file, status, inserted, skipped}              │
   │                                                            │
   │   on ANY exception ──▶ db.rollback()                       │
   │                    ──▶ shutil.move(file → failed/)         │
   │                    ──▶ return {file, status:"failed", err} │
   └────────────────────────────────────────────────────────────┘
                               │
                               ▼
     Response: [{"file":"march.csv",  "status":"ok","inserted":47,"skipped":0},
                {"file":"jan-mar.csv","status":"ok","inserted":92,"skipped":47},   ← overlap
                {"file":"april.csv",  "status":"failed","error":"..."}]
```

**Atomicity: the file is the unit.** One transaction *per file*, not per batch. A malformed file C
cannot roll back already-committed files A and B — each `db.commit()` draws a durable line and a
later `rollback()` can only discard the currently open transaction. The file move is a filesystem
side effect *outside* the DB transaction, so it is sequenced immediately after that file's commit to
keep DB and filesystem consistent.

**Rejection granularity:** the whole file is rejected on any parse/insert failure — no partial
ingestion. Rows with neither a debit nor a credit amount are the one exception: they are logged and
skipped rather than failing the file.

### 4.1 CSV parsing detail (`csv_parser.parse_csv`)

```
  raw bank CSV
  ┌──────────────────────────────────┐
  │ (bank preamble / account blurb)  │  ← scanned past, line by line
  │ Transaction Date,Reference,...   │  ← header located by "Transaction Date"
  │ 08 May 2026,...,13.20,,Settled   │
  └──────────────────────────────────┘
                │  csv.DictReader(f, fieldnames=headers)
                ▼
  per row:  "08 May 2026"      →  date(2026, 5, 8)          transaction_date
            Debit  "13.20"     →  -1320  (negated)          amount_cents
            Credit "150.00"    →  15000                     amount_cents
            Description        →  mask_card(...)            description   [v1.1]
            Transaction Code   →  transaction_code
            Transaction Ref1   →  vendor_name    ← display only, never hashed
            Status == settled  →  True/False                is_settled
            (unset)            →  None / False              category, is_category_manual
            (derived)          →  sha256(...)               fingerprint   [v1.1] §4.3
                │
                ▼
        list[dict] ── raises if the list is empty
```

`Transaction Ref2` and `Transaction Ref3` are read by the `DictReader` and deliberately discarded:
they are stripped substrings of `Description`, so they carry nothing the description does not
already contain. Ref2 is the masked card number; Ref3 is the payment-network reference, which for
card rows makes `Description` unique per transaction.

Money is stored as **integer cents**, negative for debits, positive for credits. No
cents→dollars conversion happens server-side; the frontend formats.

### 4.2 Summary recomputation (`aggregator.recompute_summary`)

```
   period "2026-05"
        │  derive [start_date, end_date) month window
        ▼
   SELECT coalesce(category,'Uncategorised') AS category,
          sum(amount_cents), count(id)
     FROM transaction_records
    WHERE transaction_date >= start AND < end
    GROUP BY category
        │
        ▼
   for each row → sqlite INSERT ... ON CONFLICT(period, category)
                  DO UPDATE SET total_cents, tx_count      ← idempotent upsert
```

Recompute-from-scratch per affected period (not incremental), guarded by the
`UNIQUE(period, category)` constraint. Re-running an ingest for a period converges to the same
result rather than double-counting.

Note this protects against re-running the *aggregation*, not against duplicate underlying rows —
`SUM`/`COUNT` faithfully totals whatever sits in `transaction_records`. Row-level duplicates are
§4.3's job; the idempotent upsert would only make a wrong number stable, not correct.

### 4.3 Row identity — fingerprint idempotency  **[v1.1 — designed, not yet built]**

One mechanism, applied uniformly to every row regardless of transaction type. No branching on
`transaction_code`, no special path for card rows.

```
  fingerprint = sha256( normalise(transaction_date)   |
                        normalise(amount_cents)       |
                        normalise(masked description) |
                        normalise(transaction_code)   )

  normalise = strip → collapse internal whitespace → casefold
```

**What is excluded, and why each matters**

| Field | Reason for exclusion |
|---|---|
| `is_settled` | A pending row that settles between two exports is the *same* transaction. Hashing this makes the settled copy look new — the likeliest false negative. |
| `category`, `is_category_manual` | Filled in after ingest. Hashing them changes a row's fingerprint the moment you categorise it. |
| `vendor_name` (Ref1), Ref2, Ref3 | Stripped substrings of `Description`; they add no information the hash does not already have. |
| `source_file` | Removed from the model entirely. Identity is a property of the transaction, not of the export it arrived in. |

**Card number masking.** `Description` embeds the bank's masked PAN, so masking `Ref2` alone would
be pointless — the digits sit in the description too. A single regex substitution is applied before
storage *and* before hashing:

```
  [\dxX*]{4}-[\dxX*]{4}-[\dxX*]{4}-[\dxX*]{4}   →   XXXX-XXXX-XXXX-XXXX
```

No key, no config, no reversal — the digits never reach the database at all, on top of the
SQLCipher encryption already in place. The substitution is **idempotent**, so a row re-parsed any
number of times yields a byte-identical description and therefore a stable fingerprint.

**Dedupe is count reconciliation, not existence.** Two identical $4.50 coffees on the same day are
legitimate and share a fingerprint — no hash can separate them, because the bank's export contains
nothing that distinguishes them. A `UNIQUE(fingerprint)` constraint would silently discard the
second, which is worse than a duplicate because it is invisible. Hence: **indexed, NOT unique.**

The question asked is not *"is this row a duplicate?"* but *"how many rows of this kind should
exist?"*

```
   one query, before any row is examined:

     SELECT fingerprint, count(*) FROM transaction_records
      WHERE fingerprint IN (…this file's fingerprints…)
      GROUP BY fingerprint          →   existing = {fp: n}

   then walk the file, counting occurrences (seen[fp]):

     seen[fp] <= existing[fp]  →  skip    (already covered by a stored row)
     seen[fp]  > existing[fp]  →  insert  (nothing left to match against)
```

| DB has | File has | Result |
|---|---|---|
| 1 | 1 | 0 inserted — re-import of the same file |
| 2 | 2 | 0 inserted — both coffees already stored |
| 0 | 2 | **2 inserted** — both real coffees survive |
| 2 | 3 | 1 inserted — third was still pending in the earlier export |

Net effect per fingerprint: the table converges to `max(db_count, file_count)`.

**Two deliberate non-behaviours.**

- **No within-file dedupe.** The file is authoritative as issued by the bank; if it contains two
  identical rows, those are two real transactions. The only conflict that exists is file ↔ database.
- **Never deletes.** If the DB holds 3 and a narrower re-export holds 1, nothing is inserted and
  nothing is removed. A narrow export must not erase history a wider import established.

---

## 5. Read Flows

```
  GET /transactions?date_from&date_to&category&retrieve_limit=50&offset=0
        │
        ▼  queries.get_transactions()
     SELECT * FROM transaction_records
       [WHERE transaction_date >= date_from]
       [AND   transaction_date <= date_to]
       [AND   category = lower(category)]
       LIMIT limit OFFSET offset
        │
        ▼  TransactionResponse (from_attributes=True)  → JSON list


  GET /summary?period=YYYY-MM          (defaults to the current month)
        │
        ▼  queries.get_summary_by_period()  → WHERE period = ?

  GET /summary/monthly                 (trailing 12 months, computed from today)
        │
        ▼  queries.get_summary_monthly()   → WHERE period BETWEEN start AND end
        │      periods are "YYYY-MM" strings; lexical compare == chronological
        ▼  SummaryResponse → JSON list
```

Reads never recompute from raw transactions — the dashboard is served entirely off the
pre-aggregated `Summary` table, which is what keeps the payload small and the read path cheap.

---

## 6. Data Model

```
┌─────────────────────────── transaction_records ───────────────────────────┐
│ id                  INTEGER  PK                                            │
│ transaction_date    DATE     NOT NULL                                      │
│ amount_cents        INTEGER  NOT NULL   ← $12.50 == 1250; debits negative   │
│ description         TEXT                ← LLM-inferable; card no. masked    │
│ transaction_code    TEXT(10)            ← bank's internal code              │
│ vendor_name         TEXT                ← from "Transaction Ref1", display  │
│ category            TEXT(100)           ← nullable, filled later            │
│ is_settled          BOOLEAN  default F                                     │
│ is_category_manual  BOOLEAN  default F  ← protects manual edits from        │
│                                            future auto-categorisation       │
│ fingerprint         TEXT(64) INDEXED    ← row identity for dedupe, §4.3     │
│                                            deliberately NOT UNIQUE          │
└────────────────────────────────┬───────────────────────────────────────────┘
                                 │  aggregated by (period, category)
                                 ▼
┌──────────────────────────────── Summary ───────────────────────────────────┐
│ id           INTEGER  PK                                                   │
│ period       TEXT(7)  NOT NULL          ← "2026-05"                        │
│ category     TEXT(100) NOT NULL                                            │
│ total_cents  INTEGER  NOT NULL                                             │
│ tx_count     INTEGER  NOT NULL                                             │
│ updated_at   DATETIME default now                                          │
│ UNIQUE(period, category)                ← powers the upsert                │
└────────────────────────────────────────────────────────────────────────────┘
```

**Ingest idempotency — today (v1.0).** Enforced at *file* granularity: `insert_records` selects for
any existing row with the same `source_file` and raises `"<file> has already been ingested"` if
found. Re-exporting an overlapping date range under a different filename double-inserts every
overlapping row, silently, in both `/transactions` and `/summary`.

**Ingest idempotency — target (v1.1).** Enforced per *row* by `fingerprint` count reconciliation
(§4.3). The `source_file` column and its guard are **removed**: the guard is redundant once row
identity exists, and the column's only remaining use was provenance. Filenames stop mattering
entirely.

---

## 7. Security & Configuration

```
  .env (git-ignored, chmod 600, never baked into the image via .dockerignore)
   ├── FAST_API_KEY       → Bearer token compared with hmac.compare_digest
   ├── PASS_PHRASE        → SQLCipher key, url-quoted into the connection string
   ├── DATABASE_FILEPATH  → data/db/database.db
   ├── INBOX / OUTBOX / FAILED_BOX  → server-side paths, never client-supplied
```

- Every router is constructed with `dependencies=[Depends(verify_api_key)]`, so auth is applied at
  the router level rather than per-endpoint — no endpoint can be added unprotected by accident.
- Constant-time comparison (`hmac.compare_digest`) avoids timing leaks on the key.
- `FAST_API_KEY` missing at import time is a hard `RuntimeError` — the app refuses to boot rather
  than run unauthenticated.
- The DB file is encrypted at rest (AES-256), so it is safe to back up to untrusted cloud storage.
- **[v1.1] Card numbers never enter the database.** The bank's masked PAN appears in both
  `Description` and `Transaction Ref2`; the parser rewrites any `xxxx-xxxx-xxxx-xxxx` pattern to
  `XXXX-XXXX-XXXX-XXXX` before storage, and Ref2 is discarded outright. Defence in depth beneath
  SQLCipher — relevant because the target host is a hobby Pi and backups leave the machine.

---

## 8. Deployment Topology

```
  ┌───────────────────── Host (PC today → Raspberry Pi later) ─────────────────┐
  │                                                                            │
  │   ./data ──────────────┐               ./logs ──────────┐                   │
  │   (inbox, outbox,      │ bind mount    (app.log)        │ bind mount        │
  │    failed, db)         │                                │                   │
  │                        ▼                                ▼                   │
  │   ┌──────────────────────────────────────────────────────────────────┐      │
  │   │ container: fastapi-backend   (python:3.12-slim)                  │      │
  │   │   /app/data          /logs                                       │      │
  │   │   CMD uvicorn app.main:app --host 0.0.0.0 --port 8000            │      │
  │   │   env_file: .env       restart: unless-stopped                   │      │
  │   └──────────────────────────────────────────────────────────────────┘      │
  │                        │ ports 0.0.0.0:8000 → 8000                          │
  └────────────────────────┼───────────────────────────────────────────────────┘
                           ▼
             Network layer (Tailscale / Cloudflare Tunnel / Caddy+HTTPS)
                           ▼
                      Remote frontend
```

Everything under `data/` is host state and must survive rebuilds; everything else is rebuildable
from source. **Host migration = copy `data/` + the compose file + `.env`, then `docker compose up -d`.**
No code changes required. The Python base image and all runtime deps are pinned so an ARM64 rebuild
on a Pi matches what was tested locally.

Logging: the `app` namespace logger writes DEBUG to `/logs/app.log` via a `FileHandler`, format
`%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d — %(message)s`. Uvicorn's own output goes to
the container's stdout log driver.

---

## 9. Implementation Progress

| Area | Component | State | Notes |
|---|---|---|---|
| API | `POST /ingest` | ✅ Done | Returns per-file report |
| API | `GET /transactions` | ✅ Done | Date range, category, limit/offset |
| API | `GET /summary` | ✅ Done | Defaults to current month |
| API | `GET /summary/monthly` | ✅ Done | Trailing 12 months |
| Auth | Bearer + constant-time compare | ✅ Done | Router-level dependency |
| Ingest | `csv_parser.parse_csv` | ✅ Done | Preamble skip, cents, debit/credit sign |
| Ingest | `pipeline.process_file` / `ingest_inbox` | ✅ Done | Per-file transaction + file move |
| Ingest | `handler.py` watchdog adapter | ⚪ Retired | Superseded by manual sync; see below |
| DB | Models, session, SQLCipher, WAL | ✅ Done | Schema via `create_all` at import |
| DB | Queries (insert/read) | ✅ Done | File-level dedupe guard — superseded by v1.1 |
| Summary | `recompute_summary` upsert | ✅ Done | Idempotent per period |
| Ingest | `identity.py` mask + fingerprint | ❌ Not started | Design settled, §4.3 |
| Ingest | Fingerprint count reconciliation | ❌ Not started | Replaces the `source_file` guard |
| DB | Drop `source_file`, add `fingerprint` | 🟡 Partial | Column added to the model; `source_file` not yet removed |
| Deploy | Dockerfile + compose + bind mounts | ✅ Done | Verified running |
| Test | Parser unit tests | 🟡 Partial | Hardcoded Windows paths; won't run as-is on Linux |
| Test | E2E ingest/summary | 🟡 Manual | `test/e2e_watcher.py` is a throwaway driver, not CI |
| Schema | Alembic migrations | ❌ Not started | Dependency pinned, no `alembic/` directory yet |
| Feature | Category assignment (manual / LLM) | ❌ Not started | Columns exist; everything ingests as `Uncategorised` |

### Retired components still present in the tree

`app/ingest/handler.py` (`CSVHandler`, `_wait_until_stable`) and `test/e2e_watcher.py` are the
original event-driven ingestion path. The trigger moved to manual sync because imports happen on
irregular days, a single explicit action gives the user direct feedback, and the file-stability
problem — the only reason `_wait_until_stable` existed — disappears when the file has been sitting
still for minutes before Sync is tapped. Both files import `watchdog`, which is **not** in
`requirements.txt`, so neither runs inside the container.

### Known gaps

- **No Alembic baseline.** Schema is created by `Base.metadata.create_all(engine)` on import of
  `app/db/session.py`. This adds columns for new tables but will not alter existing ones — a real
  migration story is needed before any schema change to a table holding data. The v1.1 schema change
  sidesteps this *once*, because the current DB is disposable test data whose source CSV still sits
  in `outbox/`: delete `database.db`, re-ingest, done. **That is the last free schema change.**
- **Row-level dedupe.** Re-exporting an overlapping date range under a *different* filename will
  double-insert; the guard only matches on `source_file`. Fix designed in §4.3, not yet built.
- **Parser tests are not runnable** on the current host (absolute Windows paths, no fixture files
  committed since `*.csv` is git-ignored). Masking (§4.3) makes a redacted fixture safe to commit,
  which unblocks this.
- **`ingest_inbox` logs "No valid files found"** unconditionally at the end of the scan, including
  on successful runs — cosmetic, but misleading in the log.
- **`main.py` hardcodes `/logs/app.log`**, which exists only inside the container. Running uvicorn
  directly on the host fails unless `/logs` is created.
- **Categories are never populated.** Every summary row currently aggregates under
  `Uncategorised`.

### v1.1 build order

1. `app/ingest/identity.py` — `mask_card()`, `normalise()`, `compute_fingerprint()`
2. `csv_parser.py` — mask description, attach fingerprint, stop emitting `source_file`
3. `models.py` — `fingerprint` added ✅; still to remove `source_file`
4. `queries.insert_records` — count reconciliation, guard deleted, returns `{inserted, skipped}`
5. `pipeline.py` — surface the counts; fix the unconditional "No valid files found" log
6. Wipe `data/db/database.db`, move the CSV back from `outbox/`, re-ingest
7. Redacted fixture + tests: **second ingest of the same file must insert 0**
8. Fold §4.3 into the main narrative and drop the [v1.1] tags once it ships

**Resolved design question.** Whether `Transaction Ref3` (and therefore the token inside
`Description`) is regenerated per export — which would change every card row's fingerprint and
double-insert on the first overlapping import. Owner confirms the bank does not regenerate it, so
`Description` stays in the hash unmodified apart from card masking. If that ever proves wrong, the
fix is one regex stripping trailing digit runs before hashing.
