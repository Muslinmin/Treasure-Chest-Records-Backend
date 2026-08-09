import logging
import os
import time

import sentry_sdk
import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routers import ingest
from app.api.routers import transactions
from app.api.routers import summary
from app.api.routers import categories

_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.DEBUG)

# Console is unconditional: most cloud platforms only capture stdout/stderr
# for their log aggregator, and a container's local filesystem is typically
# ephemeral anyway. The file handler is best-effort, for local/host runs
# where /logs is a real bind mount — its absence must not crash the app.
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
_console_handler.setFormatter(_fmt)
_app_logger.addHandler(_console_handler)

try:
    _file_handler = logging.FileHandler("/logs/app.log")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(_fmt)
    _app_logger.addHandler(_file_handler)
except OSError:
    _app_logger.warning("Could not open /logs/app.log — file logging disabled, console only")

# No-op until SENTRY_DSN is set (see .env). FastAPI/Starlette request
# tracing and error capture are auto-instrumented once it's active — no
# further integration wiring needed.
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN") or None,
    environment=os.getenv("ENVIRONMENT", "development"),
    traces_sample_rate=1.0,  # single-user app: full trace visibility costs nothing
)

app = FastAPI()

_request_logger = logging.getLogger("app.request")


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Logs every request's wall-clock duration. Sentry's own performance
    tracing covers the same ground once SENTRY_DSN is set; this half needs
    no external service and works whether or not Sentry is configured."""

    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        _request_logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} in {duration_ms:.1f}ms"
        )
        return response


app.add_middleware(RequestTimingMiddleware)

app.include_router(ingest.router)
app.include_router(transactions.router)
app.include_router(summary.router)
app.include_router(categories.router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
