import uvicorn
from fastapi import FastAPI
from app.api.routers import ingest
from app.api.routers import transactions
from app.api.routers import summary


import logging

_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

_file_handler = logging.FileHandler("app.log")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.DEBUG)
_app_logger.addHandler(_file_handler)

app = FastAPI()

app.include_router(ingest.router)
app.include_router(transactions.router)
app.include_router(summary.router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

