"""
1. db = SessionLocal()     ← open a session
2. yield db                ← hand it to the endpoint, pause here
3. endpoint runs           ← your route handler does its work
4. finally: db.close()     ← we resume here after the endpoint finishes


App starts
    └── create engine (once, knows DB location + password)
            │
            ▼
Request comes in
    └── get_db() opens a session
            │
            ▼
    Endpoint does DB work (reads/writes)
            │
            ▼
    Session closes (commit or rollback)
            │
            ▼
    Request done

"""

import os

from dotenv import load_dotenv

from sqlalchemy import create_engine

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker
from urllib.parse import *

from app.db.models import Base
from app.db.queries import seed_categories


load_dotenv()

PASS_PHRASE = quote_plus(os.getenv("PASS_PHRASE"))
DATABASE_PATH = os.getenv("DATABASE_FILEPATH")



CONNECTION = f"sqlite+pysqlcipher://:{PASS_PHRASE}@/{DATABASE_PATH}"


# an Engine, which the Session will use for connection
# resources
engine = create_engine(CONNECTION)


@event.listens_for(engine, "connect")
def on_connect(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

Base.metadata.create_all(engine)


SessionLocal  = sessionmaker(bind=engine)

with SessionLocal() as _seed_session:
    seed_categories(_seed_session)
    _seed_session.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()




