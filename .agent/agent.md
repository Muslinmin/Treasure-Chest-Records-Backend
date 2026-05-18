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
- **watchdog** — monitors the host folder for new CSV files.
- **Python `csv` module** (stdlib) — parses CSV files row by row. No pandas dependency.
- **Docker + Docker Compose** — packages and runs the service; portable between hosts.

## Project Structure

```
my-backend/
├── app/                  # FastAPI application code
│   ├── main.py           # FastAPI instance + Uvicorn entrypoint
│   ├── api/              # endpoint definitions
│   ├── db/               # SQLAlchemy models, session, SQLCipher setup
│   ├── ingest/           # watchdog handler + CSV parsing
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

2. **File detection.** Watchdog raises a "file created" event. Before processing, the handler waits briefly until the file's size and modification time are stable, to ensure the file has finished being written.

3. **Parsing.** The CSV is read row by row using the standard `csv` module. Each row is validated against the expected transaction schema (date, amount, category, description, etc.). Malformed rows are logged and either skipped or used to reject the entire file, depending on the chosen policy.

4. **Database ingestion.** Inside a single database transaction, parsed rows are inserted into the `transactions` table of the encrypted SQLite database. Using one transaction guarantees the file is either fully ingested or not at all — no partial state.

5. **Summary update.** Still within the same transaction, rows in the `summary` table are upserted: monthly totals, category breakdowns, rolling averages, and any other aggregations the visualization needs. This keeps reads cheap — the frontend never recomputes from raw transactions.

6. **File cleanup.** Once the transaction commits successfully, the CSV is moved to `data/processed/`. If anything failed mid-pipeline, the CSV is moved to `data/failed/` for inspection, and the database is left untouched.

7. **Data becomes available.** The new records and updated summary are now queryable through the API.

## Exposed Endpoints

The API exposes two categories of data:

- **Summary data** — the precomputed aggregations used for charts and trend visualizations. Small, fast payload, used to render the dashboard.
- **Transaction data** — the underlying records. The frontend can request either the latest N records or the full available range, with optional filters (date range, category, etc.).

Authentication sits in front of every endpoint via an API key header, even in single-user mode, because the API is reachable by a remote frontend.

## Open Decision: Pull vs. Push

Whether the frontend pulls data from the backend on demand, or the backend pushes updates to the frontend in real time, is intentionally left open. Both are technically supported by the chosen stack:

- A **pull** model uses standard REST endpoints (`GET /summary`, `GET /transactions?limit=...`) that the frontend calls when it loads or refreshes. Simplest to build, easiest to reason about, well-suited to a workflow where CSV imports are infrequent and the user opens the app deliberately.
- A **push** model uses WebSockets or Server-Sent Events. When a CSV is ingested, the backend broadcasts an update so connected frontends see the new data without re-requesting. More moving parts, but the dashboard updates live.
- A **hybrid** is also possible: REST for the initial load and historical data, plus a lightweight WebSocket/SSE channel for "data updated, refetch summary" notifications.

The choice affects how step 5 (summary update) signals downstream — either by simply updating the database and waiting to be queried, or by additionally emitting an event to subscribed clients. The ingestion pipeline itself is identical either way.

## Deployment

The service runs inside a Docker container. The host machine is initially the user's personal computer; later it migrates to a Raspberry Pi (or other always-on host) without code changes. Migration is a copy of the `data/` folder and the compose file to the new host, then `docker compose up -d`.

Remote access from the frontend is handled at the network layer (Tailscale, Cloudflare Tunnel, or a public URL with HTTPS via Caddy), independent of the application code.

## Operational Notes

- The SQLCipher passphrase is loaded from `data/.env` at startup. The `.env` file must have restricted permissions (`chmod 600`) and must never be committed to version control.
- The encrypted database file should be backed up off-host on a regular schedule. Because the file is already encrypted at rest, it can safely be pushed to any cloud storage (B2, S3, etc.) without leaking sensitive data.
- Pin versions in `requirements.txt` and pin the Python base image in the `Dockerfile`, so builds on a future host (e.g., a Pi running ARM64) match what was tested locally.
