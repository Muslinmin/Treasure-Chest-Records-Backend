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

## Phase 3 — The API Layer ✅ DONE

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

**File: `app/api/auth/api_key.py`** ✅
- Uses `HTTPBearer` — client sends `Authorization: Bearer <key>`.
- `verify_api_key(api_key: HTTPAuthorizationCredentials)` — extracts `.credentials`, compares against `FAST_API_KEY` using `hmac.compare_digest` (constant-time).
- Import-time guard: raises `RuntimeError("FAST_API_KEY is not set")` if env var is missing.
- Auth applied at router level: `APIRouter(dependencies=[Depends(verify_api_key)])`.

**File: `app/db/queries.py`** ✅
- `insert_records(db, records)` — stages Transaction objects. Raises `ValueError` if `source_file` already ingested.
- `get_transactions(db, limit, offset, date_from, date_to, category) -> list[Transaction]` — conditional filters, `db.scalars(stmt).all()`.
- `get_summary_by_period(db, period) -> list[Summary]` — exact period match.
- `get_summary_monthly(db, start_period, end_period) -> list[Summary]` — string range on `Summary.period`.

**File: `app/ingest/pipeline.py`** ✅ e2e tested
- `process_file(filepath, db, processed_path, failed_path) -> dict`
- `ingest_inbox(db, inbox_path, processed_path, failed_path) -> list[dict]`

**File: `app/api/routers/ingest.py`** ✅ e2e tested
- Thin `POST /ingest`. Sources all paths from env vars. Router-level auth.

**File: `app/api/routers/transactions.py`** ✅ e2e tested
- `GET /transactions` with `TransactionResponse(BaseModel)` — `from_attributes=True`, `@computed_field amount` (cents → dollars).
- Query params: `retrieve_limit` (default 50), `offset` (default 0), `date_from`, `date_to`, `category`.

**File: `app/api/routers/summary.py`** ✅ e2e tested
- `GET /summary?period=` — defaults to current month via `date.today().strftime("%Y-%m")`.
- `GET /summary/monthly` — computes `start_period = date(year-1, month, 1)`, returns last 12 months.
- `SummaryResponse(BaseModel)` — `from_attributes=True`, `@computed_field amount`.

**File: `app/summary/aggregator.py`** ✅
- `recompute_summary(db, period)` — date range filter (not `startswith`), upserts into `Summary`.
- Parses period with `year, month = map(int, period.split("-"))`. December handled: `date(year+1, 1, 1)`.

**File: `app/main.py`** ✅
- Registers all three routers. Logging to `app.log`. Uvicorn on `127.0.0.1:8000`.

### 3.4 Checkpoint Questions (all answered)

- **Dependency injection vs global db?** — Caller controls the session lifecycle. `get_transactions` participates in whatever transaction the caller owns; it doesn't create or close sessions itself.
- **Why dollars in the API?** — Cents is an internal storage detail. The API boundary is where the conversion happens — clients should never need to know how data is stored.
- **Why does `process_file` receive a session?** — Same function can be called by the route, a test, or a future adapter without changing its logic. Caller owns the lifecycle.
- **One session, one commit per file?** — `commit()` doesn't close the session; SQLAlchemy starts a fresh transaction on the next write. Opening a new session per file would break the caller's ownership contract and add unnecessary overhead.
- **Inbox path from server config?** — Path traversal risk. If the client supplied the path, a malicious caller could point the server at any directory on the filesystem.

---

## Phase 4 — Docker ✅ DONE

### 4.1 Read First

| Topic | URL | What to focus on |
|---|---|---|
| Dockerfile best practices | https://docs.docker.com/develop/develop-images/dockerfile_best-practices/ | Layer caching, `COPY requirements.txt` before `COPY .` |
| Docker Compose bind mounts | https://docs.docker.com/compose/compose-file/05-services/#volumes | How `./data:/app/data` maps the host folder into the container |
| `.dockerignore` | https://docs.docker.com/engine/reference/builder/#dockerignore-file | Always exclude `data/`, `.env`, `__pycache__`, `.git` |
| Multi-arch builds | https://docs.docker.com/build/building/multi-platform/ | We'll need `linux/arm64` for the Pi later |

### 4.2 What Was Written

**`requirements.txt`** — direct deps only; pip resolves transitive deps at build time.
```
fastapi==0.136.1
uvicorn[standard]==0.47.0
sqlalchemy==2.0.49
sqlcipher3-binary==0.5.7
pydantic==2.13.4
python-dotenv==1.2.2
alembic==1.18.4
```

**`Dockerfile`** — key decisions:
- Base image: `python:3.12-slim` (matches host Python version).
- `sqlcipher3-binary` bundles the SQLCipher C library — no `apt-get` system deps needed.
- Layer order: `COPY requirements.txt` → `RUN pip install` → `COPY . .` — code changes don't bust the pip cache.
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`

**`docker-compose.yaml`** — key decisions:
- Bind mount `./data:/app/data` — live database and CSVs on the host, visible inside the container.
- `env_file: ./data/.env` to inject environment variables.
- `restart: unless-stopped` so it comes back after a reboot.
- `ports: 127.0.0.1:8000:8000` — bound to localhost only; Tailscale handles external routing.

**`.dockerignore`** — excludes `data/`, `.env`, `__pycache__/`, `*.pyc`, `.git/`, `.gitignore`, `tests/`.

**Key decisions made during Phase 4:**
- Direct-deps-only `requirements.txt` chosen over full `pip freeze` output — simpler to maintain for a personal project; reproducibility risk accepted.
- `sqlcipher3-binary` (not `sqlcipher3`) confirmed — bundles the C library, no system-level install needed in the container.
- Python version in Dockerfile set to `3.12-slim` to match the host development environment.

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
- Phase 3 ✅ — all routes done and e2e tested; auth on `Authorization: Bearer`; all checkpoint questions answered
- Phase 4 ✅ — `requirements.txt`, `Dockerfile`, `docker-compose.yaml`, `.dockerignore` all written

## Next Build Step

Run and verify the Docker build:
```powershell
docker compose up --build
```
Watch for errors in the build output. First run pulls the base image and installs deps (slow). Once running, hit `http://127.0.0.1:8000/docs` to confirm the API is live inside the container.
