"""§10.7 — PRAGMA foreign_keys=ON fires on connect, in both session.py and conftest.py.

SQLite parses but does not enforce REFERENCES unless this pragma is set, and it
is per-connection, not a property of the file. This is the one behaviour that
cannot be exercised through the models yet (no FK is declared until the
merchant_categories table lands in step 7) — so the test asserts the pragma
value directly rather than a constraint violation.
"""

from sqlalchemy import text


class TestForeignKeysPragma:
    def test_foreign_keys_pragma_is_on_for_the_test_engine(self, db):
        value = db.execute(text("PRAGMA foreign_keys")).scalar()
        assert value == 1
