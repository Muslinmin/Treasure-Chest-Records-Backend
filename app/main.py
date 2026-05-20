import uvicorn
from fastapi import FastAPI
from app.api.routers import ingest
from app.api.routers import transactions
from app.api.routers import summary

app = FastAPI()

app.include_router(ingest.router)
app.include_router(transactions.router)
app.include_router(summary.router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)

