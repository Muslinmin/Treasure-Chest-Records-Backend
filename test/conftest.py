"""Shared test fixtures.

Tests deliberately never import ``app.db.session`` — that module opens a real
SQLCipher connection and runs ``create_all`` at import time, which would require
the passphrase and a real DB file. The ORM models are engine-agnostic, so tests
build their own plain-SQLite engine instead and exercise the same schema.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.queries import seed_categories

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db() -> Session:
    """A throwaway in-memory database with the real schema applied."""
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    seed_categories(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def boxes(tmp_path: Path) -> dict[str, Path]:
    """inbox / outbox / failed triplet under a per-test temp directory."""
    paths = {name: tmp_path / name for name in ("inbox", "outbox", "failed")}
    for path in paths.values():
        path.mkdir()
    return paths
