# Project Spec

## Objective

A personal backend service to manage transaction records. The user manually drops CSV files (exported from a bank, app, or other source) into a designated folder on the host machine. The backend detects each new file, parses it, ingests the records into an encrypted local database, and updates a set of precomputed summaries used for visualization. A remote frontend — a Flutter app on mobile or a desktop client — reads from the backend to display trends, breakdowns, and recent transactions.

The system is built for single-user, single-tenant use. It runs first on the user's personal computer inside a Docker container, and is designed to migrate to a Raspberry Pi (or another always-on host) later without code changes — only the host changes.

## Tech Stack

- **FastAPI** — defines the HTTP API endpoints.
- **Uvicorn** — ASGI server that runs the FastAPI app.
- **SQLAlchemy 2.x** — ORM for database access.
- **Alembic** — database schema versioning and migrations.
- **SQLite + SQLCipher** — relational database with transparent AES-256 encryption at rest. Accessed via the `sqlcipher3-binary` package and the `sqlite+pysqlcipher://` SQLAlchemy URL scheme.
- **Python `csv` module** (stdlib) — parses CSV files row by row. No pandas dependency.
- **Docker + Docker Compose** — packages and runs the service; portable between hosts.

## Project Structure

```
my-backend/
├── app/                  # FastAPI application code
│   ├── main.py           # FastAPI instance + Uvicorn entrypoint
│   ├── api/              # endpoint definitions (routers: transactions, summary, ingest)
│   ├── db/               # SQLAlchemy models, session, SQLCipher setup
│   ├── ingest/           # csv_parser.py + pipeline.py (manual-sync pipeline)
│   ├── summary/          # aggregation logic
│   └── auth/             # API key validation
├── alembic/              # Alembic migrations
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── data/                 # stateful, lives on the host (bind-mounted into the container)
    ├── inbox/            # user drops CSVs here
    ├── processed/        # successfully ingested CSVs are moved here
    ├── failed/           # CSVs that failed parsing/ingestion
    ├── db/               # encrypted SQLite database file
    └── .env              # SQLCipher key, API key, runtime config
```

Everything under `data/` is host state and must be preserved across container rebuilds and host migrations. Everything else can be rebuilt from source.

## Expected Flow

The end-to-end pipeline from CSV to frontend is as follows:

1. **CSV arrives.** The user drops a `.csv` file into `data/inbox/`, which is a bind-mounted host directory visible to the container.

2. **Manual sync trigger.** The user taps Sync, which calls `POST /ingest`. The endpoint scans `data/inbox/` for `*.csv` files. There is no background daemon or file-system watcher — by the time the user taps Sync, the file has been stable for minutes.

3. **Parsing.** The CSV is read row by row using the standard `csv` module. Each row is validated against the expected transaction schema. If any row is malformed, the entire file is rejected and moved to `failed/` — no partial ingestion.

4. **Database ingestion.** Each file is processed in its own database transaction (one commit per file, one shared session for the batch). This means a bad file C does not roll back already-committed files A and B. The file is the unit of atomicity.

5. **Summary update.** Still within the same transaction, rows in the `summary` table are upserted: monthly totals, category breakdowns, rolling averages, and any other aggregations the visualization needs. This keeps reads cheap — the frontend never recomputes from raw transactions.

6. **File cleanup.** Once the transaction commits successfully, the CSV is moved to `data/processed/`. If anything failed mid-pipeline, the CSV is moved to `data/failed/` for inspection, and the database is left untouched.

7. **Data becomes available.** The new records and updated summary are now queryable through the API.

## Exposed Endpoints

The API exposes two categories of data:

- **Summary data** — the precomputed aggregations used for charts and trend visualizations. Small, fast payload, used to render the dashboard.
- **Transaction data** — the underlying records. The frontend can request either the latest N records or the full available range, with optional filters (date range, category, etc.).

Authentication sits in front of every endpoint via an API key header, even in single-user mode, because the API is reachable by a remote frontend.

## Pull vs. Push — Decided: Pull-only for v1

The frontend calls `GET /summary` and `GET /transactions` on load and on focus. No push channel. SSE/WebSockets are deferred — they would touch the ingest path, `main.py`, and the frontend simultaneously, and add complexity that isn't justified for a single-user app where imports happen deliberately.

## Deployment

The service runs inside a Docker container. The host machine is initially the user's personal computer; later it migrates to a Raspberry Pi (or other always-on host) without code changes. Migration is a copy of the `data/` folder and the compose file to the new host, then `docker compose up -d`.

Remote access from the frontend is handled at the network layer (Tailscale, Cloudflare Tunnel, or a public URL with HTTPS via Caddy), independent of the application code.

## Operational Notes

- The SQLCipher passphrase is loaded from `data/.env` at startup. The `.env` file must have restricted permissions (`chmod 600`) and must never be committed to version control.
- The encrypted database file should be backed up off-host on a regular schedule. Because the file is already encrypted at rest, it can safely be pushed to any cloud storage (B2, S3, etc.) without leaking sensitive data.
- Pin versions in `requirements.txt` and pin the Python base image in the `Dockerfile`, so builds on a future host (e.g., a Pi running ARM64) match what was tested locally.
- Application logs write to `app.log` in the project root via a `FileHandler` on the `app` namespace logger. Format: `%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d — %(message)s`. Level: DEBUG. Terminal output goes through Uvicorn's default handler. In Docker, consider replacing the file handler with stdout-only logging and using the container's log driver instead.
