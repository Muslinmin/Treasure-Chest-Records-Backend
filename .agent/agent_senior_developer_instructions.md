# Developer Guide — Personal Finance Backend

## Role Assignment
- **Senior Developer** = the agent reading this file. You review, explain, unblock, and guide architecture decisions.
- **Junior Developer & Product Owner** = the human interacting with you. They write the code, make product decisions, and consult you on design and snippets.

---

**This document is your (the agent's) source of truth for how to guide the junior developer. Every instruction, reading list, and checkpoint below is directed at them — delivered through you.**

---

## How We Work Together

You write the code. I review, explain, and unblock you. Before you write any module, you read the sources I link here, then come back and we write it together — line by line if needed. You should be able to explain every line you commit. If you can't, ask me before pushing.

As product owner, you also make the final call on open decisions (like pull vs. push). I will give you my recommendation, but it's your product.

### Code Review Rules (for the Senior Developer)

- **Never write the full implementation.** Provide the function signature and 
  a commented skeleton only. The junior developer writes the body.
- **Always give a comment skeleton before any coding task.** Every new file or 
  function starts with comments describing what each block needs to do — 
  not how to do it. The junior developer figures out the how.
- **One concept at a time.** Explain the why before assigning the what. 
  If a new concept is required (e.g. dependency injection, upsert), explain 
  it in plain English first, then give the skeleton.
- **Checkpoint questions before moving phases.** Never advance to the next 
  file until the junior developer can answer why the current file works the 
  way it does.

---

## Phase 0 — Before You Write Anything ✅ DONE

### 0.1 Understand the Shape of the System

Read this in order. Don't skip.

1. **The spec** (`agent.md`) — you already have this. Re-read the "Expected Flow" section until you can narrate it without looking.
2. **What is ASGI?** — FastAPI runs on ASGI. Knowing why matters for understanding async.
   - https://asgi.readthedocs.io/en/latest/
   - Key takeaway: ASGI lets a single Python process handle many connections concurrently without threads.
3. **Docker concepts (bind mounts vs volumes)** — we use bind mounts for `data/`. Know the difference before you touch the compose file.
   - https://docs.docker.com/storage/bind-mounts/
   - https://docs.docker.com/storage/volumes/

### 0.2 Set Up Your Local Environment

Before the first `docker compose up`, you need:
- Docker Desktop (or Docker Engine on Linux) installed and running.
- Python 3.11+ on your host (for running Alembic migrations locally during development).
- A code editor with a Python linter (VS Code + Pylance recommended).
- `git` initialized in your project root with a `.gitignore` that excludes `data/`.

**Your first task:** create the folder structure from the spec. Do it manually in your terminal so you feel it.

```bash
mkdir -p my-backend/app/{api,db,ingest,summary,auth}
mkdir -p my-backend/alembic
mkdir -p my-backend/data/{inbox,processed,failed,db}
touch my-backend/data/.env
chmod 600 my-backend/data/.env
```

Then commit the empty structure (with a `.gitkeep` in each empty folder). This is your baseline.

---

## Phase 1 — The Database Layer ✅ DONE

**Why start here?** Everything else depends on the database. The API cannot return data that isn't stored. The ingest pipeline cannot write data with no schema. You define the schema first.

### 1.1 Read First

| Topic | URL | What to focus on |
|---|---|---|
| SQLAlchemy 2.x ORM basics | https://docs.sqlalchemy.org/en/20/orm/quickstart.html | `DeclarativeBase`, `mapped_column`, `Mapped` type hints |
| SQLAlchemy sessions | https://docs.sqlalchemy.org/en/20/orm/session_basics.html | `Session`, `sessionmaker`, context managers |
| SQLite WAL mode | https://www.sqlite.org/wal.html | Why we enable WAL: concurrent reads don't block writes |
| SQLCipher overview | https://www.zetetic.net/sqlcipher/sqlcipher-api/ | How the `PRAGMA key` unlocks the database |
| Alembic tutorial | https://alembic.sqlalchemy.org/en/latest/tutorial.html | `alembic init`, `alembic revision --autogenerate`, `alembic upgrade head` |

### 1.2 What Was Written

**File: `app/db/models.py`** — two ORM classes, `Transaction` and `Summary`.

Actual schema (deviates from original spec — these are the real column names):

```
transaction_records          ← __tablename__
  id                INTEGER  PRIMARY KEY
  transaction_date  DATE     NOT NULL
  amount_cents      INTEGER  NOT NULL     ← $12.50 stored as 1250
  description       TEXT     nullable
  transaction_code  TEXT(10) nullable     ← bank's internal code
  vendor_name       TEXT     nullable
  is_settled        BOOLEAN  NOT NULL
  category          TEXT     nullable     ← filled later; null = not yet categorised
  is_category_manual BOOLEAN default False
  source_file       TEXT     nullable

Summary                      ← __tablename__ (capital S)
  id           INTEGER  PRIMARY KEY
  period       TEXT(7)  NOT NULL          ← "2026-05"
  category     TEXT     NOT NULL
  total_cents  INTEGER  NOT NULL
  tx_count     INTEGER  NOT NULL
  updated_at   DATETIME default now
  UNIQUE(period, category)
```

**File: `app/db/session.py`** — builds the SQLCipher engine, registers the WAL pragma listener, calls `Base.metadata.create_all(engine)` (dev only — Alembic deferred), exposes `SessionLocal` and `get_db()`.

**Key decisions made during Phase 1:**
- `create_all` is used in dev to auto-create tables. When the project has real data, switch to Alembic migrations — `create_all` won't apply schema changes to existing tables.
- The `on_connect` WAL pragma listener must be registered **before** `create_all` is called, so the first connection also gets WAL mode.
- `PRAGMA key` is handled automatically by the `pysqlcipher` URL scheme — it does not need to be in `on_connect`.
- `.env` paths on Windows must use forward slashes (e.g. `C:/Users/.../data/failed`). Backslash paths cause silent escape-sequence corruption (`\f` → form feed `\x0c`).

### 1.3 Alembic (deferred)

Alembic migrations are not set up yet. `create_all` is the current mechanism. Before going to production or before any schema change, set up Alembic properly:

```bash
alembic init alembic
# Edit alembic/env.py to point at your models and engine
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

**Checkpoint questions (already answered):**
- Why integer cents instead of float? — `0.1 + 0.2 = 0.30000000000000004`. Money must never float.
- What does `PRAGMA key` do? — Unlocks the SQLCipher-encrypted database file on first connection.
- Why is `alembic upgrade head` safer than `CREATE TABLE IF NOT EXISTS`? — Alembic applies incremental deltas to existing tables; `create_all` skips tables that already exist and won't add new columns.

---

## Phase 2 — The Ingest Pipeline ✅ DONE

**Why this is Phase 2 and not Phase 3?** The API serves data that was ingested. Without the ingest pipeline, the API has nothing to serve. Build the writer before the reader.

### 2.1 Read First

| Topic | URL | What to focus on |
|---|---|---|
| Python `csv` module | https://docs.python.org/3/library/csv.html | `csv.DictReader`, handling missing fields, dialect detection |
| `pathlib.Path` | https://docs.python.org/3/library/pathlib.html | `Path.rename()`, `Path.stat()`, `Path.suffix` |
| `pathlib` / `shutil` | https://docs.python.org/3/library/shutil.html | `shutil.move()` for cross-device-safe file moves |
| Python `logging` | https://docs.python.org/3/howto/logging.html | Set up a named logger per module, not `print()` |
| SQLAlchemy `insert` with `on_conflict_do_update` | https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#insert-on-conflict-upsert | How to upsert the summary rows |

### 2.2 What Was Written

**File: `app/ingest/csv_parser.py`** (named `csv_parser.py`, not `parser.py`)
- `parse_csv(filepath: Path) -> list[dict]`
- Uses `csv.DictReader`, validates required columns, converts date strings and amount strings to their proper types.

**File: `app/ingest/handler.py`** — RETIRED
- Replaced by `pipeline.py`. Do not port the watchdog observer or `_wait_until_stable` into any new code.

**File: `app/ingest/pipeline.py`** — NOT WRITTEN YET (next task)
- `process_file(filepath, db)` — one file: parse, insert, recompute, commit, move to `processed/`. Catches its own exceptions and moves to `failed/`; always returns a result dict, never raises.
- `ingest_inbox(db, inbox_path)` — scans inbox for `*.csv`, calls `process_file` per file, aggregates and returns a per-file report.
- Both **receive an open session** — they never create or close one. Session lifecycle is owned by the caller (the route via `Depends(get_db)`, or a test pointing at a temp dir).
- Use `shutil.move()` (not `Path.rename()`) — cross-device safe.
- Guard the failed-move in the `except` block so it cannot itself raise and escape the loop.
- `insert_records` moves out of `handler.py` into `queries.py` — do not keep a copy here.

**File: `app/summary/aggregator.py`**
- `recompute_summary(db, period: str)`
- Groups `transaction_records` by `coalesce(category, "Uncategorised")` for the given period
- Upserts into `Summary` using `ON CONFLICT DO UPDATE`
- Called inside the same transaction as the insert — same commit, same rollback

**Key decisions made during Phase 2:**
- Null categories are coalesced to `"Uncategorised"` in the summary (not filtered out). Filtering would create silent gaps in the summary.
- Both SELECT and GROUP BY use the same `coalesce` expression — they must match or results are inconsistent.
- The whole file is rejected if parsing fails — no partial ingestion.
- **Per-file transaction, shared session.** One `commit()` per file, one `SessionLocal()` for the whole batch. `commit()` does not close the session; SQLAlchemy begins a fresh transaction on the next write. Do not open a new session per file.
- **No stability polling.** The old `_wait_until_stable` is retired with watchdog. By the time the user taps Sync, the file has been stable for minutes. A one-shot open-and-parse is sufficient.
- **Ingestion trigger is `POST /ingest`.** There is no background observer thread. The inbox path is sourced from server-side config (`.env`) and passed in as a function argument — it is never supplied by the HTTP client (path-traversal risk).

**Checkpoint questions (already answered):**
- Why move to `failed/` not delete? — The file is evidence. It can be re-processed once the issue is fixed.
- Why per-file transaction instead of one transaction for the whole batch? — A bad file C must not roll back already-committed files A and B. The file is the unit of atomicity.
- Why does `process_file` receive a session rather than create one? — So the same function can be called by the route, by a test pointing at a temp dir, or by a future watchdog adapter — without changing the core logic. The caller owns the session lifecycle.

---

## Phase 3 — The API Layer 🔄 IN PROGRESS

Now the database has data. Now we expose it.

### 3.1 Read First

| Topic | URL | What to focus on |
|---|---|---|
| FastAPI first steps | https://fastapi.tiangolo.com/tutorial/first-steps/ | Route decorators, path parameters, query parameters |
| FastAPI dependency injection | https://fastapi.tiangolo.com/tutorial/dependencies/ | How `Depends(get_db)` threads the session into your route |
| FastAPI security (API key header) | https://fastapi.tiangolo.com/tutorial/security/ | `APIKeyHeader`, raising `HTTPException(403)` |
| FastAPI bigger applications | https://fastapi.tiangolo.com/tutorial/bigger-applications/ | `APIRouter`, splitting routes across files |
| Pydantic v2 basics | https://docs.pydantic.dev/latest/ | `BaseModel`, field validators, how FastAPI uses it for serialization |
| HTTP status codes | https://developer.mozilla.org/en-US/docs/Web/HTTP/Status | Know 200, 201, 400, 401, 403, 404, 422, 500 |

### 3.2 Actual Project Structure (deviates from original spec)

```
app/
├── main.py                     ← FastAPI instance, router registration, Uvicorn, logging config (done)
├── api/
│   ├── auth/
│   │   └── api_key.py          ← auth dependency (done, header rename pending)
│   └── routers/
│       ├── transactions.py     ← GET /transactions (skeleton done, query not wired)
│       ├── ingest.py           ← POST /ingest (done, e2e tested)
│       └── summary.py          ← GET /summary, GET /summary/monthly (stub only)
├── db/
│   ├── models.py
│   ├── session.py
│   └── queries.py              ← insert_records (done) + get_transactions (not written)
├── ingest/
│   ├── csv_parser.py
│   └── pipeline.py             ← process_file + ingest_inbox (done, e2e tested)
```

Auth lives under `app/api/auth/`, not `app/auth/`. Routers live under `app/api/routers/`. Query logic is separated into `app/db/queries.py` — route handlers never write SQL directly.

### 3.3 What Has Been Written

**File: `app/api/auth/api_key.py`** ✅ (pending header rename)
- Currently uses `APIKeyHeader(name="API-KEY")`. **This header name must be updated** — the `X-` prefix (RFC 6648, 2012) is deprecated and the current casing is non-standard. Agreed direction: `Authorization: Bearer <key>` via FastAPI's `HTTPBearer`, or at minimum `Api-Key` (Title-Case, no prefix). Do not rename to `X-API-Key`.
- `verify_api_key` compares against `FAST_API_KEY` from `.env`, raises `HTTP 403` on mismatch with no detail message.
- Needs `hmac.compare_digest` instead of `!=` (constant-time comparison — one-line fix).
- Needs an import-time guard: if `FAST_API_KEY` is unset/`None`, raise at startup — not silently 403 every request.
- Auth is applied at the **router level**: `APIRouter(dependencies=[Depends(verify_api_key)])` — not per-route.

**File: `app/db/queries.py`** ✅
- `insert_records(db, records)` — stages Transaction objects into the session (does not commit). Checks `source_file` before inserting: if any transaction with the same `source_file` already exists, raises `ValueError("{file} has already been ingested")`. Log says "Staged N records".
- `get_transactions(...)` — NOT written yet.

**File: `app/ingest/pipeline.py`** ✅ e2e tested
- `process_file(filepath, db, processed_path, failed_path) -> dict` — parse → insert → recompute → commit → move to `processed/`. Catches its own exceptions, rolls back, guards the failed-move, always returns a result dict.
- `ingest_inbox(db, inbox_path, processed_path, failed_path) -> list[dict]` — iterates inbox, calls `process_file` per `.csv`, returns per-file report. Logs a warning if no valid files found.

**File: `app/api/routers/ingest.py`** ✅
- Thin `POST /ingest`. Sources all three paths from env vars server-side. Calls `ingest_inbox`, returns the result list. Router-level auth.

**File: `app/api/routers/transactions.py`** ✅ skeleton
```
GET /transactions
  Query params: retrieve_limit (int, default 50), offset (int, default 0),
                date_from (date | None), date_to (date | None),
                category (str | None)
  Body: pass — not yet wired to queries.get_transactions
```

**File: `app/main.py`** ✅
- FastAPI instance, registers all three routers (transactions, summary, ingest).
- Logging: `FileHandler("app.log")` + custom formatter (`asctime`, `levelname`, `filename`, `lineno`, `message`) attached to the `app` namespace logger at DEBUG level. Terminal output via Uvicorn's default handler.
- Uvicorn runs on `127.0.0.1:8000`, `reload=False`.

### 3.4 What Still Needs to Be Written

**File: `app/db/queries.py`** — add `get_transactions`
A `get_transactions(db, limit, offset, date_from, date_to, category) -> list[Transaction]` function that builds a conditional SQLAlchemy `select` statement. Each optional filter is only applied if the caller passes a non-None value.

**File: `app/api/routers/transactions.py`** — wire the query + Pydantic response model
- Call `queries.get_transactions(...)` in the route body.
- Define `TransactionResponse(BaseModel)` with `model_config = ConfigDict(from_attributes=True)` and a `@computed_field` for cents → dollars conversion.
- Set `response_model=list[TransactionResponse]`.

**File: `app/api/routers/summary.py`** — implement both routes
```
GET /summary
  Query params: period (str, optional — defaults to current month)
  Returns: list of { period, category, total_cents, tx_count }

GET /summary/monthly
  Returns: last 12 months of totals across all categories
```

**Checkpoint:** Before moving to Phase 4, you must be able to answer:
- What is dependency injection and why is it better than importing a global `db` object?
- Why does the API return `amount` in dollars even though it's stored in cents?
- Why does `process_file` receive a session rather than create one internally, and what does that buy us?
- Why is there one session for the whole batch but one `commit()` per file? What would break if you opened a new session per file?
- Why is the inbox path sourced from server-side config and not accepted from the HTTP request?

---

## Phase 4 — Docker

### 4.1 Read First

| Topic | URL | What to focus on |
|---|---|---|
| Dockerfile best practices | https://docs.docker.com/develop/develop-images/dockerfile_best-practices/ | Layer caching, `COPY requirements.txt` before `COPY .` |
| Docker Compose bind mounts | https://docs.docker.com/compose/compose-file/05-services/#volumes | How `./data:/app/data` maps the host folder into the container |
| `.dockerignore` | https://docs.docker.com/engine/reference/builder/#dockerignore-file | Always exclude `data/`, `.env`, `__pycache__`, `.git` |
| Multi-arch builds | https://docs.docker.com/build/building/multi-platform/ | We'll need `linux/arm64` for the Pi later |

### 4.2 What You Will Write

**`Dockerfile`** — key decisions:
- Base image: `python:3.11-slim` (pin the exact digest for reproducibility).
- Install system deps needed by `sqlcipher3-binary` (libsqlcipher-dev on Debian).
- Copy `requirements.txt`, run `pip install`, then copy the app source. This order matters for Docker layer caching.
- Expose port 8000.
- `CMD ["python", "-m", "app.main"]`

**`docker-compose.yml`** — key decisions:
- Bind mount `./data` to `/app/data` inside the container.
- `env_file: ./data/.env` to inject environment variables.
- `restart: unless-stopped` so it comes back after a reboot.
- No ports exposed to `0.0.0.0` if you're using Tailscale — bind only to `127.0.0.1:8000` on the host.

**`requirements.txt`** — pin every version. Example:
```
fastapi==0.111.0
uvicorn[standard]==0.29.0
sqlalchemy==2.0.30
alembic==1.13.1
sqlcipher3-binary==0.5.3
python-dotenv==1.0.1
pydantic==2.7.1
```

Run `pip freeze > requirements.txt` after your virtual env is confirmed working. Never leave versions unpinned.

---

## Phase 5 — Open Decisions

### 5.1 Pull vs. Push — DECIDED: Pull-only for v1
REST endpoints only. The frontend refetches `GET /summary` on load and on focus. SSE or WebSockets are deferred — they would touch the ingest path, `main.py`, and the frontend simultaneously and aren't justified for a single-user deliberate-import workflow.

### 5.2 CSV Schema Flexibility — DECIDED: Single bank for v1
The parser is built for one fixed CSV schema. If multiple banks are needed later, the recommended approach is a `mapping.json` per bank that maps their column names to the internal schema — no code changes needed per new bank.

### 5.3 Rejected Rows Policy — DECIDED: Reject whole file
If parsing fails, the entire file moves to `failed/`. Partial ingestion creates silent data gaps — you think you have all of June but rows were silently skipped. Knowing the import failed is better than unknowingly missing data.

---

## Our Working Agreement

1. **You write first, I review.** Don't ask me to write it for you upfront. Write your best attempt, share it here, and I'll tell you what to fix and why. You learn more from fixing than from copying.
2. **One module at a time.** Don't skip to the API before the database is working. The phases above are in dependency order.
3. **Test manually at each checkpoint.** Before moving phases, confirm the current phase works. For Phase 1, that means querying the database directly and seeing rows. For Phase 2, that means dropping a CSV and watching the log.
4. **Ask about the "why", not just the "how".** If I tell you to do something and you don't understand why, stop and ask. You're also the product owner — you need to trust the architecture, not just execute it.
5. **Commit at each checkpoint.** Small, working commits. Not one giant commit at the end.

---

## Quick Reference — Key Concepts to Know Cold

| Concept | One-line definition |
|---|---|
| ASGI | Async web interface spec; lets FastAPI handle many requests without blocking threads |
| ORM | Object-Relational Mapper; lets you talk to the DB in Python objects, not raw SQL |
| Migration | A versioned script that changes the DB schema; Alembic manages these |
| Bind mount | A host directory mapped into a container; changes on the host appear inside |
| SQLCipher | SQLite variant with transparent AES-256 encryption; unlocked via `PRAGMA key` |
| WAL mode | Write-Ahead Logging; allows concurrent reads while a write is in progress |
| Dependency injection | FastAPI's way of passing shared resources (like a DB session) into route handlers |
| Upsert | Insert if not exists, update if it does; used for the summary table |
| API key auth | A shared secret in a request header; simpler than OAuth for single-user private APIs |
| Lifespan | FastAPI's startup/shutdown hook; not used in current build (no observer), but needed if watchdog is re-added — see Appendix A of architecture review |

---

## Current Status

- Phase 0 ✅
- Phase 1 ✅
- Phase 2 ✅ — `csv_parser.py`, `aggregator.py`, `pipeline.py` done; `handler.py` retired; e2e ingest tested and passing
- Phase 3 🔄 — `queries.py` (insert_records done, get_transactions not written); `ingest.py` ✅; `main.py` ✅; `transactions.py` skeleton only; `summary.py` stub only; auth header rename pending
- Phase 4 ⬜ — Docker not started

## Next Build Step

Write, for review (junior developer writes first, senior developer reviews):
1. `get_transactions(db, limit, offset, date_from, date_to, category)` in `queries.py`
2. Wire it into `transactions.py` with a Pydantic `TransactionResponse` model (cents → dollars `@computed_field`)
3. `summary.py` routes (`GET /summary` and `GET /summary/monthly`)

Fix before or alongside:
- Auth header rename (`API-KEY` → `Authorization: Bearer`)
- `hmac.compare_digest` in `api_key.py`
- `FAST_API_KEY` startup guard in `api_key.py`
- `aggregator.py` date filter (`startswith` → range bounds)
