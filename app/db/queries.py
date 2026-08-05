
from app.db.models import Transaction, Summary, Category, MerchantCategory
from sqlalchemy.orm import Session
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from collections import Counter
from datetime import date
import logging


logger = logging.getLogger(__name__)

# §10.5 — reserved, seeded at first run, and undeletable: rules.py (§10.4) emits
# these unconditionally, so removing one would make the rules layer write a
# category that does not exist. `Unknown` is also the LLM's "I cannot tell"
# escape hatch (§10.8).
SYSTEM_CATEGORIES = ["Unknown", "Transfer In", "Transfer Out", "Interest", "Income"]

# §10.13 — the starting taxonomy, decided against the real merchant clusters in
# §10.1-10.3 (Grab, McDonald's, Spotify, Google, DAISO, SimplyGo, Flyscoot) and
# cross-checked against common budgeting-app category sets. Ordinary rows: not
# is_system, editable/deletable like anything the user adds later.
STARTING_CATEGORIES = [
    "Groceries",
    "Dining & Takeout",
    "Transport",
    "Shopping",
    "Subscriptions & Digital Services",
    "Travel",
    "Health & Wellness",
    "Bills & Utilities",
    "Housing",
    "Personal Care",
    "Education",
    "Gifts & Donations",
    "Fees & Charges",
]


def insert_records(db: Session, records: list[dict]) -> dict:
    """Stage the rows this file contributes that the database does not yet hold.

    Dedupe is count reconciliation, not existence. Two identical $4.50 coffees on
    the same day are legitimate and share a fingerprint — no hash can separate
    them, because the bank's export contains nothing that distinguishes them. So
    the question asked is not "is this row a duplicate?" but "how many rows of
    this kind should exist?"

        seen[fp] <= existing[fp]  →  skip    (already covered by a stored row)
        seen[fp]  > existing[fp]  →  insert  (nothing left to match against)

    Net effect per fingerprint: the table converges to max(db_count, file_count).

    Two deliberate non-behaviours:

    - No within-file dedupe. The file is authoritative as issued by the bank; if
      it contains two identical rows, those are two real transactions. The only
      conflict that exists is file ↔ database.
    - Never deletes. If the DB holds 3 and a narrower re-export holds 1, nothing
      is inserted and nothing is removed. A narrow export must not erase history
      a wider import established.

    Returns ``{"inserted": int, "skipped": int}``. Re-ingesting a file the
    database already covers is a success with ``inserted == 0``, not an error.
    """
    fingerprints = {record["fingerprint"] for record in records}

    # One query, before any row is examined.
    stmt = (
        select(Transaction.fingerprint, func.count())
        .where(Transaction.fingerprint.in_(fingerprints))
        .group_by(Transaction.fingerprint)
    )
    existing = dict(db.execute(stmt).all())

    seen = Counter()
    inserted = 0
    skipped = 0

    for record in records:
        fingerprint = record["fingerprint"]
        seen[fingerprint] += 1
        if seen[fingerprint] <= existing.get(fingerprint, 0):
            skipped += 1
            continue
        db.add(Transaction(**record))
        inserted += 1

    logger.info(f"Staged {inserted} records, skipped {skipped} already present")
    return {"inserted": inserted, "skipped": skipped}


def get_transactions(
    db: Session,
    limit: int,
    offset: int,
    date_from: date | None,
    date_to: date | None,
    category: str | None,
) -> list[Transaction]:
    stmt = select(Transaction)
    
    if date_from is not None:
        stmt= stmt.where(Transaction.transaction_date >= date_from)
    
    if date_to is not None:
        stmt= stmt.where(Transaction.transaction_date <= date_to)
    
    if category is not None:
        stmt = stmt.where(func.lower(Transaction.category) == category.lower())

    stmt = stmt.limit(limit).offset(offset)
    records = db.scalars(stmt).all()
    logger.info("Transactions retrieved!")
    return records


def get_summary_by_period(db: Session, period: str) -> list[Summary]:
    stmt = select(Summary)
    stmt = stmt.where(Summary.period == period)
    records = db.scalars(stmt).all()
    logger.info(f"Summary for {period} retrieved!")
    return records


def get_summary_monthly(db: Session, start_period: str, end_period: str) -> list[Summary]:
    stmt = select(Summary)

    stmt= stmt.where(Summary.period >= start_period, Summary.period <= end_period)


    records = db.scalars(stmt).all()
    logger.info(f"Summary for period range {start_period} and {end_period} retrieved!")

    return records


def seed_categories(db: Session) -> None:
    """Insert the reserved system categories and the starting taxonomy.

    Idempotent: only names not already present are inserted, so this is safe to
    call on every app start (session.py) and every test db fixture (conftest.py).
    """
    existing = set(db.scalars(select(Category.name)).all())

    for name in SYSTEM_CATEGORIES:
        if name not in existing:
            db.add(Category(name=name, is_system=True))

    for name in STARTING_CATEGORIES:
        if name not in existing:
            db.add(Category(name=name, is_system=False))


def list_categories(db: Session, include_inactive: bool = False) -> list[Category]:
    stmt = select(Category)
    if not include_inactive:
        stmt = stmt.where(Category.is_active.is_(True))
    stmt = stmt.order_by(Category.name)
    return db.scalars(stmt).all()


def get_category(db: Session, name: str) -> Category | None:
    return db.scalars(select(Category).where(Category.name == name)).first()


def create_category(db: Session, name: str, carved_from: list[str]) -> Category:
    """Add a category to the taxonomy (§10.5).

    ``carved_from`` states scope: ``[]`` is a pure addition — only merchants
    seen for the first time after this point can receive it. A non-empty list
    means "break down these parents"; re-deriving the merchants currently filed
    under them is service.py's job (build step 8) and is not wired here yet —
    this function only validates the parents exist and creates the row, so it
    never silently no-ops the request, it just doesn't yet perform the
    re-derivation half of it.
    """
    if get_category(db, name) is not None:
        raise ValueError(f"category '{name}' already exists")

    for parent in carved_from:
        parent_row = get_category(db, parent)
        if parent_row is None or not parent_row.is_active:
            raise ValueError(f"carved_from parent '{parent}' is not an active category")

    category = Category(name=name, is_system=False, is_active=True)
    db.add(category)
    db.flush()
    return category


def soft_delete_category(db: Session, name: str) -> Category:
    """Deactivate a category. Rows already carrying it keep their label."""
    category = get_category(db, name)
    if category is None:
        raise ValueError(f"category '{name}' does not exist")
    if category.is_system:
        raise ValueError(f"'{name}' is a system category and cannot be deleted")

    category.is_active = False
    return category


def hard_delete_category(db: Session, name: str, reassign_to: str) -> set[str]:
    """Delete a category, bulk-reassigning its rows to ``reassign_to``.

    Returns the set of "YYYY-MM" periods touched, so the caller can recompute
    summaries for exactly those periods. Both transaction_records and
    merchant_categories are reassigned first — the latter is required, not
    optional, once merchant_categories exists: its FK is ON DELETE RESTRICT
    (§10.7), so any cache row still pointing at ``name`` would block the
    delete below.
    """
    category = get_category(db, name)
    if category is None:
        raise ValueError(f"category '{name}' does not exist")
    if category.is_system:
        raise ValueError(f"'{name}' is a system category and cannot be deleted")

    target = get_category(db, reassign_to)
    if target is None or not target.is_active:
        raise ValueError(f"reassign_to target '{reassign_to}' is not an active category")

    affected_dates = db.scalars(
        select(Transaction.transaction_date).where(Transaction.category == name)
    ).all()
    affected_periods = {d.strftime("%Y-%m") for d in affected_dates}

    db.execute(
        Transaction.__table__.update()
        .where(Transaction.category == name)
        .values(category=reassign_to)
    )
    db.execute(
        MerchantCategory.__table__.update()
        .where(MerchantCategory.category == name)
        .values(category=reassign_to)
    )
    db.delete(category)
    return affected_periods


def get_uncategorised(db: Session) -> list[Transaction]:
    """Rows §10.1's pipeline still needs to resolve.

    ``is_category_manual`` rows are excluded even if ``category`` happens to
    be null — a manual edit that clears a category is still a manual
    decision, not an invitation to auto-categorise.
    """
    stmt = select(Transaction).where(
        Transaction.category.is_(None), Transaction.is_category_manual.is_(False)
    )
    return db.scalars(stmt).all()


def load_merchant_cache(db: Session) -> dict[str, MerchantCategory]:
    """The whole merchant_categories table, keyed by merchant_key.

    Loaded once per run — §10.1's layers 1-3 resolve against this in memory,
    not with a query per row.
    """
    rows = db.scalars(select(MerchantCategory)).all()
    return {row.merchant_key: row for row in rows}


def upsert_merchant(db: Session, merchant_key: str, category: str, source: str) -> MerchantCategory:
    """Cache a merchant key → category resolution (rule, LLM, or manual)."""
    stmt = (
        sqlite_insert(MerchantCategory)
        .values(merchant_key=merchant_key, category=category, source=source)
        .on_conflict_do_update(
            index_elements=["merchant_key"],
            set_={"category": category, "source": source, "updated_at": func.now()},
        )
    )
    db.execute(stmt)
    db.flush()
    return db.scalars(
        select(MerchantCategory).where(MerchantCategory.merchant_key == merchant_key)
    ).one()


def apply_category_to_key(
    db: Session, merchant_key: str, transaction_ids: list[int], category: str
) -> int:
    """Write ``category`` onto every listed row, and bump the cache's hit_count.

    Never touches ``is_category_manual`` rows — those are protected from
    every rebuild, including this one (§10.5).
    """
    if not transaction_ids:
        return 0

    result = db.execute(
        Transaction.__table__.update()
        .where(
            Transaction.id.in_(transaction_ids),
            Transaction.is_category_manual.is_(False),
        )
        .values(category=category)
    )
    updated = result.rowcount

    if updated:
        db.execute(
            MerchantCategory.__table__.update()
            .where(MerchantCategory.merchant_key == merchant_key)
            .values(hit_count=MerchantCategory.hit_count + updated)
        )
    return updated